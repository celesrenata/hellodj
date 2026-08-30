"""Pure request→response mapping for the orchestrator's playback HTTP API.

``discord-bot-core`` (and the web-ui) forward playback intents to the standing
orchestrator over HTTP as ``POST /v1/playback`` with a JSON body shaped by
``discord_bot_core.playback.client.PlaybackRequest.to_payload``::

    {"action": "play", "guildId": "..", "channelId": "..",
     "requestedBy": "..", "query": "..", "source": ".."}

The orchestrator owns routing / classification / filtering / bans / queue
persistence (the :class:`~playback_orchestrator.router.PlaybackRouter` +
:class:`~playback_orchestrator.persistence.SessionStore`) but — until now — it
exposed NO HTTP endpoint, so every bot ``/play`` POST hit the health server's
404 and the user saw "Playback service is unavailable right now." This module
supplies the missing surface: a pure :func:`handle_playback` that maps a decoded
request body to a response body matching
``discord_bot_core.playback.client.PlaybackResult.from_payload`` (``{ok, message,
data}``), driving the real router/store. It imports no HTTP library and no
boto3, so it is fully unit-testable with an in-memory store.

The concrete wiring (build the store from env, decode the HTTP body, serve the
route) lives in :mod:`playback_bootstrap` / :mod:`__main__`.
"""

from __future__ import annotations

import logging
from typing import Any

from .persistence import SessionStore
from .router import PlaybackRouter, PlayRequest, RouteOutcome

_LOG = logging.getLogger("playback_orchestrator.playback_api")

__all__ = ["PLAYBACK_ROUTE", "PlaybackService", "handle_playback"]

#: The HTTP path the bot's PlaybackClient posts to (mirrors ``client.submit``).
PLAYBACK_ROUTE = "/v1/playback"

#: Actions that enqueue a track (routed through ban/classify/filter/persist).
_ENQUEUE_ACTIONS = frozenset({"play", "enqueue"})


class PlaybackService:
    """Services decoded playback requests against the router + session store.

    Dependency-injected (router + store) so it is unit-testable with an
    in-memory :class:`SessionStore`; holds no HTTP/boto3 dependency. Each public
    method returns a ``{ok, message, data}`` mapping ready to serialize as the
    HTTP response body.
    """

    def __init__(self, router: PlaybackRouter, store: SessionStore) -> None:
        self._router = router
        self._store = store

    def handle(self, body: dict[str, Any]) -> dict[str, Any]:
        """Dispatch one decoded request body to the matching action handler.

        Unknown/missing actions and malformed ids yield an ``ok:false`` result
        with a clear message rather than raising, so the HTTP layer always has a
        well-formed body to return (never a 500 for a bad request shape).
        """
        action = str(body.get("action", "")).strip().lower()
        try:
            guild_id = int(body.get("guildId"))
            user_id = int(body.get("requestedBy"))
        except (TypeError, ValueError):
            _LOG.warning(
                "playback api: rejected request with bad ids (action=%s)", action
            )
            return _fail("Invalid request: missing guild or user id.")

        # DEBUG: the decoded request shape (no secrets) so a beta trace shows
        # exactly what the orchestrator received per action.
        _LOG.debug(
            "playback api: action=%s guild=%s user=%s source=%s query=%s",
            action,
            guild_id,
            user_id,
            body.get("source") or "(default)",
            _truncate(body.get("query")),
        )

        if action in _ENQUEUE_ACTIONS:
            return self._enqueue(guild_id, user_id, body)
        if action == "skip":
            return self._skip(guild_id)
        if action == "stop":
            return self._stop(guild_id)
        if action == "queue":
            return self._queue(guild_id)
        if action == "now_playing":
            return self._now_playing(guild_id)
        if action in ("pause", "resume"):
            # The orchestrator persists queue/session; the actual audio
            # pause/resume is effected by the connected bot instance. Ack the
            # intent so the command has a clean, truthful reply.
            return _ok(f"{action.capitalize()} acknowledged.")
        return _fail(f"Unsupported playback action: {action or '(none)'}.")

    # -- action handlers --------------------------------------------------

    def _enqueue(
        self, guild_id: int, user_id: int, body: dict[str, Any]
    ) -> dict[str, Any]:
        """Route a play/enqueue request through the full pipeline."""
        query = str(body.get("query", "") or "").strip()
        if not query:
            return _fail("Nothing to play: provide a search query or URL.")
        source = body.get("source")
        decision = self._router.route(
            PlayRequest(
                guild_id=guild_id,
                user_id=user_id,
                query=query,
                title=None,
                author=None,
            )
        )
        ok = decision.outcome is RouteOutcome.ENQUEUED
        # INFO: the terminal routing outcome per guild — the key playback event
        # (enqueued / banned / filtered) with content type + queue depth.
        content_type = (
            decision.classification.content_type.value
            if decision.classification is not None
            else "?"
        )
        _LOG.info(
            "playback api: %s guild=%s user=%s type=%s queue_len=%s",
            decision.outcome.value,
            guild_id,
            user_id,
            content_type,
            decision.queue_length if decision.queue_length is not None else "-",
        )
        data: dict[str, Any] = {"outcome": decision.outcome.value}
        if source:
            data["source"] = str(source)
        if decision.queue_length is not None:
            data["queueLength"] = decision.queue_length
        if decision.classification is not None:
            data["contentType"] = decision.classification.content_type.value
        return {"ok": ok, "message": decision.message, "data": data}

    def _skip(self, guild_id: int) -> dict[str, Any]:
        """Pop the head of the queue (skip the current/next track)."""
        popped = self._store.dequeue(guild_id)
        if popped is None:
            _LOG.info("playback api: skip guild=%s (queue empty)", guild_id)
            return _ok("Nothing to skip — the queue is empty.")
        _LOG.info("playback api: skip guild=%s track=%s", guild_id, popped.title)
        return _ok(f"Skipped: {popped.title}.", {"skipped": popped.title})

    def _stop(self, guild_id: int) -> dict[str, Any]:
        """Clear the queue (stop playback)."""
        self._store.clear_queue(guild_id)
        _LOG.info("playback api: stop guild=%s (queue cleared)", guild_id)
        return _ok("Stopped and cleared the queue.")

    def _queue(self, guild_id: int) -> dict[str, Any]:
        """Return a snapshot of the current queue."""
        items = self._store.get_queue(guild_id)
        titles = [item.title for item in items]
        msg = (
            "The queue is empty."
            if not titles
            else f"{len(titles)} track(s) queued."
        )
        return _ok(msg, {"queue": titles})

    def _now_playing(self, guild_id: int) -> dict[str, Any]:
        """Return the currently playing item from session state."""
        session = self._store.get_session(guild_id)
        current = session.current if session is not None else None
        if not current:
            return _ok("Nothing is playing right now.")
        title = str(current.get("title", "")) or "the current track"
        return _ok(f"Now playing: {title}.", {"current": current})


def handle_playback(
    service: PlaybackService | None, body: dict[str, Any]
) -> dict[str, Any]:
    """Service one request, degrading to a clean ``ok:false`` when unconfigured.

    ``service`` is ``None`` when the orchestrator has no session table wired
    (degraded mode). Rather than a 404 (which the bot maps to "unavailable"),
    return a well-formed body so the reply is truthful and specific. Any
    unexpected error servicing the request is caught and returned as a failure
    body so the HTTP layer never 500s on a routing hiccup.
    """
    if service is None:
        return _fail("Playback service is not configured.")
    try:
        return service.handle(body)
    except Exception as exc:  # noqa: BLE001 - always return a body, never 500
        _LOG.warning("playback api: error servicing request: %s", exc)
        return _fail("Playback request could not be processed.")


#: Max query length logged (a full URL/search shouldn't bloat a log line).
_MAX_LOGGED_QUERY = 120


def _truncate(value: Any) -> str:
    """Return a log-safe, length-capped rendering of a user-supplied query."""
    if not value:
        return "(none)"
    text = str(value).strip()
    return text if len(text) <= _MAX_LOGGED_QUERY else text[:_MAX_LOGGED_QUERY] + "…"


def _ok(message: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build a success response body."""
    return {"ok": True, "message": message, "data": data or {}}


def _fail(message: str) -> dict[str, Any]:
    """Build a failure response body."""
    return {"ok": False, "message": message, "data": {}}

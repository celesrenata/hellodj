"""Typed HTTP/JSON client that delegates playback to the orchestrator.

The design makes ``playback-orchestrator`` the single owner of routing, content
classification, filtering, bans, and session/queue persistence. ``bot-core``
therefore contains *no* playback logic: it translates Discord command intents
into typed :class:`PlaybackRequest` objects and forwards them here.

The transport is abstracted behind :class:`Transport` so the client is testable
without a live orchestrator or a specific HTTP library. A concrete transport
(e.g. aiohttp) is supplied at wiring time; nothing in this module imports a
networking dependency, keeping it import-clean and unit-testable.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol

log = logging.getLogger(__name__)

__all__ = [
    "PlaybackAction",
    "PlaybackClient",
    "PlaybackError",
    "PlaybackRequest",
    "PlaybackResult",
    "Transport",
]


class PlaybackAction(Enum):
    """The playback intents bot-core can forward to the orchestrator.

    These mirror the user-facing commands (play/queue/skip/etc.) but carry no
    playback logic — the orchestrator interprets them.
    """

    PLAY = "play"
    ENQUEUE = "enqueue"
    SKIP = "skip"
    PAUSE = "pause"
    RESUME = "resume"
    STOP = "stop"
    NOW_PLAYING = "now_playing"
    QUEUE = "queue"


@dataclass(frozen=True)
class PlaybackRequest:
    """A typed playback intent forwarded to the orchestrator.

    Attributes:
        action: The playback action to perform.
        guild_id: Discord guild the request originates from.
        channel_id: Voice/text channel context for the request.
        requested_by: Discord user id that issued the command.
        query: Optional search query or track reference (for play/enqueue).
        source: Optional source hint (youtube/spotify/tidal/soundcloud).
    """

    action: PlaybackAction
    guild_id: int
    channel_id: int
    requested_by: int
    query: str | None = None
    source: str | None = None

    def to_payload(self) -> dict[str, Any]:
        """Serialize to the JSON body sent to the orchestrator."""
        payload: dict[str, Any] = {
            "action": self.action.value,
            "guildId": str(self.guild_id),
            "channelId": str(self.channel_id),
            "requestedBy": str(self.requested_by),
        }
        if self.query is not None:
            payload["query"] = self.query
        if self.source is not None:
            payload["source"] = self.source
        return payload


@dataclass(frozen=True)
class PlaybackResult:
    """Outcome of a playback request as reported by the orchestrator.

    Attributes:
        ok: Whether the orchestrator accepted and acted on the request.
        message: Human-readable status suitable for a Discord reply.
        data: Additional structured payload (e.g. queue snapshot).
    """

    ok: bool
    message: str = ""
    data: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> PlaybackResult:
        """Build a result from the orchestrator's JSON response body."""
        return cls(
            ok=bool(payload.get("ok", False)),
            message=str(payload.get("message", "")),
            data=dict(payload.get("data", {})),
        )


class PlaybackError(RuntimeError):
    """Raised when the orchestrator cannot be reached or returns an error."""


class Transport(Protocol):
    """Structural type for the async HTTP transport the client depends on.

    An aiohttp-backed adapter satisfies this in production; tests provide a fake
    that records calls and returns canned payloads.
    """

    async def post_json(
        self, url: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """POST ``payload`` as JSON to ``url`` and return the parsed response."""
        ...


class PlaybackClient:
    """Forwards playback requests to the playback-orchestrator.

    The client is a thin, typed façade: it builds the request URL, delegates the
    actual HTTP call to the injected :class:`Transport`, and maps the response
    into a :class:`PlaybackResult`.
    """

    def __init__(self, base_url: str, transport: Transport) -> None:
        """Initialise the client.

        Args:
            base_url: Base URL of the orchestrator (no trailing slash required).
            transport: Injected async HTTP transport (mockable in tests).
        """
        self._base_url = base_url.rstrip("/")
        self._transport = transport

    @property
    def base_url(self) -> str:
        """The orchestrator base URL this client targets."""
        return self._base_url

    async def submit(self, request: PlaybackRequest) -> PlaybackResult:
        """Forward a playback request to the orchestrator.

        Args:
            request: The typed playback intent to forward.

        Returns:
            The orchestrator's :class:`PlaybackResult`.

        Raises:
            PlaybackError: If the transport fails or the response is unusable.
        """
        url = f"{self._base_url}/v1/playback"
        # DEBUG: the outbound orchestrator hop (action + guild), so a beta trace
        # can follow a command from the cog through to the orchestrator.
        log.debug(
            "playback: POST %s action=%s guild=%s",
            url,
            request.action.value,
            request.guild_id,
        )
        try:
            raw = await self._transport.post_json(url, request.to_payload())
        except Exception as exc:
            log.debug(
                "playback: transport error to %s: %s", url, exc, exc_info=True
            )
            raise PlaybackError(
                f"failed to reach playback-orchestrator at {url}: {exc}"
            ) from exc
        if not isinstance(raw, dict):
            raise PlaybackError(
                "playback-orchestrator returned a non-object response"
            )
        return PlaybackResult.from_payload(raw)

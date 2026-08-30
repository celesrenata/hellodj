"""PlaybackRouter — routes a play request through the orchestrator pipeline.

Unlike the legacy on-prem router (which was coupled to discord.py interaction
objects and multiple bot instances), the AWS ``playback-orchestrator`` router
is a transport-agnostic decision layer. ``discord-bot-core`` and ``web-ui``
call it over HTTP/JSON with a plain :class:`PlayRequest`; the router runs the
pipeline and returns a typed :class:`RouteDecision`.

Pipeline (in order):

1. **Ban check** — a banned user is rejected before any other work.
2. **Classification** — the request is classified audio / video / radio.
3. **Content filter** — per-guild rules can block the request.
4. **Persistence** — the accepted item is enqueued to ``hellodj-session``
   through the single-writer :class:`SessionStore`.

Requirements: 6.1, 6.4, 7.4, 7.5
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum

from .classifier import ClassificationResult, ContentType, Mode, classify
from .content_filter import ContentFilter, FilterRule
from .persistence import QueueItem, SessionStore
from .user_bans import UserBans

__all__ = ["PlayRequest", "RouteOutcome", "RouteDecision", "PlaybackRouter"]

_LOG = logging.getLogger("playback_orchestrator.router")


class RouteOutcome(Enum):
    """The terminal outcome of routing a play request."""

    ENQUEUED = "enqueued"
    BANNED = "banned"
    FILTERED = "filtered"


@dataclass(frozen=True)
class PlayRequest:
    """A transport-agnostic play request.

    Attributes:
        guild_id: The guild the request targets.
        user_id: The requesting user's Discord id.
        query: The raw play request (URL or search text).
        mode: Optional forced content class (``auto`` by default).
        attachment_content_type: MIME type of an attached file, if any.
        title: Optional resolved track title (improves filter accuracy).
        author: Optional resolved track author (for artist filtering).
    """

    guild_id: int
    user_id: int
    query: str
    mode: Mode = "auto"
    attachment_content_type: str | None = None
    title: str | None = None
    author: str | None = None


@dataclass(frozen=True)
class RouteDecision:
    """The result of routing a :class:`PlayRequest`.

    Attributes:
        outcome: Whether the request was enqueued, banned, or filtered.
        classification: The classification result, when the request reached the
            classification stage (``None`` for a banned request).
        blocked_rule: The content-filter rule that blocked the request, when
            ``outcome`` is :attr:`RouteOutcome.FILTERED`.
        queue_length: The queue length after enqueue, when enqueued.
        message: A short, user-facing explanation for the outcome.
    """

    outcome: RouteOutcome
    classification: ClassificationResult | None = None
    blocked_rule: FilterRule | None = None
    queue_length: int | None = None
    message: str = ""


class PlaybackRouter:
    """Routes play requests through ban → classify → filter → persist.

    Args:
        session_store: The single-writer :class:`SessionStore` over
            ``hellodj-session``.
        content_filter: Optional per-guild content filter. When ``None``, no
            content blocking is applied.
        user_bans: Optional per-guild ban list. When ``None``, no ban
            enforcement is applied.
    """

    _BAN_MESSAGE = "You are restricted from using playback commands in this server."
    _FILTER_MESSAGE = "This content is blocked in this server."

    def __init__(
        self,
        session_store: SessionStore,
        *,
        content_filter: ContentFilter | None = None,
        user_bans: UserBans | None = None,
    ) -> None:
        self._store = session_store
        self._content_filter = content_filter
        self._user_bans = user_bans

    def route(self, request: PlayRequest) -> RouteDecision:
        """Run the routing pipeline for a single play request."""
        if self._is_banned(request.guild_id, request.user_id):
            _LOG.debug(
                "router: BANNED guild=%s user=%s",
                request.guild_id,
                request.user_id,
            )
            return RouteDecision(outcome=RouteOutcome.BANNED, message=self._BAN_MESSAGE)

        classification = classify(
            request.query,
            mode=request.mode,
            attachment_content_type=request.attachment_content_type,
        )
        # DEBUG: how the request was classified (content type + source hint) —
        # the key signal for diagnosing wrong-source or misrouted playback.
        _LOG.debug(
            "router: classified guild=%s type=%s source_hint=%s",
            request.guild_id,
            classification.content_type.value,
            classification.source_hint or "(none)",
        )

        blocked = self._blocked_rule(request, classification)
        if blocked is not None:
            _LOG.debug(
                "router: FILTERED guild=%s by rule_type=%s rule_id=%s",
                request.guild_id,
                getattr(blocked, "rule_type", "?"),
                getattr(blocked, "rule_id", "?"),
            )
            return RouteDecision(
                outcome=RouteOutcome.FILTERED,
                classification=classification,
                blocked_rule=blocked,
                message=self._FILTER_MESSAGE,
            )

        queue = self._store.enqueue(
            request.guild_id,
            self._build_item(request, classification),
        )
        _LOG.debug(
            "router: ENQUEUED guild=%s type=%s queue_len=%d",
            request.guild_id,
            classification.content_type.value,
            len(queue),
        )
        return RouteDecision(
            outcome=RouteOutcome.ENQUEUED,
            classification=classification,
            queue_length=len(queue),
            message=f"Added to the {classification.content_type.value} queue.",
        )

    # -- Pipeline stages -------------------------------------------------

    def _is_banned(self, guild_id: int, user_id: int) -> bool:
        """Return whether the user is banned (no enforcement when unset)."""
        if self._user_bans is None:
            return False
        return self._user_bans.is_banned(guild_id, user_id)

    def _blocked_rule(
        self,
        request: PlayRequest,
        classification: ClassificationResult,
    ) -> FilterRule | None:
        """Return the content-filter rule blocking the request, if any."""
        if self._content_filter is None:
            return None
        url = request.query if "://" in request.query else None
        title = request.title if request.title is not None else request.query
        return self._content_filter.check_track(
            request.guild_id,
            title=title,
            author=request.author,
            url=url,
        )

    def _build_item(
        self,
        request: PlayRequest,
        classification: ClassificationResult,
    ) -> QueueItem:
        """Build the :class:`QueueItem` to enqueue for an accepted request."""
        content_type = _content_type_value(classification.content_type)
        is_url = "://" in request.query
        return QueueItem(
            title=request.title or request.query,
            url=request.query if is_url else "",
            content_type=content_type,
            source=classification.source_hint,
            requested_by=request.user_id,
        )


def _content_type_value(content_type: ContentType) -> str:
    """Return the string form of a :class:`ContentType`."""
    return content_type.value

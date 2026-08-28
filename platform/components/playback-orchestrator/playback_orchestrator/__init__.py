"""HelloDJ playback-orchestrator component.

This component turns a raw play request into a routed, filtered, and persisted
playback action. It is the **single writer** for session/queue state on the
``hellodj-session`` hot table (DAX-fronted, optimistic-locked), keeping all
session mutations serialized (design: "The single writer for session/queue
state").

Public surface:

* :class:`~.classifier.ContentType` / :func:`~.classifier.classify` — pure
  audio/video/radio classification of a query.
* :class:`~.content_filter.ContentFilter` — per-guild content-blocking rules.
* :class:`~.user_bans.UserBans` — per-guild playback ban list.
* :class:`~.persistence.SessionStore` — single-writer unified queue/session
  persistence to ``hellodj-session`` via the shared data-access layer.
* :class:`~.router.PlaybackRouter` / :class:`~.router.RouteDecision` — routes a
  request through ban → classify → filter → persist.

Each module stays well under the 500-line per-file ceiling (R13.3) with full
type hints and PEP 8 style. The component is independently deployable and
versioned (R15.1, R15.3).

Requirements: 6.1, 6.4, 7.4, 7.5, 15.1, 15.3
"""

from __future__ import annotations

from .classifier import ClassificationResult, ContentType, classify
from .content_filter import ContentFilter, FilterRule
from .persistence import QueueItem, SessionState, SessionStore
from .router import PlaybackRouter, RouteDecision, RouteOutcome
from .token_watchdog import TokenWatchdog
from .user_bans import BanEntry, UserBans

__all__ = [
    "ClassificationResult",
    "ContentType",
    "classify",
    "ContentFilter",
    "FilterRule",
    "QueueItem",
    "SessionState",
    "SessionStore",
    "PlaybackRouter",
    "RouteDecision",
    "RouteOutcome",
    "TokenWatchdog",
    "BanEntry",
    "UserBans",
]

__version__ = "0.1.0"

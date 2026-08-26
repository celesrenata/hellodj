"""Unified queue/session persistence — the single writer to ``hellodj-session``.

The playback-orchestrator is the **single writer** for session and queue state
(design: "unified queue persistence as single writer to ``hellodj-session``").
This module wraps the shared :class:`hellodj_platform_logic.data_access.\
SessionTable` (DAX-fronted hot path, optimistic-locked) and exposes typed,
serialized mutations of a guild's session and queue.

All writes go through :meth:`SessionTable.put_state`, whose optimistic-lock
read-modify-write guarantees that concurrent mutations are serialized behind a
``version`` guard. Because the orchestrator is the only component that calls
this store, session/queue state has exactly one writer end-to-end (R7.5).

Keys follow the design's ``hellodj-session`` schema:

* ``PK`` = ``GUILD#<guild_id>``
* ``SK`` = ``SESSION`` (session metadata) or ``QUEUE`` (the unified queue)

Requirements: 6.1, 6.4, 7.4, 7.5
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

from hellodj_platform_logic.data_access import SessionTable

#: A pure function mapping the current session state to the desired new state.
SessionMutator = Callable[["SessionState"], "SessionState"]

__all__ = [
    "QueueItem",
    "SessionState",
    "SessionStore",
    "SESSION_SK",
    "QUEUE_SK",
    "guild_pk",
]

#: Sort keys for the two session/queue items the orchestrator owns.
SESSION_SK = "SESSION"
QUEUE_SK = "QUEUE"


def guild_pk(guild_id: int) -> str:
    """Return the ``hellodj-session`` partition key for a guild."""
    return f"GUILD#{guild_id}"


@dataclass(frozen=True)
class QueueItem:
    """A single queued playback item.

    Attributes:
        title: Human-readable track title.
        url: Playable/resolvable URL for the item.
        content_type: ``"audio"``, ``"video"``, or ``"radio"``.
        source: Source hint (for example ``"youtube"`` or ``"tidal"``).
        requested_by: Discord id of the requester, if known.
        duration_ms: Track duration in milliseconds, if known.
    """

    title: str
    url: str
    content_type: str = "audio"
    source: str = ""
    requested_by: int | None = None
    duration_ms: int | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a DynamoDB-friendly mapping for this item."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> QueueItem:
        """Build a :class:`QueueItem` from a stored mapping."""
        return cls(
            title=str(data.get("title", "")),
            url=str(data.get("url", "")),
            content_type=str(data.get("content_type", "audio")),
            source=str(data.get("source", "")),
            requested_by=_opt_int(data.get("requested_by")),
            duration_ms=_opt_int(data.get("duration_ms")),
        )


@dataclass
class SessionState:
    """Session metadata for a guild's active playback.

    Attributes:
        voice_channel_id: Voice channel the session is bound to, if any.
        text_channel_id: Text channel for status messages, if any.
        current: The currently playing item, if any.
        source_provider: The default source provider for the session.
        repeat_mode: ``"off"``, ``"one"``, or ``"all"``.
        auto_resume: Whether the session should auto-resume after a restart.
        filters: Active audio filter settings.
    """

    voice_channel_id: int | None = None
    text_channel_id: int | None = None
    current: dict[str, Any] | None = None
    source_provider: str = "youtube"
    repeat_mode: str = "off"
    auto_resume: bool = True
    filters: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Return a DynamoDB-friendly mapping for this session state."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> SessionState:
        """Build a :class:`SessionState` from a stored mapping."""
        current = data.get("current")
        filters = data.get("filters")
        return cls(
            voice_channel_id=_opt_int(data.get("voice_channel_id")),
            text_channel_id=_opt_int(data.get("text_channel_id")),
            current=dict(current) if isinstance(current, Mapping) else None,
            source_provider=str(data.get("source_provider", "youtube")),
            repeat_mode=str(data.get("repeat_mode", "off")),
            auto_resume=bool(data.get("auto_resume", True)),
            filters=dict(filters) if isinstance(filters, Mapping) else {},
        )


class SessionStore:
    """Single-writer session/queue repository over ``hellodj-session``.

    Every mutation is routed through :meth:`SessionTable.put_state`, so writes
    are serialized behind the table's optimistic ``version`` lock. Reads go
    through the DAX hot path with fall-through to DynamoDB.

    Args:
        session_table: An injected :class:`SessionTable` (constructed by the
            caller with the DynamoDB/DAX resource tables). Injecting it keeps
            this module import-safe and testable against moto / DynamoDB Local.
    """

    def __init__(self, session_table: SessionTable) -> None:
        self._table = session_table

    # -- Session metadata ------------------------------------------------

    def get_session(self, guild_id: int) -> SessionState | None:
        """Return the session metadata for a guild, or ``None`` if absent."""
        item = self._table.get(guild_pk(guild_id), SESSION_SK)
        if item is None:
            return None
        state = item.get("state", {})
        return SessionState.from_dict(state if isinstance(state, Mapping) else {})

    def save_session(self, guild_id: int, state: SessionState) -> SessionState:
        """Persist the session metadata for a guild (optimistic-locked write).

        Returns the :class:`SessionState` that was committed.
        """
        payload = state.to_dict()
        self._table.put_state(guild_pk(guild_id), SESSION_SK, lambda _current: payload)
        return state

    def update_session(
        self,
        guild_id: int,
        mutator: SessionMutator,
    ) -> SessionState:
        """Read-modify-write the session metadata under the optimistic lock.

        The ``mutator`` receives the current :class:`SessionState` (a fresh
        default when none exists) and returns the desired new state. The read,
        mutation, and write happen inside :meth:`SessionTable.put_state`, so a
        concurrent writer forces a transparent retry.
        """

        def _apply(current: dict[str, Any]) -> Mapping[str, Any]:
            state = SessionState.from_dict(current) if current else SessionState()
            new_state = mutator(state)
            return new_state.to_dict()

        committed = self._table.put_state(guild_pk(guild_id), SESSION_SK, _apply)
        return SessionState.from_dict(committed["state"])

    # -- Unified queue ---------------------------------------------------

    def get_queue(self, guild_id: int) -> list[QueueItem]:
        """Return the unified queue for a guild (empty when absent)."""
        item = self._table.get(guild_pk(guild_id), QUEUE_SK)
        if item is None:
            return []
        raw_items = item.get("state", {}).get("items", [])
        return [QueueItem.from_dict(entry) for entry in raw_items]

    def set_queue(self, guild_id: int, items: Sequence[QueueItem]) -> list[QueueItem]:
        """Replace the unified queue for a guild (optimistic-locked write)."""
        payload = {"items": [item.to_dict() for item in items]}
        self._table.put_state(guild_pk(guild_id), QUEUE_SK, lambda _current: payload)
        return list(items)

    def enqueue(self, guild_id: int, item: QueueItem) -> list[QueueItem]:
        """Append an item to the unified queue under the optimistic lock.

        Returns the full queue after the append.
        """

        def _apply(current: dict[str, Any]) -> Mapping[str, Any]:
            existing = current.get("items", []) if current else []
            return {"items": [*existing, item.to_dict()]}

        committed = self._table.put_state(guild_pk(guild_id), QUEUE_SK, _apply)
        return [QueueItem.from_dict(entry) for entry in committed["state"]["items"]]

    def dequeue(self, guild_id: int) -> QueueItem | None:
        """Pop the head of the unified queue under the optimistic lock.

        Returns the popped :class:`QueueItem`, or ``None`` when the queue is
        empty. A sentinel captures the popped head across the retrying
        read-modify-write so the return value reflects the committed state.
        """
        popped: list[dict[str, Any]] = []

        def _apply(current: dict[str, Any]) -> Mapping[str, Any]:
            popped.clear()
            items = list(current.get("items", [])) if current else []
            if items:
                popped.append(items.pop(0))
            return {"items": items}

        self._table.put_state(guild_pk(guild_id), QUEUE_SK, _apply)
        return QueueItem.from_dict(popped[0]) if popped else None

    def clear_queue(self, guild_id: int) -> None:
        """Empty the unified queue for a guild (optimistic-locked write)."""
        self._table.put_state(guild_pk(guild_id), QUEUE_SK, lambda _current: {"items": []})


def _opt_int(value: Any) -> int | None:
    """Coerce ``value`` to ``int`` or ``None`` when missing/invalid."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None

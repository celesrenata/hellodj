"""Guild authorization policy state machine.

Rule (unchanged from the legacy bot): *a new guild must be explicitly approved
by a bot administrator before HelloDJ operates in it.*

- On join, a guild enters :attr:`GuildStatus.PENDING`.
- While pending, the bot refuses commands.
- An administrator approves (:attr:`GuildStatus.APPROVED`) or denies
  (:attr:`GuildStatus.DENIED`) the guild via the admin portal.
- If not approved within :data:`PENDING_EXPIRY_SECONDS`, the guild is auto-denied
  and the bot leaves.
- Previously approved guilds remain approved across restarts.

The legacy implementation persisted to a JSON file on an NFS mount and used
module-level global state. This refactor extracts a :class:`PolicyStore`
protocol so the persistence backend (DynamoDB ``hellodj-core`` guild items in
the AWS platform, or an in-memory store in tests) is injected. The policy logic
itself is a pure state machine over :class:`PolicyEntry` values.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, replace
from enum import Enum
from typing import Protocol

__all__ = [
    "PENDING_EXPIRY_SECONDS",
    "GuildPolicy",
    "GuildStatus",
    "InMemoryPolicyStore",
    "PolicyEntry",
    "PolicyStore",
]

#: How long a guild may remain pending before it is auto-denied (24 hours).
PENDING_EXPIRY_SECONDS = 24 * 60 * 60


class GuildStatus(Enum):
    """Authorization status of a guild."""

    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"


@dataclass(frozen=True)
class PolicyEntry:
    """Immutable policy record for a single guild.

    Attributes:
        guild_id: Discord guild id.
        status: Current authorization status.
        reason: Human-readable explanation for the current status.
        checked_at: Epoch seconds when the status was last set.
        name: Best-known guild display name (for the admin portal).
    """

    guild_id: int
    status: GuildStatus
    reason: str = ""
    checked_at: int = 0
    name: str = ""


class PolicyStore(Protocol):
    """Persistence backend for guild policy entries.

    Implementations back onto DynamoDB in production and an in-memory dict in
    tests. All methods are synchronous and pure with respect to their store.
    """

    def get(self, guild_id: int) -> PolicyEntry | None:
        """Return the stored entry for ``guild_id`` or ``None``."""
        ...

    def put(self, entry: PolicyEntry) -> None:
        """Insert or replace ``entry``."""
        ...

    def delete(self, guild_id: int) -> None:
        """Remove the entry for ``guild_id`` if present."""
        ...

    def all(self) -> list[PolicyEntry]:
        """Return every stored entry."""
        ...


class InMemoryPolicyStore:
    """A simple in-memory :class:`PolicyStore` (default / test backend)."""

    def __init__(self) -> None:
        self._entries: dict[int, PolicyEntry] = {}

    def get(self, guild_id: int) -> PolicyEntry | None:
        return self._entries.get(int(guild_id))

    def put(self, entry: PolicyEntry) -> None:
        self._entries[int(entry.guild_id)] = entry

    def delete(self, guild_id: int) -> None:
        self._entries.pop(int(guild_id), None)

    def all(self) -> list[PolicyEntry]:
        return list(self._entries.values())


class Clock(Protocol):
    """A zero-argument callable returning epoch seconds (injectable clock)."""

    def __call__(self) -> float:
        ...


class GuildPolicy:
    """Guild authorization policy over an injected :class:`PolicyStore`.

    The class holds no global state; construct it with a store and (optionally)
    a clock for deterministic tests.
    """

    def __init__(
        self,
        store: PolicyStore | None = None,
        *,
        clock: Clock | None = None,
    ) -> None:
        """Initialise the policy.

        Args:
            store: The persistence backend. Defaults to an in-memory store.
            clock: A zero-argument callable returning epoch seconds. Defaults to
                :func:`time.time`; injectable for deterministic tests.
        """
        self._store: PolicyStore = store or InMemoryPolicyStore()
        self._clock: Clock = clock or time.time

    def _now(self) -> int:
        return int(self._clock())

    def check_on_join(self, guild_id: int, name: str = "") -> GuildStatus:
        """Resolve a guild's status when the bot joins it.

        Already-approved and already-denied guilds keep their status; unknown or
        pending guilds are set (or kept) pending awaiting admin approval.

        Args:
            guild_id: The joined guild's id.
            name: The guild's display name for the admin portal.

        Returns:
            The resolved :class:`GuildStatus`.
        """
        entry = self._store.get(guild_id)
        if entry and entry.status is GuildStatus.APPROVED:
            return GuildStatus.APPROVED
        if entry and entry.status is GuildStatus.DENIED:
            return GuildStatus.DENIED

        pending = PolicyEntry(
            guild_id=int(guild_id),
            status=GuildStatus.PENDING,
            reason="awaiting admin approval",
            checked_at=self._now(),
            name=name or (entry.name if entry else ""),
        )
        self._store.put(pending)
        return GuildStatus.PENDING

    def is_authorized(self, guild_id: int) -> bool:
        """Return whether the guild is approved for operation."""
        entry = self._store.get(guild_id)
        return entry is not None and entry.status is GuildStatus.APPROVED

    def approve(self, guild_id: int) -> PolicyEntry:
        """Approve a guild (called from the admin portal)."""
        return self._set_status(
            guild_id, GuildStatus.APPROVED, "approved by administrator"
        )

    def deny(self, guild_id: int, reason: str = "denied by administrator") -> PolicyEntry:
        """Deny a guild (called from the admin portal)."""
        return self._set_status(guild_id, GuildStatus.DENIED, reason)

    def clear(self, guild_id: int) -> None:
        """Drop the policy entry for a guild (used on guild remove)."""
        self._store.delete(guild_id)

    def _set_status(
        self, guild_id: int, status: GuildStatus, reason: str
    ) -> PolicyEntry:
        existing = self._store.get(guild_id)
        entry = PolicyEntry(
            guild_id=int(guild_id),
            status=status,
            reason=reason,
            checked_at=self._now(),
            name=existing.name if existing else "",
        )
        self._store.put(entry)
        return entry

    def pending_guilds(self) -> list[PolicyEntry]:
        """Return every guild currently in the pending state."""
        return [e for e in self._store.all() if e.status is GuildStatus.PENDING]

    def expired_pending(
        self, expiry_seconds: int = PENDING_EXPIRY_SECONDS
    ) -> list[int]:
        """Return the ids of pending guilds whose approval window has elapsed.

        This method is pure: it computes which guilds are expired but does not
        mutate state or leave any guild. The caller (the guild-policy watchdog)
        decides what to do — typically :meth:`deny` and leave the guild.

        Args:
            expiry_seconds: Age past which a pending guild is considered expired.

        Returns:
            Guild ids that have been pending longer than ``expiry_seconds``.
        """
        now = self._now()
        expired: list[int] = []
        for entry in self._store.all():
            if entry.status is not GuildStatus.PENDING:
                continue
            if now - entry.checked_at > expiry_seconds:
                expired.append(entry.guild_id)
        return expired

    def expire_and_deny(
        self, expiry_seconds: int = PENDING_EXPIRY_SECONDS
    ) -> list[int]:
        """Deny every expired pending guild and return the affected ids.

        Args:
            expiry_seconds: Age past which a pending guild is expired.

        Returns:
            The guild ids that were transitioned to denied.
        """
        expired = self.expired_pending(expiry_seconds)
        for guild_id in expired:
            existing = self._store.get(guild_id)
            self._store.put(
                replace(
                    existing,
                    status=GuildStatus.DENIED,
                    reason="expired — not approved within the approval window",
                    checked_at=self._now(),
                )
                if existing
                else PolicyEntry(
                    guild_id=guild_id,
                    status=GuildStatus.DENIED,
                    reason="expired — not approved within the approval window",
                    checked_at=self._now(),
                )
            )
        return expired

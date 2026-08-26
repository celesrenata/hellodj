"""Per-guild playback ban list for the playback-orchestrator.

Tracks which Discord users are barred from issuing playback commands in a
given guild. Ported from the legacy on-prem module but kept storage-agnostic:
bans are held in memory and loaded/persisted by the caller through the
DynamoDB ``hellodj-core`` config path, so the orchestrator's only DynamoDB
writer stays the session-persistence layer.

Requirements: 6.1, 6.4
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime

__all__ = ["BanEntry", "UserBans"]


def _utc_now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class BanEntry:
    """A single per-guild playback ban.

    Attributes:
        user_id: Discord id of the banned user.
        banned_by: Discord id of the moderator who issued the ban.
        banned_at: ISO-8601 UTC timestamp of when the ban was issued.
    """

    user_id: int
    banned_by: int
    banned_at: str

    def to_dict(self) -> dict[str, object]:
        """Return a JSON/DynamoDB-friendly mapping for this ban."""
        return {
            "user_id": self.user_id,
            "banned_by": self.banned_by,
            "banned_at": self.banned_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> BanEntry:
        """Build a :class:`BanEntry` from a stored mapping."""
        return cls(
            user_id=int(data["user_id"]),
            banned_by=int(data.get("banned_by", 0)),
            banned_at=str(data.get("banned_at") or _utc_now_iso()),
        )


@dataclass
class UserBans:
    """In-memory, per-guild playback ban list.

    The store maps ``guild_id`` to an ordered list of :class:`BanEntry`. The
    class is storage-agnostic: the caller seeds it from and flushes it back to
    DynamoDB config.
    """

    _bans: dict[int, list[BanEntry]] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, data: Mapping[int, Iterable[Mapping[str, object]]]) -> UserBans:
        """Construct a ban list from a ``guild_id -> [ban dicts]`` mapping."""
        bans: dict[int, list[BanEntry]] = {}
        for guild_id, raw in data.items():
            bans[int(guild_id)] = [BanEntry.from_dict(entry) for entry in raw]
        return cls(_bans=bans)

    def ban_user(self, guild_id: int, user_id: int, banned_by: int) -> bool:
        """Ban a user. Return ``True`` when newly banned, ``False`` if already.

        The operation is idempotent: banning an already-banned user is a no-op
        that returns ``False``.
        """
        entries = self._bans.setdefault(guild_id, [])
        if any(entry.user_id == user_id for entry in entries):
            return False
        entries.append(
            BanEntry(user_id=user_id, banned_by=banned_by, banned_at=_utc_now_iso())
        )
        return True

    def unban_user(self, guild_id: int, user_id: int) -> bool:
        """Unban a user. Return ``True`` when a ban was removed."""
        entries = self._bans.get(guild_id)
        if not entries:
            return False
        for index, entry in enumerate(entries):
            if entry.user_id == user_id:
                entries.pop(index)
                if not entries:
                    del self._bans[guild_id]
                return True
        return False

    def is_banned(self, guild_id: int, user_id: int) -> bool:
        """Return whether ``user_id`` is banned in ``guild_id``."""
        entries = self._bans.get(guild_id)
        if not entries:
            return False
        return any(entry.user_id == user_id for entry in entries)

    def list_bans(self, guild_id: int) -> list[BanEntry]:
        """Return a copy of the bans for ``guild_id`` (empty if none)."""
        return list(self._bans.get(guild_id, ()))

    def to_mapping(self) -> dict[int, list[dict[str, object]]]:
        """Return a ``guild_id -> [ban dicts]`` mapping for persistence."""
        return {
            guild_id: [entry.to_dict() for entry in entries]
            for guild_id, entries in self._bans.items()
        }

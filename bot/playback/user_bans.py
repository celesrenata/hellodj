"""HelloDJ — Per-guild user ban storage.

Lightweight module for managing per-guild user bans (preventing specific
Discord users from using playback commands). Stores bans in
``data/user_bans.json`` keyed by guild_id (string).

Uses atomic writes and follows the same persistence pattern as ContentFilter.

Shape of data/user_bans.json:
    {
        "<guild_id>": {
            "banned_users": [
                {"user_id": 123456789, "banned_by": 987654321, "banned_at": "..."}
            ]
        }
    }
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone

__all__ = ["UserBans"]

log = logging.getLogger(__name__)


class UserBans:
    """Per-guild user ban list with persistent JSON storage."""

    def __init__(self, data_path: str = "data/user_bans.json") -> None:
        self._data_path = data_path
        self._data: dict[str, dict] = {}  # { "guild_id": {"banned_users": [...]} }
        self._lock = asyncio.Lock()
        self._load()

    # ── Public API ──────────────────────────────────────────────────────

    async def ban_user(self, guild_id: int, user_id: int, banned_by: int) -> bool:
        """Ban a user in a guild. Returns True if newly banned, False if already banned."""
        async with self._lock:
            gid_str = str(guild_id)
            if gid_str not in self._data:
                self._data[gid_str] = {"banned_users": []}

            banned_users = self._data[gid_str]["banned_users"]

            # Check if already banned
            for entry in banned_users:
                if entry["user_id"] == user_id:
                    return False

            banned_users.append({
                "user_id": user_id,
                "banned_by": banned_by,
                "banned_at": datetime.now(timezone.utc).isoformat(),
            })
            self._save()

        log.info(
            "UserBans: banned user %s in guild %s by user %s",
            user_id, guild_id, banned_by,
        )
        return True

    async def unban_user(self, guild_id: int, user_id: int) -> bool:
        """Unban a user in a guild. Returns True if found and removed."""
        async with self._lock:
            gid_str = str(guild_id)
            guild_data = self._data.get(gid_str)
            if guild_data is None:
                return False

            banned_users = guild_data["banned_users"]
            for i, entry in enumerate(banned_users):
                if entry["user_id"] == user_id:
                    banned_users.pop(i)
                    # Clean up empty guild entries
                    if not banned_users:
                        del self._data[gid_str]
                    self._save()
                    log.info("UserBans: unbanned user %s in guild %s", user_id, guild_id)
                    return True

        return False

    def is_banned(self, guild_id: int, user_id: int) -> bool:
        """Check if a user is banned in a guild."""
        gid_str = str(guild_id)
        guild_data = self._data.get(gid_str)
        if guild_data is None:
            return False
        return any(
            entry["user_id"] == user_id for entry in guild_data["banned_users"]
        )

    def list_bans(self, guild_id: int) -> list[dict]:
        """List all banned users for a guild. Returns an empty list if none."""
        gid_str = str(guild_id)
        guild_data = self._data.get(gid_str)
        if guild_data is None:
            return []
        return list(guild_data["banned_users"])

    # ── Persistence ─────────────────────────────────────────────────────

    def _load(self) -> None:
        """Load ban data from disk. Safe to call once at init."""
        os.makedirs(os.path.dirname(self._data_path) or "data", exist_ok=True)
        if not os.path.exists(self._data_path):
            self._data = {}
            return
        try:
            with open(self._data_path, "r", encoding="utf-8") as f:
                self._data = json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            log.error(
                "UserBans: could not read %s (%s); starting with empty bans.",
                self._data_path, exc,
            )
            self._data = {}

    def _save(self) -> None:
        """Atomically persist the in-memory store. Call while holding ``_lock``."""
        os.makedirs(os.path.dirname(self._data_path) or "data", exist_ok=True)
        tmp = f"{self._data_path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self._data_path)

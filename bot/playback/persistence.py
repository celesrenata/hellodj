"""HelloDJ — Unified session persistence with composite keys.

Stores playback sessions keyed by ``"guild_id:channel_id"`` in
``data/sessions.json``. Supports both audio and video session types,
migrates legacy guild_id-only keys on first load, and handles
restoration errors gracefully (mark suspended, never discard).

Follows the same atomic-write + asyncio.Lock pattern as ``bot/session.py``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from typing import Literal

from debug import get_debug_logger

__all__ = [
    "save_session",
    "load_all",
    "clear_session",
    "migrate_legacy",
]

log = logging.getLogger(__name__)
dbg = get_debug_logger("persistence")

SESSIONS_FILE = "data/sessions.json"

# In-memory cache of composite-keyed sessions
# Key: "guild_id:channel_id" → session dict
_data: dict[str, dict] = {}
_lock = asyncio.Lock()


# ── Helpers ────────────────────────────────────────────────────────────────


def _composite_key(guild_id: int, channel_id: int) -> str:
    """Build the JSON key string for a composite (guild, channel) pair."""
    return f"{guild_id}:{channel_id}"


def _parse_composite_key(key: str) -> tuple[int, int] | None:
    """Parse a composite key string into (guild_id, channel_id), or None if invalid."""
    parts = key.split(":", 1)
    if len(parts) != 2:
        return None
    try:
        return int(parts[0]), int(parts[1])
    except (ValueError, TypeError):
        return None


def _is_legacy_key(key: str) -> bool:
    """Return True if the key is a legacy guild_id-only format (no colon)."""
    return ":" not in key


def _save() -> None:
    """Atomically persist the in-memory store. Call while holding ``_lock``."""
    os.makedirs("data", exist_ok=True)
    tmp = f"{SESSIONS_FILE}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(_data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, SESSIONS_FILE)


def _load_raw() -> dict[str, dict]:
    """Load the raw JSON data from disk, returning empty dict on failure."""
    os.makedirs("data", exist_ok=True)
    if not os.path.exists(SESSIONS_FILE):
        return {}
    try:
        with open(SESSIONS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            log.error("sessions.json root is not a dict; starting with empty state.")
            return {}
        return data
    except (json.JSONDecodeError, OSError) as exc:
        log.error(
            "Could not read %s (%s); starting with empty state.",
            SESSIONS_FILE,
            exc,
        )
        return {}


# ── Migration ──────────────────────────────────────────────────────────────


async def migrate_legacy(data: dict[str, dict]) -> dict[str, dict]:
    """Convert guild_id-keyed entries to composite ``guild_id:channel_id`` keys.

    Rules:
    - If the key already contains ":", it's already migrated — keep as-is.
    - If a legacy entry has a valid ``voice_channel_id``, migrate to composite.
    - If a legacy entry lacks ``voice_channel_id``, skip with a warning (Req 10.7).
    - All other fields are preserved unchanged (Req 10.5).
    """
    migrated: dict[str, dict] = {}
    legacy_count = 0
    skipped_count = 0

    for key, entry in data.items():
        if not _is_legacy_key(key):
            # Already composite-keyed — keep as-is
            migrated[key] = entry
            continue

        # Legacy key: attempt migration
        legacy_count += 1
        voice_channel_id = entry.get("voice_channel_id")

        if voice_channel_id is None:
            log.warning(
                "Legacy session key=%r has no voice_channel_id; skipping migration.",
                key,
            )
            skipped_count += 1
            continue

        # Validate voice_channel_id is a usable integer
        try:
            channel_id = int(voice_channel_id)
        except (ValueError, TypeError):
            log.warning(
                "Legacy session key=%r has invalid voice_channel_id=%r; skipping.",
                key,
                voice_channel_id,
            )
            skipped_count += 1
            continue

        # Build new composite key
        new_key = _composite_key(int(key), channel_id)

        # Ensure session_type is set (legacy sessions are all audio)
        if "session_type" not in entry:
            entry["session_type"] = "audio"

        # Ensure bot_instance_index is set (legacy = primary instance)
        if "bot_instance_index" not in entry:
            entry["bot_instance_index"] = 0

        migrated[new_key] = entry
        dbg.event(
            "migrate_legacy",
            old_key=key,
            new_key=new_key,
            channel_id=channel_id,
        )

    if legacy_count > 0:
        log.info(
            "Migrated %d legacy session(s) to composite keys (%d skipped).",
            legacy_count - skipped_count,
            skipped_count,
        )

    return migrated


# ── Public API ─────────────────────────────────────────────────────────────


async def save_session(
    guild_id: int,
    channel_id: int,
    *,
    session_type: Literal["audio", "video"],
    voice_channel_id: int | None,
    text_channel_id: int | None,
    current: dict | None,
    queue: list[dict],
    auto_resume: bool = True,
    source_provider: str = "youtube",
    repeat_mode: str = "off",
    filters: dict | None = None,
    crossfade_seconds: float = 0.0,
    tune_enabled: bool = False,
    bot_instance_index: int = 0,
    autoplay_enabled: bool = False,
    autoplay_genres: list[str] | None = None,
) -> None:
    """Persist a channel session to disk using composite key format.

    This saves the full session state so it can be restored after a restart.
    """
    key = _composite_key(guild_id, channel_id)
    dbg.event(
        "save_session",
        key=key,
        session_type=session_type,
        queue_len=len(queue),
        auto_resume=auto_resume,
        current_title=current.get("title") if current else None,
    )

    record = {
        "session_type": session_type,
        "voice_channel_id": voice_channel_id,
        "text_channel_id": text_channel_id,
        "current": current,
        "queue": queue,
        "auto_resume": auto_resume,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "source_provider": source_provider,
        "repeat_mode": repeat_mode,
        "filters": filters or {},
        "crossfade_seconds": crossfade_seconds,
        "tune_enabled": tune_enabled,
        "bot_instance_index": bot_instance_index,
        "autoplay_enabled": autoplay_enabled,
        "autoplay_genres": autoplay_genres or [],
    }

    async with _lock:
        _data[key] = record
        _save()


async def load_all() -> dict[tuple[int, int], dict]:
    """Load all sessions from disk, migrating legacy keys if needed.

    Returns a dict keyed by (guild_id, channel_id) tuples.
    Sessions with ``session_type="video"`` are loaded but NOT marked for
    auto-resume (Req 10.4). Audio sessions with ``auto_resume=True`` are
    eligible for restoration by the caller.

    On corrupt/unreadable file: starts with empty state and logs error.
    """
    global _data

    async with _lock:
        raw = _load_raw()

        # Check if any legacy keys exist and migrate
        has_legacy = any(_is_legacy_key(k) for k in raw)
        if has_legacy:
            raw = await migrate_legacy(raw)

        _data = raw

        # Persist the migrated data back to disk
        if has_legacy:
            _save()

    # Build the return dict with tuple keys
    result: dict[tuple[int, int], dict] = {}
    for key, entry in _data.items():
        parsed = _parse_composite_key(key)
        if parsed is None:
            log.warning("Skipping unparseable session key: %r", key)
            continue
        result[parsed] = entry

    dbg.event("load_all", total_sessions=len(result))
    return result


async def clear_session(guild_id: int, channel_id: int) -> None:
    """Remove a session from persistence."""
    key = _composite_key(guild_id, channel_id)
    async with _lock:
        if _data.pop(key, None) is not None:
            _save()
            dbg.event("clear_session", key=key)


async def mark_suspended(guild_id: int, channel_id: int, reason: str) -> None:
    """Mark a session as suspended (restoration failed).

    The session data is preserved on disk so users can still view the queue.
    A ``suspended`` flag and ``suspended_reason`` are added to the record.
    """
    key = _composite_key(guild_id, channel_id)
    async with _lock:
        entry = _data.get(key)
        if entry is None:
            return
        entry["suspended"] = True
        entry["suspended_reason"] = reason
        entry["updated_at"] = datetime.now(timezone.utc).isoformat()
        _save()
        dbg.event("mark_suspended", key=key, reason=reason)


async def set_auto_resume(guild_id: int, channel_id: int, value: bool) -> None:
    """Update the auto_resume flag for a session without touching other fields."""
    key = _composite_key(guild_id, channel_id)
    async with _lock:
        entry = _data.get(key)
        if entry is None:
            return
        entry["auto_resume"] = value
        entry["updated_at"] = datetime.now(timezone.utc).isoformat()
        _save()


def get(guild_id: int, channel_id: int) -> dict | None:
    """Read a session record without acquiring the lock (reads are safe)."""
    key = _composite_key(guild_id, channel_id)
    return _data.get(key)


def get_all_raw() -> dict[str, dict]:
    """Return the raw in-memory data dict (for diagnostics)."""
    return dict(_data)

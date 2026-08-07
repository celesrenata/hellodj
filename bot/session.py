"""Persistent, guild-scoped playback-session storage backed by a JSON file.

Unlike playlists (``storage.py``), this captures the *live* state — the current
track, the pending queue, and the voice/text channels — so the bot can resume
after an internet drop, a crash, or an intentional stop.

The state is rewritten on every queue change. An ``auto_resume`` flag records
whether an interruption was unintended (resume automatically) or a member-
initiated stop that was parked for a later ``/continue``.
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timezone

log = logging.getLogger(__name__)

SESSIONS_FILE = "sessions.json"

# { "<guild_id>": {voice_channel_id, text_channel_id, current, queue, auto_resume, updated_at,
#                   autoplay_enabled, autoplay_genres, source_provider, repeat_mode, filters} }
_data: dict[str, dict] = {}
_lock = asyncio.Lock()


def load() -> None:
    """Load sessions from disk into memory. Safe to call once at startup."""
    global _data
    if not os.path.exists(SESSIONS_FILE):
        _data = {}
        return
    try:
        with open(SESSIONS_FILE, "r", encoding="utf-8") as f:
            _data = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        log.error("Could not read %s (%s); starting with no saved sessions.", SESSIONS_FILE, exc)
        _data = {}


def _save() -> None:
    """Atomically persist the in-memory store. Call while holding ``_lock``."""
    tmp = f"{SESSIONS_FILE}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(_data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, SESSIONS_FILE)


# --- read helpers (no lock needed; reads are atomic enough for this use) ---

def get(gid: int) -> dict | None:
    return _data.get(str(gid))


def all() -> dict[str, dict]:
    return dict(_data)


# --- mutations (lock-guarded, persisted) ---

async def save_guild(
    gid: int,
    *,
    voice_channel_id: int | None,
    text_channel_id: int | None,
    current: dict | None,
    queue: list[dict],
    auto_resume: bool = True,
    autoplay_enabled: bool = False,
    autoplay_genres: list[str] | None = None,
    source_provider: str = "youtube",
    repeat_mode: str = "off",
    filters: dict | None = None,
) -> None:
    """Snapshot a guild's live playback state to disk."""
    async with _lock:
        _data[str(gid)] = {
            "voice_channel_id": voice_channel_id,
            "text_channel_id": text_channel_id,
            "current": current,
            "queue": queue,
            "auto_resume": auto_resume,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            # Extended fields
            "autoplay_enabled": autoplay_enabled,
            "autoplay_genres": autoplay_genres or [],
            "source_provider": source_provider,
            "repeat_mode": repeat_mode,
            "filters": filters or {},
        }
        _save()


async def set_auto_resume(gid: int, value: bool) -> None:
    """Flip the auto-resume flag without touching the saved queue."""
    async with _lock:
        entry = _data.get(str(gid))
        if entry is None:
            return
        entry["auto_resume"] = value
        entry["updated_at"] = datetime.now(timezone.utc).isoformat()
        _save()


async def clear(gid: int) -> None:
    """Forget a guild's saved session entirely."""
    async with _lock:
        if _data.pop(str(gid), None) is not None:
            _save()

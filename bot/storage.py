"""Persistent, guild-scoped playlist storage backed by a JSON file.

Playlists are *shared* within a guild: anyone may view, edit, play, or delete
any playlist. All mutations are serialized behind a lock and written atomically.
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timezone

log = logging.getLogger(__name__)

PLAYLISTS_FILE = "playlists.json"

# { "<guild_id>": { "<playlist name>": {tracks, created_by, created_at, visibility, description} } }
_data: dict[str, dict] = {}
_lock = asyncio.Lock()


class PlaylistError(Exception):
    """Raised for expected, user-facing problems (duplicate name, missing playlist…)."""


def load() -> None:
    """Load playlists from disk into memory. Safe to call once at startup."""
    global _data
    if not os.path.exists(PLAYLISTS_FILE):
        _data = {}
        return
    try:
        with open(PLAYLISTS_FILE, "r", encoding="utf-8") as f:
            _data = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        log.error("Could not read %s (%s); starting with empty playlists.", PLAYLISTS_FILE, exc)
        _data = {}


def _save() -> None:
    """Atomically persist the in-memory store. Call while holding ``_lock``."""
    tmp = f"{PLAYLISTS_FILE}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(_data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, PLAYLISTS_FILE)


def _guild(gid: int) -> dict:
    return _data.setdefault(str(gid), {})


def _find(gid: int, name: str) -> str | None:
    """Return the stored playlist key matching ``name`` case-insensitively."""
    lowered = name.casefold()
    for key in _guild(gid):
        if key.casefold() == lowered:
            return key
    return None


# --- read helpers (no lock needed; reads are atomic enough for this use) ---

def names(gid: int) -> list[str]:
    return sorted(_guild(gid).keys(), key=str.casefold)


def list_playlists(gid: int) -> dict[str, dict]:
    return dict(_guild(gid))


def get(gid: int, name: str) -> dict | None:
    key = _find(gid, name)
    return _guild(gid)[key] if key else None


# --- mutations (lock-guarded, persisted) ---

async def create(
    gid: int,
    name: str,
    user_id: int,
    visibility: str = "public",
    description: str = "",
) -> None:
    name = name.strip()
    if not name:
        raise PlaylistError("Playlist name cannot be empty.")
    async with _lock:
        if _find(gid, name):
            raise PlaylistError(f"A playlist named **{name}** already exists.")
        _guild(gid)[name] = {
            "tracks": [],
            "created_by": str(user_id),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "visibility": visibility,
            "description": description,
        }
        _save()


async def edit(
    gid: int,
    name: str,
    *,
    new_name: str | None = None,
    visibility: str | None = None,
    description: str | None = None,
) -> str:
    """Modify playlist metadata. Returns the resolved (possibly new) playlist name."""
    async with _lock:
        key = _find(gid, name)
        if not key:
            raise PlaylistError(f"No playlist named **{name}**.")

        entry = _guild(gid)[key]
        if new_name is not None:
            new_name = new_name.strip()
            if not new_name:
                raise PlaylistError("Playlist name cannot be empty.")
            if _find(gid, new_name) and _find(gid, new_name) != key:
                raise PlaylistError(f"A playlist named **{new_name}** already exists.")
            # Rename
            _guild(gid)[new_name] = entry
            del _guild(gid)[key]
            key = new_name

        if visibility is not None:
            entry["visibility"] = visibility
        if description is not None:
            entry["description"] = description

        _save()
        return key


async def delete(gid: int, name: str) -> None:
    async with _lock:
        key = _find(gid, name)
        if not key:
            raise PlaylistError(f"No playlist named **{name}**.")
        del _guild(gid)[key]
        _save()


async def add_track(gid: int, name: str, track: dict) -> str:
    """Append a track ({url, title, duration}); creates the playlist if missing.
    Returns the resolved playlist name."""
    async with _lock:
        key = _find(gid, name)
        if not key:
            key = name.strip()
            if not key:
                raise PlaylistError("Playlist name cannot be empty.")
            _guild(gid)[key] = {
                "tracks": [],
                "created_by": None,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "visibility": "public",
                "description": "",
            }
        _guild(gid)[key]["tracks"].append(track)
        _save()
        return key


async def remove_track(gid: int, name: str, index: int) -> dict:
    """Remove the track at zero-based ``index``; returns the removed track."""
    async with _lock:
        key = _find(gid, name)
        if not key:
            raise PlaylistError(f"No playlist named **{name}**.")
        tracks = _guild(gid)[key]["tracks"]
        if index < 0 or index >= len(tracks):
            raise PlaylistError("That track number is out of range.")
        removed = tracks.pop(index)
        _save()
        return removed

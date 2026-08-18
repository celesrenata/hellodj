"""HelloDJ — Shared blacklist module — imported by bot.py and cogs/admin.py.

data/blacklist.json (written by the web UI at web-ui/app.py BLACKLIST_FILE) is
the source of truth. The bot loads it at startup (setup_hook) and can reload it
on demand via the admin cog's /blacklist reload command.

Shape of data/blacklist.json: { "<guild_id>": [user_id, ...], ... }.
"""

import json
import logging
import os

log = logging.getLogger(__name__)

# Shared file written by the web UI (web-ui/app.py BLACKLIST_FILE).
BLACKLIST_FILE = "data/blacklist.json"

# Track blacklist — separate file so it never collides with the user-id
# blacklist.json that the web UI owns. Shape: { "<guild_id>": [url, ...], ... }.
TRACK_BLACKLIST_FILE = "data/track_blacklist.json"

# Guild → list of user IDs
blacklist: dict[int, list[int]] = {}

# Guild → list of blocked track URLs (the playable URL of a track)
track_blacklist: dict[int, list[str]] = {}


def load() -> None:
    """Load data/blacklist.json into the in-memory blacklist (idempotent).

    The dict is mutated in place (clear + update) rather than reassigned so the
    module-level references held by bot.py, cogs/admin.py, and
    voice/voice_commands.py all observe the reloaded contents.
    """
    os.makedirs("data", exist_ok=True)
    if not os.path.exists(BLACKLIST_FILE):
        log.info("HelloDJ: %s not found — starting with empty blacklist.", BLACKLIST_FILE)
        return

    try:
        with open(BLACKLIST_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        log.error(
            "HelloDJ could not read %s (%s); keeping current blacklist.",
            BLACKLIST_FILE, exc,
        )
        return

    new: dict[int, list[int]] = {}
    for gid_str, ids in raw.items():
        try:
            gid = int(gid_str)
        except (ValueError, TypeError):
            continue
        new[gid] = [int(uid) for uid in (ids or []) if isinstance(uid, (int, str))]

    blacklist.clear()
    blacklist.update(new)
    log.info("HelloDJ blacklist loaded %d guild entries from %s", len(blacklist), BLACKLIST_FILE)


def reload() -> None:
    """Reload the blacklist from disk (same as load())."""
    load()


def save() -> None:
    """Write the in-memory blacklist to data/blacklist.json atomically."""
    os.makedirs("data", exist_ok=True)
    tmp = f"{BLACKLIST_FILE}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(blacklist, f, ensure_ascii=False, indent=2)
        os.replace(tmp, BLACKLIST_FILE)
        log.info("HelloDJ blacklist saved to %s (%d guild entries)", BLACKLIST_FILE, len(blacklist))
    except OSError as exc:
        log.error("HelloDJ could not write %s (%s)", BLACKLIST_FILE, exc)


def is_blacklisted(guild_id: int, user_id: int) -> bool:
    return user_id in blacklist.get(guild_id, [])


# ── Track blacklist (the /block button on the now-playing panel) ──────

def load_track_blacklist() -> None:
    """Load data/track_blacklist.json into the in-memory track blacklist."""
    os.makedirs("data", exist_ok=True)
    if not os.path.exists(TRACK_BLACKLIST_FILE):
        return
    try:
        with open(TRACK_BLACKLIST_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        log.error(
            "HelloDJ could not read %s (%s); keeping current track blacklist.",
            TRACK_BLACKLIST_FILE, exc,
        )
        return

    new: dict[int, list[str]] = {}
    for gid_str, urls in raw.items():
        try:
            gid = int(gid_str)
        except (ValueError, TypeError):
            continue
        new[gid] = [str(u) for u in (urls or []) if isinstance(u, str)]

    track_blacklist.clear()
    track_blacklist.update(new)
    log.info(
        "HelloDJ track blacklist loaded %d guild entries from %s",
        len(track_blacklist), TRACK_BLACKLIST_FILE,
    )


def save_track_blacklist() -> None:
    """Write the in-memory track blacklist to data/track_blacklist.json atomically."""
    os.makedirs("data", exist_ok=True)
    tmp = f"{TRACK_BLACKLIST_FILE}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(track_blacklist, f, ensure_ascii=False, indent=2)
        os.replace(tmp, TRACK_BLACKLIST_FILE)
        log.info(
            "HelloDJ track blacklist saved to %s (%d guild entries)",
            TRACK_BLACKLIST_FILE, len(track_blacklist),
        )
    except OSError as exc:
        log.error("HelloDJ could not write %s (%s)", TRACK_BLACKLIST_FILE, exc)


def add_blacklist_entry(guild_id: int, track_info: dict) -> str | None:
    """Permanently block a track in a guild by its playable URL.

    ``track_info`` is a queue entry dict (as produced by player._track_entry,
    e.g. ``state["current"]``) which carries the playable URL in
    ``webpage_url`` / ``url`` / ``uri``. Returns the URL that was blocked, or
    None if no playable URL could be found.
    """
    url = track_info.get("webpage_url") or track_info.get("url") or track_info.get("uri")
    if not url:
        return None

    guild_list = track_blacklist.setdefault(guild_id, [])
    if url not in guild_list:
        guild_list.append(url)
        save_track_blacklist()
        log.info("HelloDJ blocked track %r in guild %s", url, guild_id)
    return url


def is_track_blacklisted(guild_id: int, url: str) -> bool:
    """True if a playable URL is blocked in this guild."""
    return url in track_blacklist.get(guild_id, [])

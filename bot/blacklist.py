"""HelloDJ — Shared blacklist module — imported by bot.py and cogs/admin.py.

data/blacklist.json (written by the web UI at web-ui/app.py BLACKLIST_FILE) is
the source of truth. The bot loads it at startup (setup_hook) and can reload it
on demand via the admin cog's /blacklist reload command.

Shape of data/blacklist.json: { "<guild_id>": [user_id, ...], ... }.
"""

import json
import logging

log = logging.getLogger(__name__)

# Shared file written by the web UI (web-ui/app.py BLACKLIST_FILE).
BLACKLIST_FILE = "data/blacklist.json"

# Guild → list of user IDs
blacklist: dict[int, list[int]] = {}


def load() -> None:
    """Load data/blacklist.json into the in-memory blacklist (idempotent).

    The dict is mutated in place (clear + update) rather than reassigned so the
    module-level references held by bot.py, cogs/admin.py, and
    voice/voice_commands.py all observe the reloaded contents.
    """
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


def is_blacklisted(guild_id: int, user_id: int) -> bool:
    return user_id in blacklist.get(guild_id, [])

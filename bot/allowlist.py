"""HelloDJ — Shared allowlist module — imported by bot.py and cogs/admin.py.

data/allowlist.json stores a per-guild allowlist: guild → list[user_ids].
Users in the allowlist are permitted to use the bot when the guild is in
allow-all mode (see guild_settings.py).

Shape of data/allowlist.json: { "<guild_id>": [user_id, ...], ... }.
"""

import json
import logging
import os

log = logging.getLogger(__name__)

# Shared file for the allowlist.
ALLOWLIST_FILE = "data/allowlist.json"

# Guild → list of user IDs
allowlist: dict[int, list[int]] = {}


def load() -> None:
    """Load data/allowlist.json into the in-memory allowlist (idempotent).

    The dict is mutated in place (clear + update) rather than reassigned so the
    module-level references held by bot.py and cogs/admin.py all observe the
    reloaded contents.
    """
    os.makedirs("data", exist_ok=True)
    if not os.path.exists(ALLOWLIST_FILE):
        log.info("HelloDJ: %s not found — starting with empty allowlist.", ALLOWLIST_FILE)
        return

    try:
        with open(ALLOWLIST_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        log.error(
            "HelloDJ could not read %s (%s); keeping current allowlist.",
            ALLOWLIST_FILE, exc,
        )
        return

    new: dict[int, list[int]] = {}
    for gid_str, ids in raw.items():
        try:
            gid = int(gid_str)
        except (ValueError, TypeError):
            continue
        new[gid] = [int(uid) for uid in (ids or []) if isinstance(uid, (int, str))]

    allowlist.clear()
    allowlist.update(new)
    log.info("HelloDJ allowlist loaded %d guild entries from %s", len(allowlist), ALLOWLIST_FILE)


def reload() -> None:
    """Reload the allowlist from disk (same as load())."""
    load()


def save() -> None:
    """Write the in-memory allowlist to data/allowlist.json atomically."""
    os.makedirs("data", exist_ok=True)
    tmp = f"{ALLOWLIST_FILE}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(allowlist, f, ensure_ascii=False, indent=2)
        os.replace(tmp, ALLOWLIST_FILE)
        log.info("HelloDJ allowlist saved to %s (%d guild entries)", ALLOWLIST_FILE, len(allowlist))
    except OSError as exc:
        log.error("HelloDJ could not write %s (%s)", ALLOWLIST_FILE, exc)


def is_allowed(guild_id: int, user_id: int) -> bool:
    """Check if a user is in the allowlist for a guild."""
    return user_id in allowlist.get(guild_id, [])


def add(guild_id: int, user_id: int) -> None:
    """Add a user to the guild's allowlist. No-op if already present."""
    if guild_id not in allowlist:
        allowlist[guild_id] = []
    if user_id not in allowlist[guild_id]:
        allowlist[guild_id].append(user_id)
    save()


def remove(guild_id: int, user_id: int) -> None:
    """Remove a user from the guild's allowlist. No-op if not present."""
    ids = allowlist.get(guild_id)
    if ids and user_id in ids:
        ids.remove(user_id)
        save()

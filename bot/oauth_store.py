"""HelloDJ — Shared OAuth binding store and bot guilds snapshot writer.

Two files live under ``data/`` (the same NFS/hellodj-data mount):

* ``data/oauth.json`` — WRITTEN by the web-ui subtask, READ by this module to
  enforce owner/admin permissions. Schema:
      { "owner_user_id": str, "owner_username": str,
        "admin_user_ids": [str...],
        "discord_token": {access_token, refresh_token, expires_in} | null }

* ``data/bot_guilds.json`` — WRITTEN by this module from the bot's live gateway
  guilds, READ by the web-ui subtask. Schema:
      { "<guild_id>": { "name": str, "icon": str|null, "member_count": int,
                        "channels": [ { "id": str, "name": str, "type": str } ] } }

Both follow the existing ``storage.py`` / ``session.py`` conventions: lock-
guarded, atomic (write temp then rename), JSON under ``data/``.
"""

import asyncio
import json
import logging
import os
import time

log = logging.getLogger(__name__)

OAUTH_FILE = "data/oauth.json"
GUILDS_FILE = "data/bot_guilds.json"

# --- OAuth binding store (written by web-ui, read by bot) ---

_oauth: dict = {}
_oauth_mtime: float = 0.0


def load_oauth() -> None:
    """Load the OAuth binding store from disk into memory at startup."""
    global _oauth, _oauth_mtime
    os.makedirs("data", exist_ok=True)
    if not os.path.exists(OAUTH_FILE):
        _oauth = {}
        _oauth_mtime = 0.0
        return
    try:
        with open(OAUTH_FILE, "r", encoding="utf-8") as f:
            _oauth = json.load(f)
        _oauth_mtime = os.path.getmtime(OAUTH_FILE)
    except (json.JSONDecodeError, OSError) as exc:
        log.error(
            "HelloDJ could not read %s (%s); disabling OAuth-bound admins.",
            OAUTH_FILE,
            exc,
        )
        _oauth = {}
        _oauth_mtime = 0.0


def _reload_oauth_if_changed() -> None:
    """Reload the binding store when the file mtime changes (periodic check)."""
    global _oauth, _oauth_mtime
    try:
        mtime = os.path.getmtime(OAUTH_FILE)
        if mtime == _oauth_mtime:
            return
        with open(OAUTH_FILE, "r", encoding="utf-8") as f:
            _oauth = json.load(f)
        _oauth_mtime = mtime
        log.info("HelloDJ reloaded OAuth bindings from %s", OAUTH_FILE)
    except OSError:
        # File missing/removed -> clear bindings
        _oauth = {}
        _oauth_mtime = 0.0
    except json.JSONDecodeError as exc:
        log.error(
            "HelloDJ could not parse %s (%s); keeping previous bindings.",
            OAUTH_FILE,
            exc,
        )


def is_bound_admin(user_id: int) -> bool:
    """True if the user's Discord id equals the stored owner or is in admin ids."""
    _reload_oauth_if_changed()
    uid = str(user_id)
    owner = _oauth.get("owner_user_id")
    if owner is not None and uid == str(owner):
        return True
    for admin_id in _oauth.get("admin_user_ids", []) or []:
        if admin_id is not None and uid == str(admin_id):
            return True
    return False


# --- Bot guilds snapshot (written by bot, read by web-ui) ---

_guilds_lock = asyncio.Lock()
_last_guilds_write: float = 0.0
GUILDS_THROTTLE_SECONDS = 5.0


async def write_guilds(guilds_data: dict, *, force: bool = False) -> bool:
    """Throttle-write the live gateway guild snapshot atomically.

    Returns True if a write actually happened (False when throttled).
    Pass ``force=True`` (e.g. on_ready) to bypass the throttle.
    """
    global _last_guilds_write
    now = time.monotonic()
    if not force and now - _last_guilds_write < GUILDS_THROTTLE_SECONDS:
        return False
    _last_guilds_write = now

    os.makedirs("data", exist_ok=True)
    tmp = f"{GUILDS_FILE}.tmp"
    async with _guilds_lock:
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(guilds_data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, GUILDS_FILE)
        except OSError as exc:
            log.error("HelloDJ could not write %s (%s)", GUILDS_FILE, exc)
            return False
    return True

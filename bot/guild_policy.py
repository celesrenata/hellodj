"""HelloDJ — Guild authorization policy.

Enforces the rule: *HelloDJ may only operate on a guild (server) where at least
one bot administrator is a member.* An administrator is the bot owner
(``BOT_OWNER_ID`` env var) or any OAuth-bound admin (see ``oauth_store``).

State is persisted to ``data/guild_policy.json`` (same NFS data mount as the
other stores) and follows the ``storage.py`` / ``guild_settings.py``
conventions: asyncio-lock guarded, atomic write (temp then rename), JSON under
``data/``, explicit logging.

Schema of data/guild_policy.json:
    { "<guild_id>": { "authorized": bool, "reason": str, "checked_at": int }, ... }

Behavior notes:
- A guild with no entry yet is treated as authorized (fail-open) until a check
  runs, so commands are not spuriously blocked during a startup gap. Every guild
  the bot is in is checked on join and re-checked at startup.
- If NO administrator ids are configured (no ``BOT_OWNER_ID`` and no OAuth
  bindings), checks fail OPEN (authorized) with a loud warning: we cannot
  determine "the administrators" yet, and locking the bot out of every server
  on first boot would break the deployment. Once an owner/admin is bound, the
  policy becomes enforceable.
- On an unauthorized guild, the caller should refuse commands and (on join)
  leave the guild; this module only records the decision.
"""

import asyncio
import json
import logging
import os
import time

import oauth_store

log = logging.getLogger(__name__)

POLICY_FILE = "data/guild_policy.json"

# guild_id (int) -> {"authorized": bool, "reason": str, "checked_at": int}
_data: dict[int, dict] = {}
_lock = asyncio.Lock()


def load() -> None:
    """Load data/guild_policy.json into memory at startup."""
    global _data
    os.makedirs("data", exist_ok=True)
    if not os.path.exists(POLICY_FILE):
        log.info("HelloDJ: %s not found — starting with empty guild policy.", POLICY_FILE)
        _data = {}
        return

    try:
        with open(POLICY_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        log.error(
            "HelloDJ could not read %s (%s); keeping current policy.",
            POLICY_FILE, exc,
        )
        return

    new: dict[int, dict] = {}
    for gid_str, entry in raw.items():
        try:
            gid = int(gid_str)
        except (ValueError, TypeError):
            continue
        if isinstance(entry, dict):
            new[gid] = {
                "authorized": bool(entry.get("authorized", False)),
                "reason": str(entry.get("reason", "")),
                "checked_at": int(entry.get("checked_at", 0) or 0),
            }
    _data.clear()
    _data.update(new)
    log.info("HelloDJ guild policy loaded %d guild entries from %s", len(_data), POLICY_FILE)


async def _save() -> None:
    """Atomically persist the in-memory policy. Call while holding ``_lock``."""
    os.makedirs("data", exist_ok=True)
    tmp = f"{POLICY_FILE}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, POLICY_FILE)
    except OSError as exc:
        log.error("HelloDJ could not write %s (%s)", POLICY_FILE, exc)


def _current_admin_ids() -> set[int]:
    """Return the set of bot-administrator Discord user ids.

    Union of ``BOT_OWNER_ID`` (env) and the OAuth-bound owner + admins from
    ``oauth_store.get_admin_ids()`` (which reloads on change). The bot owner is
    always considered an administrator.
    """
    ids: set[int] = set()
    owner_env = os.getenv("BOT_OWNER_ID", "")
    if owner_env:
        try:
            ids.add(int(owner_env))
        except (TypeError, ValueError):
            log.warning("HelloDJ could not parse BOT_OWNER_ID %r", owner_env)
    ids |= oauth_store.get_admin_ids()
    return ids


async def _admin_member_present(guild, admin_ids: set[int]) -> bool:
    """Return True if any admin id is a member of ``guild``.

    Fast path: scan the cached ``guild.members`` (members intent is enabled).
    Slow path: if no admin is found in cache, fetch the full member list to
    confirm absence — large-guild member caches can be partial.
    """
    if any(m.id in admin_ids for m in guild.members):
        return True

    # Confirm absence against the authoritative member list before declaring
    # the guild unauthorized (member cache may be incomplete for large guilds).
    try:
        async for member in guild.fetch_members():
            if member.id in admin_ids:
                return True
    except Exception as exc:
        # fetch_members can fail (missing intent, rate limit, API error). Fall
        # back to the cache result: treat absence as authoritative only when we
        # could actually verify it; otherwise report the cached answer.
        log.warning(
            "guild_policy: could not fetch members for guild %s (%s); "
            "relying on cache",
            getattr(guild, "id", "?"), exc,
        )
    return False


def _no_admins_configured(admin_ids: set[int]) -> bool:
    """True when no administrator ids are configured at all."""
    return not admin_ids


async def check_guild(guild) -> bool:
    """Check whether any bot administrator is a member of ``guild`` and record it.

    Returns True when the guild is authorized to operate, False otherwise. The
    policy entry is persisted under the lock. When no admins are configured the
    check fails OPEN (returns True) with a loud warning (see module docstring).
    """
    gid = int(guild.id)
    admin_ids = _current_admin_ids()

    if _no_admins_configured(admin_ids):
        log.warning(
            "guild_policy: no BOT_OWNER_ID and no OAuth-bound admins configured — "
            "guild %s (%s) authorized by default (fail-open)",
            getattr(guild, "name", "?"), gid,
        )
        async with _lock:
            _data[gid] = {
                "authorized": True,
                "reason": "no administrators configured (fail-open)",
                "checked_at": int(time.time()),
            }
            await _save()
        return True

    present = await _admin_member_present(guild, admin_ids)
    async with _lock:
        if present:
            _data[gid] = {
                "authorized": True,
                "reason": "administrator present",
                "checked_at": int(time.time()),
            }
            log.info(
                "guild_policy: guild %s (%s) authorized — administrator present",
                getattr(guild, "name", "?"), gid,
            )
        else:
            _data[gid] = {
                "authorized": False,
                "reason": "no bot administrator is a member",
                "checked_at": int(time.time()),
            }
            log.warning(
                "guild_policy: guild %s (%s) UNAUTHORIZED — no bot administrator "
                "is a member",
                getattr(guild, "name", "?"), gid,
            )
        await _save()
    return present


async def is_authorized(guild_id: int) -> bool:
    """Return whether the guild is currently authorized.

    A guild with no recorded policy yet defaults to authorized (fail-open) so a
    startup gap does not spuriously block commands. Records are written by
    ``check_guild`` on join and at startup.
    """
    entry = _data.get(int(guild_id))
    if entry is None:
        return True
    return bool(entry.get("authorized", False))


async def set_unauthorized(guild_id: int, reason: str) -> None:
    """Mark a guild unauthorized and persist. Used by the caller after a leave."""
    gid = int(guild_id)
    async with _lock:
        _data[gid] = {
            "authorized": False,
            "reason": reason,
            "checked_at": int(time.time()),
        }
        await _save()
    log.warning("guild_policy: marked guild %s unauthorized (%s)", gid, reason)


async def set_authorized(guild_id: int) -> None:
    """Mark a guild authorized and persist (e.g. after an admin joins)."""
    gid = int(guild_id)
    async with _lock:
        _data[gid] = {
            "authorized": True,
            "reason": "administrator present",
            "checked_at": int(time.time()),
        }
        await _save()


async def clear(guild_id: int) -> None:
    """Drop the policy entry for a guild (used on guild remove)."""
    gid = int(guild_id)
    async with _lock:
        if gid in _data:
            del _data[gid]
            await _save()
            log.info("guild_policy: cleared policy for guild %s", gid)

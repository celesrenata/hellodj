"""HelloDJ — Guild authorization policy.

Enforces the rule: *New guilds must be explicitly approved by a bot
administrator (via the web-ui admin portal) before HelloDJ will operate.*

- When the bot joins a new guild, it enters a **pending** state.
- While pending, the bot refuses all commands and notifies the guild.
- An administrator must approve the guild via the admin portal.
- If not approved within 24 hours, the bot automatically leaves.
- Previously approved guilds remain approved across restarts.

State is persisted to ``data/guild_policy.json`` (same NFS data mount as the
other stores).

Schema of data/guild_policy.json:
    { "<guild_id>": { "status": "approved"|"pending"|"denied",
                      "reason": str, "checked_at": int, "name": str }, ... }
"""

import asyncio
import json
import logging
import os
import time

import oauth_store

log = logging.getLogger(__name__)

POLICY_FILE = "data/guild_policy.json"
PENDING_EXPIRY_SECONDS = 24 * 60 * 60  # 24 hours

# guild_id (int) -> {"status": str, "reason": str, "checked_at": int, "name": str}
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
            # Migrate old format: "authorized" bool -> "status" string
            if "status" not in entry and "authorized" in entry:
                entry["status"] = "approved" if entry["authorized"] else "denied"
            new[gid] = {
                "status": entry.get("status", "pending"),
                "reason": str(entry.get("reason", "")),
                "checked_at": int(entry.get("checked_at", 0) or 0),
                "name": entry.get("name", ""),
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
    """Return the set of bot-administrator Discord user ids."""
    from config import cfg
    ids: set[int] = set()
    owner_env = cfg("discord.owner_id", "")
    if owner_env:
        try:
            ids.add(int(owner_env))
        except (TypeError, ValueError):
            log.warning("HelloDJ could not parse BOT_OWNER_ID %r", owner_env)
    ids |= oauth_store.get_admin_ids()
    return ids


async def check_guild(guild) -> str:
    """Check a guild's authorization status on join.

    Returns the status: "approved", "pending", or "denied".
    New guilds get "pending" status. Previously approved guilds stay approved.
    """
    gid = int(guild.id)
    name = getattr(guild, "name", "?")

    # If already in policy, return existing status
    entry = _data.get(gid)
    if entry and entry.get("status") == "approved":
        log.info("guild_policy: guild %s (%s) already approved", name, gid)
        return "approved"

    if entry and entry.get("status") == "denied":
        log.info("guild_policy: guild %s (%s) is denied", name, gid)
        return "denied"

    # New guild or pending — set to pending
    async with _lock:
        _data[gid] = {
            "status": "pending",
            "reason": "awaiting admin approval",
            "checked_at": int(time.time()),
            "name": name,
        }
        await _save()
    log.info("guild_policy: guild %s (%s) set to PENDING — awaiting approval", name, gid)
    return "pending"


async def is_authorized(guild_id: int) -> bool:
    """Return whether the guild is approved for operation."""
    entry = _data.get(int(guild_id))
    if entry is None:
        return False  # Unknown guilds are not authorized
    return entry.get("status") == "approved"


async def approve_guild(guild_id: int) -> None:
    """Approve a guild (called from admin portal)."""
    gid = int(guild_id)
    async with _lock:
        entry = _data.get(gid, {})
        _data[gid] = {
            "status": "approved",
            "reason": "approved by administrator",
            "checked_at": int(time.time()),
            "name": entry.get("name", ""),
        }
        await _save()
    log.info("guild_policy: guild %s approved by administrator", gid)


async def deny_guild(guild_id: int) -> None:
    """Deny a guild (called from admin portal)."""
    gid = int(guild_id)
    async with _lock:
        entry = _data.get(gid, {})
        _data[gid] = {
            "status": "denied",
            "reason": "denied by administrator",
            "checked_at": int(time.time()),
            "name": entry.get("name", ""),
        }
        await _save()
    log.warning("guild_policy: guild %s denied by administrator", gid)


async def set_unauthorized(guild_id: int, reason: str) -> None:
    """Mark a guild denied and persist."""
    gid = int(guild_id)
    async with _lock:
        entry = _data.get(gid, {})
        _data[gid] = {
            "status": "denied",
            "reason": reason,
            "checked_at": int(time.time()),
            "name": entry.get("name", ""),
        }
        await _save()
    log.warning("guild_policy: marked guild %s denied (%s)", gid, reason)


async def set_authorized(guild_id: int) -> None:
    """Mark a guild approved and persist."""
    await approve_guild(guild_id)


async def clear(guild_id: int) -> None:
    """Drop the policy entry for a guild (used on guild remove)."""
    gid = int(guild_id)
    async with _lock:
        if gid in _data:
            del _data[gid]
            await _save()
            log.info("guild_policy: cleared policy for guild %s", gid)


def get_pending_guilds() -> list[dict]:
    """Return all guilds in pending status (for the admin portal)."""
    now = int(time.time())
    pending = []
    for gid, entry in _data.items():
        if entry.get("status") == "pending":
            pending.append({
                "guild_id": str(gid),
                "name": entry.get("name", "Unknown"),
                "pending_since": entry.get("checked_at", 0),
                "expires_in": max(0, PENDING_EXPIRY_SECONDS - (now - entry.get("checked_at", 0))),
            })
    return pending


def get_all_guilds() -> list[dict]:
    """Return all guilds and their status (for the admin portal)."""
    return [
        {
            "guild_id": str(gid),
            "name": entry.get("name", "Unknown"),
            "status": entry.get("status", "unknown"),
            "reason": entry.get("reason", ""),
            "checked_at": entry.get("checked_at", 0),
        }
        for gid, entry in _data.items()
    ]


async def expire_pending_guilds(bot) -> list[int]:
    """Remove guilds that have been pending for > 24 hours. Returns guild IDs removed.

    Should be called periodically (e.g. from the watchdog).
    """
    now = int(time.time())
    expired = []

    for gid, entry in list(_data.items()):
        if entry.get("status") != "pending":
            continue
        elapsed = now - entry.get("checked_at", 0)
        if elapsed > PENDING_EXPIRY_SECONDS:
            expired.append(gid)

    for gid in expired:
        async with _lock:
            _data[gid] = {
                "status": "denied",
                "reason": "expired — not approved within 24 hours",
                "checked_at": now,
                "name": _data.get(gid, {}).get("name", ""),
            }
            await _save()
        log.warning("guild_policy: guild %s expired (not approved in 24h)", gid)
        # Leave the guild
        guild = bot.get_guild(gid)
        if guild:
            try:
                system_channel = getattr(guild, "system_channel", None)
                if system_channel and system_channel.permissions_for(guild.me).send_messages:
                    await system_channel.send(
                        "HelloDJ was not approved for this server within 24 hours. "
                        "Leaving. Contact a HelloDJ administrator to get approved."
                    )
                await guild.leave()
                log.info("guild_policy: left expired guild %s (%s)", guild.name, gid)
            except Exception as exc:
                log.error("guild_policy: could not leave expired guild %s: %s", gid, exc)

    return expired

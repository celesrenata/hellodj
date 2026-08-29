"""Shared helpers for the guild route modules.

Small, dependency-light helpers used by both :mod:`guild_routes` and
:mod:`guild_bot_routes`. Kept in their own module so the two route modules can
each import them WITHOUT importing each other (avoids a circular import when
``guild_routes`` registers the bot routes from ``guild_bot_routes``).
"""

from __future__ import annotations

from typing import Any

from flask import current_app, session

from guild_admin_service import can_manage_guild

__all__ = [
    "svc",
    "current_user",
    "require_login",
    "can_manage",
    "user_guild_list",
]


def svc(name: str) -> Any:
    """Return an app-extension service by name, or ``None`` in degraded mode."""
    return current_app.extensions.get(name)


def current_user() -> dict[str, Any]:
    """Return the authenticated session user mapping (empty when none)."""
    return session.get("user") or {}


def require_login() -> bool:
    """Return whether an authenticated session exists."""
    return bool(session.get("user"))


def can_manage(guild_id: str) -> bool:
    """Resolve the authorization facts and apply ``can_manage_guild`` (R5.2)."""
    user = current_user()
    guild_admin = svc("guild_admin")
    owner_sub = guild_admin.owner_of(guild_id) if guild_admin else None
    admin_ids = (
        guild_admin.admin_discord_ids(guild_id) if guild_admin else set()
    )
    return can_manage_guild(
        guild_id=guild_id,
        user_sub=user.get("sub"),
        discord_id=user.get("discord_id"),
        is_super_admin=bool(user.get("is_admin")),
        owner_sub=owner_sub,
        admin_discord_ids=admin_ids,
    )


def user_guild_list() -> list[dict[str, Any]]:
    """Return the current user's guilds (owned + administered) for rendering.

    Sourced from :class:`GuildAdminService`: guilds the session subject OWNS
    (via the ``OWNER#<sub>`` reverse index) plus guilds the session's linked
    Discord id ADMINISTERS. Rows are ``{guild_id, name}`` deduplicated by id; an
    administered guild with no stored name shows its id. Degrades to an empty
    list (no service, or a logged-out/anonymous session) so the page still
    renders. This is what makes a just-claimed guild appear in the list.
    """
    guild_admin = svc("guild_admin")
    user = current_user()
    sub = user.get("sub", "")
    discord_id = user.get("discord_id", "")
    if guild_admin is None or not sub:
        return []
    rows: dict[str, dict[str, Any]] = {}
    for g in guild_admin.guilds_owned_by(sub):
        rows[g["guild_id"]] = {"guild_id": g["guild_id"], "name": g["name"]}
    if discord_id:
        for gid in guild_admin.guilds_administered_by_discord(discord_id):
            if gid not in rows:
                rows[gid] = {
                    "guild_id": gid,
                    "name": guild_admin.guild_name(gid),
                }
    return sorted(rows.values(), key=lambda r: (r["name"] or r["guild_id"]))

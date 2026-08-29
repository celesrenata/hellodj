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

__all__ = ["svc", "current_user", "require_login", "can_manage"]


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

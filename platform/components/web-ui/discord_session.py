"""Discord-login session establishment.

Resolves the account a Discord OAuth login lands in and sets ``session['user']``
accordingly. Two paths:

* the Discord id is linked to its OWN account (via the ``user_for_discord`` GSI1
  reverse index) — the normal returning-user login (R3.2); or
* the Discord id is an appointed **account co-admin** — Option B, where the
  appointed id logs straight INTO the owner's account (shared access). The
  session identity becomes the owner's Cognito subject; the acting co-admin's
  own Discord id is recorded separately for auditability.

A Discord id that is neither linked nor appointed leaves the session untouched
so the caller can bounce it to login with a ``not_linked`` error.

Extracted from :mod:`auth` to keep that module under the 500-line ceiling.
"""

from __future__ import annotations

from flask import current_app, session
from hellodj_platform_logic.types import AuthProvider

__all__ = ["establish_discord_session"]


def establish_discord_session(discord_id: str) -> None:
    """Establish an authenticated Discord-login session for ``discord_id``.

    Sets ``session['user']`` when the Discord id resolves to an account (its own
    linked account, or — Option B — an account it co-administers). Leaves the
    session untouched when it resolves to nothing, so the caller can react.
    """
    profiles = current_app.extensions.get("user_profiles")
    if not profiles:
        return
    sub = profiles.user_for_discord(discord_id)
    acting_as_admin = False
    if not sub:
        # Not the Discord id's OWN account: it may be an appointed account
        # co-admin. Option B — the appointed id logs straight INTO the owner's
        # account (shared access), so resolve the owner subject and establish
        # the session as the owner. A Discord id that is neither linked nor
        # appointed cannot log in (the caller bounces to login with not_linked).
        sub = _owner_for_account_admin(discord_id)
        acting_as_admin = bool(sub)
    if not sub:
        return
    profile = profiles.get(sub) or {}
    session["user"] = {
        "provider": AuthProvider.DISCORD_OAUTH.value,
        "sub": sub,
        # Preserve the OWNER's linked Discord id (from the profile) as the
        # session identity so guild/source ownership checks keep resolving to
        # the owner; the co-admin's own Discord id is recorded separately for
        # audit without altering the owner-scoped authorization facts.
        "discord_id": profile.get("discord_id", discord_id),
        "discord_linked": True,
        "email": profile.get("email", ""),
        # When a co-admin is acting on the owner's account, record who is
        # actually driving the session (their Discord id) for auditability.
        "acting_as_account_admin": acting_as_admin,
        "admin_actor_discord_id": discord_id if acting_as_admin else "",
    }


def _owner_for_account_admin(discord_id: str) -> str | None:
    """Return the owner subject a Discord id co-administers, or ``None``.

    Consults :class:`AccountAdminService` (the account-delegated-admin
    allowlist). Returns ``None`` in degraded mode (no service wired) so an
    unappointed Discord id simply cannot log in this way.
    """
    acct_admin = current_app.extensions.get("account_admin")
    if not acct_admin:
        return None
    return acct_admin.owner_for_discord(discord_id)

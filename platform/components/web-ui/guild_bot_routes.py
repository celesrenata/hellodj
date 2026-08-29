"""Per-guild bot-application routes: assign, release, rename, avatar.

Extracted from :mod:`guild_routes` to keep that module within the 500-line
ceiling (mirrors the :mod:`source_account_routes` split).

A guild draws bots from the GLOBAL application pool
(:class:`bot_app_pool.BotAppAssignmentService`). Each assigned bot is a distinct
Discord application, so a guild can run several at once. Identity is per bot
(:class:`bot_identity.BotIdentityService` keyed by ``client_id``):

* Default name (no ``custom_name`` entitlement): ``HelloDJ`` for the first bot,
  then ``HelloDJ#1``, ``HelloDJ#2``, … by claim index
  (:func:`bot_identity.default_bot_name`).
* With the guild OWNER's ``custom_name`` / ``custom_avatar`` entitlement the
  owner may rename / set an avatar per bot; without it those controls are
  disabled and rejected server-side.

Every route is ownership-gated via :func:`guild_routes._can_manage` and the
identity routes additionally require the owner's entitlement.
"""

from __future__ import annotations

from typing import Any

from flask import (
    Blueprint,
    redirect,
    render_template,
    request,
    url_for,
)

from guild_common import (
    can_manage as _can_manage,
)
from guild_common import (
    current_user as _user,
)
from guild_common import (
    require_login as _require_login,
)
from guild_common import (
    svc as _svc,
)

__all__ = [
    "register_bot_routes",
    "guild_owner_entitlements",
    "guild_bot_max",
    "bot_context",
    "render_bots",
]


def guild_owner_entitlements(guild_id: str) -> dict[str, Any]:
    """Return the guild OWNER's effective entitlements (empty if unresolved)."""
    guild_admin = _svc("guild_admin")
    ent = _svc("entitlement_service")
    owner_sub = guild_admin.owner_of(guild_id) if guild_admin else None
    if ent is None or not owner_sub:
        return {}
    return ent.get_effective(owner_sub)


def guild_bot_max(guild_id: str) -> int:
    """Return the max simultaneous bots this guild may run (owner entitlement).

    Resolves the guild OWNER's ``max_bots_per_guild`` via the shared
    :func:`entitlements_core.effective_max_bots_per_guild`, defaulting to the
    secure baseline of 1 when no owner/entitlement is resolvable.
    """
    from entitlements_core import effective_max_bots_per_guild  # noqa: PLC0415

    ent = guild_owner_entitlements(guild_id)
    if not ent:
        return 1
    return effective_max_bots_per_guild(ent)


def bot_context(guild_id: str) -> dict[str, Any]:
    """Return the bots-panel render context (claims, quota, per-bot identity).

    Each bot row is enriched with its effective display name (a custom nickname
    when the owner has ``custom_name`` and one is set, else the default
    ``HelloDJ`` / ``HelloDJ#N`` by claim index), its avatar status, and the
    ``custom_name`` / ``custom_avatar`` gates for the rename + avatar controls.
    """
    from bot_identity import default_bot_name  # noqa: PLC0415

    assign = _svc("bot_app_assignment")
    identity_svc = _svc("guild_identity_service")
    ent = guild_owner_entitlements(guild_id)
    can_name = bool(ent.get("custom_name", False))
    can_avatar = bool(ent.get("custom_avatar", False))
    base = {
        "bot_max": guild_bot_max(guild_id),
        "can_custom_name": can_name,
        "can_custom_avatar": can_avatar,
    }
    if assign is None:
        return {**base, "bots": [], "pool_size": 0}
    bots = assign.list_claims(guild_id)
    for bot in bots:
        ident = (
            identity_svc.get_identity(guild_id, client_id=bot["client_id"])
            if identity_svc
            else {}
        )
        nickname = ident.get("nickname", "") if can_name else ""
        bot["default_name"] = default_bot_name(bot["index"])
        bot["nickname"] = nickname
        bot["display_name"] = nickname or bot["default_name"]
        bot["avatar_present"] = bool(ident.get("avatar_present", False))
        bot["apply_status"] = ident.get("apply_status", "none")
    return {**base, "bots": bots, "pool_size": assign.pool_size()}


def render_bots(
    guild_id: str, *, add_error: str = "", upload_error: str = ""
) -> str:
    """Render the bots partial (HTMX swap target) with claims + identity."""
    ctx = bot_context(guild_id)
    return render_template(
        "partials/guild_bot_list.html",
        guild_id=guild_id,
        bots=ctx["bots"],
        bot_max=ctx["bot_max"],
        pool_size=ctx["pool_size"],
        can_custom_name=ctx["can_custom_name"],
        can_custom_avatar=ctx["can_custom_avatar"],
        add_error=add_error,
        upload_error=upload_error,
    )


def register_bot_routes(bp: Blueprint) -> None:
    """Register the per-guild bot assign/release/rename/avatar routes on ``bp``."""

    @bp.route("/guilds/<guild_id>/bots", methods=["POST"])
    def add_bot(guild_id: str):  # type: ignore[unused-ignore]
        """Assign the next free pool bot to this guild. Ownership-gated.

        Enforces the guild owner's ``max_bots_per_guild`` entitlement and
        assigns the next unclaimed global-pool application; surfaces a clear
        quota/pool-exhausted message in the bots partial on failure.
        """
        if not _require_login():
            return redirect(url_for("pages.login"))
        if not _can_manage(guild_id):
            return redirect(url_for("pages.guilds"))
        from bot_app_pool import (  # noqa: PLC0415
            PoolExhaustedError,
            QuotaReachedError,
        )

        assign = _svc("bot_app_assignment")
        add_error = ""
        if assign is not None:
            try:
                assign.assign_next(
                    guild_id,
                    max_bots=guild_bot_max(guild_id),
                    claimed_by=_user().get("sub", ""),
                )
            except (QuotaReachedError, PoolExhaustedError) as exc:
                add_error = str(exc)
        return render_bots(guild_id, add_error=add_error)

    @bp.route("/guilds/<guild_id>/bots/<client_id>/remove", methods=["POST"])
    def remove_bot(guild_id: str, client_id: str):  # type: ignore[unused-ignore]
        """Release this guild's claim on a pool bot. Ownership-gated."""
        if not _require_login():
            return redirect(url_for("pages.login"))
        if not _can_manage(guild_id):
            return redirect(url_for("pages.guilds"))
        assign = _svc("bot_app_assignment")
        if assign is not None:
            assign.release(guild_id, client_id)
        return render_bots(guild_id)

    @bp.route("/guilds/<guild_id>/bots/<client_id>/name", methods=["POST"])
    def set_bot_name(guild_id: str, client_id: str):  # type: ignore[unused-ignore]
        """Rename a specific bot. Ownership + ``custom_name`` entitlement gated.

        A guild whose owner lacks ``custom_name`` cannot set a custom nickname
        (control disabled in the UI and rejected here) — the bot then shows the
        default ``HelloDJ`` / ``HelloDJ#N`` name.
        """
        if not _require_login():
            return redirect(url_for("pages.login"))
        if not _can_manage(guild_id):
            return redirect(url_for("pages.guilds"))
        if not guild_owner_entitlements(guild_id).get("custom_name", False):
            return render_bots(
                guild_id,
                add_error="Renaming bots is not enabled for this guild.",
            )
        identity_svc = _svc("guild_identity_service")
        nickname = request.form.get("nickname", "").strip()
        if identity_svc is not None:
            identity_svc.set_nickname(
                guild_id,
                nickname,
                requested_by=_user().get("sub", ""),
                client_id=client_id,
            )
        return render_bots(guild_id)

    @bp.route("/guilds/<guild_id>/bots/<client_id>/avatar", methods=["POST"])
    def set_bot_avatar_pooled(guild_id: str, client_id: str):  # type: ignore[unused-ignore]
        """Set a specific bot's avatar. Ownership + ``custom_avatar`` gated."""
        if not _require_login():
            return redirect(url_for("pages.login"))
        if not _can_manage(guild_id):
            return redirect(url_for("pages.guilds"))
        if not guild_owner_entitlements(guild_id).get("custom_avatar", False):
            return render_bots(
                guild_id,
                add_error="Custom bot avatars are not enabled for this guild.",
            )
        identity_svc = _svc("guild_identity_service")
        upload = request.files.get("avatar")
        upload_error = ""
        if identity_svc is not None and upload is not None:
            from bot_identity import AvatarValidationError  # noqa: PLC0415

            try:
                identity_svc.set_avatar(
                    guild_id,
                    upload.read(),
                    requested_by=_user().get("sub", ""),
                    client_id=client_id,
                )
            except AvatarValidationError as exc:
                upload_error = str(exc)
        return render_bots(guild_id, upload_error=upload_error)

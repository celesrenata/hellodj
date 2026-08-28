"""Guild management + per-guild source routes (user self-service).

Covers the authenticated-user surface:

* ``/account``            — profile + Discord link status
* ``/guilds/<gid>``       — manage a guild's admins + sources
* appoint / remove Guild_Admins by Discord id
* connect / disconnect per-guild source OAuth

Every guild/source route is gated by ``_can_manage`` (the pure
``can_manage_guild`` decision) so a user can only touch guilds they own or
administer, and per-guild source secrets stay isolated (R4.3, R5.2).

Requirements: 3, 4, 5
"""

from __future__ import annotations

from typing import Any

from flask import (
    Blueprint,
    current_app,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from guild_admin_service import can_manage_guild
from guild_sources import SUPPORTED_PROVIDERS
from source_oauth import source_provider_configured

__all__ = ["build_guild_blueprint"]


def _svc(name: str) -> Any:
    return current_app.extensions.get(name)


def _user() -> dict[str, Any]:
    return session.get("user") or {}


def _require_login() -> bool:
    return bool(session.get("user"))


def _can_manage(guild_id: str) -> bool:
    """Resolve the authorization facts and apply ``can_manage_guild`` (R5.2)."""
    user = _user()
    guild_admin = _svc("guild_admin")
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


def build_guild_blueprint() -> Blueprint:
    """Construct the guild-management + per-guild-sources blueprint."""
    bp = Blueprint("guild", __name__)

    @bp.route("/account")
    def account():  # type: ignore[unused-ignore]
        """User profile + Discord link status."""
        if not _require_login():
            return redirect(url_for("pages.login"))
        profiles = _svc("user_profiles")
        user = _user()
        profile = profiles.get(user["sub"]) if profiles and user.get("sub") else {}
        return render_template(
            "pages/account.html",
            layout=_layout(),
            nav_items=_nav(),
            active="account",
            profile=profile,
        )

    @bp.route("/guilds/<guild_id>")
    def guild_detail(guild_id: str):  # type: ignore[unused-ignore]
        """Manage a single guild's admins + sources. Ownership-gated."""
        if not _require_login():
            return redirect(url_for("pages.login"))
        if not _can_manage(guild_id):
            return redirect(url_for("pages.guilds"))
        guild_admin = _svc("guild_admin")
        sources = _svc("guild_sources")
        identity_svc = _svc("guild_identity_service")
        # Which providers are wired for interactive OAuth (client id present).
        # Unconfigured providers render a disabled "Needs setup" control rather
        # than an active Connect link that would silently no-op (R2.1, R1.2).
        providers_configured = {
            p: source_provider_configured(p) for p in SUPPORTED_PROVIDERS
        }
        return render_template(
            "pages/guild_detail.html",
            layout=_layout(),
            nav_items=_nav(),
            active="guilds",
            guild_id=guild_id,
            admins=guild_admin.list_admins(guild_id) if guild_admin else [],
            sources=sources.status(guild_id) if sources else [],
            providers=SUPPORTED_PROVIDERS,
            providers_configured=providers_configured,
            identity=(
                identity_svc.get_identity(guild_id) if identity_svc else {}
            ),
            error=request.args.get("error", ""),
            error_provider=request.args.get("provider", ""),
        )

    @bp.route("/guilds/<guild_id>/identity/nickname", methods=["POST"])
    def set_bot_nickname(guild_id: str):  # type: ignore[unused-ignore]
        """Persist the bot's desired server nickname. Ownership-gated (R2.7)."""
        if not _require_login():
            return redirect(url_for("pages.login"))
        if not _can_manage(guild_id):
            return redirect(url_for("pages.guilds"))
        identity_svc = _svc("guild_identity_service")
        nickname = request.form.get("nickname", "").strip()
        if identity_svc is not None:
            identity_svc.set_nickname(
                guild_id, nickname, requested_by=_user().get("sub", "")
            )
        return _render_identity(guild_id, identity_svc)

    @bp.route("/guilds/<guild_id>/identity/avatar", methods=["POST"])
    def set_bot_avatar(guild_id: str):  # type: ignore[unused-ignore]
        """Persist the bot's desired per-guild avatar. Ownership-gated (R2.8)."""
        if not _require_login():
            return redirect(url_for("pages.login"))
        if not _can_manage(guild_id):
            return redirect(url_for("pages.guilds"))
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
                )
            except AvatarValidationError as exc:
                upload_error = str(exc)
        return _render_identity(
            guild_id, identity_svc, upload_error=upload_error
        )

    @bp.route("/guilds/<guild_id>/admins", methods=["POST"])
    def appoint_admin(guild_id: str):  # type: ignore[unused-ignore]
        """Appoint a Discord id as a guild admin. Ownership-gated (R4.1)."""
        if not _require_login():
            return redirect(url_for("pages.login"))
        if not _can_manage(guild_id):
            return redirect(url_for("pages.guilds"))
        discord_id = request.form.get("discord_id", "").strip()
        guild_admin = _svc("guild_admin")
        if guild_admin and discord_id.isdigit():
            guild_admin.appoint_admin(
                guild_id, discord_id, appointed_by=_user().get("sub", "")
            )
        return render_template(
            "partials/guild_admin_list.html",
            guild_id=guild_id,
            admins=guild_admin.list_admins(guild_id) if guild_admin else [],
        )

    @bp.route("/guilds/<guild_id>/admins/<discord_id>/remove", methods=["POST"])
    def remove_admin(guild_id: str, discord_id: str):  # type: ignore[unused-ignore]
        """Remove a Discord-id guild admin. Ownership-gated (R4.2)."""
        if not _require_login():
            return redirect(url_for("pages.login"))
        if not _can_manage(guild_id):
            return redirect(url_for("pages.guilds"))
        guild_admin = _svc("guild_admin")
        if guild_admin:
            guild_admin.remove_admin(guild_id, discord_id)
        return render_template(
            "partials/guild_admin_list.html",
            guild_id=guild_id,
            admins=guild_admin.list_admins(guild_id) if guild_admin else [],
        )

    @bp.route("/guilds/<guild_id>/sources/<provider>/disconnect", methods=["POST"])
    def disconnect_source(guild_id: str, provider: str):  # type: ignore[unused-ignore]
        """Disconnect (delete) a guild's per-provider source. Ownership-gated."""
        if not _require_login():
            return redirect(url_for("pages.login"))
        if not _can_manage(guild_id):
            return redirect(url_for("pages.guilds"))
        sources = _svc("guild_sources")
        if sources and sources.is_supported(provider):
            sources.disconnect(guild_id, provider)
        return render_template(
            "partials/guild_source_list.html",
            guild_id=guild_id,
            sources=sources.status(guild_id) if sources else [],
            providers_configured={
                p: source_provider_configured(p) for p in SUPPORTED_PROVIDERS
            },
        )

    return bp


def _render_identity(
    guild_id: str, identity_svc: Any, *, upload_error: str = ""
) -> str:
    """Render the identity form partial with the current apply status.

    Reused by both identity routes so the HTMX swap surfaces the bot-applier's
    ``apply_status`` / ``apply_error`` (Pending / Applied / error) and any
    upload-time validation error (R2.8, R2.9).
    """
    return render_template(
        "partials/guild_identity_form.html",
        guild_id=guild_id,
        identity=identity_svc.get_identity(guild_id) if identity_svc else {},
        upload_error=upload_error,
    )


# ---- shared helpers imported from pages to keep one nav/layout source ---- #


def _layout() -> str:
    from pages import _layout as pages_layout  # noqa: PLC0415

    return pages_layout()


def _nav() -> list[dict[str, Any]]:
    from pages import _nav_for_current_user  # noqa: PLC0415

    return _nav_for_current_user()

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
    url_for,
)

from guild_bot_routes import (
    bot_context,
    primary_bot_invite_url,
    register_bot_routes,
)
from guild_common import can_manage, current_user, require_login, svc
from guild_sources import SUPPORTED_PROVIDERS
from source_oauth import source_provider_configured

__all__ = ["build_guild_blueprint", "OAUTH_SOURCE_PROVIDERS"]

#: The OAuth providers offered on the per-user account connections panel
#: (R1.1). SoundCloud is intentionally excluded — it is search-only and needs
#: no OAuth (R1.7). This mirrors the per-guild ``SUPPORTED_PROVIDERS`` set.
OAUTH_SOURCE_PROVIDERS = SUPPORTED_PROVIDERS


def _account_source_status(sub: str) -> dict[str, Any]:
    """Return a ``{provider: status}`` map for the account panel (no token).

    Reads plaintext status from :class:`SourceCredentialService` keyed by the
    user's ``sub`` — never decrypts, never returns a token value (R8.1, R8.3).
    Degrades to an empty map when no unified store is wired or no sub is known.
    """
    creds = current_app.extensions.get("source_credentials")
    if creds is None or not sub:
        return {}
    return {row["provider"]: row for row in creds.status(sub)}


def _account_providers_configured() -> dict[str, bool]:
    """Return which account providers have their OAuth client id configured.

    An unconfigured provider renders a disabled "Needs setup" control instead
    of an active Connect link that would silently no-op (R1.2).
    """
    return {p: source_provider_configured(p) for p in OAUTH_SOURCE_PROVIDERS}


#: Thin aliases to the shared guild helpers (kept for minimal churn across this
#: module's routes). Their canonical home is :mod:`guild_common` so the bot
#: route module can import them without a circular dependency.
_svc = svc
_user = current_user
_require_login = require_login
_can_manage = can_manage


def build_guild_blueprint() -> Blueprint:
    """Construct the guild-management + per-guild-sources blueprint."""
    bp = Blueprint("guild", __name__)

    @bp.route("/account")
    def account():  # type: ignore[unused-ignore]
        """User profile + Discord link status + per-user source connections.

        Renders the Discord link control (linked/not-linked + enable + reset,
        R8.4) and a per-provider connections panel over the unified per-user
        source-credential store keyed by the session ``sub`` (R1.1). Each
        provider shows its plaintext status (connected / last-refresh /
        refresh_status) with NO token value ever rendered (R8.1, R8.3), and a
        Connect control that is active only when the provider's OAuth client id
        is configured — otherwise a disabled "Needs setup" control (R1.2).
        SoundCloud is intentionally absent (search-only, R1.7).
        """
        if not _require_login():
            return redirect(url_for("pages.login"))
        profiles = _svc("user_profiles")
        user = _user()
        sub = user.get("sub", "")
        profile = profiles.get(sub) if profiles and sub else {}
        ent = _svc("entitlement_service")
        acct_admin = _svc("account_admin")
        return render_template(
            "pages/account.html",
            layout=_layout(),
            nav_items=_nav(),
            active="account",
            profile=profile,
            providers=OAUTH_SOURCE_PROVIDERS,
            source_status=_account_source_status(sub),
            providers_configured=_account_providers_configured(),
            entitlements=(ent.get_effective(sub) if ent and sub else {}),
            entitlements_available=bool(ent and sub),
            tally=(ent.get_tally(sub) if ent and sub else {}),
            account_admins=(
                acct_admin.list_admins(sub) if acct_admin and sub else []
            ),
            connected=request.args.get("connected", ""),
            error=request.args.get("error", ""),
            error_provider=request.args.get("provider", ""),
        )

    @bp.route("/account/admins", methods=["POST"])
    def account_appoint_admin():  # type: ignore[unused-ignore]
        """Appoint a Discord id as a co-admin of the caller's OWN account.

        Keyed by the caller's session ``sub`` — a user can only appoint admins
        on their own account, never another's. The Discord id must be numeric
        (a Discord user id); a non-numeric value is ignored (no partial write).
        Returns the account-admin list partial for an HTMX swap. Idempotent via
        the service. No-ops in degraded mode (no account_admin service).
        """
        if not _require_login():
            return redirect(url_for("pages.login"))
        sub = _user().get("sub", "")
        acct_admin = _svc("account_admin")
        discord_id = request.form.get("discord_id", "").strip()
        if acct_admin and sub and discord_id.isdigit():
            acct_admin.appoint_admin(sub, discord_id)
        return render_template(
            "partials/account_admin_list.html",
            account_admins=(
                acct_admin.list_admins(sub) if acct_admin and sub else []
            ),
        )

    @bp.route("/account/admins/<discord_id>/remove", methods=["POST"])
    def account_remove_admin(discord_id: str):  # type: ignore[unused-ignore]
        """Remove a Discord-id co-admin from the caller's OWN account.

        Keyed by the caller's session ``sub`` so a user can only revoke admins
        on their own account. Returns the refreshed list partial (HTMX swap).
        """
        if not _require_login():
            return redirect(url_for("pages.login"))
        sub = _user().get("sub", "")
        acct_admin = _svc("account_admin")
        if acct_admin and sub:
            acct_admin.remove_admin(sub, discord_id)
        return render_template(
            "partials/account_admin_list.html",
            account_admins=(
                acct_admin.list_admins(sub) if acct_admin and sub else []
            ),
        )

    @bp.route("/account/sources/<provider>/disconnect", methods=["POST"])
    def account_disconnect_source(provider: str):  # type: ignore[unused-ignore]
        """Disconnect (delete) the user's credential for a provider (R8.2).

        Deletes only the calling user's ``SOURCECRED#<provider>`` item via
        :meth:`SourceCredentialService.disconnect` and returns the refreshed
        connections partial so the change reflects without a full page reload
        (HTMX partial). No-ops safely in degraded mode (no unified store) and
        never touches another user's credential (keyed by the session sub).
        """
        if not _require_login():
            return redirect(url_for("pages.login"))
        sub = _user().get("sub", "")
        creds = _svc("source_credentials")
        if creds is not None and sub and provider in OAUTH_SOURCE_PROVIDERS:
            creds.disconnect(sub, provider)
        return render_template(
            "partials/account_source_list.html",
            providers=OAUTH_SOURCE_PROVIDERS,
            source_status=_account_source_status(sub),
            providers_configured=_account_providers_configured(),
        )

    @bp.route("/account/discord/reset", methods=["POST"])
    def account_discord_reset():  # type: ignore[unused-ignore]
        """Reset (unlink) the user's Discord link, then re-render the control.

        Clears the Discord id + GSI1 reverse index for the calling user via
        :meth:`UserProfileService.unlink_discord` (R8.4) and returns the Discord
        link partial reflecting the now-unlinked state (HTMX swap). No-ops in
        degraded mode; never affects another account.
        """
        if not _require_login():
            return redirect(url_for("pages.login"))
        profiles = _svc("user_profiles")
        sub = _user().get("sub", "")
        if profiles is not None and sub:
            profiles.unlink_discord(sub)
        profile = profiles.get(sub) if profiles and sub else {}
        return render_template(
            "partials/account_discord_link.html",
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
        identity_svc = _svc("guild_identity_service")
        activation = _svc("guild_activation")
        bots_ctx = bot_context(guild_id)
        # Sources are PER-USER, not per-guild: a guild "uses" a source by
        # binding to the managing user's connected credential (unified store,
        # keyed by the caller's sub). So the Sources tab reflects the manager's
        # OWN account connections and drives Connect through the per-account
        # flow (device-code for YouTube, fixed-callback OAuth for Spotify/Tidal)
        # — NOT the deprecated per-guild connect route, which had no device-code
        # path (YouTube always failed) and used an unregistered per-guild
        # redirect URI (Spotify/Tidal failed the provider allowlist).
        sub = _user().get("sub", "")
        providers_configured = {
            p: source_provider_configured(p) for p in OAUTH_SOURCE_PROVIDERS
        }
        # Resolve the guild's stored Discord name (recorded at claim time) so
        # the page header reads e.g. "Guild Under the Influence" instead of the
        # raw snowflake id; falls back to '' (template shows "Guild <id>") when
        # the name is unknown (older claim that stored no name / degraded mode).
        guild_name = guild_admin.guild_name(guild_id) if guild_admin else ""
        return render_template(
            "pages/guild_detail.html",
            layout=_layout(),
            nav_items=_nav(),
            active="guilds",
            guild_id=guild_id,
            guild_name=guild_name,
            admins=guild_admin.list_admins(guild_id) if guild_admin else [],
            source_status=_account_source_status(sub),
            providers=OAUTH_SOURCE_PROVIDERS,
            providers_configured=providers_configured,
            identity=(
                identity_svc.get_identity(guild_id) if identity_svc else {}
            ),
            bots=bots_ctx["bots"],
            bot_max=bots_ctx["bot_max"],
            pool_size=bots_ctx["pool_size"],
            can_custom_name=bots_ctx["can_custom_name"],
            can_custom_avatar=bots_ctx["can_custom_avatar"],
            primary_invite_url=primary_bot_invite_url(),
            activation=_activation_context(activation, guild_id),
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

    # Per-guild bot-application routes (assign/release/rename/avatar) live in
    # guild_bot_routes to keep this module under the 500-line ceiling.
    register_bot_routes(bp)

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

    @bp.route("/guilds/<guild_id>/activation/regenerate", methods=["POST"])
    def regenerate_activation(guild_id: str):  # type: ignore[unused-ignore]
        """Regenerate the guild's activation key. Ownership-gated.

        Mints a NEW key and clears activation (the old key can no longer
        activate the guild — on-prem deactivate parity), then returns the
        activation partial for an HTMX swap.
        """
        if not _require_login():
            return redirect(url_for("pages.login"))
        if not _can_manage(guild_id):
            return redirect(url_for("pages.guilds"))
        activation = _svc("guild_activation")
        if activation is not None:
            activation.regenerate_key(guild_id)
        return render_template(
            "partials/guild_activation.html",
            guild_id=guild_id,
            activation=_activation_context(activation, guild_id),
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


def _activation_context(activation: Any, guild_id: str) -> dict[str, Any]:
    """Return the guild's activation ``{key, activated}`` for rendering.

    Generates the key on first view (on-prem dashboard parity) so the admin
    always has a key to run ``/activate <key>`` with. Degrades to an empty,
    not-activated context when no activation service is wired.
    """
    if activation is None:
        return {"key": "", "activated": False}
    key = activation.get_or_create_key(guild_id)
    status = activation.status(guild_id)
    return {"key": key, "activated": bool(status.get("activated", False))}


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

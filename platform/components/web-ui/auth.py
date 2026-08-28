"""Authentication blueprint for the web-ui.

Every authentication entry point routes through the shared
:func:`hellodj_platform_logic.auth_routing.route_auth` decision function so the
web-ui and the CDK infrastructure agree on which identity provider handles
which purpose (the auth-routing invariant):

* Admin auth / initial registration / account recovery -> **Cognito** hosted UI.
* Day-to-day login of a registered/appointed user -> **Discord OAuth**.
* Tidal source auth callback -> **first-party Tidal OAuth** (independent of
  Cognito), forwarded to the ``tidal-stream`` component.

OAuth client credentials come from Secrets Manager; no secret material is
embedded in code. State/PKCE values are kept in the signed Flask session.

Requirements: 8.1, 8.2, 8.3, 8.4, 8.5, 9.2, 9.5, 14.x
"""

from __future__ import annotations

import secrets as pysecrets
import urllib.parse
from typing import Any

from flask import (
    Blueprint,
    current_app,
    redirect,
    request,
    session,
    url_for,
)
from hellodj_platform_logic.auth_routing import route_auth
from hellodj_platform_logic.types import AuthProvider, AuthPurpose, UserType

import registration_mode
from auth_forms import (
    handle_login,
    handle_login_challenge,
    handle_recover,
    handle_register,
)
from auth_oauth import (
    discord_id_from_code,
)
from source_oauth import (
    source_authorize_url,
    source_tokens_from_request,
)
from source_token_exchange import compose_youtube_tokens, source_exchange_spotify

__all__ = ["auth_bp", "build_auth_blueprint"]

DISCORD_AUTHORIZE_URL = "https://discord.com/oauth2/authorize"


def _new_state() -> str:
    """Return a URL-safe random CSRF/state token."""
    return pysecrets.token_urlsafe(32)


def build_auth_blueprint() -> Blueprint:
    """Construct the auth blueprint.

    Routes rely on ``current_app.config`` for provider settings resolved at
    startup (Cognito domain/client id, Discord client id, redirect base, and
    the tidal-stream service URL) plus the :class:`SecretsProvider` stored on
    the app.
    """
    bp = Blueprint("auth", __name__, url_prefix="/auth")

    @bp.route("/login")
    def login():  # type: ignore[unused-ignore]
        """Start day-to-day login: routes to Discord OAuth by default (R8.4)."""
        provider = route_auth(
            AuthPurpose.DAY_TO_DAY_LOGIN, UserType.REGISTERED
        )
        assert provider is AuthProvider.DISCORD_OAUTH
        return _start_discord_oauth()

    @bp.route("/admin", methods=["GET", "POST"])
    def admin_login():  # type: ignore[unused-ignore]
        """Admin auth via first-party form calling Cognito server-side (R8.2).

        Routing still resolves to Cognito (the identity provider) — only the UI
        surface is first-party now, so the hosted UI is no longer used.
        """
        provider = route_auth(AuthPurpose.ADMIN_AUTH, UserType.ADMIN)
        assert provider is AuthProvider.COGNITO
        return handle_login()

    @bp.route("/admin/challenge", methods=["POST"])
    def admin_login_challenge():  # type: ignore[unused-ignore]
        """Complete a login challenge (new-password / MFA) (R1.3, R1.4)."""
        return handle_login_challenge()

    @bp.route("/register", methods=["GET", "POST"])
    def register():  # type: ignore[unused-ignore]
        """Initial registration via first-party Cognito ``SignUp`` form (R8.3).

        Gated by the global Registration_Mode: when CLOSED, both GET and POST
        are rejected with a redirect to the login page carrying a
        registration-closed notice, before the form is rendered or Cognito
        ``SignUp`` is invoked (R2.1, R2.2). When OPEN the existing first-party
        flow runs unchanged (R2.3, R2.4).
        """
        provider = route_auth(
            AuthPurpose.INITIAL_REGISTRATION, UserType.ANONYMOUS
        )
        assert provider is AuthProvider.COGNITO
        if not registration_mode.is_open(_global_config()):
            return redirect(url_for("pages.login", registration="closed"))
        return handle_register()

    @bp.route("/recover", methods=["GET", "POST"])
    def recover():  # type: ignore[unused-ignore]
        """Account recovery via first-party Cognito ``ForgotPassword`` (R8.5)."""
        provider = route_auth(
            AuthPurpose.ACCOUNT_RECOVERY, UserType.REGISTERED
        )
        assert provider is AuthProvider.COGNITO
        return handle_recover()

    @bp.route("/discord/callback")
    def discord_callback():  # type: ignore[unused-ignore]
        """Discord OAuth login callback: resolve the linked account (R3.2).

        A returning user who signs in via Discord is resolved to their Cognito
        subject through the GSI1 reverse index (``user_for_discord``); the
        session is then established without a password. A Discord identity that
        is not linked to any account cannot log in this way (they must register
        and link first), so we bounce back to login with a clear error rather
        than minting a session with no account behind it.
        """
        error = request.args.get("error")
        if error:
            return redirect(url_for("pages.login", error="denied"))
        state = request.args.get("state", "")
        if not state or state != session.pop("discord_state", None):
            return redirect(url_for("pages.login", error="state_mismatch"))
        code = request.args.get("code", "")
        discord_id = discord_id_from_code(
            code, _redirect_uri("auth.discord_callback")
        )
        if not discord_id:
            return redirect(url_for("pages.login", error="discord_failed"))
        _establish_discord_session(discord_id)
        if not session.get("user"):
            # No account is linked to this Discord identity (R3.2/R3.4): a
            # Discord login only works once the account has been linked.
            return redirect(url_for("pages.login", error="not_linked"))
        return redirect(url_for("pages.dashboard"))

    @bp.route("/discord/link")
    def discord_link():  # type: ignore[unused-ignore]
        """Start linking a user's Discord account (R3.1).

        Reached in two ways: an already-authenticated user linking Discord
        after the fact, OR the post-registration handoff where the invitee has
        just registered (``session['pending_link_sub']`` carries their Cognito
        subject but no authenticated session — R2.4). Either context is enough
        to begin the OAuth flow; without one we bounce to login.
        """
        if not _link_subject():
            return redirect(url_for("pages.login"))
        state = _new_state()
        session["discord_link_state"] = state
        params = {
            "client_id": _discord_client_id(),
            "response_type": "code",
            "scope": "identify",
            "redirect_uri": _redirect_uri("auth.discord_link_callback"),
            "state": state,
        }
        return redirect(
            f"{DISCORD_AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"
        )

    @bp.route("/discord/link/callback")
    def discord_link_callback():  # type: ignore[unused-ignore]
        """Finish Discord linking then log the user in via Discord (R3.1-3.4).

        Links the Discord id to the subject (from an authenticated session or
        the post-registration handoff) through ``link_discord`` — which sets
        the GSI1 reverse index (R3.3) and enforces one-account-per-identity
        (R3.4). A Discord id already linked to a different account raises
        ``ValueError`` and is surfaced as a clear ``already_linked`` error
        (never a 500). On success the authenticated session is established so
        Discord OAuth is the login method thereafter (R3.2).
        """
        sub = _link_subject()
        if not sub:
            return redirect(url_for("pages.login"))
        state = request.args.get("state", "")
        if not state or state != session.pop("discord_link_state", None):
            return redirect(url_for("guild.account", error="state_mismatch"))
        code = request.args.get("code", "")
        discord_id = discord_id_from_code(
            code, _redirect_uri("auth.discord_link_callback")
        )
        profiles = current_app.extensions.get("user_profiles")
        if not discord_id or not profiles:
            return redirect(url_for("guild.account", error="discord_failed"))
        try:
            profiles.link_discord(sub, discord_id)
        except ValueError:
            return redirect(url_for("guild.account", error="already_linked"))
        # Linking succeeded: establish the authenticated session (Discord OAuth
        # is the login method from now on, R3.2) and clear any pending handoff.
        session.pop("pending_link_sub", None)
        _establish_discord_session(discord_id)
        return redirect(url_for("guild.account"))

    @bp.route("/tidal/callback")
    def tidal_callback():  # type: ignore[unused-ignore]
        """First-party Tidal OAuth callback (R9.2, R9.5).

        Tidal source auth is fully independent of Cognito: the routing function
        confirms the provider is the first-party Tidal integration, and the
        full callback URL is forwarded to the ``tidal-stream`` component which
        owns the single-app-id token exchange/refresh (via ``tidal_refresh``).
        """
        provider = route_auth(
            AuthPurpose.TIDAL_SOURCE_AUTH, UserType.ADMIN
        )
        assert provider is AuthProvider.TIDAL_FIRST_PARTY
        tidal_stream_url = current_app.config.get("TIDAL_STREAM_URL", "")
        if not tidal_stream_url:
            # No downstream service wired (e.g. tests): land back on config.
            return redirect(url_for("pages.config", tidal="pending"))
        query = request.query_string.decode()
        forwarded = f"{tidal_stream_url.rstrip('/')}/auth/callback?{query}"
        return redirect(forwarded)

    @bp.route("/sources/<guild_id>/<provider>/connect")
    def source_connect(guild_id: str, provider: str):  # type: ignore[unused-ignore]
        """Start a per-guild source OAuth flow (R5.1).

        Stashes the guild + provider so the provider callback stores the tokens
        into the correct isolated Per_Guild_Secret. Ownership is enforced by the
        guild routes before the connect link is ever shown, and re-checked here.
        """
        if not session.get("user"):
            return redirect(url_for("pages.login"))
        if not _guild_source_authorized(guild_id):
            return redirect(url_for("pages.guilds"))
        state = _new_state()
        session["source_state"] = state
        session["source_guild"] = guild_id
        session["source_provider"] = provider
        authorize_url = source_authorize_url(provider, state, guild_id)
        if not authorize_url:
            # Provider not wired for interactive OAuth (empty client id): show a
            # clear "needs setup" error on the guild page instead of a silent
            # no-op (R2.1, R1.2).
            return redirect(
                url_for(
                    "guild.guild_detail",
                    guild_id=guild_id,
                    error="provider_not_configured",
                    provider=provider,
                )
            )
        return redirect(authorize_url)

    @bp.route("/sources/<guild_id>/<provider>/callback")
    def source_callback(guild_id: str, provider: str):  # type: ignore[unused-ignore]
        """Finish a per-guild source OAuth flow and store isolated tokens."""
        if not session.get("user"):
            return redirect(url_for("pages.login"))
        state = request.args.get("state", "")
        if not state or state != session.pop("source_state", None):
            return redirect(url_for("guild.guild_detail", guild_id=guild_id))
        if not _guild_source_authorized(guild_id):
            return redirect(url_for("pages.guilds"))
        connected_by = (session.get("user") or {}).get("sub", "")
        sources = current_app.extensions.get("guild_sources")
        if provider in ("youtube", "youtube_music"):
            # YouTube has no per-guild sidecar: the web-ui completes the
            # code->refresh-token exchange and attaches a PoToken so the guild
            # secret holds the {oauth_refresh_token, pot_token,
            # pot_visitor_data} the playback path needs (R2.3, R2.4).
            tokens = compose_youtube_tokens(
                provider,
                request.args.get("code", ""),
                guild_id,
                connected_by=connected_by,
            )
            if not tokens:
                # Refresh token missing or potoken-server down: surface a clear
                # error instead of a silent no-op / partial secret.
                session.pop("source_guild", None)
                session.pop("source_provider", None)
                return redirect(
                    url_for(
                        "guild.guild_detail",
                        guild_id=guild_id,
                        error="youtube_connect_failed",
                        provider=provider,
                    )
                )
        elif provider == "spotify":
            # Spotify has no per-guild sidecar for the exchange: the web-ui
            # completes the code->refresh-token exchange with the resolved
            # Spotify client id/secret and stores the refresh-token-centric
            # shape the bot's global Spotify fallback also uses (R2.2). Tidal
            # stays on the sidecar forward path (tidal_callback), untouched.
            tokens = source_exchange_spotify(
                request.args.get("code", ""), guild_id
            )
            if not tokens:
                # Exchange failed (no refresh token / client secret unavailable):
                # surface a clear error rather than a silent no-op.
                session.pop("source_guild", None)
                session.pop("source_provider", None)
                return redirect(
                    url_for(
                        "guild.guild_detail",
                        guild_id=guild_id,
                        error="spotify_connect_failed",
                        provider=provider,
                    )
                )
        else:
            tokens = source_tokens_from_request(provider)
        if sources and tokens:
            sources.store_tokens(
                guild_id,
                provider,
                tokens,
                connected_by=connected_by,
            )
        session.pop("source_guild", None)
        session.pop("source_provider", None)
        return redirect(url_for("guild.guild_detail", guild_id=guild_id))

    @bp.route("/logout", methods=["POST", "GET"])
    def logout():  # type: ignore[unused-ignore]
        """Clear the session."""
        session.clear()
        return redirect(url_for("pages.login"))

    return bp


def _global_config() -> dict[str, Any]:
    """Return the global config payload, or ``{}`` in no-datastore mode.

    An absent config store yields an empty payload, which
    :func:`registration_mode.is_open` normalizes to the secure-default CLOSED
    state (invite-only), so registration stays closed unless a store is wired
    and an admin has opened it.
    """
    store = current_app.extensions.get("config_store")
    return store.get_global() if store else {}


def _guild_source_authorized(guild_id: str) -> bool:
    """Re-check the caller may manage this guild's sources (defense in depth)."""
    from guild_admin_service import can_manage_guild  # noqa: PLC0415

    user = session.get("user") or {}
    ga = current_app.extensions.get("guild_admin")
    owner_sub = ga.owner_of(guild_id) if ga else None
    admin_ids = ga.admin_discord_ids(guild_id) if ga else set()
    return can_manage_guild(
        guild_id=guild_id,
        user_sub=user.get("sub"),
        discord_id=user.get("discord_id"),
        is_super_admin=bool(user.get("is_admin")),
        owner_sub=owner_sub,
        admin_discord_ids=admin_ids,
    )


def _link_subject() -> str | None:
    """Return the Cognito subject to link Discord to, or ``None``.

    Prefers an already-authenticated session (a user linking Discord after the
    fact) and falls back to the post-registration handoff key
    ``pending_link_sub`` (the invitee just registered but holds no
    authenticated session yet — R2.4/R3.1).
    """
    user = session.get("user") or {}
    if user.get("sub"):
        return str(user["sub"])
    pending = session.get("pending_link_sub")
    return str(pending) if pending else None


def _establish_discord_session(discord_id: str) -> None:
    """Establish an authenticated Discord-login session for ``discord_id``.

    Resolves the Cognito subject linked to the Discord id via the GSI1 reverse
    index (``user_for_discord``) and, when found, sets ``session['user']`` with
    Discord as the provider (R3.2). When no account is linked the session is
    left untouched so the caller can react (e.g. bounce to login).
    """
    profiles = current_app.extensions.get("user_profiles")
    if not profiles:
        return
    sub = profiles.user_for_discord(discord_id)
    if not sub:
        return
    profile = profiles.get(sub) or {}
    session["user"] = {
        "provider": AuthProvider.DISCORD_OAUTH.value,
        "sub": sub,
        "discord_id": discord_id,
        "discord_linked": True,
        "email": profile.get("email", ""),
    }





def _discord_client_id() -> str:
    """Return the Discord OAuth client id (plain env, else the secret ARN).

    Resolves via :func:`source_token_exchange.discord_client_credentials` so the
    authorize URL carries a real ``client_id`` even when only the
    ``hellodj/<stage>/discord-oauth`` Secrets Manager secret is configured (not
    the plain env). Imported lazily to avoid a circular import at module load.
    """
    from source_token_exchange import discord_client_credentials  # noqa: PLC0415

    client_id, _secret = discord_client_credentials()
    return client_id


def _start_discord_oauth():
    """Build the Discord authorize redirect with CSRF state."""
    state = _new_state()
    session["discord_state"] = state
    params = {
        "client_id": _discord_client_id(),
        "response_type": "code",
        "scope": "identify",
        "redirect_uri": _redirect_uri("auth.discord_callback"),
        "state": state,
    }
    return redirect(f"{DISCORD_AUTHORIZE_URL}?{urllib.parse.urlencode(params)}")


def _redirect_uri(endpoint: str) -> str:
    """Return an absolute redirect URI for an auth endpoint."""
    base = current_app.config.get("PUBLIC_BASE_URL", "").rstrip("/")
    if base:
        return base + url_for(endpoint)
    return url_for(endpoint, _external=True)


#: Importable blueprint instance for apps that prefer a module-level object.
auth_bp = build_auth_blueprint()

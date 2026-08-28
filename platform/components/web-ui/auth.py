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

import base64
import hashlib
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

from auth_oauth import (
    discord_id_from_code,
    exchange_code_for_groups,
)
from source_oauth import (
    source_authorize_url,
    source_tokens_from_request,
)

__all__ = ["auth_bp", "build_auth_blueprint"]

DISCORD_AUTHORIZE_URL = "https://discord.com/oauth2/authorize"


def _new_state() -> str:
    """Return a URL-safe random CSRF/state token."""
    return pysecrets.token_urlsafe(32)


def _pkce_pair() -> tuple[str, str]:
    """Return a (verifier, challenge) PKCE pair using S256."""
    verifier = pysecrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


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

    @bp.route("/admin")
    def admin_login():  # type: ignore[unused-ignore]
        """Start admin auth: routes to the Cognito hosted UI (R8.2)."""
        provider = route_auth(AuthPurpose.ADMIN_AUTH, UserType.ADMIN)
        assert provider is AuthProvider.COGNITO
        return _start_cognito(AuthPurpose.ADMIN_AUTH)

    @bp.route("/register")
    def register():  # type: ignore[unused-ignore]
        """Start initial registration: routes to Cognito (R8.3)."""
        provider = route_auth(
            AuthPurpose.INITIAL_REGISTRATION, UserType.ANONYMOUS
        )
        assert provider is AuthProvider.COGNITO
        return _start_cognito(AuthPurpose.INITIAL_REGISTRATION)

    @bp.route("/recover")
    def recover():  # type: ignore[unused-ignore]
        """Start account recovery: routes to Cognito (R8.5)."""
        provider = route_auth(
            AuthPurpose.ACCOUNT_RECOVERY, UserType.REGISTERED
        )
        assert provider is AuthProvider.COGNITO
        return _start_cognito(AuthPurpose.ACCOUNT_RECOVERY)

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

    @bp.route("/cognito/callback")
    def cognito_callback():  # type: ignore[unused-ignore]
        """Cognito hosted-UI callback for admin/registration/recovery."""
        state = request.args.get("state", "")
        if not state or state != session.pop("cognito_state", None):
            return redirect(url_for("pages.login", error="state_mismatch"))
        # Exchange the authorization code for tokens and read the group claim
        # so admin group membership drives the admin panel gate. The admin
        # account is not a standard user — it administers all other accounts —
        # so `is_admin` must reflect Cognito `admins` group membership.
        code = request.args.get("code", "")
        groups = exchange_code_for_groups(
            code,
            session.pop("cognito_verifier", ""),
            _redirect_uri("auth.cognito_callback"),
        )
        session["user"] = {
            "provider": AuthProvider.COGNITO.value,
            "is_admin": "admins" in groups,
            "groups": groups,
        }
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
            "client_id": current_app.config.get("DISCORD_CLIENT_ID", ""),
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
            # Provider not wired for interactive OAuth: land back on the guild.
            return redirect(url_for("guild.guild_detail", guild_id=guild_id))
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
        tokens = source_tokens_from_request(provider)
        sources = current_app.extensions.get("guild_sources")
        if sources and tokens:
            sources.store_tokens(
                guild_id,
                provider,
                tokens,
                connected_by=(session.get("user") or {}).get("sub", ""),
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





def _start_discord_oauth():
    """Build the Discord authorize redirect with CSRF state."""
    state = _new_state()
    session["discord_state"] = state
    params = {
        "client_id": current_app.config.get("DISCORD_CLIENT_ID", ""),
        "response_type": "code",
        "scope": "identify",
        "redirect_uri": _redirect_uri("auth.discord_callback"),
        "state": state,
    }
    return redirect(f"{DISCORD_AUTHORIZE_URL}?{urllib.parse.urlencode(params)}")


def _start_cognito(purpose: AuthPurpose):
    """Build the Cognito hosted-UI redirect (PKCE) for a Cognito purpose."""
    state = _new_state()
    verifier, challenge = _pkce_pair()
    session["cognito_state"] = state
    session["cognito_verifier"] = verifier
    domain = current_app.config.get("COGNITO_DOMAIN", "")
    endpoint = "signup" if purpose is AuthPurpose.INITIAL_REGISTRATION else "login"
    params: dict[str, Any] = {
        "client_id": current_app.config.get("COGNITO_CLIENT_ID", ""),
        "response_type": "code",
        "scope": "openid email profile",
        "redirect_uri": _redirect_uri("auth.cognito_callback"),
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return redirect(f"{domain}/{endpoint}?{urllib.parse.urlencode(params)}")


def _redirect_uri(endpoint: str) -> str:
    """Return an absolute redirect URI for an auth endpoint."""
    base = current_app.config.get("PUBLIC_BASE_URL", "").rstrip("/")
    if base:
        return base + url_for(endpoint)
    return url_for(endpoint, _external=True)


#: Importable blueprint instance for apps that prefer a module-level object.
auth_bp = build_auth_blueprint()

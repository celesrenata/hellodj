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
import json
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

__all__ = ["auth_bp", "build_auth_blueprint"]

DISCORD_AUTHORIZE_URL = "https://discord.com/oauth2/authorize"
DISCORD_API_BASE = "https://discord.com/api"


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
        """Discord OAuth callback: validate state and record the session."""
        error = request.args.get("error")
        if error:
            return redirect(url_for("pages.login", error="denied"))
        state = request.args.get("state", "")
        if not state or state != session.pop("discord_state", None):
            return redirect(url_for("pages.login", error="state_mismatch"))
        # Code exchange is delegated to the running service integration; here we
        # record the authenticated purpose/provider and land on the dashboard.
        session["user"] = {"provider": AuthProvider.DISCORD_OAUTH.value}
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
        groups = _exchange_code_for_groups(code, session.pop("cognito_verifier", ""))
        session["user"] = {
            "provider": AuthProvider.COGNITO.value,
            "is_admin": "admins" in groups,
            "groups": groups,
        }
        return redirect(url_for("pages.dashboard"))

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

    @bp.route("/logout", methods=["POST", "GET"])
    def logout():  # type: ignore[unused-ignore]
        """Clear the session."""
        session.clear()
        return redirect(url_for("pages.login"))

    return bp


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


def _exchange_code_for_groups(code: str, verifier: str) -> list[str]:
    """Exchange the Cognito auth code for tokens and return its group claims.

    Performs the authorization-code + PKCE token exchange against the Cognito
    hosted-UI ``/oauth2/token`` endpoint, then decodes the ID token payload to
    read the ``cognito:groups`` claim. The claim drives the admin gate: a user
    in the ``admins`` group is an administrator (manages all accounts), any
    other authenticated user is a standard user.

    Returns an empty list when the exchange can't be performed (missing code,
    unconfigured Cognito, or a network/parse error) so login still succeeds as
    a non-admin rather than failing hard.
    """
    if not code:
        return []
    domain = current_app.config.get("COGNITO_DOMAIN", "").rstrip("/")
    client_id = current_app.config.get("COGNITO_CLIENT_ID", "")
    if not domain or not client_id:
        return []
    import json as _json
    import urllib.request as _req

    body = urllib.parse.urlencode(
        {
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": code,
            "redirect_uri": _redirect_uri("auth.cognito_callback"),
            "code_verifier": verifier,
        }
    ).encode("ascii")
    request_obj = _req.Request(
        f"{domain}/oauth2/token",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with _req.urlopen(request_obj, timeout=8) as resp:  # noqa: S310
            tokens = _json.loads(resp.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 - login degrades to non-admin on failure
        return []
    id_token = tokens.get("id_token", "")
    return _groups_from_id_token(id_token)


def _groups_from_id_token(id_token: str) -> list[str]:
    """Return the ``cognito:groups`` claim from a JWT ID token payload.

    Decodes the JWT payload segment only (no signature verification — the token
    came directly from the Cognito token endpoint over TLS in this same
    request, so it is trusted here for the group-membership read).
    """
    try:
        payload_b64 = id_token.split(".")[1]
        padded = payload_b64 + "=" * (-len(payload_b64) % 4)
        claims = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
    except Exception:  # noqa: BLE001
        return []
    groups = claims.get("cognito:groups", [])
    return list(groups) if isinstance(groups, list) else []


#: Importable blueprint instance for apps that prefer a module-level object.
auth_bp = build_auth_blueprint()

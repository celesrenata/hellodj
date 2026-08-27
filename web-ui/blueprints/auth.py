"""Discord OAuth2 authentication blueprint for HelloDJ SaaS platform.

Handles the full OAuth2 flow:
- GET  /auth/login    — redirect to Discord OAuth2 with CSRF state
- GET  /auth/callback — validate state, exchange code, UPSERT tenant, create session
- POST /auth/logout   — invalidate session, clear cookie
- GET  /auth/me       — return current tenant profile JSON

Session tokens are stored in Redis with 24h sliding TTL (7-day absolute max).
Tenant records are UPSERTed in PostgreSQL on each successful login.
Rate limiting: 10 login initiations per IP per 5-minute window.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import time
from urllib.parse import urlencode

import httpx
import redis
from flask import Blueprint, jsonify, make_response, redirect, request

from services.session_service import SessionService
from services.tenant_service import TenantService

log = logging.getLogger(__name__)

auth_bp = Blueprint("auth", __name__, url_prefix="/auth")

# ---------------------------------------------------------------------------
# Configuration (read from environment)
# ---------------------------------------------------------------------------

DISCORD_CLIENT_ID = os.environ.get("DISCORD_CLIENT_ID") or os.environ.get(
    "DISCORD_APPID", ""
)
DISCORD_CLIENT_SECRET = os.environ.get("DISCORD_CLIENT_SECRET", "")
DISCORD_REDIRECT_URI = os.environ.get(
    "DISCORD_REDIRECT_URI", "https://hellodj.celestium.life/auth/callback"
)

DISCORD_AUTH_URL = "https://discord.com/api/oauth2/authorize"
DISCORD_TOKEN_URL = "https://discord.com/api/oauth2/token"
DISCORD_API_BASE = "https://discord.com/api/v10"

REDIS_URL = os.environ.get(
    "REDIS_URL", "redis://redis.redis-service.svc.cluster.local:6379/0"
)
PG_URI = os.environ.get(
    "HELLODJ_PG_URI",
    "postgresql://hellodj:hellodj@postgresql-rw.postgresql-service.svc.cluster.local:5432/hellodj",
)

# Session cookie config
COOKIE_NAME = "hellodj_session"
# Session TTL: 24h sliding (managed by SessionService)
SESSION_TTL_SECONDS = 86400
# Cookie max-age: 7 days (absolute lifetime)
COOKIE_MAX_AGE = 7 * 24 * 60 * 60
# OAuth state TTL: 5 minutes (short-lived CSRF protection)
STATE_TTL_SECONDS = 5 * 60
# HTTP timeout for Discord API calls
DISCORD_HTTP_TIMEOUT = 10.0

LOGIN_REDIRECT_URL = os.environ.get("LOGIN_REDIRECT_URL", "/")
LOGIN_PAGE_URL = os.environ.get("LOGIN_PAGE_URL", "/")


# ---------------------------------------------------------------------------
# Redis, SessionService, and TenantService helpers (lazy singletons)
# ---------------------------------------------------------------------------

_redis_client: redis.Redis | None = None
_session_service: SessionService | None = None
_tenant_service: TenantService | None = None


def _get_redis() -> redis.Redis:
    """Get a Redis client (lazy singleton)."""
    global _redis_client
    if _redis_client is None:
        _redis_client = redis.from_url(REDIS_URL, decode_responses=True)
    return _redis_client


def _get_session_service() -> SessionService:
    """Get the SessionService instance (lazy singleton)."""
    global _session_service
    if _session_service is None:
        _session_service = SessionService(_get_redis())
    return _session_service


def _get_tenant_service() -> TenantService:
    """Get the TenantService instance (lazy singleton)."""
    global _tenant_service
    if _tenant_service is None:
        _tenant_service = TenantService(PG_URI)
    return _tenant_service


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_client_ip() -> str:
    """Extract the client's real IP address.

    Parses X-Forwarded-For header (first entry) as set by the ingress proxy,
    falling back to request.remote_addr for direct connections.
    """
    forwarded_for = request.headers.get("X-Forwarded-For", "")
    if forwarded_for:
        # X-Forwarded-For: client, proxy1, proxy2
        return forwarded_for.split(",")[0].strip()
    return request.remote_addr or "unknown"


def _check_login_rate_limit(ip: str) -> tuple[bool, int]:
    """Check if IP has exceeded 10 login attempts in 5 minutes.

    Uses a fixed-window counter: INCR + EXPIRE pattern.

    Returns:
        (allowed, retry_after_seconds). If allowed is False, retry_after
        indicates how many seconds until the window resets.

    Raises:
        redis.ConnectionError: If Redis is unreachable.
    """
    r = _get_redis()
    key = f"ratelimit:login:{ip}"
    count = r.incr(key)
    if count == 1:
        r.expire(key, 300)  # 5 minute window

    if count > 10:
        ttl = r.ttl(key)
        return False, max(ttl, 1)

    return True, 0


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@auth_bp.route("/login", methods=["GET"])
def login():
    """Redirect to Discord OAuth2 authorization page.

    Generates a 256-bit cryptographically random state parameter, stores it
    in Redis with a short TTL for CSRF protection. Rate-limits by IP.
    """
    client_ip = _get_client_ip()

    # Rate limit check (before generating state or touching Redis further)
    try:
        allowed, retry_after = _check_login_rate_limit(client_ip)
    except redis.ConnectionError as exc:
        log.error("Redis unavailable during login rate limit check: %s", exc)
        return make_response("Login temporarily unavailable", 503)

    if not allowed:
        response = make_response(
            "Rate limit exceeded. Please try again later.", 429
        )
        response.headers["Retry-After"] = str(retry_after)
        return response

    # Generate 256-bit random state (32 bytes → url-safe base64)
    state = secrets.token_urlsafe(32)

    # Store state in Redis with 5-minute TTL
    try:
        r = _get_redis()
        r.set(f"oauth_state:{state}", "1", ex=STATE_TTL_SECONDS)
    except redis.ConnectionError as exc:
        log.error("Redis unavailable during state storage: %s", exc)
        return make_response("Login temporarily unavailable", 503)

    params = {
        "client_id": DISCORD_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": DISCORD_REDIRECT_URI,
        "scope": "identify email guilds",
        "state": state,
        "prompt": "consent",
    }

    url = f"{DISCORD_AUTH_URL}?{urlencode(params)}"
    return redirect(url)


@auth_bp.route("/callback", methods=["GET"])
def callback():
    """Handle Discord OAuth2 callback.

    Validates state, exchanges code for token, fetches user profile,
    UPSERTs tenant via TenantService, builds roles list, creates session
    via SessionService, and sets the hellodj_session cookie.
    """
    error = request.args.get("error")
    code = request.args.get("code")
    state = request.args.get("state")

    # User denied authorization
    if error == "access_denied" or error:
        log.warning("Discord OAuth2 denied: error=%s", error)
        return redirect(f"{LOGIN_PAGE_URL}?error=denied")

    if not code:
        log.warning("Discord OAuth2 callback missing code parameter")
        return redirect(f"{LOGIN_PAGE_URL}?error=denied")

    # Validate state parameter (CSRF protection)
    r = _get_redis()
    if not state:
        log.warning("Discord OAuth2 callback missing state parameter")
        return redirect(f"{LOGIN_PAGE_URL}?error=state_mismatch")

    state_key = f"oauth_state:{state}"
    stored = r.get(state_key)
    if not stored:
        log.warning("Discord OAuth2 state mismatch or expired: state=%s", state)
        return redirect(f"{LOGIN_PAGE_URL}?error=state_mismatch")

    # Consume the state (one-time use)
    r.delete(state_key)

    # Exchange code for access token (10s timeout)
    try:
        token_data = _exchange_code(code)
    except Exception as exc:
        log.error("Discord OAuth2 code exchange failed: %s", exc)
        return redirect(f"{LOGIN_PAGE_URL}?error=service_unavailable")

    access_token = token_data.get("access_token")
    if not access_token:
        log.error("Discord OAuth2 code exchange returned no access_token")
        return redirect(f"{LOGIN_PAGE_URL}?error=service_unavailable")

    refresh_token = token_data.get("refresh_token", "")
    expires_in = token_data.get("expires_in", 604800)

    # Fetch user profile from Discord
    try:
        profile = _fetch_user_profile(access_token)
    except Exception as exc:
        log.error("Discord user profile fetch failed: %s", exc)
        return redirect(f"{LOGIN_PAGE_URL}?error=service_unavailable")

    discord_user_id = int(profile["id"])
    discord_username = profile.get("global_name") or profile.get("username", "")
    email = profile.get("email")
    avatar = profile.get("avatar")

    # UPSERT tenant via TenantService
    try:
        tenant_svc = _get_tenant_service()
        tenant = tenant_svc.upsert(discord_user_id, discord_username, email)
    except Exception as exc:
        log.error("Tenant UPSERT failed: %s", exc)
        return redirect(f"{LOGIN_PAGE_URL}?error=service_unavailable")

    # Build roles list from accessible tenants
    try:
        accessible = tenant_svc.list_accessible_tenants(discord_user_id)
    except Exception as exc:
        log.error("Failed to list accessible tenants: %s", exc)
        # Fall back to just the owned tenant
        accessible = []

    # Build roles: start with the owned tenant as "owner"
    roles = [{"tenant_id": str(tenant["id"]), "role": "owner"}]

    # Add delegated roles from accessible tenants (skip the owned one to avoid
    # duplicates — it's already included above as "owner")
    for entry in accessible:
        if str(entry["tenant_id"]) != str(tenant["id"]):
            roles.append({
                "tenant_id": str(entry["tenant_id"]),
                "role": entry["role"],
            })

    # Determine operator status
    is_operator = str(discord_user_id) == os.environ.get(
        "OPERATOR_DISCORD_ID", ""
    )

    # Determine active tenant: owned tenant ID (or first accessible)
    active_tenant_id = str(tenant["id"])

    # Build session data
    session_data = {
        "tenant_id": str(tenant["id"]),
        "discord_user_id": str(discord_user_id),
        "discord_username": discord_username,
        "email": email,
        "avatar": avatar,
        "is_operator": is_operator,
        "roles": roles,
        "active_tenant_id": active_tenant_id,
        "ip_address": _get_client_ip(),
        "created_at": time.time(),
        "discord_access_token": access_token,
        "discord_refresh_token": refresh_token,
        "discord_token_expires_at": time.time() + expires_in,
        "refresh_retry_count": 0,
    }

    # Create session via SessionService
    try:
        session_svc = _get_session_service()
        session_token = session_svc.create(session_data)
    except Exception as exc:
        log.error("Session creation failed: %s", exc)
        return redirect(f"{LOGIN_PAGE_URL}?error=service_unavailable")

    # Set session cookie and redirect to dashboard
    response = make_response(redirect(LOGIN_REDIRECT_URL))
    response.set_cookie(
        COOKIE_NAME,
        session_token,
        max_age=COOKIE_MAX_AGE,
        httponly=True,
        secure=True,
        samesite="Lax",
        path="/",
    )
    log.info(
        "Login successful: discord_user_id=%s username=%s tenant_id=%s operator=%s",
        discord_user_id,
        discord_username,
        tenant["id"],
        is_operator,
    )
    return response


@auth_bp.route("/logout", methods=["GET", "POST"])
def logout():
    """Revoke Discord token, destroy session, and clear the session cookie.

    Supports both GET (link-based) and POST (form-based) logout.
    If the user is unauthenticated (no cookie or expired session),
    redirects to login without performing any cleanup.
    """
    session_token = request.cookies.get(COOKIE_NAME)

    # Unauthenticated user — redirect without cleanup (Requirement 8.6)
    if not session_token:
        return redirect(LOGIN_PAGE_URL)

    # Load session to get Discord access token for revocation
    session_svc = _get_session_service()
    session_data = session_svc.load(session_token)

    if session_data is None:
        # Session expired or invalid — just redirect without cleanup (Req 8.6)
        response = make_response(redirect(LOGIN_PAGE_URL))
        response.set_cookie(
            COOKIE_NAME,
            "",
            max_age=0,
            path="/",
            httponly=True,
            secure=True,
            samesite="Lax",
        )
        return response

    # Best-effort Discord token revocation (Requirement 8.3, 8.4)
    discord_access_token = session_data.get("discord_access_token")
    if discord_access_token:
        _revoke_discord_token(discord_access_token)

    # Destroy session via SessionService (Requirement 8.1)
    session_svc.destroy(session_token)
    log.info("Session invalidated: token=%s...", session_token[:8])

    # Clear cookie and redirect (Requirement 8.2, 8.5)
    response = make_response(redirect(LOGIN_PAGE_URL))
    response.set_cookie(
        COOKIE_NAME,
        "",
        max_age=0,
        path="/",
        httponly=True,
        secure=True,
        samesite="Lax",
    )
    return response


def _revoke_discord_token(access_token: str) -> None:
    """Revoke a Discord OAuth2 access token (best-effort).

    POSTs to Discord's token revocation endpoint with a 5-second timeout.
    Logs failures but does not raise — logout proceeds regardless.
    """
    try:
        with httpx.Client(timeout=5.0) as client:
            resp = client.post(
                "https://discord.com/api/oauth2/token/revoke",
                data={
                    "token": access_token,
                    "token_type_hint": "access_token",
                    "client_id": DISCORD_CLIENT_ID,
                    "client_secret": DISCORD_CLIENT_SECRET,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            if resp.status_code != 200:
                log.warning(
                    "Discord token revocation failed: status=%d body=%s",
                    resp.status_code,
                    resp.text[:200],
                )
            else:
                log.info("Discord token revoked successfully")
    except Exception as exc:
        log.warning("Discord token revocation error (best-effort): %s", exc)


@auth_bp.route("/me", methods=["GET"])
def me():
    """Return the current tenant's profile as JSON.

    Returns 401 if not authenticated or session is invalid/expired.
    """
    session_token = request.cookies.get(COOKIE_NAME)
    if not session_token:
        return jsonify({"error": "Not authenticated"}), 401

    r = _get_redis()
    session_data = r.get(f"session:{session_token}")
    if not session_data:
        # Session expired or invalid — clear the stale cookie
        response = make_response(jsonify({"error": "Session expired"}), 401)
        response.set_cookie(
            COOKIE_NAME,
            "",
            max_age=0,
            httponly=True,
            secure=True,
            samesite="Lax",
            path="/",
        )
        return response

    profile = json.loads(session_data)
    return jsonify(profile), 200


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _exchange_code(code: str) -> dict:
    """Exchange authorization code for access token with Discord.

    Uses httpx with a 10-second timeout as specified in the design.
    """
    with httpx.Client(timeout=DISCORD_HTTP_TIMEOUT) as client:
        resp = client.post(
            DISCORD_TOKEN_URL,
            data={
                "client_id": DISCORD_CLIENT_ID,
                "client_secret": DISCORD_CLIENT_SECRET,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": DISCORD_REDIRECT_URI,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if resp.status_code != 200:
            raise RuntimeError(
                f"Discord token exchange failed: status={resp.status_code} body={resp.text}"
            )
        return resp.json()


def _fetch_user_profile(access_token: str) -> dict:
    """Fetch the authenticated user's profile from Discord API.

    Uses httpx with a 10-second timeout.
    """
    with httpx.Client(timeout=DISCORD_HTTP_TIMEOUT) as client:
        resp = client.get(
            f"{DISCORD_API_BASE}/users/@me",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if resp.status_code != 200:
            raise RuntimeError(
                f"Discord profile fetch failed: status={resp.status_code} body={resp.text}"
            )
        return resp.json()

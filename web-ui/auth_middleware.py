"""Session middleware and auth decorators for the HelloDJ SaaS platform.

Provides:
- @login_required: validates session token from cookie, enforces IP binding,
  absolute expiry, sliding expiry, Discord token refresh, attaches session to flask.g
- @operator_required: extends @login_required, checks operator status from session

Session storage: Redis key `session:{token}` → JSON with tenant/session data.
Cookie name: `hellodj_session`
Operator identity: env var `OPERATOR_DISCORD_ID`
"""

from __future__ import annotations

import functools
import json
import logging
import os
import time
from urllib.parse import urlencode

import httpx
import redis
from flask import abort, g, jsonify, redirect, request, url_for

from services.session_service import SessionService, ServiceUnavailableError

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Redis connection (lazy singleton)
# ---------------------------------------------------------------------------

_redis_client: redis.Redis | None = None


def _get_redis() -> redis.Redis:
    """Return a Redis client, creating one on first call."""
    global _redis_client
    if _redis_client is None:
        redis_url = os.getenv(
            "REDIS_URL",
            "redis://redis.redis-service.svc.cluster.local:6379/0",
        )
        _redis_client = redis.Redis.from_url(redis_url, decode_responses=True)
    return _redis_client


def set_redis_client(client: redis.Redis) -> None:
    """Override the Redis client (for testing with fakeredis)."""
    global _redis_client
    _redis_client = client


# ---------------------------------------------------------------------------
# Session service (lazy singleton)
# ---------------------------------------------------------------------------

_session_service: SessionService | None = None


def _get_session_service() -> SessionService:
    """Return a SessionService instance, creating one on first call."""
    global _session_service
    if _session_service is None:
        _session_service = SessionService(_get_redis())
    return _session_service


def set_session_service(service: SessionService) -> None:
    """Override the SessionService instance (for testing)."""
    global _session_service
    _session_service = service


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SESSION_COOKIE_NAME = "hellodj_session"
SESSION_KEY_PREFIX = "session:"
ABSOLUTE_LIFETIME = 604800  # 7 days in seconds


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_client_ip() -> str:
    """Get the client IP address from X-Forwarded-For header or remote_addr.

    Parses the first entry from X-Forwarded-For (leftmost = original client).
    Falls back to request.remote_addr if header is absent.
    """
    forwarded_for = request.headers.get("X-Forwarded-For")
    if forwarded_for:
        # X-Forwarded-For: client, proxy1, proxy2
        return forwarded_for.split(",")[0].strip()
    return request.remote_addr or "unknown"


def _redirect_to_login():
    """Build redirect response to login with next URL preserved."""
    next_url = request.url
    login_url = url_for("auth.login", _external=False)
    separator = "?" if "?" not in login_url else "&"
    redirect_url = f"{login_url}{separator}{urlencode({'next': next_url})}"
    return redirect(redirect_url)


def _maybe_refresh_discord_token(token: str, session: dict) -> None:
    """Refresh Discord access token if within 1 hour of expiry.

    Uses a distributed lock to prevent concurrent refresh attempts from
    multiple requests hitting the same session simultaneously.

    Handles:
    - invalid_grant: destroys session (user must re-auth)
    - Network errors: increments retry counter, destroys after 3 consecutive failures
    """
    expires_at = session.get("discord_token_expires_at", 0)
    if not expires_at:
        return

    # Not yet due for refresh (more than 1 hour remaining)
    if expires_at - time.time() >= 3600:
        return

    refresh_token = session.get("discord_refresh_token")
    if not refresh_token:
        return

    r = _get_redis()
    svc = _get_session_service()

    # Acquire distributed lock to prevent concurrent refreshes
    lock_key = f"session_refresh_lock:{token}"
    try:
        acquired = r.set(lock_key, "1", nx=True, ex=30)
    except redis.RedisError:
        # Can't acquire lock — skip refresh this request
        return

    if not acquired:
        # Another request is handling the refresh
        return

    try:
        client_id = os.environ.get("DISCORD_CLIENT_ID") or os.environ.get(
            "DISCORD_APPID", ""
        )
        client_secret = os.environ.get("DISCORD_CLIENT_SECRET", "")

        response = httpx.post(
            "https://discord.com/api/oauth2/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": client_id,
                "client_secret": client_secret,
            },
            timeout=10.0,
        )

        if response.status_code == 200:
            new_tokens = response.json()
            # Update session fields with new token data
            session["discord_access_token"] = new_tokens["access_token"]
            session["discord_refresh_token"] = new_tokens.get(
                "refresh_token", refresh_token
            )
            session["discord_token_expires_at"] = (
                time.time() + new_tokens["expires_in"]
            )
            session["refresh_retry_count"] = 0

            # Persist full updated session
            key = f"{SESSION_KEY_PREFIX}{token}"
            try:
                remaining_ttl = r.ttl(key)
                if remaining_ttl > 0:
                    r.set(key, json.dumps(session), ex=remaining_ttl)
            except redis.RedisError as exc:
                log.warning("Failed to persist refreshed token: %s", exc)

        elif response.status_code == 400:
            # Check for invalid_grant
            try:
                error_data = response.json()
            except Exception:
                error_data = {}

            if error_data.get("error") == "invalid_grant":
                log.warning(
                    "Discord token refresh returned invalid_grant, "
                    "destroying session (token=%s...)",
                    token[:8],
                )
                svc.destroy(token)
                return
            else:
                # Other 400 error — treat as network error
                _handle_refresh_network_error(token, session, svc, r)
        else:
            # Non-200, non-400 response — treat as network error
            _handle_refresh_network_error(token, session, svc, r)

    except (httpx.RequestError, httpx.TimeoutException) as exc:
        log.warning("Discord token refresh network error: %s", exc)
        _handle_refresh_network_error(token, session, svc, r)
    finally:
        # Release the lock
        try:
            r.delete(lock_key)
        except redis.RedisError:
            pass


def _handle_refresh_network_error(
    token: str, session: dict, svc: SessionService, r: redis.Redis
) -> None:
    """Handle network errors during Discord token refresh.

    Increments retry counter. After 3 consecutive failures, destroys the session.
    """
    retry_count = session.get("refresh_retry_count", 0) + 1
    session["refresh_retry_count"] = retry_count

    if retry_count >= 3:
        log.warning(
            "Discord token refresh failed 3 consecutive times, "
            "destroying session (token=%s...)",
            token[:8],
        )
        svc.destroy(token)
    else:
        # Persist updated retry count
        key = f"{SESSION_KEY_PREFIX}{token}"
        try:
            remaining_ttl = r.ttl(key)
            if remaining_ttl > 0:
                r.set(key, json.dumps(session), ex=remaining_ttl)
        except redis.RedisError as exc:
            log.warning("Failed to persist retry count: %s", exc)


# ---------------------------------------------------------------------------
# Decorators
# ---------------------------------------------------------------------------


def login_required(f):
    """Decorator that enforces an authenticated session.

    Reads the `hellodj_session` cookie, validates it against Redis, checks
    IP binding and absolute expiry, extends sliding TTL, triggers Discord
    token refresh if needed, and attaches session data to `flask.g`.

    On invalid/expired token:
    - Destroys the session in Redis
    - Redirects to /auth/login?next={original_url}

    On Redis unavailability:
    - Returns HTTP 503 JSON error
    """

    @functools.wraps(f)
    def decorated_function(*args, **kwargs):
        token = request.cookies.get(SESSION_COOKIE_NAME)

        if not token:
            return _redirect_to_login()

        svc = _get_session_service()

        # Load session from Redis
        try:
            session = svc.load(token)
        except ServiceUnavailableError:
            return (
                jsonify({"error": "Service temporarily unavailable"}),
                503,
            )

        if session is None:
            return _redirect_to_login()

        # IP binding check
        client_ip = _get_client_ip()
        stored_ip = session.get("ip_address")
        if stored_ip and stored_ip != client_ip:
            log.info(
                "IP mismatch for session (token=%s...): stored=%s, current=%s",
                token[:8],
                stored_ip,
                client_ip,
            )
            try:
                svc.destroy(token)
            except ServiceUnavailableError:
                pass
            return _redirect_to_login()

        # Absolute expiry check (7 days)
        created_at = session.get("created_at", 0)
        if time.time() - created_at > ABSOLUTE_LIFETIME:
            log.info(
                "Session absolute expiry reached (token=%s...)", token[:8]
            )
            try:
                svc.destroy(token)
            except ServiceUnavailableError:
                pass
            return _redirect_to_login()

        # Sliding expiry extension
        try:
            svc.extend(token)
        except ServiceUnavailableError:
            # Non-fatal — session is still valid this request
            log.warning("Failed to extend session TTL (token=%s...)", token[:8])

        # Discord token refresh (if within 1 hour of expiry)
        _maybe_refresh_discord_token(token, session)

        # Attach session data to request context
        g.session = session
        g.tenant_id = session.get("active_tenant_id")
        g.is_operator = session.get("is_operator", False)

        # Backward compatibility: existing blueprints read g.tenant
        # This will be removed in task 4.4 when routes are migrated
        g.tenant = session

        return f(*args, **kwargs)

    return decorated_function


def operator_required(f):
    """Decorator that enforces operator (platform owner) access.

    Extends @login_required behavior: first validates the session, then checks
    that the authenticated session has `is_operator` set to True.

    Non-operator users receive HTTP 403 with no panel content exposed.
    """

    @functools.wraps(f)
    @login_required
    def decorated_function(*args, **kwargs):
        session = g.session

        if not session.get("is_operator", False):
            discord_user_id = str(session.get("discord_user_id", ""))
            log.info(
                "Operator access denied for discord_user_id=%s",
                discord_user_id,
            )
            abort(403)

        return f(*args, **kwargs)

    return decorated_function


# ---------------------------------------------------------------------------
# RBAC service (lazy singleton)
# ---------------------------------------------------------------------------

_rbac_service = None


def get_rbac_service():
    """Return an RBACService instance, creating one on first call.

    Imports RBACService lazily to avoid circular imports at module load time.
    """
    global _rbac_service
    if _rbac_service is None:
        from services.rbac import RBACService

        pg_uri = os.getenv(
            "HELLODJ_PG_URI",
            "postgresql://hellodj:hellodj@postgresql-rw.postgresql-service.svc.cluster.local:5432/hellodj",
        )
        _rbac_service = RBACService(pg_uri=pg_uri, redis_client=_get_redis())
    return _rbac_service


def set_rbac_service(service) -> None:
    """Override the RBACService instance (for testing)."""
    global _rbac_service
    _rbac_service = service


# ---------------------------------------------------------------------------
# Role-based access decorator
# ---------------------------------------------------------------------------


def role_required(minimum_role: str):
    """Decorator factory that enforces a minimum role for the active tenant.

    Resolves the user's effective role via RBACService and compares it against
    the required minimum using the role hierarchy:
        operator(5) > owner(4) > admin(3) > editor(2) > viewer(1)

    The tenant_id is extracted from URL route kwargs first, then from
    g.tenant_id (the session's active tenant context).

    Responses:
    - 404 if the user has no relationship to the tenant (prevents enumeration)
    - 403 if the user's role is insufficient
    - Sets g.effective_role on success for downstream handlers

    Usage:
        @role_required("editor")
        def update_settings(tenant_id): ...

        @role_required("owner")
        def manage_delegates(tenant_id): ...
    """

    def decorator(f):
        @functools.wraps(f)
        @login_required
        def wrapper(*args, **kwargs):
            tenant_id = kwargs.get("tenant_id") or g.tenant_id
            if not tenant_id:
                abort(400, description="No active tenant context")

            rbac = get_rbac_service()
            effective = rbac.get_effective_role(g.session, tenant_id)

            if effective is None:
                # No relationship → 404 to prevent tenant enumeration
                abort(404)

            effective_level = rbac.ROLE_HIERARCHY.get(effective, 0)
            required_level = rbac.ROLE_HIERARCHY.get(minimum_role, 0)

            if effective_level < required_level:
                abort(403, description=f"Requires {minimum_role} role")

            g.effective_role = effective
            return f(*args, **kwargs)

        return wrapper

    return decorator

"""Redis-backed session management for the HelloDJ SaaS platform.

Provides SessionService for creating, loading, extending, destroying, and
updating sessions stored in Redis. Sessions are keyed by a cryptographically
random token and associated with Discord user IDs via a secondary index.

Session lifecycle:
- Created on OAuth2 callback (24h sliding TTL, 7-day absolute max)
- Extended on each validated request (sliding window)
- Destroyed on logout or security violation (IP mismatch, absolute expiry)
- Invalidated in bulk on role changes

Usage:
    from services.session_service import SessionService

    svc = SessionService(redis_client)
    token = svc.create(session_data)
    session = svc.load(token)
    svc.extend(token)
    svc.destroy(token)
"""

from __future__ import annotations

import json
import logging
import secrets

import redis

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SESSION_TTL = 86400  # 24h sliding window
ABSOLUTE_LIFETIME = 604800  # 7 days hard max
COOKIE_NAME = "hellodj_session"
KEY_PREFIX = "session:"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ServiceUnavailableError(Exception):
    """Raised when Redis is unreachable during a session operation."""

    pass


# ---------------------------------------------------------------------------
# SessionService
# ---------------------------------------------------------------------------


class SessionService:
    """Manages session CRUD in Redis.

    Each session is stored as a JSON blob at ``session:{token}`` with a
    sliding TTL. A secondary index ``user_sessions:{discord_user_id}``
    (Redis SET) tracks all active tokens for a given user, enabling bulk
    invalidation on role changes.
    """

    def __init__(self, redis_client: redis.Redis):
        self._redis = redis_client

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def create(self, session_data: dict) -> str:
        """Create a new session and return the token.

        Generates a cryptographically random token, stores the session JSON
        at ``session:{token}`` with EX=SESSION_TTL, and adds the token to
        the user's session set ``user_sessions:{discord_user_id}``.

        Args:
            session_data: Dict containing at minimum ``discord_user_id``.

        Returns:
            The generated session token (URL-safe, 32 bytes of entropy).

        Raises:
            ServiceUnavailableError: If Redis is unreachable.
        """
        token = secrets.token_urlsafe(32)
        key = f"{KEY_PREFIX}{token}"
        discord_user_id = str(session_data.get("discord_user_id", ""))

        try:
            self._redis.set(key, json.dumps(session_data), ex=SESSION_TTL)
            if discord_user_id:
                user_sessions_key = f"user_sessions:{discord_user_id}"
                self._redis.sadd(user_sessions_key, token)
        except redis.ConnectionError as exc:
            log.error("Redis connection failed during session create: %s", exc)
            raise ServiceUnavailableError(
                "Session store unavailable"
            ) from exc

        log.info(
            "Session created for discord_user_id=%s (token=%s...)",
            discord_user_id,
            token[:8],
        )
        return token

    def load(self, token: str) -> dict | None:
        """Load a session by token.

        Returns the session dict, or None if the token is missing, expired,
        or the stored data is malformed.

        Raises:
            ServiceUnavailableError: If Redis is unreachable.
        """
        if not token:
            return None

        key = f"{KEY_PREFIX}{token}"

        try:
            raw = self._redis.get(key)
        except redis.ConnectionError as exc:
            log.error("Redis connection failed during session load: %s", exc)
            raise ServiceUnavailableError(
                "Session store unavailable"
            ) from exc

        if raw is None:
            return None

        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            log.warning("Malformed session data for token=%s... (evicting)", token[:8])
            try:
                self._redis.delete(key)
            except redis.RedisError:
                pass
            return None

    def extend(self, token: str) -> None:
        """Reset the sliding TTL on a session.

        Called on each successful session validation to keep active sessions
        alive for another SESSION_TTL period.

        Raises:
            ServiceUnavailableError: If Redis is unreachable.
        """
        if not token:
            return

        key = f"{KEY_PREFIX}{token}"

        try:
            self._redis.expire(key, SESSION_TTL)
        except redis.ConnectionError as exc:
            log.error("Redis connection failed during session extend: %s", exc)
            raise ServiceUnavailableError(
                "Session store unavailable"
            ) from exc

    def destroy(self, token: str) -> None:
        """Delete a session and remove it from the user's session set.

        Loads the session first to find the discord_user_id for the
        secondary index cleanup.

        Raises:
            ServiceUnavailableError: If Redis is unreachable.
        """
        if not token:
            return

        key = f"{KEY_PREFIX}{token}"

        try:
            # Load session to get discord_user_id for set cleanup
            raw = self._redis.get(key)
            self._redis.delete(key)

            if raw:
                try:
                    session = json.loads(raw)
                    discord_user_id = str(session.get("discord_user_id", ""))
                    if discord_user_id:
                        user_sessions_key = f"user_sessions:{discord_user_id}"
                        self._redis.srem(user_sessions_key, token)
                except (json.JSONDecodeError, TypeError):
                    pass
        except redis.ConnectionError as exc:
            log.error("Redis connection failed during session destroy: %s", exc)
            raise ServiceUnavailableError(
                "Session store unavailable"
            ) from exc

        log.info("Session destroyed (token=%s...)", token[:8])

    def update_field(self, token: str, field: str, value) -> None:
        """Update a single field in the session JSON.

        Loads the session, updates the specified field, and re-stores
        with the remaining TTL preserved.

        Args:
            token: Session token.
            field: The field name to update.
            value: The new value for the field.

        Raises:
            ServiceUnavailableError: If Redis is unreachable.
        """
        if not token:
            return

        key = f"{KEY_PREFIX}{token}"

        try:
            raw = self._redis.get(key)
            if raw is None:
                return

            remaining_ttl = self._redis.ttl(key)
            if remaining_ttl <= 0:
                return

            session = json.loads(raw)
            session[field] = value
            self._redis.set(key, json.dumps(session), ex=remaining_ttl)
        except redis.ConnectionError as exc:
            log.error("Redis connection failed during update_field: %s", exc)
            raise ServiceUnavailableError(
                "Session store unavailable"
            ) from exc
        except (json.JSONDecodeError, TypeError):
            log.warning(
                "Malformed session data during update_field (token=%s...)", token[:8]
            )

    def switch_tenant(
        self, token: str, tenant_id: str, accessible_tenants: list[str]
    ) -> bool:
        """Switch the active tenant context for a session.

        Validates that ``tenant_id`` is in the user's accessible tenants
        list before updating ``active_tenant_id`` in the session.

        Args:
            token: Session token.
            tenant_id: The tenant ID to switch to.
            accessible_tenants: List of tenant IDs the user can access.

        Returns:
            True if the switch succeeded, False if tenant_id is not
            in the accessible list.

        Raises:
            ServiceUnavailableError: If Redis is unreachable.
        """
        if tenant_id not in accessible_tenants:
            return False

        self.update_field(token, "active_tenant_id", tenant_id)
        return True

    def invalidate_user_sessions(
        self, discord_user_id: str, tenant_id: str | None = None
    ) -> int:
        """Invalidate all sessions for a user, optionally scoped to a tenant.

        Iterates the ``user_sessions:{discord_user_id}`` SET, loads each
        session, and deletes those matching the criteria. If ``tenant_id``
        is provided, only sessions referencing that tenant (in their roles
        list) are deleted.

        Args:
            discord_user_id: The Discord user ID whose sessions to scan.
            tenant_id: Optional tenant ID filter. If None, all sessions
                for the user are invalidated.

        Returns:
            Count of sessions invalidated.

        Raises:
            ServiceUnavailableError: If Redis is unreachable.
        """
        user_sessions_key = f"user_sessions:{discord_user_id}"
        count = 0

        try:
            tokens = self._redis.smembers(user_sessions_key)
        except redis.ConnectionError as exc:
            log.error(
                "Redis connection failed during invalidate_user_sessions: %s", exc
            )
            raise ServiceUnavailableError(
                "Session store unavailable"
            ) from exc

        for token in tokens:
            # Ensure token is a string (Redis may return bytes if decode_responses=False)
            if isinstance(token, bytes):
                token = token.decode("utf-8")

            session_key = f"{KEY_PREFIX}{token}"

            try:
                raw = self._redis.get(session_key)
            except redis.ConnectionError as exc:
                log.error(
                    "Redis connection failed reading session during invalidation: %s",
                    exc,
                )
                raise ServiceUnavailableError(
                    "Session store unavailable"
                ) from exc

            if raw is None:
                # Session already expired — clean up the set
                try:
                    self._redis.srem(user_sessions_key, token)
                except redis.RedisError:
                    pass
                continue

            # If tenant_id filter is specified, only delete matching sessions
            if tenant_id is not None:
                try:
                    session = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    # Malformed — delete it anyway
                    pass
                else:
                    roles = session.get("roles", [])
                    has_tenant = any(
                        r.get("tenant_id") == tenant_id for r in roles
                    )
                    if not has_tenant:
                        continue

            # Delete the session
            try:
                self._redis.delete(session_key)
                self._redis.srem(user_sessions_key, token)
                count += 1
            except redis.ConnectionError as exc:
                log.error(
                    "Redis connection failed deleting session during invalidation: %s",
                    exc,
                )
                raise ServiceUnavailableError(
                    "Session store unavailable"
                ) from exc

        log.info(
            "Invalidated %d session(s) for discord_user_id=%s (tenant_filter=%s)",
            count,
            discord_user_id,
            tenant_id,
        )
        return count

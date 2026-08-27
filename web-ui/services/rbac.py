"""Role-based access control service for the HelloDJ SaaS platform.

Handles role management (grant/revoke), permission checking, and effective
role resolution for the multi-tenant delegation system.

Role hierarchy (total order):
    operator(5) > owner(4) > admin(3) > editor(2) > viewer(1)

Usage:
    from services.rbac import RBACService, DelegateLimitError, InvalidRoleError

    rbac = RBACService(pg_uri=PG_URI, redis_client=redis_client)
    roles = rbac.get_user_roles(discord_user_id=123456789)
    rbac.grant_role(tenant_id, discord_user_id, "editor", granted_by=owner_id)
    rbac.check_permission(session, tenant_id, "editor")
"""

from __future__ import annotations

import logging
from typing import Any

import psycopg2
import psycopg2.extras
import redis

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class DelegateLimitError(Exception):
    """Raised when a tenant has reached the maximum number of delegates."""

    pass


class InvalidRoleError(Exception):
    """Raised when an invalid role is specified for delegation."""

    pass


# ---------------------------------------------------------------------------
# RBACService
# ---------------------------------------------------------------------------


class RBACService:
    """Role-based access control for tenant resources.

    Uses PostgreSQL for persistent role storage and Redis (via SessionService)
    for session invalidation on role changes.
    """

    ROLE_HIERARCHY: dict[str, int] = {
        "operator": 5,
        "owner": 4,
        "admin": 3,
        "editor": 2,
        "viewer": 1,
    }

    MAX_DELEGATES_PER_TENANT: int = 20

    # Roles that can be assigned via delegation (not operator/owner)
    ASSIGNABLE_ROLES: set[str] = {"admin", "editor", "viewer"}

    def __init__(self, pg_uri: str, redis_client: redis.Redis):
        """Initialize the RBAC service.

        Args:
            pg_uri: PostgreSQL connection URI for the tenant/roles database.
            redis_client: Redis client for session invalidation.
        """
        self._pg_uri = pg_uri
        self._redis = redis_client

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_conn(self):
        """Create a new psycopg2 connection with RealDictCursor factory."""
        return psycopg2.connect(
            self._pg_uri,
            cursor_factory=psycopg2.extras.RealDictCursor,
        )

    def _get_session_service(self):
        """Lazy import of SessionService to avoid circular imports."""
        from services.session_service import SessionService

        return SessionService(self._redis)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_user_roles(self, discord_user_id: int) -> list[dict]:
        """Query all tenant_roles for a given Discord user.

        Args:
            discord_user_id: The Discord user's numeric ID.

        Returns:
            List of dicts with keys: tenant_id, role.
            Example: [{"tenant_id": "uuid-1", "role": "admin"}, ...]
        """
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT tenant_id, role FROM tenant_roles "
                    "WHERE discord_user_id = %s",
                    (discord_user_id,),
                )
                rows = cur.fetchall()

        return [{"tenant_id": str(row["tenant_id"]), "role": row["role"]} for row in rows]

    def grant_role(
        self,
        tenant_id: str,
        discord_user_id: int,
        role: str,
        granted_by: int,
    ) -> None:
        """Assign a role to a Discord user for a tenant.

        Validates the role, checks the delegate limit (max 20 per tenant),
        performs an UPSERT into tenant_roles, and invalidates affected sessions.

        Args:
            tenant_id: UUID of the tenant.
            discord_user_id: Discord user ID to grant the role to.
            role: One of 'admin', 'editor', 'viewer'.
            granted_by: Discord user ID of the granting user (owner).

        Raises:
            InvalidRoleError: If role is not in ('admin', 'editor', 'viewer').
            DelegateLimitError: If the tenant already has 20 delegates.
        """
        if role not in self.ASSIGNABLE_ROLES:
            raise InvalidRoleError(
                f"Invalid role '{role}'. Must be one of: "
                f"{', '.join(sorted(self.ASSIGNABLE_ROLES))}"
            )

        with self._get_conn() as conn:
            with conn.cursor() as cur:
                # Check current delegate count (exclude the user being
                # granted, in case this is an update)
                cur.execute(
                    "SELECT COUNT(*) AS cnt FROM tenant_roles "
                    "WHERE tenant_id = %s AND discord_user_id != %s",
                    (tenant_id, discord_user_id),
                )
                count = cur.fetchone()["cnt"]

                if count >= self.MAX_DELEGATES_PER_TENANT:
                    raise DelegateLimitError(
                        f"Maximum delegate limit of {self.MAX_DELEGATES_PER_TENANT} "
                        f"reached for tenant {tenant_id}"
                    )

                # UPSERT: insert or update existing role
                cur.execute(
                    """
                    INSERT INTO tenant_roles
                        (tenant_id, discord_user_id, role, granted_by, granted_at)
                    VALUES (%s, %s, %s, %s, now())
                    ON CONFLICT (tenant_id, discord_user_id)
                    DO UPDATE SET role = EXCLUDED.role,
                                  granted_by = EXCLUDED.granted_by,
                                  granted_at = now()
                    """,
                    (tenant_id, discord_user_id, role, granted_by),
                )
            conn.commit()

        # Invalidate affected user's sessions
        try:
            session_svc = self._get_session_service()
            session_svc.invalidate_user_sessions(
                discord_user_id=str(discord_user_id),
                tenant_id=tenant_id,
            )
        except Exception as exc:
            log.warning(
                "Failed to invalidate sessions for user=%s tenant=%s: %s",
                discord_user_id,
                tenant_id,
                exc,
            )

        log.info(
            "Granted role=%s to user=%s for tenant=%s (by=%s)",
            role,
            discord_user_id,
            tenant_id,
            granted_by,
        )

    def revoke_role(self, tenant_id: str, discord_user_id: int) -> None:
        """Remove a user's role for a tenant.

        Deletes the role from tenant_roles and invalidates affected sessions.

        Args:
            tenant_id: UUID of the tenant.
            discord_user_id: Discord user ID whose access is being revoked.
        """
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM tenant_roles "
                    "WHERE tenant_id = %s AND discord_user_id = %s",
                    (tenant_id, discord_user_id),
                )
            conn.commit()

        # Invalidate affected user's sessions
        try:
            session_svc = self._get_session_service()
            session_svc.invalidate_user_sessions(
                discord_user_id=str(discord_user_id),
                tenant_id=tenant_id,
            )
        except Exception as exc:
            log.warning(
                "Failed to invalidate sessions for user=%s tenant=%s: %s",
                discord_user_id,
                tenant_id,
                exc,
            )

        log.info(
            "Revoked role for user=%s from tenant=%s",
            discord_user_id,
            tenant_id,
        )

    def check_permission(
        self,
        session: dict[str, Any],
        tenant_id: str,
        required_role: str,
    ) -> bool:
        """Check if the session has the required role for a tenant.

        Uses the role hierarchy: operator > owner > admin > editor > viewer.
        Returns True if the user's effective role >= required_role.

        Args:
            session: The session dict (from Redis).
            tenant_id: UUID of the tenant to check access for.
            required_role: Minimum role needed (e.g. "editor").

        Returns:
            True if the user's effective role level >= required role level.
        """
        effective_role = self.get_effective_role(session, tenant_id)
        if effective_role is None:
            return False

        effective_level = self.ROLE_HIERARCHY.get(effective_role, 0)
        required_level = self.ROLE_HIERARCHY.get(required_role, 0)

        return effective_level >= required_level

    def get_effective_role(
        self,
        session: dict[str, Any],
        tenant_id: str,
    ) -> str | None:
        """Get the user's effective role for a specific tenant.

        Priority:
        1. is_operator → "operator"
        2. tenant owned by user (matching discord_user_id in roles as "owner") → "owner"
        3. tenant_roles entry in session's roles list → the assigned role
        4. None (no access)

        Args:
            session: The session dict (from Redis). Expected keys:
                - is_operator (bool)
                - roles (list of {"tenant_id": str, "role": str})
            tenant_id: UUID of the tenant to check.

        Returns:
            The effective role string, or None if no access.
        """
        # 1. Operator has access to everything
        if session.get("is_operator"):
            return "operator"

        # 2 & 3. Check roles list for this tenant
        roles = session.get("roles", [])
        for role_entry in roles:
            if str(role_entry.get("tenant_id")) == str(tenant_id):
                return role_entry.get("role")

        # 4. No access
        return None

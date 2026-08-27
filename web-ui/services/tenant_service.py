"""Tenant management service for the HelloDJ SaaS platform.

Handles tenant CRUD operations against PostgreSQL:
- Auto-creation (UPSERT) on Discord OAuth2 login
- Lookup by tenant UUID or Discord user ID
- Listing accessible tenants (owned + delegated via tenant_roles)

Uses psycopg2 with connection-per-call and RealDictCursor, matching existing
service conventions in the web-ui codebase.

Usage:
    from services.tenant_service import TenantService

    svc = TenantService(pg_uri="postgresql://...")
    tenant = svc.upsert(discord_user_id=123, discord_username="celes", email="a@b.com")
"""

from __future__ import annotations

import logging

import psycopg2
import psycopg2.extras

log = logging.getLogger(__name__)


class TenantService:
    """Manages tenant CRUD operations against PostgreSQL."""

    def __init__(self, pg_uri: str):
        self._pg_uri = pg_uri

    def _get_conn(self):
        """Get a psycopg2 connection to PostgreSQL."""
        return psycopg2.connect(self._pg_uri)

    def upsert(
        self,
        discord_user_id: int,
        discord_username: str,
        email: str | None,
    ) -> dict:
        """UPSERT tenant on login. Returns tenant dict with id.

        Inserts a new tenant if one doesn't exist for the given discord_user_id,
        or updates the username/email if one already exists.

        Truncates discord_username to 32 chars and email to 254 chars before INSERT.
        Uses a single transaction with a 5-second statement timeout.

        Returns:
            Dict with keys: id, discord_user_id, discord_username, email, created_at
        """
        # Truncate inputs to safe lengths
        discord_username = discord_username[:32]
        if email is not None:
            email = email[:254]

        conn = self._get_conn()
        try:
            conn.autocommit = False
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SET LOCAL statement_timeout = '5000'")
                cur.execute(
                    """
                    INSERT INTO tenants (discord_user_id, discord_username, email)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (discord_user_id) DO UPDATE
                        SET discord_username = EXCLUDED.discord_username,
                            email = EXCLUDED.email,
                            updated_at = now()
                    RETURNING id, discord_user_id, discord_username, email, created_at
                    """,
                    (discord_user_id, discord_username, email),
                )
                tenant = cur.fetchone()
                conn.commit()
                return dict(tenant)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def get_by_id(self, tenant_id: str) -> dict | None:
        """Fetch a tenant by UUID.

        Args:
            tenant_id: The tenant's UUID as a string.

        Returns:
            Tenant dict or None if not found.
        """
        conn = self._get_conn()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT id, discord_user_id, discord_username, email, created_at
                    FROM tenants
                    WHERE id = %s
                    """,
                    (tenant_id,),
                )
                row = cur.fetchone()
                return dict(row) if row else None
        finally:
            conn.close()

    def get_by_discord_user_id(self, discord_user_id: int) -> dict | None:
        """Fetch a tenant by Discord user ID.

        Args:
            discord_user_id: The Discord user's numeric ID.

        Returns:
            Tenant dict or None if not found.
        """
        conn = self._get_conn()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT id, discord_user_id, discord_username, email, created_at
                    FROM tenants
                    WHERE discord_user_id = %s
                    """,
                    (discord_user_id,),
                )
                row = cur.fetchone()
                return dict(row) if row else None
        finally:
            conn.close()

    def list_accessible_tenants(self, discord_user_id: int) -> list[dict]:
        """Get all tenants a user can access (owned + delegated).

        Queries the user's owned tenant (from the tenants table) UNION ALL
        tenants they have delegated roles for (from tenant_roles joined with
        tenants).

        Args:
            discord_user_id: The Discord user's numeric ID.

        Returns:
            List of dicts with keys: tenant_id, role, discord_username, created_at
        """
        conn = self._get_conn()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT
                        t.id AS tenant_id,
                        'owner' AS role,
                        t.discord_username,
                        t.created_at
                    FROM tenants t
                    WHERE t.discord_user_id = %s

                    UNION ALL

                    SELECT
                        tr.tenant_id,
                        tr.role,
                        t.discord_username,
                        t.created_at
                    FROM tenant_roles tr
                    JOIN tenants t ON t.id = tr.tenant_id
                    WHERE tr.discord_user_id = %s
                    """,
                    (discord_user_id, discord_user_id),
                )
                rows = cur.fetchall()
                return [dict(row) for row in rows]
        finally:
            conn.close()

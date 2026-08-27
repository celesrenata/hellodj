"""HelloDJ SaaS platform services — centralized factory functions.

Provides lazy-singleton accessors for the core service layer:
- get_session_service() → SessionService (Redis-backed sessions)
- get_tenant_service()  → TenantService (PostgreSQL tenant CRUD)
- get_rbac_service()    → RBACService (role-based access control)

Each factory reads connection info from environment variables on first call
and caches the instance for subsequent calls. Override functions are provided
for testing (inject fakes/mocks without touching environment).

Usage:
    from services import get_session_service, get_tenant_service, get_rbac_service

    session_svc = get_session_service()
    tenant_svc = get_tenant_service()
    rbac_svc = get_rbac_service()
"""

from __future__ import annotations

import logging
import os

import redis

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Redis connection (lazy singleton)
# ---------------------------------------------------------------------------

_redis_client: redis.Redis | None = None


def _get_redis() -> redis.Redis:
    """Return a shared Redis client, creating one on first call."""
    global _redis_client
    if _redis_client is None:
        redis_url = os.environ.get(
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
# SessionService (lazy singleton)
# ---------------------------------------------------------------------------

_session_service = None


def get_session_service():
    """Return a SessionService instance, creating one on first call.

    Uses the shared Redis client from _get_redis().
    """
    global _session_service
    if _session_service is None:
        from services.session_service import SessionService

        _session_service = SessionService(_get_redis())
    return _session_service


def set_session_service(service) -> None:
    """Override the SessionService instance (for testing)."""
    global _session_service
    _session_service = service


# ---------------------------------------------------------------------------
# TenantService (lazy singleton)
# ---------------------------------------------------------------------------

_tenant_service = None


def get_tenant_service():
    """Return a TenantService instance, creating one on first call.

    Reads HELLODJ_PG_URI from environment for the PostgreSQL connection.
    """
    global _tenant_service
    if _tenant_service is None:
        from services.tenant_service import TenantService

        pg_uri = os.environ.get(
            "HELLODJ_PG_URI",
            "postgresql://hellodj:hellodj@postgresql-rw.postgresql-service.svc.cluster.local:5432/hellodj",
        )
        _tenant_service = TenantService(pg_uri=pg_uri)
    return _tenant_service


def set_tenant_service(service) -> None:
    """Override the TenantService instance (for testing)."""
    global _tenant_service
    _tenant_service = service


# ---------------------------------------------------------------------------
# RBACService (lazy singleton)
# ---------------------------------------------------------------------------

_rbac_service = None


def get_rbac_service():
    """Return an RBACService instance, creating one on first call.

    Reads HELLODJ_PG_URI from environment for PostgreSQL and uses the
    shared Redis client for session invalidation.
    """
    global _rbac_service
    if _rbac_service is None:
        from services.rbac import RBACService

        pg_uri = os.environ.get(
            "HELLODJ_PG_URI",
            "postgresql://hellodj:hellodj@postgresql-rw.postgresql-service.svc.cluster.local:5432/hellodj",
        )
        _rbac_service = RBACService(pg_uri=pg_uri, redis_client=_get_redis())
    return _rbac_service


def set_rbac_service(service) -> None:
    """Override the RBACService instance (for testing)."""
    global _rbac_service
    _rbac_service = service

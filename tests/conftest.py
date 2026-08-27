"""Shared test fixtures for HelloDJ SaaS platform property-based tests.

Provides:
- pg_pool: asyncpg connection pool to a testcontainers PostgreSQL instance (schema applied)
- redis_client: fakeredis client
- mock_k8s_client: mocked kubernetes-client for orchestrator tests
- mock_discord_oauth: mocked Discord OAuth2 responses

Used by all 12 correctness property tests from the hellodj-saas-platform design.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import AsyncGenerator, Generator
from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg
import fakeredis
import pytest
import pytest_asyncio

# Ensure bot/ is importable
_bot_dir = str(Path(__file__).resolve().parent.parent / "bot")
if _bot_dir not in sys.path:
    sys.path.insert(0, _bot_dir)


# ---------------------------------------------------------------------------
# Hypothesis global settings
# ---------------------------------------------------------------------------

from hypothesis import settings as hypothesis_settings

hypothesis_settings.register_profile(
    "saas", max_examples=100, deadline=None
)
hypothesis_settings.load_profile("saas")


# ---------------------------------------------------------------------------
# PostgreSQL schema SQL (from scripts/migrate_schema.py)
# ---------------------------------------------------------------------------

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS credentials (
    key         TEXT PRIMARY KEY,
    value       BYTEA NOT NULL,
    updated_at  TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS tenants (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    discord_user_id   BIGINT UNIQUE NOT NULL,
    discord_username  TEXT,
    email             TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS subscriptions (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   UUID NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    plan        TEXT NOT NULL CHECK (plan IN ('base', 'trial')),
    addons      TEXT[] DEFAULT '{}',
    status      TEXT NOT NULL CHECK (status IN ('active', 'past_due', 'cancelled', 'expired', 'pending_payment')),
    started_at  TIMESTAMPTZ NOT NULL,
    expires_at  TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS bot_instances (
    id                          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id                   UUID NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    discord_bot_token_encrypted BYTEA,
    guild_ids                   BIGINT[],
    status                      TEXT NOT NULL CHECK (status IN ('provisioning', 'running', 'stopped', 'error', 'pending_resources', 'failed')),
    node_name                   TEXT,
    pod_name                    TEXT,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS payments (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    paypal_txn_id   TEXT UNIQUE,
    amount_cents    INTEGER NOT NULL CHECK (amount_cents > 0),
    currency        TEXT NOT NULL DEFAULT 'USD',
    status          TEXT NOT NULL CHECK (status IN ('pending', 'completed', 'refunded', 'failed')),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS trial_applications (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   UUID NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    status      TEXT NOT NULL CHECK (status IN ('pending', 'approved', 'rejected')),
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    decided_at  TIMESTAMPTZ,
    decided_by  TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
    tenant_id       UUID NOT NULL,
    guild_id        BIGINT NOT NULL,
    channel_id      BIGINT NOT NULL,
    session_data    JSONB NOT NULL,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, guild_id, channel_id),
    CONSTRAINT session_data_size CHECK (octet_length(session_data::text) <= 1048576)
);

CREATE TABLE IF NOT EXISTS playlists (
    tenant_id   UUID NOT NULL,
    playlist_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    guild_id    BIGINT NOT NULL,
    name        TEXT NOT NULL CHECK (char_length(name) <= 100),
    tracks      JSONB NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT playlist_tracks_size CHECK (octet_length(tracks::text) <= 5242880)
);

CREATE UNIQUE INDEX IF NOT EXISTS playlists_unique_name
    ON playlists (tenant_id, guild_id, lower(name));

CREATE INDEX IF NOT EXISTS idx_subscriptions_tenant ON subscriptions(tenant_id);
CREATE INDEX IF NOT EXISTS idx_subscriptions_status ON subscriptions(status);
CREATE INDEX IF NOT EXISTS idx_bot_instances_tenant ON bot_instances(tenant_id);
CREATE INDEX IF NOT EXISTS idx_bot_instances_status ON bot_instances(status);
CREATE INDEX IF NOT EXISTS idx_payments_tenant ON payments(tenant_id);
CREATE INDEX IF NOT EXISTS idx_payments_created ON payments(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_trial_applications_status ON trial_applications(status);
CREATE INDEX IF NOT EXISTS idx_sessions_tenant ON sessions(tenant_id);
CREATE INDEX IF NOT EXISTS idx_playlists_tenant_guild ON playlists(tenant_id, guild_id);
"""


# ---------------------------------------------------------------------------
# PostgreSQL fixture (testcontainers)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def pg_container():
    """Start a PostgreSQL testcontainer for the test session.

    Yields the container instance. Automatically stopped after session ends.
    """
    from testcontainers.postgres import PostgresContainer

    container = PostgresContainer(
        image="postgres:16-alpine",
        username="testuser",
        password="testpass",
        dbname="hellodj_test",
    )
    container.start()
    yield container
    container.stop()


@pytest.fixture(scope="session")
def pg_connection_url(pg_container) -> str:
    """Return asyncpg-compatible connection URL for the test PG container."""
    # testcontainers provides a psycopg2-style URL; convert for asyncpg
    host = pg_container.get_container_host_ip()
    port = pg_container.get_exposed_port(5432)
    return f"postgresql://testuser:testpass@{host}:{port}/hellodj_test"


@pytest.fixture(scope="session")
def _apply_schema(pg_connection_url: str):
    """Apply the full SaaS platform schema to the test database (once per session)."""

    async def _apply():
        conn = await asyncpg.connect(pg_connection_url)
        try:
            await conn.execute(_SCHEMA_SQL)
        finally:
            await conn.close()

    asyncio.get_event_loop_policy()
    loop = asyncio.new_event_loop()
    loop.run_until_complete(_apply())
    loop.close()


@pytest_asyncio.fixture
async def pg_pool(
    pg_connection_url: str, _apply_schema
) -> AsyncGenerator[asyncpg.Pool, None]:
    """Provide an asyncpg connection pool to the test PostgreSQL instance.

    Schema is applied once per session. Each test gets a fresh pool.
    Tables are truncated between tests to ensure isolation.
    """
    pool = await asyncpg.create_pool(pg_connection_url, min_size=2, max_size=5)
    assert pool is not None

    # Truncate all tables for test isolation (order matters due to FK constraints)
    async with pool.acquire() as conn:
        await conn.execute(
            """
            TRUNCATE TABLE playlists, sessions, trial_applications,
                          payments, bot_instances, subscriptions, tenants,
                          credentials
            CASCADE;
            """
        )

    yield pool

    await pool.close()


# ---------------------------------------------------------------------------
# Redis fixture (fakeredis)
# ---------------------------------------------------------------------------


@pytest.fixture
def redis_client() -> Generator[fakeredis.FakeRedis, None, None]:
    """Provide a fakeredis client that resets between tests.

    Simulates the Redis session/cache store without needing a real Redis server.
    Supports all Redis commands used by the platform (strings, sets, pub/sub keys).
    """
    client = fakeredis.FakeRedis(decode_responses=True)
    yield client
    client.flushall()
    client.close()


@pytest_asyncio.fixture
async def async_redis_client():
    """Provide an async fakeredis client for async test contexts."""
    client = fakeredis.FakeAsyncRedis(decode_responses=True)
    yield client
    await client.aclose()


# ---------------------------------------------------------------------------
# Kubernetes client mock
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_k8s_client() -> MagicMock:
    """Provide a mocked Kubernetes client for Bot Orchestrator tests.

    Mocks the kubernetes-client CoreV1Api and relevant methods:
    - create_namespaced_pod
    - delete_namespaced_pod
    - read_namespaced_pod
    - list_namespaced_pod
    - read_namespaced_pod_status
    """
    k8s_client = MagicMock()

    # CoreV1Api mock
    core_v1 = MagicMock()
    k8s_client.CoreV1Api.return_value = core_v1

    # Pod creation returns a mock pod with metadata
    pod_mock = MagicMock()
    pod_mock.metadata.name = "tenant-bot-abc123"
    pod_mock.metadata.namespace = "hellodj-service"
    pod_mock.status.phase = "Running"
    pod_mock.status.conditions = []

    core_v1.create_namespaced_pod = MagicMock(return_value=pod_mock)
    core_v1.delete_namespaced_pod = MagicMock(return_value=None)
    core_v1.read_namespaced_pod = MagicMock(return_value=pod_mock)

    # Pod list for health checks
    pod_list_mock = MagicMock()
    pod_list_mock.items = [pod_mock]
    core_v1.list_namespaced_pod = MagicMock(return_value=pod_list_mock)

    # Pod status
    status_mock = MagicMock()
    status_mock.status.phase = "Running"
    status_mock.status.container_statuses = [
        MagicMock(ready=True, restart_count=0)
    ]
    core_v1.read_namespaced_pod_status = MagicMock(return_value=status_mock)

    # Attach the core_v1 mock for direct access
    k8s_client.core_v1 = core_v1

    return k8s_client


# ---------------------------------------------------------------------------
# Discord OAuth2 mock
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_discord_oauth() -> MagicMock:
    """Provide mocked Discord OAuth2 responses for Auth Service tests.

    Simulates:
    - Token exchange (POST /oauth2/token)
    - User profile fetch (GET /users/@me)
    - Error scenarios (denied, expired, service unavailable)
    """
    oauth_mock = MagicMock()

    # Successful token exchange response
    oauth_mock.exchange_code = AsyncMock(
        return_value={
            "access_token": "mock_access_token_abc123",
            "token_type": "Bearer",
            "expires_in": 604800,
            "refresh_token": "mock_refresh_token_xyz789",
            "scope": "identify email",
        }
    )

    # Successful user profile response
    oauth_mock.get_user_profile = AsyncMock(
        return_value={
            "id": "123456789012345678",
            "username": "testuser",
            "discriminator": "0",
            "avatar": "abc123def456",
            "email": "testuser@example.com",
            "verified": True,
            "global_name": "Test User",
        }
    )

    # Error simulation helpers
    oauth_mock.exchange_code_denied = AsyncMock(
        side_effect=Exception("access_denied: The resource owner denied the request")
    )
    oauth_mock.exchange_code_timeout = AsyncMock(
        side_effect=TimeoutError("Discord OAuth2 token exchange timed out (10s)")
    )
    oauth_mock.exchange_code_invalid = AsyncMock(
        side_effect=ValueError("Invalid authorization code")
    )

    return oauth_mock

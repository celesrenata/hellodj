"""End-to-end integration tests for HelloDJ SaaS platform.

Tests the full lifecycle flows that span multiple services:
1. Trial flow: OAuth2 → tenant creation → trial apply → approve → verify active with 30-day expiry → Base_Plan features
2. Subscription flow: Create tenant → create subscription → simulate payment → activate → verify feature flags include addons
3. Expiry flow: Create active subscription → expire → verify 3-day grace → verify bot instance status changes

Uses testcontainers PostgreSQL for real database testing.
Mocks: K8s client (bot orchestrator), Redis (fakeredis).

Requirements: 4.2, 4.3, 6.3, 7.5, 7.6, 7.7, 10.1
"""

from __future__ import annotations

import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import fakeredis
import psycopg2
import psycopg2.extras
import psycopg2.extensions
import pytest

# Register UUID adapter for psycopg2
psycopg2.extensions.register_adapter(
    uuid.UUID, lambda u: psycopg2.extensions.AsIs(f"'{u}'")
)

# Ensure web-ui/ and bot/ are importable
_webui_dir = str(Path(__file__).resolve().parent.parent / "web-ui")
_bot_dir = str(Path(__file__).resolve().parent.parent / "bot")
if _webui_dir not in sys.path:
    sys.path.insert(0, _webui_dir)
if _bot_dir not in sys.path:
    sys.path.insert(0, _bot_dir)

from services.subscription_manager import (
    GRACE_PERIOD_DAYS,
    SubscriptionManager,
)
from services import trial_manager
from services.feature_flags import compute_features, get_features
from services.bot_orchestrator import BotOrchestrator


# ---------------------------------------------------------------------------
# Schema SQL (shared across integration tests)
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
"""


# ---------------------------------------------------------------------------
# Session-scoped fixtures: testcontainer, schema
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def pg_container():
    """Start a PostgreSQL testcontainer for the entire test session."""
    from testcontainers.postgres import PostgresContainer

    container = PostgresContainer(
        image="postgres:16-alpine",
        username="testuser",
        password="testpass",
        dbname="hellodj_e2e",
    )
    container.start()
    yield container
    container.stop()


@pytest.fixture(scope="session")
def pg_url(pg_container) -> str:
    """Return psycopg2-compatible connection URL."""
    host = pg_container.get_container_host_ip()
    port = pg_container.get_exposed_port(5432)
    return f"postgresql://testuser:testpass@{host}:{port}/hellodj_e2e"


@pytest.fixture(scope="session")
def _apply_schema(pg_url: str):
    """Apply schema once for the test session."""
    conn = psycopg2.connect(pg_url)
    try:
        with conn.cursor() as cur:
            cur.execute(_SCHEMA_SQL)
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Per-test fixtures: clean DB, services, mocks
# ---------------------------------------------------------------------------


@pytest.fixture
def db_url(pg_url: str, _apply_schema) -> str:
    """Provide a clean database URL with all tables truncated between tests."""
    conn = psycopg2.connect(pg_url)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                TRUNCATE TABLE playlists, sessions, trial_applications,
                              payments, bot_instances, subscriptions, tenants,
                              credentials
                CASCADE;
                """
            )
        conn.commit()
    finally:
        conn.close()
    return pg_url


@pytest.fixture
def redis_client():
    """Provide a fakeredis client that resets between tests."""
    client = fakeredis.FakeRedis(decode_responses=True)
    yield client
    client.flushall()
    client.close()


@pytest.fixture
def mock_k8s():
    """Mock Kubernetes API for bot orchestrator."""
    mock_api = MagicMock()
    pod_mock = MagicMock()
    pod_mock.metadata.name = "tenant-bot-test1234"
    pod_mock.metadata.namespace = "hellodj-service"
    pod_mock.status.phase = "Running"
    mock_api.create_namespaced_pod = MagicMock(return_value=pod_mock)
    mock_api.delete_namespaced_pod = MagicMock(return_value=None)
    mock_api.read_namespaced_pod = MagicMock(return_value=pod_mock)
    return mock_api


@pytest.fixture
def subscription_mgr(db_url: str) -> SubscriptionManager:
    """Provide a SubscriptionManager connected to the test database."""
    return SubscriptionManager(pg_uri=db_url)


@pytest.fixture
def orchestrator(db_url: str, mock_k8s) -> BotOrchestrator:
    """Provide a BotOrchestrator with mocked K8s and test DB."""
    orch = BotOrchestrator(
        pg_uri=db_url,
        redis_url="redis://localhost:6379",
        k8s_configured=True,  # Skip K8s config loading
    )
    # Patch the K8s API method to return our mock
    orch._get_k8s_core_api = MagicMock(return_value=mock_k8s)
    return orch


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def _create_tenant(db_url: str, discord_user_id: int = 123456789012345678,
                   username: str = "testuser", email: str = "test@example.com") -> uuid.UUID:
    """Insert a tenant record and return its UUID."""
    tid = uuid.uuid4()
    conn = psycopg2.connect(db_url)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO tenants (id, discord_user_id, discord_username, email)
                VALUES (%s, %s, %s, %s)
                """,
                (tid, discord_user_id, username, email),
            )
        conn.commit()
    finally:
        conn.close()
    return tid


def _get_subscription(db_url: str, subscription_id: uuid.UUID) -> dict | None:
    """Read a subscription record by ID."""
    conn = psycopg2.connect(db_url)
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM subscriptions WHERE id = %s", (subscription_id,))
            row = cur.fetchone()
            return dict(row) if row else None
    finally:
        conn.close()


def _get_bot_instances(db_url: str, tenant_id: uuid.UUID) -> list[dict]:
    """Read all bot instances for a tenant."""
    conn = psycopg2.connect(db_url)
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT * FROM bot_instances WHERE tenant_id = %s",
                (tenant_id,),
            )
            return [dict(row) for row in cur.fetchall()]
    finally:
        conn.close()


def _insert_payment(db_url: str, tenant_id: uuid.UUID, amount_cents: int,
                    txn_id: str = "PAYPAL-TXN-001") -> uuid.UUID:
    """Insert a completed payment record and return its UUID."""
    payment_id = uuid.uuid4()
    conn = psycopg2.connect(db_url)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO payments (id, tenant_id, paypal_txn_id, amount_cents, status)
                VALUES (%s, %s, %s, %s, 'completed')
                """,
                (payment_id, tenant_id, txn_id, amount_cents),
            )
        conn.commit()
    finally:
        conn.close()
    return payment_id


# ---------------------------------------------------------------------------
# Test Class 1: Trial Flow (End-to-End)
# Req 4.2, 4.3, 6.3: OAuth2 → tenant creation → trial apply → approve → active
# ---------------------------------------------------------------------------


class TestTrialFlowE2E:
    """Integration test: Full trial lifecycle from tenant creation to expiry.

    Flow:
    1. Simulate OAuth2 creating a new tenant in the DB
    2. Apply for trial
    3. Approve trial (admin action)
    4. Verify subscription is active with 30-day expiry
    5. Verify feature flags are Base_Plan only (audio=True, video=False)
    """

    def test_trial_full_lifecycle(self, db_url: str, redis_client):
        """Test the complete trial flow: create tenant → apply → approve → verify features."""
        # Step 1: Simulate OAuth2 tenant creation (Req 4.2, 4.3)
        tenant_id = _create_tenant(
            db_url,
            discord_user_id=111222333444555666,
            username="trial_user",
            email="trial@example.com",
        )

        # Step 2: Apply for trial (Req 6.1)
        with patch.object(trial_manager, "PG_URI", db_url):
            application = trial_manager.apply(str(tenant_id))

        assert application["status"] == "pending"
        assert str(application["tenant_id"]) == str(tenant_id)
        app_id = str(application["id"])

        # Step 3: Approve trial (Req 6.3 — admin approves)
        with patch.object(trial_manager, "PG_URI", db_url):
            result = trial_manager.approve(app_id, decided_by="operator_admin")

        assert result["application"]["status"] == "approved"
        subscription = result["subscription"]
        assert subscription["plan"] == "trial"
        assert subscription["status"] == "active"
        assert str(subscription["tenant_id"]) == str(tenant_id)

        # Step 4: Verify 30-day expiry (Req 6.3, 6.4)
        expires_at = subscription["expires_at"]
        started_at = subscription["started_at"]
        duration = expires_at - started_at
        # Allow 1 second tolerance for test execution time
        assert timedelta(days=29, hours=23) <= duration <= timedelta(days=30, seconds=2)

        # Step 5: Verify feature flags are Base_Plan only (Req 13.1)
        # compute_features reads the plan/addons from the subscription
        flags = compute_features(subscription["plan"], subscription["addons"])
        assert flags["audio"] is True
        assert flags["video"] is False
        assert flags["tidal_hifi"] is False
        assert flags["lossless"] is False
        assert flags["priority_queue"] is False
        assert flags["max_bot_instances"] == 1

    def test_trial_apply_rejected_when_active_subscription_exists(self, db_url: str):
        """Applying for trial when subscription already exists raises TrialError."""
        tenant_id = _create_tenant(
            db_url,
            discord_user_id=222333444555666777,
            username="existing_sub_user",
        )

        # Create an active subscription for this tenant
        mgr = SubscriptionManager(pg_uri=db_url)
        sub = mgr.create_subscription(tenant_id, "base")
        mgr.activate(sub["id"])

        # Attempt to apply for trial should fail
        with patch.object(trial_manager, "PG_URI", db_url):
            with pytest.raises(trial_manager.TrialError, match="already has an active"):
                trial_manager.apply(str(tenant_id))

    def test_trial_feature_flags_served_via_cache(self, db_url: str, redis_client):
        """Feature flags for trial tenant are correctly cached in Redis."""
        tenant_id = _create_tenant(
            db_url,
            discord_user_id=333444555666777888,
            username="cache_test_user",
        )

        # Create and approve trial
        with patch.object(trial_manager, "PG_URI", db_url):
            app = trial_manager.apply(str(tenant_id))
            trial_manager.approve(str(app["id"]), decided_by="admin")

        # Get features via the full get_features path (queries DB + caches in Redis)
        conn = psycopg2.connect(db_url)
        try:
            flags = get_features(str(tenant_id), conn, redis_client)
        finally:
            conn.close()

        assert flags["audio"] is True
        assert flags["video"] is False
        assert flags["tidal_hifi"] is False

        # Verify Redis has the cached value
        cached = redis_client.get(f"features:{tenant_id}")
        assert cached is not None


# ---------------------------------------------------------------------------
# Test Class 2: Subscription Flow (End-to-End)
# Req 7.5, 7.6: Create subscription → payment → activate → feature flags
# ---------------------------------------------------------------------------


class TestSubscriptionFlowE2E:
    """Integration test: Full subscription lifecycle including payment and provisioning.

    Flow:
    1. Create tenant (simulates OAuth2 completion)
    2. Create subscription (pending_payment)
    3. Simulate PayPal IPN payment
    4. Activate subscription
    5. Verify feature flags include addon features
    6. Verify bot instance provisioned
    """

    def test_base_plan_subscription_flow(self, db_url: str, redis_client, orchestrator, mock_k8s):
        """Test base plan: create → pay → activate → provision bot → verify features."""
        # Step 1: Create tenant
        tenant_id = _create_tenant(
            db_url,
            discord_user_id=444555666777888999,
            username="subscriber_user",
        )

        # Step 2: Create subscription (Req 7.5 — status pending_payment)
        mgr = SubscriptionManager(pg_uri=db_url)
        subscription = mgr.create_subscription(tenant_id, "base")
        assert subscription["status"] == "pending_payment"
        assert subscription["plan"] == "base"
        sub_id = subscription["id"]

        # Step 3: Simulate PayPal payment (Req 8.3 — payment verified)
        _insert_payment(db_url, tenant_id, amount_cents=699, txn_id="PAYPAL-BASE-001")

        # Step 4: Activate subscription (Req 7.6 — payment verified → active)
        activated = mgr.activate(sub_id)
        assert activated["status"] == "active"

        # Step 5: Provision bot instance (Req 10.1)
        instance = orchestrator.provision(tenant_id, activated)
        assert instance["status"] == "provisioning"
        assert str(instance["tenant_id"]) == str(tenant_id)
        mock_k8s.create_namespaced_pod.assert_called_once()

        # Step 6: Verify feature flags (Base_Plan: audio only)
        conn = psycopg2.connect(db_url)
        try:
            flags = get_features(str(tenant_id), conn, redis_client)
        finally:
            conn.close()

        assert flags["audio"] is True
        assert flags["video"] is False
        assert flags["tidal_hifi"] is False
        assert flags["max_bot_instances"] == 1

    def test_subscription_with_video_addon(self, db_url: str, redis_client, orchestrator, mock_k8s):
        """Test subscription with video addon: features include video capabilities."""
        # Create tenant with active base plan first (addon prerequisite)
        tenant_id = _create_tenant(
            db_url,
            discord_user_id=555666777888999000,
            username="video_subscriber",
        )

        # Create and activate base plan
        mgr = SubscriptionManager(pg_uri=db_url)
        base_sub = mgr.create_subscription(tenant_id, "base")
        mgr.activate(base_sub["id"])

        # Now create subscription with video addon
        addon_sub = mgr.create_subscription(tenant_id, "base", addons=["video"])
        assert addon_sub["status"] == "pending_payment"
        assert addon_sub["addons"] == ["video"]

        # Simulate payment ($6.99 + $1.99 = $8.98 = 898 cents)
        _insert_payment(db_url, tenant_id, amount_cents=898, txn_id="PAYPAL-VIDEO-001")

        # Activate addon subscription
        activated = mgr.activate(addon_sub["id"])
        assert activated["status"] == "active"

        # Provision bot with video tier (requires GPU VF)
        instance = orchestrator.provision(tenant_id, activated)
        assert str(instance["tenant_id"]) == str(tenant_id)
        mock_k8s.create_namespaced_pod.assert_called()

        # Verify feature flags include video features
        conn = psycopg2.connect(db_url)
        try:
            flags = get_features(str(tenant_id), conn, redis_client)
        finally:
            conn.close()

        assert flags["audio"] is True
        assert flags["video"] is True
        assert flags["activity"] is True
        assert flags["hls"] is True
        assert flags["visualizer"] is True
        # Premium features still disabled
        assert flags["tidal_hifi"] is False
        assert flags["lossless"] is False

    def test_subscription_with_premium_addon(self, db_url: str, redis_client):
        """Test subscription with premium addon: features include Tidal/lossless."""
        tenant_id = _create_tenant(
            db_url,
            discord_user_id=666777888999000111,
            username="premium_subscriber",
        )

        # Create and activate base plan
        mgr = SubscriptionManager(pg_uri=db_url)
        base_sub = mgr.create_subscription(tenant_id, "base")
        mgr.activate(base_sub["id"])

        # Create subscription with premium addon
        premium_sub = mgr.create_subscription(tenant_id, "base", addons=["premium"])
        _insert_payment(db_url, tenant_id, amount_cents=898, txn_id="PAYPAL-PREMIUM-001")
        mgr.activate(premium_sub["id"])

        # Verify feature flags include premium features
        conn = psycopg2.connect(db_url)
        try:
            flags = get_features(str(tenant_id), conn, redis_client)
        finally:
            conn.close()

        assert flags["audio"] is True
        assert flags["tidal_hifi"] is True
        assert flags["lossless"] is True
        assert flags["priority_queue"] is True
        # Video features still disabled
        assert flags["video"] is False


# ---------------------------------------------------------------------------
# Test Class 3: Expiry Flow (End-to-End)
# Req 7.7: Subscription expiry → 3-day grace → bot deactivation
# ---------------------------------------------------------------------------


class TestExpiryFlowE2E:
    """Integration test: Subscription expiry with grace period and bot deactivation.

    Flow:
    1. Create active subscription with provisioned bot
    2. Expire subscription
    3. Verify 3-day grace period set
    4. Verify bot instance status changes appropriately
    """

    def test_expiry_with_grace_period(self, db_url: str, orchestrator, mock_k8s):
        """Test that expiry sets 3-day grace period before deactivation (Req 7.7)."""
        # Create tenant and active subscription
        tenant_id = _create_tenant(
            db_url,
            discord_user_id=777888999000111222,
            username="expiry_user",
        )

        mgr = SubscriptionManager(pg_uri=db_url)
        subscription = mgr.create_subscription(tenant_id, "base")
        mgr.activate(subscription["id"])

        # Provision bot
        instance = orchestrator.provision(tenant_id, {"plan": "base", "addons": []})
        assert instance["status"] == "provisioning"

        # Expire the subscription
        expired = mgr.expire(subscription["id"])
        assert expired["status"] == "expired"

        # Verify grace period: expires_at should be ~3 days from now
        grace_expiry = expired["expires_at"]
        now = datetime.now(timezone.utc)
        time_until_grace = grace_expiry - now
        # Allow tolerance for test execution
        assert timedelta(days=2, hours=23) <= time_until_grace <= timedelta(days=3, seconds=5)

    def test_expiry_then_bot_deprovision(self, db_url: str, orchestrator, mock_k8s):
        """Test that after grace period, bot instance can be deprovisioned (Req 7.7, 10.6)."""
        tenant_id = _create_tenant(
            db_url,
            discord_user_id=888999000111222333,
            username="deprovision_user",
        )

        mgr = SubscriptionManager(pg_uri=db_url)
        subscription = mgr.create_subscription(tenant_id, "base")
        mgr.activate(subscription["id"])

        # Provision bot instance
        instance = orchestrator.provision(tenant_id, {"plan": "base", "addons": []})
        instance_id = instance["id"]

        # Expire the subscription
        mgr.expire(subscription["id"])

        # Simulate grace period elapsed: deprovision bot
        orchestrator.deprovision(instance_id, grace_seconds=0)

        # Verify bot instance is now stopped
        instances = _get_bot_instances(db_url, tenant_id)
        assert len(instances) == 1
        assert instances[0]["status"] == "stopped"
        mock_k8s.delete_namespaced_pod.assert_called_once()

    def test_expiry_feature_flags_reflect_no_subscription(self, db_url: str, redis_client):
        """After expiry, feature flags should reflect no active subscription (Req 7.7, 13.7)."""
        tenant_id = _create_tenant(
            db_url,
            discord_user_id=999000111222333444,
            username="flags_expiry_user",
        )

        # Create, activate, then expire subscription
        mgr = SubscriptionManager(pg_uri=db_url)
        subscription = mgr.create_subscription(tenant_id, "base")
        mgr.activate(subscription["id"])

        # Features should be active before expiry
        conn = psycopg2.connect(db_url)
        try:
            flags_before = get_features(str(tenant_id), conn, redis_client)
        finally:
            conn.close()
        assert flags_before["audio"] is True

        # Invalidate cache before expiry check
        redis_client.delete(f"features:{tenant_id}")

        # Expire
        mgr.expire(subscription["id"])

        # Features should now reflect no active subscription
        conn = psycopg2.connect(db_url)
        try:
            flags_after = get_features(str(tenant_id), conn, redis_client)
        finally:
            conn.close()

        # After expiry, subscription status is 'expired' — feature query returns no plan
        assert flags_after["audio"] is False
        assert flags_after["video"] is False
        assert flags_after["tidal_hifi"] is False

    def test_multiple_tenants_isolated_during_expiry(self, db_url: str, redis_client):
        """Expiring one tenant's subscription does not affect another's features."""
        # Create two tenants
        tenant_a = _create_tenant(
            db_url,
            discord_user_id=100200300400500600,
            username="tenant_a",
        )
        tenant_b = _create_tenant(
            db_url,
            discord_user_id=100200300400500601,
            username="tenant_b",
        )

        mgr = SubscriptionManager(pg_uri=db_url)

        # Both get active base plans
        sub_a = mgr.create_subscription(tenant_a, "base")
        mgr.activate(sub_a["id"])
        sub_b = mgr.create_subscription(tenant_b, "base")
        mgr.activate(sub_b["id"])

        # Expire tenant A's subscription
        mgr.expire(sub_a["id"])

        # Tenant B's features should still be active
        conn = psycopg2.connect(db_url)
        try:
            flags_b = get_features(str(tenant_b), conn, redis_client)
        finally:
            conn.close()

        assert flags_b["audio"] is True

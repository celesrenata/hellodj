"""Unit tests for the SubscriptionManager service.

Tests subscription lifecycle: creation, activation, expiry, cancellation,
addon prerequisite enforcement, and auto-cancel on payment timeout.

Requirements: 7.1, 7.2, 7.3, 7.4, 7.5, 7.6, 7.7, 7.8, 7.9, 7.10
"""

from __future__ import annotations

import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import psycopg2
import psycopg2.extras
import psycopg2.extensions
import pytest

# Register UUID adapter for psycopg2
psycopg2.extensions.register_adapter(uuid.UUID, lambda u: psycopg2.extensions.AsIs(f"'{u}'"))

# Ensure web-ui/ is importable
_webui_dir = str(Path(__file__).resolve().parent.parent / "web-ui")
if _webui_dir not in sys.path:
    sys.path.insert(0, _webui_dir)

from services.subscription_manager import (
    ADDONS,
    GRACE_PERIOD_DAYS,
    PAYMENT_TIMEOUT_HOURS,
    PLANS,
    AddonPrerequisiteError,
    InvalidPlanError,
    InvalidAddonError,
    InvalidStateTransitionError,
    SubscriptionManager,
    SubscriptionNotFoundError,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def pg_container():
    """Start a PostgreSQL testcontainer for the test session."""
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
def psycopg2_url(pg_container) -> str:
    """Return a psycopg2-compatible connection URL."""
    host = pg_container.get_container_host_ip()
    port = pg_container.get_exposed_port(5432)
    return f"postgresql://testuser:testpass@{host}:{port}/hellodj_test"


@pytest.fixture(scope="session")
def _apply_schema(psycopg2_url: str):
    """Apply the SaaS platform schema to the test database."""
    conn = psycopg2.connect(psycopg2_url)
    try:
        with conn.cursor() as cur:
            cur.execute("""
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
            """)
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def db_url(psycopg2_url: str, _apply_schema) -> str:
    """Provide a clean database URL with schema applied and tables truncated."""
    conn = psycopg2.connect(psycopg2_url)
    try:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE TABLE subscriptions, tenants CASCADE;")
        conn.commit()
    finally:
        conn.close()
    return psycopg2_url


@pytest.fixture
def manager(db_url: str) -> SubscriptionManager:
    """Provide a SubscriptionManager connected to the test database."""
    return SubscriptionManager(pg_uri=db_url)


@pytest.fixture
def tenant_id(db_url: str) -> uuid.UUID:
    """Create a test tenant and return its UUID."""
    tid = uuid.uuid4()
    conn = psycopg2.connect(db_url)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO tenants (id, discord_user_id, discord_username) VALUES (%s, %s, %s)",
                (tid, 123456789012345678, "testuser"),
            )
        conn.commit()
    finally:
        conn.close()
    return tid


@pytest.fixture
def tenant_id_2(db_url: str) -> uuid.UUID:
    """Create a second test tenant and return its UUID."""
    tid = uuid.uuid4()
    conn = psycopg2.connect(db_url)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO tenants (id, discord_user_id, discord_username) VALUES (%s, %s, %s)",
                (tid, 987654321098765432, "testuser2"),
            )
        conn.commit()
    finally:
        conn.close()
    return tid


# ---------------------------------------------------------------------------
# Plan and Addon Definition Tests
# ---------------------------------------------------------------------------


class TestPlanDefinitions:
    """Test that plan and addon definitions match requirements."""

    def test_base_plan_price(self):
        """Req 7.1: Base_Plan at $6.99/mo."""
        assert PLANS["base"]["price_cents"] == 699

    def test_base_plan_bot_instances(self):
        """Req 7.1: Base_Plan provides 1 Bot_Instance."""
        assert PLANS["base"]["bot_instances"] == 1

    def test_base_plan_features(self):
        """Req 7.1: Base_Plan provides audio only."""
        assert PLANS["base"]["features"] == ["audio"]

    def test_trial_plan_free(self):
        """Trial plan is free."""
        assert PLANS["trial"]["price_cents"] == 0

    def test_trial_plan_duration(self):
        """Trial plan lasts 30 days."""
        assert PLANS["trial"]["duration_days"] == 30

    def test_video_addon_price(self):
        """Req 7.2: Video_Addon at +$1.99/mo."""
        assert ADDONS["video"]["price_cents"] == 199

    def test_video_addon_features(self):
        """Req 7.2: Video_Addon enables video, activity, hls, visualizer."""
        assert set(ADDONS["video"]["features"]) == {"video", "activity", "hls", "visualizer"}

    def test_premium_addon_price(self):
        """Req 7.3: Premium_Addon at +$1.99/mo."""
        assert ADDONS["premium"]["price_cents"] == 199

    def test_premium_addon_features(self):
        """Req 7.3: Premium_Addon enables tidal_hifi, lossless, priority_queue."""
        assert set(ADDONS["premium"]["features"]) == {"tidal_hifi", "lossless", "priority_queue"}

    def test_additional_bot_addon_price(self):
        """Req 7.4: Additional_Bot_Addon at +$1.99/mo per instance."""
        assert ADDONS["additional_bot"]["price_cents"] == 199

    def test_additional_bot_addon_max(self):
        """Req 7.4: Max 9 additional instances."""
        assert ADDONS["additional_bot"]["max"] == 9

    def test_additional_bot_is_per_instance(self):
        """Req 7.4: Additional_Bot_Addon is priced per instance."""
        assert ADDONS["additional_bot"]["per_instance"] is True


# ---------------------------------------------------------------------------
# Subscription Creation Tests
# ---------------------------------------------------------------------------


class TestCreateSubscription:
    """Test create_subscription method."""

    def test_create_base_plan_pending_payment(self, manager, tenant_id):
        """Req 7.5: Subscription created with status 'pending_payment'."""
        sub = manager.create_subscription(tenant_id, "base")
        assert sub["status"] == "pending_payment"
        assert sub["plan"] == "base"
        assert str(sub["tenant_id"]) == str(tenant_id)

    def test_create_trial_plan(self, manager, tenant_id):
        """Trial subscription has an expiry date set."""
        sub = manager.create_subscription(tenant_id, "trial")
        assert sub["status"] == "pending_payment"
        assert sub["plan"] == "trial"
        assert sub["expires_at"] is not None
        # Expiry should be ~30 days from now
        now = datetime.now(timezone.utc)
        diff = sub["expires_at"] - now
        assert 29 <= diff.days <= 30

    def test_create_with_addons_requires_active_base(self, manager, tenant_id):
        """Req 7.10: Addons rejected without active Base_Plan."""
        with pytest.raises(AddonPrerequisiteError):
            manager.create_subscription(tenant_id, "base", addons=["video"])

    def test_create_invalid_plan_raises(self, manager, tenant_id):
        """Invalid plan name raises InvalidPlanError."""
        with pytest.raises(InvalidPlanError):
            manager.create_subscription(tenant_id, "premium_deluxe")

    def test_create_invalid_addon_raises(self, manager, tenant_id):
        """Invalid addon name raises InvalidAddonError."""
        # First create and activate a base plan
        sub = manager.create_subscription(tenant_id, "base")
        manager.activate(sub["id"])
        with pytest.raises(InvalidAddonError):
            manager.create_subscription(tenant_id, "base", addons=["nonexistent"])

    def test_create_with_valid_addons(self, manager, tenant_id):
        """Addons accepted when tenant has active Base_Plan."""
        # Create and activate base plan first
        base_sub = manager.create_subscription(tenant_id, "base")
        manager.activate(base_sub["id"])

        # Now create subscription with addons
        addon_sub = manager.create_subscription(tenant_id, "base", addons=["video", "premium"])
        assert addon_sub["status"] == "pending_payment"
        assert set(addon_sub["addons"]) == {"video", "premium"}

    def test_create_returns_uuid_id(self, manager, tenant_id):
        """Created subscription has a valid UUID id."""
        sub = manager.create_subscription(tenant_id, "base")
        # psycopg2 may return UUID as string — validate it's parseable
        sub_id = uuid.UUID(str(sub["id"])) if not isinstance(sub["id"], uuid.UUID) else sub["id"]
        assert isinstance(sub_id, uuid.UUID)

    def test_create_sets_started_at(self, manager, tenant_id):
        """Created subscription has started_at set to now."""
        before = datetime.now(timezone.utc)
        sub = manager.create_subscription(tenant_id, "base")
        after = datetime.now(timezone.utc)
        assert before <= sub["started_at"] <= after


# ---------------------------------------------------------------------------
# Activation Tests
# ---------------------------------------------------------------------------


class TestActivateSubscription:
    """Test activate method."""

    def test_activate_sets_status_active(self, manager, tenant_id):
        """Req 7.6: Payment verified → status 'active'."""
        sub = manager.create_subscription(tenant_id, "base")
        activated = manager.activate(sub["id"])
        assert activated["status"] == "active"

    def test_activate_updates_started_at(self, manager, tenant_id):
        """Activation updates started_at to activation time."""
        sub = manager.create_subscription(tenant_id, "base")
        before = datetime.now(timezone.utc)
        activated = manager.activate(sub["id"])
        after = datetime.now(timezone.utc)
        assert before <= activated["started_at"] <= after

    def test_activate_non_pending_raises(self, manager, tenant_id):
        """Cannot activate a subscription that is not pending_payment."""
        sub = manager.create_subscription(tenant_id, "base")
        manager.activate(sub["id"])
        with pytest.raises(InvalidStateTransitionError):
            manager.activate(sub["id"])

    def test_activate_not_found_raises(self, manager):
        """Activating a non-existent subscription raises error."""
        with pytest.raises(SubscriptionNotFoundError):
            manager.activate(uuid.uuid4())


# ---------------------------------------------------------------------------
# Expiry Tests
# ---------------------------------------------------------------------------


class TestExpireSubscription:
    """Test expire method."""

    def test_expire_sets_status_expired(self, manager, tenant_id):
        """Req 7.7: Expired subscription has status 'expired'."""
        sub = manager.create_subscription(tenant_id, "base")
        manager.activate(sub["id"])
        expired = manager.expire(sub["id"])
        assert expired["status"] == "expired"

    def test_expire_sets_grace_period(self, manager, tenant_id):
        """Req 7.7: 3-day grace period for bot deactivation."""
        sub = manager.create_subscription(tenant_id, "base")
        manager.activate(sub["id"])
        before = datetime.now(timezone.utc)
        expired = manager.expire(sub["id"])
        expected_grace = before + timedelta(days=GRACE_PERIOD_DAYS)
        # expires_at should be approximately 3 days from now
        assert expired["expires_at"] is not None
        diff = (expired["expires_at"] - before).total_seconds()
        # Allow 5 seconds of tolerance
        assert abs(diff - (GRACE_PERIOD_DAYS * 86400)) < 5

    def test_expire_non_active_raises(self, manager, tenant_id):
        """Cannot expire a subscription that is not active."""
        sub = manager.create_subscription(tenant_id, "base")
        with pytest.raises(InvalidStateTransitionError):
            manager.expire(sub["id"])

    def test_expire_not_found_raises(self, manager):
        """Expiring a non-existent subscription raises error."""
        with pytest.raises(SubscriptionNotFoundError):
            manager.expire(uuid.uuid4())


# ---------------------------------------------------------------------------
# Cancellation Tests
# ---------------------------------------------------------------------------


class TestCancelSubscription:
    """Test cancel method."""

    def test_cancel_pending_sets_cancelled(self, manager, tenant_id):
        """Cancel a pending_payment subscription."""
        sub = manager.create_subscription(tenant_id, "base")
        cancelled = manager.cancel(sub["id"])
        assert cancelled["status"] == "cancelled"

    def test_cancel_active_sets_cancelled(self, manager, tenant_id):
        """Cancel an active subscription."""
        sub = manager.create_subscription(tenant_id, "base")
        manager.activate(sub["id"])
        cancelled = manager.cancel(sub["id"])
        assert cancelled["status"] == "cancelled"

    def test_cancel_already_cancelled_raises(self, manager, tenant_id):
        """Cannot cancel an already cancelled subscription."""
        sub = manager.create_subscription(tenant_id, "base")
        manager.cancel(sub["id"])
        with pytest.raises(InvalidStateTransitionError):
            manager.cancel(sub["id"])

    def test_cancel_expired_raises(self, manager, tenant_id):
        """Cannot cancel an expired subscription."""
        sub = manager.create_subscription(tenant_id, "base")
        manager.activate(sub["id"])
        manager.expire(sub["id"])
        with pytest.raises(InvalidStateTransitionError):
            manager.cancel(sub["id"])

    def test_cancel_not_found_raises(self, manager):
        """Cancelling a non-existent subscription raises error."""
        with pytest.raises(SubscriptionNotFoundError):
            manager.cancel(uuid.uuid4())


# ---------------------------------------------------------------------------
# Auto-Cancel Tests
# ---------------------------------------------------------------------------


class TestAutoCancel:
    """Test auto_cancel_expired_pending method."""

    def test_auto_cancel_stale_pending(self, manager, tenant_id, db_url):
        """Req 7.8: Auto-cancel after 24h without payment."""
        sub = manager.create_subscription(tenant_id, "base")

        # Backdate created_at to 25 hours ago
        conn = psycopg2.connect(db_url)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE subscriptions SET created_at = %s WHERE id = %s",
                    (datetime.now(timezone.utc) - timedelta(hours=25), sub["id"]),
                )
            conn.commit()
        finally:
            conn.close()

        cancelled = manager.auto_cancel_expired_pending()
        assert len(cancelled) == 1
        assert cancelled[0]["id"] == sub["id"]
        assert cancelled[0]["status"] == "cancelled"

    def test_auto_cancel_fresh_pending_not_cancelled(self, manager, tenant_id):
        """Subscriptions less than 24h old should not be cancelled."""
        manager.create_subscription(tenant_id, "base")
        cancelled = manager.auto_cancel_expired_pending()
        assert len(cancelled) == 0

    def test_auto_cancel_active_not_affected(self, manager, tenant_id, db_url):
        """Active subscriptions are never auto-cancelled."""
        sub = manager.create_subscription(tenant_id, "base")
        manager.activate(sub["id"])

        # Backdate (shouldn't matter since status is active)
        conn = psycopg2.connect(db_url)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE subscriptions SET created_at = %s WHERE id = %s",
                    (datetime.now(timezone.utc) - timedelta(hours=48), sub["id"]),
                )
            conn.commit()
        finally:
            conn.close()

        cancelled = manager.auto_cancel_expired_pending()
        assert len(cancelled) == 0


# ---------------------------------------------------------------------------
# Addon Prerequisite Tests
# ---------------------------------------------------------------------------


class TestAddonPrerequisites:
    """Test addon prerequisite enforcement."""

    def test_addon_without_base_rejected(self, manager, tenant_id):
        """Req 7.10: Reject addon without active Base_Plan."""
        with pytest.raises(AddonPrerequisiteError):
            manager.create_subscription(tenant_id, "base", addons=["video"])

    def test_addon_with_pending_base_rejected(self, manager, tenant_id):
        """Pending Base_Plan does not satisfy the prerequisite."""
        manager.create_subscription(tenant_id, "base")
        # Base is pending_payment, not active
        with pytest.raises(AddonPrerequisiteError):
            manager.create_subscription(tenant_id, "base", addons=["premium"])

    def test_addon_with_active_base_accepted(self, manager, tenant_id):
        """Active Base_Plan satisfies the prerequisite."""
        sub = manager.create_subscription(tenant_id, "base")
        manager.activate(sub["id"])
        addon_sub = manager.create_subscription(tenant_id, "base", addons=["video"])
        assert addon_sub["status"] == "pending_payment"
        assert "video" in addon_sub["addons"]

    def test_no_addons_no_prerequisite_check(self, manager, tenant_id):
        """Creating a subscription without addons does not check prerequisite."""
        sub = manager.create_subscription(tenant_id, "base")
        assert sub["status"] == "pending_payment"


# ---------------------------------------------------------------------------
# Query Tests
# ---------------------------------------------------------------------------


class TestSubscriptionQueries:
    """Test subscription retrieval methods."""

    def test_get_subscription_found(self, manager, tenant_id):
        """Retrieve a subscription by ID."""
        sub = manager.create_subscription(tenant_id, "base")
        found = manager.get_subscription(sub["id"])
        assert found is not None
        assert found["id"] == sub["id"]

    def test_get_subscription_not_found(self, manager):
        """Returns None for non-existent subscription."""
        assert manager.get_subscription(uuid.uuid4()) is None

    def test_get_tenant_subscriptions(self, manager, tenant_id):
        """Retrieve all subscriptions for a tenant."""
        manager.create_subscription(tenant_id, "base")
        manager.create_subscription(tenant_id, "trial")
        subs = manager.get_tenant_subscriptions(tenant_id)
        assert len(subs) == 2

    def test_get_tenant_subscriptions_filtered(self, manager, tenant_id):
        """Retrieve subscriptions filtered by status."""
        sub = manager.create_subscription(tenant_id, "base")
        manager.activate(sub["id"])
        manager.create_subscription(tenant_id, "trial")  # stays pending_payment

        active_subs = manager.get_tenant_subscriptions(tenant_id, status="active")
        assert len(active_subs) == 1
        assert active_subs[0]["status"] == "active"

    def test_get_tenant_subscriptions_empty(self, manager, tenant_id):
        """Empty list when tenant has no subscriptions."""
        subs = manager.get_tenant_subscriptions(tenant_id)
        assert subs == []


# ---------------------------------------------------------------------------
# Price Computation Tests
# ---------------------------------------------------------------------------


class TestPriceComputation:
    """Test compute_total_price_cents method."""

    def test_base_plan_only(self, manager):
        """Base plan is $6.99."""
        assert manager.compute_total_price_cents("base", []) == 699

    def test_base_plus_video(self, manager):
        """Base + Video = $8.98."""
        assert manager.compute_total_price_cents("base", ["video"]) == 898

    def test_base_plus_all_addons(self, manager):
        """Base + Video + Premium + Additional Bot = $12.96."""
        price = manager.compute_total_price_cents("base", ["video", "premium", "additional_bot"])
        assert price == 699 + 199 + 199 + 199

    def test_trial_plan_free(self, manager):
        """Trial plan with no addons is $0."""
        assert manager.compute_total_price_cents("trial", []) == 0

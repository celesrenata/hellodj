"""Property-based test: Subscription Timeout Lifecycle.

**Validates: Requirements 7.7, 7.8**

Property 5:
1. For any subscription created >24h ago with status 'pending_payment',
   auto_cancel_expired_pending() sets it to 'cancelled'.
2. For any active subscription that is expired, the expires_at is set to
   now() + 3 days (grace period).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import psycopg2
import psycopg2.extras
import pytest
from hypothesis import given, settings, HealthCheck
from hypothesis import strategies as st

from tests.strategies import discord_user_ids, plans

import sys
from pathlib import Path

# Ensure web-ui/services is importable
_services_dir = str(Path(__file__).resolve().parent.parent / "web-ui" / "services")
if _services_dir not in sys.path:
    sys.path.insert(0, _services_dir)

_webui_dir = str(Path(__file__).resolve().parent.parent / "web-ui")
if _webui_dir not in sys.path:
    sys.path.insert(0, _webui_dir)


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Hours beyond the 24h threshold (25h to 720h = 30 days)
hours_past_deadline = st.integers(min_value=25, max_value=720)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _create_tenant(pg_uri: str, discord_user_id: int) -> uuid.UUID:
    """Insert a tenant record and return its UUID."""
    tenant_id = uuid.uuid4()
    conn = psycopg2.connect(pg_uri)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO tenants (id, discord_user_id, discord_username, created_at, updated_at)
                VALUES (%s, %s, %s, now(), now())
                ON CONFLICT (discord_user_id) DO UPDATE SET discord_username = EXCLUDED.discord_username
                RETURNING id
                """,
                (tenant_id, discord_user_id, f"user_{discord_user_id}"),
            )
            tenant_id = cur.fetchone()[0]
            conn.commit()
    finally:
        conn.close()
    return tenant_id


def _insert_pending_subscription(
    pg_uri: str, tenant_id: uuid.UUID, plan: str, created_at: datetime
) -> uuid.UUID:
    """Insert a subscription with status pending_payment at a specific created_at."""
    sub_id = uuid.uuid4()
    conn = psycopg2.connect(pg_uri)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO subscriptions (id, tenant_id, plan, addons, status, started_at, created_at)
                VALUES (%s, %s, %s, '{}', 'pending_payment', %s, %s)
                """,
                (sub_id, tenant_id, plan, created_at, created_at),
            )
            conn.commit()
    finally:
        conn.close()
    return sub_id


def _get_subscription(pg_uri: str, sub_id: uuid.UUID) -> dict | None:
    """Fetch a subscription by ID."""
    conn = psycopg2.connect(pg_uri)
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM subscriptions WHERE id = %s", (sub_id,))
            row = cur.fetchone()
            return dict(row) if row else None
    finally:
        conn.close()


def _cleanup_subscriptions(pg_uri: str, tenant_id: uuid.UUID):
    """Remove all subscriptions for a tenant (test cleanup)."""
    conn = psycopg2.connect(pg_uri)
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM subscriptions WHERE tenant_id = %s", (tenant_id,))
            conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Property Test 1: Unverified payment > 24h → cancelled
# ---------------------------------------------------------------------------


@settings(
    max_examples=100,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    deadline=None,
)
@given(
    discord_id=discord_user_ids,
    plan=plans,
    hours_ago=hours_past_deadline,
)
def test_auto_cancel_pending_after_24h(
    pg_connection_url: str,
    _apply_schema,
    discord_id: int,
    plan: str,
    hours_ago: int,
):
    """Property 5a: Unverified payment within 24h → cancelled.

    For any subscription created more than 24 hours ago with status
    'pending_payment', auto_cancel_expired_pending() sets it to 'cancelled'.

    **Validates: Requirements 7.8**
    """
    from subscription_manager import SubscriptionManager

    manager = SubscriptionManager(pg_uri=pg_connection_url)

    # Create a tenant
    tenant_id = _create_tenant(pg_connection_url, discord_id)

    try:
        # Insert a pending subscription created hours_ago hours in the past
        created_at = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
        sub_id = _insert_pending_subscription(
            pg_connection_url, tenant_id, plan, created_at
        )

        # Run auto-cancel
        cancelled = manager.auto_cancel_expired_pending()

        # The subscription must be in the cancelled list
        # psycopg2 may return UUIDs as strings or uuid objects depending on adapter
        cancelled_ids = [str(c["id"]) for c in cancelled]
        assert str(sub_id) in cancelled_ids, (
            f"Subscription {sub_id} created {hours_ago}h ago was not auto-cancelled. "
            f"Cancelled IDs: {cancelled_ids}"
        )

        # Verify the DB state
        sub = _get_subscription(pg_connection_url, sub_id)
        assert sub is not None
        assert sub["status"] == "cancelled", (
            f"Expected status 'cancelled' but got '{sub['status']}'"
        )
    finally:
        _cleanup_subscriptions(pg_connection_url, tenant_id)


# ---------------------------------------------------------------------------
# Property Test 2: Expired subscription → expires_at set to now + 3 days
# ---------------------------------------------------------------------------


@settings(
    max_examples=100,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    deadline=None,
)
@given(
    discord_id=discord_user_ids,
    plan=plans,
)
def test_expire_sets_grace_period(
    pg_connection_url: str,
    _apply_schema,
    discord_id: int,
    plan: str,
):
    """Property 5b: Expired subscription transitions with 3-day grace period.

    For any active subscription that is expired via expire(), the expires_at
    is set to approximately now() + 3 days (grace period).

    **Validates: Requirements 7.7**
    """
    from subscription_manager import SubscriptionManager

    manager = SubscriptionManager(pg_uri=pg_connection_url)

    # Create a tenant
    tenant_id = _create_tenant(pg_connection_url, discord_id)

    try:
        # Create and activate a subscription
        sub = manager.create_subscription(tenant_id, plan)
        sub_id = sub["id"]

        # Activate it (pending_payment → active)
        manager.activate(sub_id)

        # Now expire it
        before_expire = datetime.now(timezone.utc)
        expired_sub = manager.expire(sub_id)
        after_expire = datetime.now(timezone.utc)

        # Verify status is expired
        assert expired_sub["status"] == "expired", (
            f"Expected status 'expired' but got '{expired_sub['status']}'"
        )

        # Verify expires_at is approximately now + 3 days
        expected_min = before_expire + timedelta(days=3)
        expected_max = after_expire + timedelta(days=3)

        expires_at = expired_sub["expires_at"]
        # Handle timezone-naive datetimes from psycopg2
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)

        assert expected_min <= expires_at <= expected_max, (
            f"expires_at {expires_at} is not within expected grace period range "
            f"[{expected_min}, {expected_max}]"
        )

        # Also verify from DB directly
        db_sub = _get_subscription(pg_connection_url, sub_id)
        assert db_sub is not None
        assert db_sub["status"] == "expired"
    finally:
        _cleanup_subscriptions(pg_connection_url, tenant_id)

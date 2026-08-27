"""Property-based test: Trial Lifecycle State Machine.

**Validates: Requirements 6.3, 6.4, 6.5**

Property 4: For any tenant, approving a trial application SHALL result in an
active trial with an expiry date exactly 30 days from approval (within 2s
tolerance), AND if the tenant already has an active trial or subscription,
applying for a new trial SHALL be rejected without modifying the existing
subscription state.
"""

from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import psycopg2
import psycopg2.extras
import pytest
from hypothesis import given, settings, HealthCheck

from tests.strategies import discord_user_ids

# Ensure web-ui/services is importable
_services_dir = str(Path(__file__).resolve().parent.parent / "web-ui" / "services")
if _services_dir not in sys.path:
    sys.path.insert(0, _services_dir)
_webui_dir = str(Path(__file__).resolve().parent.parent / "web-ui")
if _webui_dir not in sys.path:
    sys.path.insert(0, _webui_dir)


def _psycopg2_url(asyncpg_url: str) -> str:
    """Convert asyncpg-style URL to psycopg2-compatible URL."""
    # Both use postgresql:// scheme, so the URL is compatible as-is
    return asyncpg_url


def _create_tenant(conn, discord_user_id: int) -> str:
    """Create a tenant record and return its UUID."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            INSERT INTO tenants (discord_user_id, discord_username)
            VALUES (%s, %s)
            RETURNING id
            """,
            (discord_user_id, f"testuser_{discord_user_id}"),
        )
        tenant_id = str(cur.fetchone()["id"])
        conn.commit()
    return tenant_id


def _truncate_tables(conn):
    """Truncate trial-related tables for test isolation."""
    with conn.cursor() as cur:
        cur.execute(
            """
            TRUNCATE TABLE trial_applications, subscriptions, tenants CASCADE;
            """
        )
        conn.commit()


@settings(
    max_examples=100,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    deadline=None,
)
@given(discord_id=discord_user_ids)
def test_trial_approval_sets_30_day_expiry(
    pg_connection_url: str, _apply_schema, discord_id: int
):
    """Property 4a: Approving a trial sets expiry to exactly 30 days from approval.

    For any tenant_id, when a trial is approved, the subscription
    expires_at = now() + 30 days (within 2s tolerance).

    **Validates: Requirements 6.3, 6.4**
    """
    pg_url = _psycopg2_url(pg_connection_url)
    conn = psycopg2.connect(pg_url)

    try:
        _truncate_tables(conn)
        tenant_id = _create_tenant(conn, discord_id)

        # Patch the trial_manager's connection function to use our test DB
        with patch("trial_manager._get_pg_conn", return_value=psycopg2.connect(pg_url)):
            import trial_manager

            # Apply for a trial
            application = trial_manager.apply(tenant_id)
            assert application["status"] == "pending"
            application_id = str(application["id"])

        # Approve the trial (need a fresh connection for the patched function)
        with patch("trial_manager._get_pg_conn", return_value=psycopg2.connect(pg_url)):
            result = trial_manager.approve(application_id, "test_operator")

        subscription = result["subscription"]

        # Verify subscription is active trial
        assert subscription["plan"] == "trial"
        assert subscription["status"] == "active"

        # Verify expires_at is exactly 30 days from started_at (within 2s tolerance)
        started_at = subscription["started_at"]
        expires_at = subscription["expires_at"]
        expected_expiry = started_at + timedelta(days=30)

        delta = abs((expires_at - expected_expiry).total_seconds())
        assert delta <= 2.0, (
            f"Trial expiry not within 2s of 30 days from start. "
            f"started_at={started_at}, expires_at={expires_at}, "
            f"expected={expected_expiry}, delta={delta}s"
        )
    finally:
        conn.close()


@settings(
    max_examples=100,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    deadline=None,
)
@given(discord_id=discord_user_ids)
def test_apply_rejected_with_active_trial(
    pg_connection_url: str, _apply_schema, discord_id: int
):
    """Property 4b: Applying with existing active trial/subscription is rejected.

    For any tenant_id with an active subscription/trial, calling apply()
    raises TrialError without modifying the existing subscription state.

    **Validates: Requirements 6.5**
    """
    pg_url = _psycopg2_url(pg_connection_url)
    conn = psycopg2.connect(pg_url)

    try:
        _truncate_tables(conn)
        tenant_id = _create_tenant(conn, discord_id)

        import trial_manager
        from trial_manager import TrialError

        # Set up an active trial: apply → approve
        with patch("trial_manager._get_pg_conn", return_value=psycopg2.connect(pg_url)):
            application = trial_manager.apply(tenant_id)
            application_id = str(application["id"])

        with patch("trial_manager._get_pg_conn", return_value=psycopg2.connect(pg_url)):
            result = trial_manager.approve(application_id, "test_operator")

        # Record the subscription state before the rejected apply attempt
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT * FROM subscriptions WHERE tenant_id = %s AND status = 'active'",
                (tenant_id,),
            )
            subscription_before = cur.fetchone()

        # Attempting to apply again should raise TrialError
        with patch("trial_manager._get_pg_conn", return_value=psycopg2.connect(pg_url)):
            with pytest.raises(TrialError):
                trial_manager.apply(tenant_id)

        # Verify subscription state is unchanged
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT * FROM subscriptions WHERE tenant_id = %s AND status = 'active'",
                (tenant_id,),
            )
            subscription_after = cur.fetchone()

        assert subscription_before is not None
        assert subscription_after is not None
        assert subscription_before["id"] == subscription_after["id"]
        assert subscription_before["plan"] == subscription_after["plan"]
        assert subscription_before["status"] == subscription_after["status"]
        assert subscription_before["expires_at"] == subscription_after["expires_at"]
    finally:
        conn.close()

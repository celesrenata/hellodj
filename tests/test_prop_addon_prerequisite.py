"""Property-based test: Addon Prerequisite Enforcement.

**Validates: Requirements 7.9, 7.10**

Property 6: For any tenant without an active Base_Plan subscription, attempting
to add any addon (Video, Premium, Additional Bot) SHALL be rejected with an error,
AND the subscription state SHALL remain unchanged (no partial writes).
"""

from __future__ import annotations

import asyncio
import uuid

import asyncpg
import pytest
from hypothesis import given, settings, HealthCheck

from tests.strategies import addon_sets, tenant_ids

import sys
from pathlib import Path

# Ensure web-ui/services is importable
_services_dir = str(Path(__file__).resolve().parent.parent / "web-ui" / "services")
if _services_dir not in sys.path:
    sys.path.insert(0, _services_dir)

from subscription_manager import SubscriptionManager, AddonPrerequisiteError


def _create_tenant_sync(pg_uri: str, tenant_id: uuid.UUID, discord_user_id: int) -> None:
    """Insert a tenant row into the database synchronously."""

    async def _insert():
        conn = await asyncpg.connect(pg_uri)
        try:
            await conn.execute(
                """
                INSERT INTO tenants (id, discord_user_id, discord_username)
                VALUES ($1, $2, $3)
                ON CONFLICT (id) DO NOTHING
                """,
                tenant_id,
                discord_user_id,
                f"test_user_{discord_user_id}",
            )
        finally:
            await conn.close()

    asyncio.run(_insert())


def _get_tenant_subscriptions_sync(pg_uri: str, tenant_id: uuid.UUID) -> list[dict]:
    """Fetch all subscriptions for a tenant synchronously."""

    async def _fetch():
        conn = await asyncpg.connect(pg_uri)
        try:
            rows = await conn.fetch(
                "SELECT * FROM subscriptions WHERE tenant_id = $1 ORDER BY created_at",
                tenant_id,
            )
            return [dict(row) for row in rows]
        finally:
            await conn.close()

    return asyncio.run(_fetch())


def _cleanup_tenant_sync(pg_uri: str, tenant_id: uuid.UUID) -> None:
    """Remove a tenant and all related data to avoid cross-example interference."""

    async def _delete():
        conn = await asyncpg.connect(pg_uri)
        try:
            await conn.execute(
                "DELETE FROM subscriptions WHERE tenant_id = $1", tenant_id
            )
            await conn.execute("DELETE FROM tenants WHERE id = $1", tenant_id)
        finally:
            await conn.close()

    asyncio.run(_delete())


@settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(tenant_id=tenant_ids, addons=addon_sets.filter(lambda a: len(a) > 0))
def test_addon_without_base_plan_is_rejected(
    pg_connection_url: str, _apply_schema, tenant_id: uuid.UUID, addons: list[str]
):
    """Property 6.1: Addon subscription without active Base_Plan is rejected.

    For any tenant_id and any non-empty addon set, if the tenant has no active
    Base_Plan, create_subscription with addons raises AddonPrerequisiteError.

    **Validates: Requirements 7.9, 7.10**
    """
    # Use a unique discord_user_id derived from tenant_id to avoid collisions
    discord_user_id = int(str(tenant_id.int)[:18])

    _create_tenant_sync(pg_connection_url, tenant_id, discord_user_id)
    try:
        manager = SubscriptionManager(pg_uri=pg_connection_url)

        # Attempt to create a subscription with addons (no active base plan exists)
        with pytest.raises(AddonPrerequisiteError):
            manager.create_subscription(
                tenant_id=tenant_id,
                plan="base",
                addons=addons,
            )
    finally:
        _cleanup_tenant_sync(pg_connection_url, tenant_id)


@settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(tenant_id=tenant_ids, addons=addon_sets.filter(lambda a: len(a) > 0))
def test_subscription_state_unchanged_after_rejection(
    pg_connection_url: str, _apply_schema, tenant_id: uuid.UUID, addons: list[str]
):
    """Property 6.2: Subscription state remains unchanged after rejection.

    After a rejected addon subscription attempt (AddonPrerequisiteError), the
    tenant's subscriptions list is unchanged — no partial writes occur.

    **Validates: Requirements 7.9, 7.10**
    """
    discord_user_id = int(str(tenant_id.int)[:18])

    _create_tenant_sync(pg_connection_url, tenant_id, discord_user_id)
    try:
        manager = SubscriptionManager(pg_uri=pg_connection_url)

        # Capture subscription state before the rejected attempt
        subs_before = _get_tenant_subscriptions_sync(pg_connection_url, tenant_id)

        # Attempt to create a subscription with addons (should be rejected)
        with pytest.raises(AddonPrerequisiteError):
            manager.create_subscription(
                tenant_id=tenant_id,
                plan="base",
                addons=addons,
            )

        # Verify subscription state is unchanged
        subs_after = _get_tenant_subscriptions_sync(pg_connection_url, tenant_id)
        assert subs_before == subs_after, (
            f"Subscription state changed after rejected addon request! "
            f"Before: {subs_before}, After: {subs_after}"
        )
    finally:
        _cleanup_tenant_sync(pg_connection_url, tenant_id)

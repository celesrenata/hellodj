"""Property-based test: Tenant Data Isolation.

**Validates: Requirements 2.3, 2.4, 10.2, 10.7**

Property 3: Writing data scoped to tenant A and reading as tenant B returns
an empty result set, regardless of overlapping guild_id/channel_id. This
verifies that the composite primary key (tenant_id, guild_id, channel_id) and
query-level tenant_id filtering prevent cross-tenant data leakage.

Uses testcontainers PostgreSQL for real DB isolation testing.
"""

from __future__ import annotations

import asyncio
import json
from uuid import UUID

import asyncpg
import pytest
from hypothesis import given, settings, HealthCheck, assume
from hypothesis import strategies as st

from tests.strategies import tenant_ids


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Guild IDs — Discord snowflakes (valid 18-digit range)
guild_ids = st.integers(min_value=100000000000000000, max_value=999999999999999999)

# Channel IDs — same snowflake range
channel_ids = st.integers(min_value=100000000000000000, max_value=999999999999999999)

# Text without null bytes (PostgreSQL text/jsonb rejects \x00)
_pg_safe_text = st.text(
    alphabet=st.characters(blacklist_characters="\x00"),
    min_size=1,
    max_size=50,
)

# Session data — arbitrary JSON-serializable dicts representing playback state
session_data = st.fixed_dictionaries(
    {
        "voice_channel_id": channel_ids,
        "current_track": _pg_safe_text,
        "queue": st.lists(
            st.text(
                alphabet=st.characters(blacklist_characters="\x00"),
                min_size=1,
                max_size=30,
            ),
            max_size=5,
        ),
        "volume": st.integers(min_value=0, max_value=100),
    }
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _insert_session(
    pool: asyncpg.Pool,
    tenant_id: UUID,
    guild_id: int,
    channel_id: int,
    data: dict,
) -> None:
    """Insert a session record scoped to tenant_id."""
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO sessions (tenant_id, guild_id, channel_id, session_data, updated_at)
            VALUES ($1, $2, $3, $4::jsonb, now())
            ON CONFLICT (tenant_id, guild_id, channel_id)
            DO UPDATE SET session_data = $4::jsonb, updated_at = now()
            """,
            tenant_id,
            guild_id,
            channel_id,
            json.dumps(data),
        )


async def _read_sessions(
    pool: asyncpg.Pool,
    tenant_id: UUID,
    guild_id: int,
    channel_id: int,
) -> list[asyncpg.Record]:
    """Read sessions filtered by tenant_id, guild_id, and channel_id.

    This simulates how a Bot_Instance reads sessions — always filtering
    by its own tenant_id at the query level (Requirement 2.4).
    """
    async with pool.acquire() as conn:
        return await conn.fetch(
            """
            SELECT tenant_id, guild_id, channel_id, session_data
            FROM sessions
            WHERE tenant_id = $1 AND guild_id = $2 AND channel_id = $3
            """,
            tenant_id,
            guild_id,
            channel_id,
        )


async def _read_all_sessions_for_tenant(
    pool: asyncpg.Pool,
    tenant_id: UUID,
) -> list[asyncpg.Record]:
    """Read ALL sessions for a tenant — broader isolation check."""
    async with pool.acquire() as conn:
        return await conn.fetch(
            """
            SELECT tenant_id, guild_id, channel_id, session_data
            FROM sessions
            WHERE tenant_id = $1
            """,
            tenant_id,
        )


# ---------------------------------------------------------------------------
# Property test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    tenant_a=tenant_ids,
    tenant_b=tenant_ids,
    guild_id=guild_ids,
    channel_id=channel_ids,
    data=session_data,
)
async def test_tenant_data_isolation(
    pg_pool: asyncpg.Pool,
    tenant_a: UUID,
    tenant_b: UUID,
    guild_id: int,
    channel_id: int,
    data: dict,
):
    """Property 3: Tenant Data Isolation.

    For two distinct tenant_ids (A and B) sharing the same guild_id and
    channel_id:
    1. Writing session data scoped to tenant A succeeds.
    2. Reading as tenant B (same guild_id/channel_id) returns EMPTY result set.
    3. Reading as tenant A returns the written data.
    4. Tenant B's full session list contains no data from tenant A.

    This proves that tenant_id-based query filtering prevents any cross-tenant
    data leakage regardless of overlapping guild_id/channel_id values.

    **Validates: Requirements 2.3, 2.4, 10.2, 10.7**
    """
    # Ensure the two tenants are distinct
    assume(tenant_a != tenant_b)

    # Clean sessions table for this example to avoid cross-example interference
    async with pg_pool.acquire() as conn:
        await conn.execute("DELETE FROM sessions")

    # Step 1: Write session data scoped to tenant A
    await _insert_session(pg_pool, tenant_a, guild_id, channel_id, data)

    # Step 2: Read as tenant B — MUST return empty (no cross-tenant leakage)
    tenant_b_results = await _read_sessions(pg_pool, tenant_b, guild_id, channel_id)
    assert tenant_b_results == [], (
        f"Cross-tenant data leakage detected! "
        f"Tenant B ({tenant_b}) saw data belonging to Tenant A ({tenant_a}) "
        f"for guild_id={guild_id}, channel_id={channel_id}. "
        f"Got: {tenant_b_results}"
    )

    # Step 3: Read as tenant A — should return the written data
    tenant_a_results = await _read_sessions(pg_pool, tenant_a, guild_id, channel_id)
    assert len(tenant_a_results) == 1, (
        f"Tenant A ({tenant_a}) should see exactly 1 session record, "
        f"got {len(tenant_a_results)}"
    )
    stored_data = json.loads(tenant_a_results[0]["session_data"])
    assert stored_data == data, (
        f"Session data mismatch for Tenant A: expected {data}, got {stored_data}"
    )

    # Step 4: Tenant B's full session list must be empty
    tenant_b_all = await _read_all_sessions_for_tenant(pg_pool, tenant_b)
    assert tenant_b_all == [], (
        f"Tenant B ({tenant_b}) should have no sessions at all, "
        f"but found {len(tenant_b_all)} records"
    )

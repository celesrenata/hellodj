"""Property-based test: Credential Store API Behavioral Equivalence.

**Validates: Requirements 1.6**

Property 2: For any sequence of operations (set, get, delete, exists, keys,
get_prefix) with arbitrary valid inputs, the PostgreSQL-backed CredentialStore
SHALL produce identical return values to the SQLite-backed CredentialStore when
initialized with the same HELLODJ_DB_KEY and given the same operation sequence.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from hypothesis import given, settings, HealthCheck
from hypothesis import strategies as st

from tests.strategies import credential_keys, credential_values


# ---------------------------------------------------------------------------
# Operation strategies
# ---------------------------------------------------------------------------

# Use a bounded key pool so operations interact with previously-set keys
_key_pool = st.shared(
    st.lists(credential_keys, min_size=3, max_size=6, unique=True),
    key="cred_key_pool",
)


def _keys_from_pool():
    """Draw a key from the shared pool."""
    return _key_pool.flatmap(lambda pool: st.sampled_from(pool))


def _prefix_from_pool():
    """Generate a prefix that's the first 1-3 chars of a key from the pool."""
    return _keys_from_pool().map(lambda k: k[:max(1, len(k) // 2)])


# Individual operation strategies
_op_set = st.tuples(st.just("set"), _keys_from_pool(), credential_values)
_op_get = st.tuples(st.just("get"), _keys_from_pool())
_op_delete = st.tuples(st.just("delete"), _keys_from_pool())
_op_exists = st.tuples(st.just("exists"), _keys_from_pool())
_op_keys = st.tuples(st.just("keys"), _prefix_from_pool())
_op_get_prefix = st.tuples(st.just("get_prefix"), _prefix_from_pool())

# A sequence of operations — start with some sets to populate, then mix all ops
_operations = st.lists(
    st.one_of(_op_set, _op_get, _op_delete, _op_exists, _op_keys, _op_get_prefix),
    min_size=3,
    max_size=20,
)


# ---------------------------------------------------------------------------
# Property test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@settings(
    max_examples=50,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(operations=_operations)
async def test_credential_store_api_behavioral_equivalence(
    pg_pool, pg_connection_url: str, tmp_path: Path, operations: list
):
    """Property 2: Credential Store API Behavioral Equivalence.

    For any sequence of operations with arbitrary valid inputs, the
    PostgreSQL-backed store produces identical results to the SQLite-backed
    store when initialized with the same HELLODJ_DB_KEY.

    **Validates: Requirements 1.6**
    """
    from credential_store_pg import CredentialStore as PgStore
    from credentials import CredentialStore as SqliteStore

    db_key = "test-equivalence-key-for-hypothesis"

    # Set env var for SQLite store (it reads HELLODJ_DB_KEY from environment)
    old_env = os.environ.get("HELLODJ_DB_KEY")
    os.environ["HELLODJ_DB_KEY"] = db_key

    # Clean PG credentials table before this example for isolation
    async with pg_pool.acquire() as conn:
        await conn.execute("DELETE FROM credentials")

    # Create a fresh SQLite store per example (unique path avoids cross-example bleed)
    sqlite_db = tmp_path / "test_equiv.db"
    if sqlite_db.exists():
        sqlite_db.unlink()
    sqlite_store = SqliteStore(db_path=sqlite_db)

    pg_store = PgStore(pg_uri=pg_connection_url, db_key=db_key)

    try:
        # Apply each operation to both stores using async API for PG
        # This avoids the background-thread event loop mismatch
        for op in operations:
            op_name = op[0]

            if op_name == "set":
                _, key, value = op
                sqlite_store.set(key, value)
                await pg_store.aset(key, value)

            elif op_name == "get":
                _, key = op
                sqlite_result = sqlite_store.get(key)
                pg_result = await pg_store.aget(key)
                assert sqlite_result == pg_result, (
                    f"get({key!r}) diverged: "
                    f"SQLite={sqlite_result!r}, PG={pg_result!r}"
                )

            elif op_name == "delete":
                _, key = op
                sqlite_store.delete(key)
                await pg_store.adelete(key)

            elif op_name == "exists":
                _, key = op
                sqlite_result = sqlite_store.exists(key)
                pg_result = await pg_store.aexists(key)
                assert sqlite_result == pg_result, (
                    f"exists({key!r}) diverged: "
                    f"SQLite={sqlite_result!r}, PG={pg_result!r}"
                )

            elif op_name == "keys":
                _, prefix = op
                sqlite_result = sorted(sqlite_store.keys(prefix))
                pg_result = sorted(await pg_store.akeys(prefix))
                assert sqlite_result == pg_result, (
                    f"keys({prefix!r}) diverged: "
                    f"SQLite={sqlite_result!r}, PG={pg_result!r}"
                )

            elif op_name == "get_prefix":
                _, prefix = op
                sqlite_result = sqlite_store.get_prefix(prefix)
                pg_result = await pg_store.aget_prefix(prefix)
                assert sqlite_result == pg_result, (
                    f"get_prefix({prefix!r}) diverged: "
                    f"SQLite={sqlite_result!r}, PG={pg_result!r}"
                )

    finally:
        # Restore env var
        if old_env is None:
            os.environ.pop("HELLODJ_DB_KEY", None)
        else:
            os.environ["HELLODJ_DB_KEY"] = old_env

        # Cleanup PG credentials table for test isolation
        async with pg_pool.acquire() as conn:
            await conn.execute("DELETE FROM credentials")

        await pg_store.close()

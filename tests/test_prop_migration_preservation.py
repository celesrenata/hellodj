"""Property-based test: Migration Data Preservation.

**Validates: Requirements 14.1, 14.4**

Property 10: For any set of credential records in SQLite, migration produces
identical key and value (byte-for-byte) in PG; existing keys not modified.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import tempfile
from pathlib import Path

import asyncpg
import pytest
from hypothesis import given, settings, HealthCheck
from hypothesis import strategies as st

from tests.strategies import credential_keys


# Strategy for raw encrypted bytes (simulates Fernet output — arbitrary bytes)
credential_raw_values = st.binary(min_size=1, max_size=500)

# Strategy for a list of unique credential records (key, raw_bytes)
credential_records = st.lists(
    st.tuples(credential_keys, credential_raw_values),
    min_size=1,
    max_size=10,
    unique_by=lambda t: t[0],
)

# Strategy for pre-existing PG records that should NOT be modified
preexisting_records = st.lists(
    st.tuples(credential_keys, credential_raw_values),
    min_size=1,
    max_size=5,
    unique_by=lambda t: t[0],
)


def _create_sqlite_db(db_path: Path, rows: list[tuple[str, bytes]]) -> None:
    """Create a SQLite database with a credentials table populated with rows."""
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE credentials (
            key TEXT PRIMARY KEY,
            value BLOB NOT NULL,
            updated_at TEXT DEFAULT (datetime('now'))
        )
        """
    )
    for key, value in rows:
        conn.execute(
            "INSERT INTO credentials (key, value) VALUES (?, ?)",
            (key, value),
        )
    conn.commit()
    conn.close()


async def _run_migration(pg_uri: str, sqlite_path: Path) -> tuple[int, int]:
    """Run the migration logic from migrate_credentials.py against the given PG."""
    # Import the migration functions directly
    import sys
    scripts_dir = str(Path(__file__).resolve().parent.parent / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)

    from migrate_credentials import read_sqlite_credentials, migrate_to_pg

    rows = read_sqlite_credentials(sqlite_path)
    return await migrate_to_pg(pg_uri, rows)


async def _fetch_all_credentials(pg_uri: str) -> dict[str, bytes]:
    """Fetch all credential rows from PG as {key: raw_value_bytes}."""
    conn = await asyncpg.connect(pg_uri)
    try:
        rows = await conn.fetch("SELECT key, value FROM credentials")
        return {row["key"]: bytes(row["value"]) for row in rows}
    finally:
        await conn.close()


async def _insert_preexisting(pg_uri: str, records: list[tuple[str, bytes]]) -> None:
    """Insert pre-existing records into PG credentials table."""
    conn = await asyncpg.connect(pg_uri)
    try:
        for key, value in records:
            await conn.execute(
                "INSERT INTO credentials (key, value) VALUES ($1, $2) "
                "ON CONFLICT (key) DO NOTHING",
                key, value,
            )
    finally:
        await conn.close()


async def _truncate_credentials(pg_uri: str) -> None:
    """Truncate the credentials table for test isolation."""
    conn = await asyncpg.connect(pg_uri)
    try:
        await conn.execute("TRUNCATE TABLE credentials")
    finally:
        await conn.close()


@settings(max_examples=50, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(records=credential_records)
def test_migration_preserves_data_byte_for_byte(
    pg_connection_url: str, _apply_schema, records: list[tuple[str, bytes]]
):
    """Property 10a: SQLite credential records migrated to PG are byte-for-byte identical.

    For any set of credential records in SQLite, after migration:
    - Every key exists in PG
    - Every value in PG is byte-for-byte identical to the SQLite source

    **Validates: Requirements 14.1, 14.4**
    """

    async def _run():
        # Clean slate
        await _truncate_credentials(pg_connection_url)

        # Create temporary SQLite DB with test records
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "hellodj.db"
            _create_sqlite_db(db_path, records)

            # Run migration
            migrated, skipped = await _run_migration(pg_connection_url, db_path)

            # All records should be migrated (no pre-existing conflicts)
            assert migrated == len(records), (
                f"Expected {len(records)} migrated, got {migrated} "
                f"(skipped={skipped})"
            )

            # Verify byte-for-byte preservation
            pg_data = await _fetch_all_credentials(pg_connection_url)

            for key, expected_value in records:
                assert key in pg_data, (
                    f"Key {key!r} not found in PG after migration"
                )
                actual_value = pg_data[key]
                assert actual_value == expected_value, (
                    f"Value mismatch for key {key!r}: "
                    f"expected {expected_value!r}, got {actual_value!r}"
                )

    asyncio.run(_run())


@settings(max_examples=50, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    preexisting=preexisting_records,
    new_records=credential_records,
)
def test_migration_does_not_modify_existing_keys(
    pg_connection_url: str,
    _apply_schema,
    preexisting: list[tuple[str, bytes]],
    new_records: list[tuple[str, bytes]],
):
    """Property 10b: Existing keys in PG are NOT modified by migration.

    For any set of pre-existing keys already in PG, running migration with
    overlapping keys does not change the pre-existing values.

    **Validates: Requirements 14.1, 14.4**
    """

    async def _run():
        # Clean slate
        await _truncate_credentials(pg_connection_url)

        # Insert pre-existing records into PG
        await _insert_preexisting(pg_connection_url, preexisting)

        # Create SQLite DB with new records that may overlap with pre-existing
        # Force some overlap by including pre-existing keys with DIFFERENT values
        overlapping = [
            (key, os.urandom(32))  # Different value than what's in PG
            for key, _ in preexisting
        ]
        # Combine: overlapping keys + genuinely new keys (dedup by key)
        seen_keys = {key for key, _ in overlapping}
        all_sqlite_records = overlapping + [
            (k, v) for k, v in new_records if k not in seen_keys
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "hellodj.db"
            _create_sqlite_db(db_path, all_sqlite_records)

            # Run migration
            await _run_migration(pg_connection_url, db_path)

            # Verify pre-existing records are UNCHANGED
            pg_data = await _fetch_all_credentials(pg_connection_url)

            for key, original_value in preexisting:
                assert key in pg_data, (
                    f"Pre-existing key {key!r} disappeared from PG after migration"
                )
                assert pg_data[key] == original_value, (
                    f"Pre-existing key {key!r} was modified by migration! "
                    f"Expected {original_value!r}, got {pg_data[key]!r}"
                )

    asyncio.run(_run())

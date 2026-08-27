"""Property-based test: Credential Encryption Round-Trip.

**Validates: Requirements 1.3, 1.5**

Property 1: For arbitrary string values and keys, `set(key, value)` then
`get(key)` returns original value unchanged; raw bytes in DB do not contain
plaintext as substring (proving encryption at rest).
"""

from __future__ import annotations

import asyncio

import asyncpg
import pytest
from hypothesis import given, settings
from hypothesis import HealthCheck

from tests.strategies import credential_keys, credential_values


DB_KEY = "test-encryption-key-for-hypothesis-roundtrip"


@settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(key=credential_keys, value=credential_values)
def test_credential_encryption_roundtrip(
    pg_connection_url: str, _apply_schema, key: str, value: str
):
    """Property 1: Credential Encryption Round-Trip.

    For arbitrary string keys and values:
    1. set(key, value) then get(key) returns the original value unchanged.
    2. The raw encrypted bytes stored in the DB do NOT contain the plaintext
       value as a substring, proving encryption at rest.

    **Validates: Requirements 1.3, 1.5**
    """
    from credential_store_pg import CredentialStore

    store = CredentialStore(pg_uri=pg_connection_url, db_key=DB_KEY)
    try:
        # 1. Round-trip: set then get must return original value
        store.set(key, value)
        retrieved = store.get(key)
        assert retrieved == value, (
            f"Round-trip failed: set({key!r}, {value!r}) then get({key!r}) "
            f"returned {retrieved!r}"
        )

        # 2. Raw bytes in DB must NOT contain plaintext as substring.
        # Only meaningful for values >= 8 bytes — shorter strings can
        # coincidentally appear within base64-encoded ciphertext output
        # without indicating an encryption failure.
        if len(value.encode("utf-8")) >= 8:
            raw_bytes = _fetch_raw_value(pg_connection_url, key)
            plaintext_bytes = value.encode("utf-8")
            assert plaintext_bytes not in raw_bytes, (
                f"Plaintext found in raw DB bytes for key {key!r}! "
                f"Encryption at rest is broken."
            )
    finally:
        # Cleanup: remove the test key to avoid cross-example interference
        try:
            store.delete(key)
        except Exception:
            pass
        # Close the store's connection pool
        store._run_sync(store.close())


def _fetch_raw_value(pg_uri: str, key: str) -> bytes:
    """Fetch the raw encrypted bytes from the credentials table."""

    async def _query():
        conn = await asyncpg.connect(pg_uri)
        try:
            row = await conn.fetchrow(
                "SELECT value FROM credentials WHERE key = $1", key
            )
            if row is None:
                raise AssertionError(f"Row for key {key!r} not found in DB")
            return row["value"]
        finally:
            await conn.close()

    return asyncio.run(_query())

"""Property-based test: Config Rendering Equivalence.

**Validates: Requirements 15.3**

Property 11: Rendering Lavalink config from PG produces output identical
(byte-for-byte) to rendering from SQLite when both contain the same credential
data.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import sys
import tempfile
from pathlib import Path

import asyncpg
import pytest
from cryptography.fernet import Fernet
from hypothesis import given, settings, HealthCheck
from hypothesis import strategies as st


# ── Strategies ─────────────────────────────────────────────────────────────────

# Credential keys relevant to Lavalink config rendering
_LAVALINK_KEYS = [
    "spotify.client_id",
    "spotify.client_secret",
    "tidal.access_token",
    "tidal.api_token",
    "tidal.td_client_id",
    "tidal.client_id",
    "tidal.td_client_secret",
    "tidal.client_secret",
    "tidal.country_code",
    "tidal.search_limit",
    "ytcipher.api_token",
    "youtube.oauth_refresh_token",
    "youtube.refresh_token",
    "youtube.pot_token",
    "youtube.pot_visitor_data",
]

# Generate a subset of credential key-value pairs for Lavalink config
_lavalink_credentials = st.lists(
    st.tuples(
        st.sampled_from(_LAVALINK_KEYS),
        st.text(
            alphabet=st.characters(
                whitelist_categories=("L", "N", "Pd"),
                whitelist_characters="_-.",
            ),
            min_size=1,
            max_size=64,
        ),
    ),
    min_size=1,
    max_size=len(_LAVALINK_KEYS),
    unique_by=lambda t: t[0],  # unique keys
)


# ── Helpers ────────────────────────────────────────────────────────────────────

DB_KEY = "test-config-rendering-equivalence-key"


def _derive_key(passphrase: str) -> bytes:
    """Derive a Fernet key from passphrase (same as render_lavalink_config.py)."""
    raw = hashlib.sha256(passphrase.encode()).digest()
    return base64.urlsafe_b64encode(raw)


def _make_fernet() -> Fernet:
    return Fernet(_derive_key(DB_KEY))


# ── Property test ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@settings(
    max_examples=50,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(cred_pairs=_lavalink_credentials)
async def test_config_rendering_equivalence(
    pg_pool,
    pg_connection_url: str,
    tmp_path: Path,
    cred_pairs: list[tuple[str, str]],
):
    """Property 11: Config Rendering Equivalence.

    Rendering from PG produces output identical (byte-for-byte) to rendering
    from SQLite when both contain the same data.

    **Validates: Requirements 15.3**
    """
    # Ensure bot/ is importable
    bot_dir = str(Path(__file__).resolve().parent.parent / "bot")
    if bot_dir not in sys.path:
        sys.path.insert(0, bot_dir)

    from render_lavalink_config import (
        PGCredentialReader,
        SQLiteCredentialReader,
        render,
    )
    from credentials import CredentialStore as SqliteStore

    fernet = _make_fernet()

    # ── Set up HELLODJ_DB_KEY env for SQLite store ──
    old_env = os.environ.get("HELLODJ_DB_KEY")
    os.environ["HELLODJ_DB_KEY"] = DB_KEY

    # ── Clean PG credentials table ──
    async with pg_pool.acquire() as conn:
        await conn.execute("DELETE FROM credentials")

    # ── Insert encrypted credentials into PostgreSQL ──
    async with pg_pool.acquire() as conn:
        for key, value in cred_pairs:
            encrypted = fernet.encrypt(value.encode())
            await conn.execute(
                "INSERT INTO credentials (key, value) VALUES ($1, $2) "
                "ON CONFLICT (key) DO UPDATE SET value = $2",
                key,
                encrypted,
            )

    # ── Insert same credentials into a temporary SQLite DB ──
    sqlite_db = tmp_path / "render_test.db"
    if sqlite_db.exists():
        sqlite_db.unlink()
    sqlite_store = SqliteStore(db_path=sqlite_db)
    for key, value in cred_pairs:
        sqlite_store.set(key, value)

    # ── Render from PG ──
    pg_reader = PGCredentialReader(pg_connection_url, fernet)
    await pg_reader.connect()
    try:
        pg_output = await render(pg_reader)
    finally:
        await pg_reader.close()

    # ── Render from SQLite ──
    # The SQLiteCredentialReader uses the bot's CredentialStore in read-only
    # mode. We need to provide it with our test DB path. We'll directly
    # instantiate a SQLiteCredentialReader-like object using the existing store.
    class _TestSQLiteReader:
        """Adapter that wraps our test SQLite store to match the reader interface."""

        def __init__(self, store: SqliteStore):
            self._store = store

        async def connect(self):
            pass

        async def close(self):
            pass

        async def get(self, key: str, default: str | None = None) -> str | None:
            return self._store.get(key, default)

    sqlite_reader = _TestSQLiteReader(sqlite_store)
    sqlite_output = await render(sqlite_reader)

    # ── Assert byte-for-byte equivalence ──
    assert pg_output == sqlite_output, (
        f"Config rendering diverged!\n"
        f"Credentials: {cred_pairs}\n"
        f"PG output length: {len(pg_output)}\n"
        f"SQLite output length: {len(sqlite_output)}\n"
        f"First difference at position: {_first_diff_pos(pg_output, sqlite_output)}"
    )

    # Restore env
    if old_env is None:
        os.environ.pop("HELLODJ_DB_KEY", None)
    else:
        os.environ["HELLODJ_DB_KEY"] = old_env


def _first_diff_pos(a: str, b: str) -> int:
    """Find the position of the first character difference between two strings."""
    for i, (ca, cb) in enumerate(zip(a, b)):
        if ca != cb:
            return i
    return min(len(a), len(b))

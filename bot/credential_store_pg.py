"""HelloDJ — Encrypted credential store backed by PostgreSQL (CNPG).

Drop-in replacement for `credentials.py` that uses the existing
CloudNativePG PostgreSQL cluster instead of SQLite. Preserves the same
Fernet encryption pattern and public API so calling code requires zero changes.

Connection string is read from `HELLODJ_PG_URI` (env) with a default pointing
to the in-cluster CNPG service. The encryption key is still `HELLODJ_DB_KEY`.

Usage (identical to SQLite store):
    from credential_store_pg import creds

    token = creds.get("discord.token")
    creds.set("spotify.client_id", "abc123")
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import os
import threading
from typing import Any, Callable, Coroutine, TypeVar

import asyncpg
from cryptography.fernet import Fernet, InvalidToken

log = logging.getLogger(__name__)

T = TypeVar("T")

# ── Defaults ───────────────────────────────────────────────────────────────────

DEFAULT_PG_URI = (
    "postgresql://hellodj@postgresql-rw.postgresql-service.svc.cluster.local:5432/hellodj"
)

_RETRY_BASE = 1.0       # Initial backoff in seconds
_RETRY_MAX = 30.0       # Maximum backoff cap
_RETRY_ATTEMPTS = 5     # Total attempts before raising

_POOL_MIN = 2
_POOL_MAX = 10


# ── Encryption key derivation (identical to credentials.py) ────────────────────

def _derive_key(passphrase: str) -> bytes:
    """Derive a 32-byte Fernet key from an arbitrary passphrase.

    Uses SHA-256 to normalize any-length input into a valid Fernet key (which
    requires url-safe base64 of 32 bytes).
    """
    raw = hashlib.sha256(passphrase.encode()).digest()
    return base64.urlsafe_b64encode(raw)


# ── Retry helper ───────────────────────────────────────────────────────────────

def _is_connection_error(exc: BaseException) -> bool:
    """Determine whether an exception represents a recoverable connection loss."""
    if isinstance(exc, (
        asyncpg.PostgresConnectionError,
        asyncpg.InterfaceError,
        OSError,
        ConnectionError,
    )):
        return True
    # asyncpg wraps some connection issues in InterfaceError subclasses
    if isinstance(exc, asyncpg.InterfaceError):
        return True
    return False


async def _retry_async(coro_factory: Callable[[], Coroutine[Any, Any, T]]) -> T:
    """Execute an async callable with exponential backoff on connection errors.

    Backoff schedule: 1s, 2s, 4s, 8s (capped at 30s), up to 5 attempts total.
    """
    delay = _RETRY_BASE
    last_exc: BaseException | None = None

    for attempt in range(_RETRY_ATTEMPTS):
        try:
            return await coro_factory()
        except Exception as exc:
            if not _is_connection_error(exc):
                raise
            last_exc = exc
            if attempt < _RETRY_ATTEMPTS - 1:
                sleep_time = min(delay, _RETRY_MAX)
                log.warning(
                    "PostgreSQL connection error (attempt %d/%d), "
                    "retrying in %.1fs: %s",
                    attempt + 1, _RETRY_ATTEMPTS, sleep_time, exc,
                )
                await asyncio.sleep(sleep_time)
                delay *= 2
            else:
                log.error(
                    "PostgreSQL connection failed after %d attempts: %s",
                    _RETRY_ATTEMPTS, exc,
                )

    raise RuntimeError(
        f"PostgreSQL connection failed after {_RETRY_ATTEMPTS} attempts"
    ) from last_exc


# ── Credential Store ───────────────────────────────────────────────────────────

class CredentialStore:
    """Thread-safe encrypted key-value store backed by PostgreSQL (CNPG).

    Provides the same public API as the SQLite-backed CredentialStore.
    Async methods are the primary implementation; sync wrappers use a
    dedicated event loop on a background thread.
    """

    def __init__(self, pg_uri: str | None = None, db_key: str | None = None):
        # Resolve connection URI
        self._pg_uri = pg_uri or os.environ.get("HELLODJ_PG_URI", DEFAULT_PG_URI)

        # Resolve and validate encryption key
        key = db_key or os.environ.get("HELLODJ_DB_KEY", "")
        if not key:
            raise RuntimeError(
                "HELLODJ_DB_KEY environment variable is required. "
                "Generate one with: python -c \"import secrets; print(secrets.token_urlsafe(32))\""
            )
        self._fernet = Fernet(_derive_key(key))

        # Pool will be initialized lazily on first use
        self._pool: asyncpg.Pool | None = None
        self._pool_lock = asyncio.Lock()

        # Dedicated event loop + thread for sync wrappers
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: threading.Thread | None = None
        self._loop_ready = threading.Event()

    # ── Event loop management ──────────────────────────────────────────────

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        """Get or create the dedicated background event loop."""
        if self._loop is not None and self._loop.is_running():
            return self._loop

        def _run_loop():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
            self._loop_ready.set()
            loop.run_forever()

        self._loop_thread = threading.Thread(
            target=_run_loop, daemon=True, name="credential-store-pg-loop"
        )
        self._loop_thread.start()
        self._loop_ready.wait()
        return self._loop  # type: ignore[return-value]

    def _run_sync(self, coro: Coroutine[Any, Any, T]) -> T:
        """Run an async coroutine synchronously via the background loop."""
        loop = self._ensure_loop()
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        return future.result()

    # ── Pool management ────────────────────────────────────────────────────

    async def _get_pool(self) -> asyncpg.Pool:
        """Get or create the asyncpg connection pool."""
        if self._pool is not None and not self._pool._closed:
            return self._pool

        async with self._pool_lock:
            # Double-check after acquiring lock
            if self._pool is not None and not self._pool._closed:
                return self._pool

            self._pool = await asyncpg.create_pool(
                self._pg_uri,
                min_size=_POOL_MIN,
                max_size=_POOL_MAX,
                command_timeout=30,
            )
            return self._pool

    async def close(self) -> None:
        """Close the connection pool."""
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    # ── Encryption ─────────────────────────────────────────────────────────

    def _encrypt(self, plaintext: str) -> bytes:
        return self._fernet.encrypt(plaintext.encode())

    def _decrypt(self, ciphertext: bytes) -> str:
        try:
            return self._fernet.decrypt(ciphertext).decode()
        except InvalidToken:
            log.error("Failed to decrypt credential — key may have changed")
            return ""

    # ── Async public API ───────────────────────────────────────────────────

    async def aget(self, key: str, default: str | None = None) -> str | None:
        """Get a credential by key (async). Returns default if not found."""
        async def _op():
            pool = await self._get_pool()
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT value FROM credentials WHERE key = $1", key
                )
                if row is None:
                    return default
                return self._decrypt(row["value"])
        return await _retry_async(_op)

    async def aset(self, key: str, value: str) -> None:
        """Set a credential (async). Overwrites if exists. Uses row-level locking."""
        encrypted = self._encrypt(value)

        async def _op():
            pool = await self._get_pool()
            async with pool.acquire() as conn:
                async with conn.transaction():
                    # Attempt row-level lock; if row doesn't exist, INSERT
                    existing = await conn.fetchrow(
                        "SELECT key FROM credentials WHERE key = $1 FOR UPDATE",
                        key,
                    )
                    if existing:
                        await conn.execute(
                            "UPDATE credentials SET value = $1, updated_at = NOW() "
                            "WHERE key = $2",
                            encrypted, key,
                        )
                    else:
                        await conn.execute(
                            "INSERT INTO credentials (key, value, updated_at) "
                            "VALUES ($1, $2, NOW())",
                            key, encrypted,
                        )
        await _retry_async(_op)

    async def adelete(self, key: str) -> None:
        """Delete a credential (async)."""
        async def _op():
            pool = await self._get_pool()
            async with pool.acquire() as conn:
                async with conn.transaction():
                    # Lock the row before deletion for safety
                    await conn.fetchrow(
                        "SELECT key FROM credentials WHERE key = $1 FOR UPDATE",
                        key,
                    )
                    await conn.execute(
                        "DELETE FROM credentials WHERE key = $1", key
                    )
        await _retry_async(_op)

    async def aget_prefix(self, prefix: str) -> dict[str, str]:
        """Get all credentials matching a key prefix (async)."""
        async def _op():
            pool = await self._get_pool()
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT key, value FROM credentials WHERE key LIKE $1",
                    prefix + "%",
                )
                return {row["key"]: self._decrypt(row["value"]) for row in rows}
        return await _retry_async(_op)

    async def aexists(self, key: str) -> bool:
        """Check if a key exists (async)."""
        async def _op():
            pool = await self._get_pool()
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT 1 FROM credentials WHERE key = $1", key
                )
                return row is not None
        return await _retry_async(_op)

    async def akeys(self, prefix: str = "") -> list[str]:
        """List all keys, optionally filtered by prefix (async)."""
        async def _op():
            pool = await self._get_pool()
            async with pool.acquire() as conn:
                if prefix:
                    rows = await conn.fetch(
                        "SELECT key FROM credentials WHERE key LIKE $1",
                        prefix + "%",
                    )
                else:
                    rows = await conn.fetch("SELECT key FROM credentials")
                return [row["key"] for row in rows]
        return await _retry_async(_op)

    # ── Sync public API (identical signatures to SQLite CredentialStore) ────

    def get(self, key: str, default: str | None = None) -> str | None:
        """Get a credential by key. Returns default if not found."""
        return self._run_sync(self.aget(key, default))

    def set(self, key: str, value: str) -> None:
        """Set a credential. Overwrites if exists."""
        self._run_sync(self.aset(key, value))

    def delete(self, key: str) -> None:
        """Delete a credential."""
        self._run_sync(self.adelete(key))

    def get_prefix(self, prefix: str) -> dict[str, str]:
        """Get all credentials matching a key prefix."""
        return self._run_sync(self.aget_prefix(prefix))

    def get_bool(self, key: str, default: bool = False) -> bool:
        """Get a credential as a boolean."""
        val = self.get(key)
        if val is None:
            return default
        return val.lower() in ("true", "1", "yes", "on")

    def get_int(self, key: str, default: int = 0) -> int:
        """Get a credential as an integer."""
        val = self.get(key)
        if val is None:
            return default
        try:
            return int(val)
        except (ValueError, TypeError):
            return default

    def get_float(self, key: str, default: float = 0.0) -> float:
        """Get a credential as a float."""
        val = self.get(key)
        if val is None:
            return default
        try:
            return float(val)
        except (ValueError, TypeError):
            return default

    def exists(self, key: str) -> bool:
        """Check if a key exists."""
        return self._run_sync(self.aexists(key))

    def keys(self, prefix: str = "") -> list[str]:
        """List all keys, optionally filtered by prefix."""
        return self._run_sync(self.akeys(prefix))


# ── Singleton ──────────────────────────────────────────────────────────────────

def _create_pg_store() -> CredentialStore:
    """Create the PostgreSQL-backed credential store from environment."""
    return CredentialStore()


# Only create the singleton if we have a DB key configured.
# This allows importing the module without immediately failing if
# HELLODJ_DB_KEY is not set (useful for tests and migration scripts).
creds: CredentialStore | None = None

try:
    creds = _create_pg_store()
except RuntimeError:
    log.warning(
        "credential_store_pg: HELLODJ_DB_KEY not set, "
        "singleton 'creds' is None. Set HELLODJ_DB_KEY to enable."
    )

"""HelloDJ — Encrypted credential store backed by SQLite.

All secrets (tokens, API keys, client secrets) and settings (provider flags,
URLs, limits) live in a single SQLite database on the shared data volume.
Secret values are Fernet-encrypted at rest; the encryption key is the ONLY
secret that must be supplied via environment variable (HELLODJ_DB_KEY).

Usage:
    from credentials import creds

    # Read
    token = creds.get("discord.token")

    # Write
    creds.set("spotify.client_id", "abc123")

    # Bulk read by prefix
    tidal = creds.get_prefix("tidal.")
    # -> {"tidal.client_id": "...", "tidal.access_token": "...", ...}

    # Delete
    creds.delete("tidal.access_token")
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import sqlite3
import threading
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

log = logging.getLogger(__name__)

# ── Encryption key ─────────────────────────────────────────────────────────────

def _derive_key(passphrase: str) -> bytes:
    """Derive a 32-byte Fernet key from an arbitrary passphrase.

    Uses SHA-256 to normalize any-length input into a valid Fernet key (which
    requires url-safe base64 of 32 bytes).
    """
    raw = hashlib.sha256(passphrase.encode()).digest()
    return base64.urlsafe_b64encode(raw)


def _get_fernet() -> Fernet:
    """Get the Fernet cipher from the environment key."""
    key = os.environ.get("HELLODJ_DB_KEY", "")
    if not key:
        raise RuntimeError(
            "HELLODJ_DB_KEY environment variable is required. "
            "Generate one with: python -c \"import secrets; print(secrets.token_urlsafe(32))\""
        )
    return Fernet(_derive_key(key))


# ── Database ───────────────────────────────────────────────────────────────────

DATA_DIR = Path(os.environ.get("DATA_DIR", "/app/data"))
DB_PATH = DATA_DIR / "hellodj.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS credentials (
    key   TEXT PRIMARY KEY,
    value BLOB NOT NULL,
    updated_at TEXT DEFAULT (datetime('now'))
);
"""


class CredentialStore:
    """Thread-safe encrypted key-value store backed by SQLite."""

    def __init__(self, db_path: Path | None = None):
        self._db_path = db_path or DB_PATH
        self._fernet: Fernet | None = None
        self._local = threading.local()
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        """Get a thread-local SQLite connection."""
        if not hasattr(self._local, "conn") or self._local.conn is None:
            self._local.conn = sqlite3.connect(
                str(self._db_path), timeout=30, check_same_thread=False
            )
            self._local.conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn.execute("PRAGMA busy_timeout=5000")
        return self._local.conn

    def _init_db(self):
        """Create the database and table if they don't exist."""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._get_conn()
        conn.executescript(_SCHEMA)
        conn.commit()

    @property
    def fernet(self) -> Fernet:
        if self._fernet is None:
            self._fernet = _get_fernet()
        return self._fernet

    def _encrypt(self, plaintext: str) -> bytes:
        return self.fernet.encrypt(plaintext.encode())

    def _decrypt(self, ciphertext: bytes) -> str:
        try:
            return self.fernet.decrypt(ciphertext).decode()
        except InvalidToken:
            log.error("Failed to decrypt credential — key may have changed")
            return ""

    # ── Public API ─────────────────────────────────────────────────────────

    def get(self, key: str, default: str | None = None) -> str | None:
        """Get a credential by key. Returns default if not found."""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT value FROM credentials WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            return default
        return self._decrypt(row[0])

    def set(self, key: str, value: str) -> None:
        """Set a credential. Overwrites if exists."""
        conn = self._get_conn()
        encrypted = self._encrypt(value)
        conn.execute(
            "INSERT INTO credentials (key, value, updated_at) VALUES (?, ?, datetime('now')) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (key, encrypted),
        )
        conn.commit()

    def delete(self, key: str) -> None:
        """Delete a credential."""
        conn = self._get_conn()
        conn.execute("DELETE FROM credentials WHERE key = ?", (key,))
        conn.commit()

    def get_prefix(self, prefix: str) -> dict[str, str]:
        """Get all credentials matching a key prefix."""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT key, value FROM credentials WHERE key LIKE ?",
            (prefix + "%",),
        ).fetchall()
        return {k: self._decrypt(v) for k, v in rows}

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
        conn = self._get_conn()
        row = conn.execute(
            "SELECT 1 FROM credentials WHERE key = ?", (key,)
        ).fetchone()
        return row is not None

    def keys(self, prefix: str = "") -> list[str]:
        """List all keys, optionally filtered by prefix."""
        conn = self._get_conn()
        if prefix:
            rows = conn.execute(
                "SELECT key FROM credentials WHERE key LIKE ?",
                (prefix + "%",),
            ).fetchall()
        else:
            rows = conn.execute("SELECT key FROM credentials").fetchall()
        return [r[0] for r in rows]


# ── Singleton ──────────────────────────────────────────────────────────────────

creds = CredentialStore()

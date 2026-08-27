#!/usr/bin/env python3
"""
Migrate credentials from SQLite (hellodj.db) to PostgreSQL.

Reads all rows from the SQLite `credentials` table and inserts them into the
PostgreSQL `credentials` table, preserving Fernet-encrypted values byte-for-byte
without re-encryption.

Skip-on-conflict semantics: existing keys in PG are not overwritten; a warning
is logged for each skipped key.

Usage:
    python scripts/migrate_credentials.py

Environment:
    HELLODJ_PG_URI  - PostgreSQL connection URI for the hellodj database
    DATA_DIR        - Path to data directory containing hellodj.db (default: /app/data)
"""

import asyncio
import logging
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import asyncpg

logging.basicConfig(
    level=logging.INFO,
    format="[migrate_credentials] %(levelname)s: %(message)s",
)
log = logging.getLogger(__name__)

DEFAULT_PG_URI = (
    "postgresql://hellodj@postgresql-rw.postgresql-service.svc.cluster.local:5432/hellodj"
)

DATA_DIR = Path(os.environ.get("DATA_DIR", "/app/data"))
DB_PATH = DATA_DIR / "hellodj.db"


def read_sqlite_credentials(db_path: Path) -> list[tuple[str, bytes, str | None]]:
    """Read all credential rows from SQLite. Returns list of (key, value, updated_at)."""
    if not db_path.exists():
        log.warning("Source file not found: %s — skipping credentials migration", db_path)
        return []

    try:
        uri = f"file:{db_path}?mode=ro&immutable=1"
        conn = sqlite3.connect(uri, uri=True, timeout=30)
    except sqlite3.OperationalError as e:
        log.warning("Cannot open SQLite database %s: %s — skipping", db_path, e)
        return []

    try:
        # Check if credentials table exists
        table_check = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='credentials'"
        ).fetchone()
        if not table_check:
            log.warning("No 'credentials' table in %s — skipping", db_path)
            return []

        rows = conn.execute(
            "SELECT key, value, updated_at FROM credentials"
        ).fetchall()

        if not rows:
            log.warning("credentials table in %s is empty — nothing to migrate", db_path)
            return []

        log.info("Read %d credential rows from SQLite", len(rows))
        return rows
    finally:
        conn.close()


async def migrate_to_pg(
    pg_uri: str, rows: list[tuple[str, bytes, str | None]]
) -> tuple[int, int]:
    """
    Insert credential rows into PostgreSQL.
    Returns (migrated_count, skipped_count).
    """
    if not rows:
        return 0, 0

    conn = await asyncpg.connect(pg_uri)
    migrated = 0
    skipped = 0

    try:
        for key, value, updated_at in rows:
            try:
                # Parse updated_at string from SQLite into a datetime object
                # SQLite stores timestamps as text (e.g. "2026-08-24 21:37:02")
                ts: datetime | None = None
                if updated_at:
                    try:
                        ts = datetime.fromisoformat(updated_at).replace(
                            tzinfo=timezone.utc
                        )
                    except (ValueError, TypeError):
                        ts = None

                # Use INSERT ... ON CONFLICT DO NOTHING for skip-on-conflict
                result = await conn.execute(
                    """
                    INSERT INTO credentials (key, value, updated_at)
                    VALUES ($1, $2, COALESCE($3, now()))
                    ON CONFLICT (key) DO NOTHING
                    """,
                    key,
                    value,
                    ts,
                )
                # asyncpg returns "INSERT 0 1" on success, "INSERT 0 0" on conflict
                if result == "INSERT 0 1":
                    migrated += 1
                else:
                    skipped += 1
                    log.warning("Skipped existing key: %s", key)
            except Exception as e:
                log.warning("Error inserting key '%s': %s — skipping", key, e)
                skipped += 1
    finally:
        await conn.close()

    return migrated, skipped


async def main() -> None:
    pg_uri = os.environ.get("HELLODJ_PG_URI", DEFAULT_PG_URI)
    db_path = Path(os.environ.get("SQLITE_DB_PATH", str(DB_PATH)))

    log.info("Source: %s", db_path)
    log.info("Target: %s", pg_uri.split("@")[-1] if "@" in pg_uri else pg_uri)
    print()

    # Read from SQLite
    rows = read_sqlite_credentials(db_path)

    # Insert into PostgreSQL
    migrated, skipped = await migrate_to_pg(pg_uri, rows)

    # Summary
    print()
    log.info("=== Migration Summary (credentials) ===")
    log.info("  Source rows read: %d", len(rows))
    log.info("  Migrated:         %d", migrated)
    log.info("  Skipped:          %d", skipped)

    if not rows:
        log.info("  (no source data — nothing to migrate)")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except asyncpg.PostgresError as e:
        log.error("PostgreSQL error: %s", e)
        sys.exit(1)
    except ConnectionRefusedError:
        log.error(
            "Could not connect to PostgreSQL. Is the CNPG cluster running?"
        )
        sys.exit(1)
    except Exception as e:
        log.error("%s: %s", type(e).__name__, e)
        sys.exit(1)

#!/usr/bin/env python3
"""
Migrate sessions from JSON file to PostgreSQL.

Reads `data/sessions.json` and inserts session records into the PostgreSQL
`sessions` table with a configurable DEFAULT_TENANT_ID.

The sessions.json structure is:
    { "<guild_id>": { voice_channel_id, text_channel_id, current, queue, ... } }

Each guild entry becomes one row in the sessions table with:
    - tenant_id = DEFAULT_TENANT_ID (configurable)
    - guild_id = the guild key (as BIGINT)
    - channel_id = voice_channel_id from the session data
    - session_data = the full JSON object for that guild

Skip-on-conflict semantics: existing (tenant_id, guild_id, channel_id) keys
are not overwritten; a warning is logged for each skipped entry.

Usage:
    python scripts/migrate_sessions.py [--tenant-id UUID]

Environment:
    HELLODJ_PG_URI    - PostgreSQL connection URI for the hellodj database
    DEFAULT_TENANT_ID - Default tenant ID to assign to migrated sessions
                        (can also be passed as --tenant-id CLI arg)
    DATA_DIR          - Path to data directory (default: /app/data)
"""

import argparse
import asyncio
import json
import logging
import os
import sys
import uuid
from pathlib import Path

import asyncpg

logging.basicConfig(
    level=logging.INFO,
    format="[migrate_sessions] %(levelname)s: %(message)s",
)
log = logging.getLogger(__name__)

DEFAULT_PG_URI = (
    "postgresql://hellodj@postgresql-rw.postgresql-service.svc.cluster.local:5432/hellodj"
)

# Default tenant ID used when no tenant context exists in source data.
# The design doc specifies "system" as default, but this must be a valid UUID.
# We use a well-known UUID derived from the string "system".
SYSTEM_TENANT_ID = "00000000-0000-0000-0000-000000000000"

DATA_DIR = Path(os.environ.get("DATA_DIR", "/app/data"))
SESSIONS_FILE = DATA_DIR / "sessions.json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Migrate sessions.json to PostgreSQL"
    )
    parser.add_argument(
        "--tenant-id",
        type=str,
        default=os.environ.get("DEFAULT_TENANT_ID", SYSTEM_TENANT_ID),
        help="Default tenant UUID to assign to migrated sessions (default: %(default)s)",
    )
    parser.add_argument(
        "--sessions-file",
        type=str,
        default=str(SESSIONS_FILE),
        help="Path to sessions.json (default: %(default)s)",
    )
    return parser.parse_args()


def read_sessions(sessions_path: Path) -> dict:
    """Read and parse sessions.json. Returns empty dict on missing/malformed file."""
    if not sessions_path.exists():
        log.warning("Source file not found: %s — skipping sessions migration", sessions_path)
        return {}

    try:
        with open(sessions_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        log.warning("Malformed JSON in %s: %s — skipping sessions migration", sessions_path, e)
        return {}
    except OSError as e:
        log.warning("Cannot read %s: %s — skipping sessions migration", sessions_path, e)
        return {}

    if not isinstance(data, dict):
        log.warning("sessions.json top-level is not an object — skipping")
        return {}

    if not data:
        log.warning("sessions.json is empty — nothing to migrate")

    return data


async def migrate_to_pg(
    pg_uri: str, sessions: dict, tenant_id: str
) -> tuple[int, int, int]:
    """
    Insert session records into PostgreSQL.
    Returns (migrated_count, skipped_count, error_count).
    """
    if not sessions:
        return 0, 0, 0

    # Validate tenant_id is a valid UUID
    try:
        tenant_uuid = uuid.UUID(tenant_id)
    except ValueError:
        log.error("Invalid tenant ID '%s' — must be a valid UUID", tenant_id)
        sys.exit(1)

    conn = await asyncpg.connect(pg_uri)
    migrated = 0
    skipped = 0
    errors = 0

    try:
        for guild_id_str, session_data in sessions.items():
            # Validate guild_id is a valid integer
            try:
                guild_id = int(guild_id_str)
            except (ValueError, TypeError):
                log.warning(
                    "Malformed guild_id key '%s': not a valid integer — skipping",
                    guild_id_str,
                )
                errors += 1
                continue

            # Validate session_data is a dict
            if not isinstance(session_data, dict):
                log.warning(
                    "Malformed session data for guild %s: not a JSON object — skipping",
                    guild_id_str,
                )
                errors += 1
                continue

            # Extract channel_id from voice_channel_id in the session data
            channel_id = session_data.get("voice_channel_id")
            if channel_id is None:
                # Use text_channel_id as fallback
                channel_id = session_data.get("text_channel_id")
            if channel_id is None:
                # Use 0 as absolute fallback (no channel info in source)
                channel_id = 0

            try:
                channel_id = int(channel_id)
            except (ValueError, TypeError):
                log.warning(
                    "Malformed channel_id for guild %s: '%s' — skipping",
                    guild_id_str,
                    channel_id,
                )
                errors += 1
                continue

            # Serialize session_data back to JSON string for JSONB column
            try:
                session_json = json.dumps(session_data, ensure_ascii=False)
            except (TypeError, ValueError) as e:
                log.warning(
                    "Cannot serialize session data for guild %s: %s — skipping",
                    guild_id_str,
                    e,
                )
                errors += 1
                continue

            try:
                result = await conn.execute(
                    """
                    INSERT INTO sessions (tenant_id, guild_id, channel_id, session_data, updated_at)
                    VALUES ($1, $2, $3, $4::jsonb, now())
                    ON CONFLICT (tenant_id, guild_id, channel_id) DO NOTHING
                    """,
                    tenant_uuid,
                    guild_id,
                    channel_id,
                    session_json,
                )
                if result == "INSERT 0 1":
                    migrated += 1
                else:
                    skipped += 1
                    log.warning(
                        "Skipped existing session: guild=%s, channel=%s",
                        guild_id_str,
                        channel_id,
                    )
            except Exception as e:
                log.warning(
                    "Error inserting session for guild %s: %s — skipping",
                    guild_id_str,
                    e,
                )
                errors += 1
    finally:
        await conn.close()

    return migrated, skipped, errors


async def main() -> None:
    args = parse_args()
    pg_uri = os.environ.get("HELLODJ_PG_URI", DEFAULT_PG_URI)
    sessions_path = Path(args.sessions_file)
    tenant_id = args.tenant_id

    log.info("Source: %s", sessions_path)
    log.info("Target: %s", pg_uri.split("@")[-1] if "@" in pg_uri else pg_uri)
    log.info("Tenant ID: %s", tenant_id)
    print()

    # Read sessions
    sessions = read_sessions(sessions_path)

    # Insert into PostgreSQL
    migrated, skipped, errors = await migrate_to_pg(pg_uri, sessions, tenant_id)

    # Summary
    print()
    log.info("=== Migration Summary (sessions) ===")
    log.info("  Source entries read: %d", len(sessions))
    log.info("  Migrated:           %d", migrated)
    log.info("  Skipped (conflict): %d", skipped)
    log.info("  Skipped (errors):   %d", errors)

    if not sessions:
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
    except KeyboardInterrupt:
        pass
    except SystemExit:
        raise
    except Exception as e:
        log.error("%s: %s", type(e).__name__, e)
        sys.exit(1)

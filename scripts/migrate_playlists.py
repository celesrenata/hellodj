#!/usr/bin/env python3
"""
Migrate playlists from JSON file to PostgreSQL.

Reads `data/playlists.json` and inserts playlist records into the PostgreSQL
`playlists` table with a configurable DEFAULT_TENANT_ID.

The playlists.json structure is:
    { "<guild_id>": { "<playlist_name>": { tracks, created_by, created_at, ... } } }

Each playlist entry becomes one row in the playlists table with:
    - tenant_id = DEFAULT_TENANT_ID (configurable)
    - guild_id = the guild key (as BIGINT)
    - name = the playlist name
    - tracks = the tracks array as JSONB
    - created_at = from source data or now()

Skip-on-conflict semantics: existing (tenant_id, guild_id, lower(name)) keys
are not overwritten; a warning is logged for each skipped entry.

Usage:
    python scripts/migrate_playlists.py [--tenant-id UUID]

Environment:
    HELLODJ_PG_URI    - PostgreSQL connection URI for the hellodj database
    DEFAULT_TENANT_ID - Default tenant ID to assign to migrated playlists
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
from datetime import datetime, timezone
from pathlib import Path

import asyncpg

logging.basicConfig(
    level=logging.INFO,
    format="[migrate_playlists] %(levelname)s: %(message)s",
)
log = logging.getLogger(__name__)

DEFAULT_PG_URI = (
    "postgresql://hellodj@postgresql-rw.postgresql-service.svc.cluster.local:5432/hellodj"
)

# Default tenant ID used when no tenant context exists in source data.
SYSTEM_TENANT_ID = "00000000-0000-0000-0000-000000000000"

DATA_DIR = Path(os.environ.get("DATA_DIR", "/app/data"))
PLAYLISTS_FILE = DATA_DIR / "playlists.json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Migrate playlists.json to PostgreSQL"
    )
    parser.add_argument(
        "--tenant-id",
        type=str,
        default=os.environ.get("DEFAULT_TENANT_ID", SYSTEM_TENANT_ID),
        help="Default tenant UUID to assign to migrated playlists (default: %(default)s)",
    )
    parser.add_argument(
        "--playlists-file",
        type=str,
        default=str(PLAYLISTS_FILE),
        help="Path to playlists.json (default: %(default)s)",
    )
    return parser.parse_args()


def read_playlists(playlists_path: Path) -> dict:
    """Read and parse playlists.json. Returns empty dict on missing/malformed file."""
    if not playlists_path.exists():
        log.warning(
            "Source file not found: %s — skipping playlists migration",
            playlists_path,
        )
        return {}

    try:
        with open(playlists_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        log.warning(
            "Malformed JSON in %s: %s — skipping playlists migration",
            playlists_path,
            e,
        )
        return {}
    except OSError as e:
        log.warning("Cannot read %s: %s — skipping playlists migration", playlists_path, e)
        return {}

    if not isinstance(data, dict):
        log.warning("playlists.json top-level is not an object — skipping")
        return {}

    if not data:
        log.warning("playlists.json is empty — nothing to migrate")

    return data


def _parse_timestamp(ts_str: str | None) -> datetime | None:
    """Parse an ISO timestamp string, returning None on failure."""
    if not ts_str:
        return None
    try:
        dt = datetime.fromisoformat(ts_str)
        # Ensure timezone-aware
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


async def migrate_to_pg(
    pg_uri: str, playlists_data: dict, tenant_id: str
) -> tuple[int, int, int]:
    """
    Insert playlist records into PostgreSQL.
    Returns (migrated_count, skipped_count, error_count).
    """
    if not playlists_data:
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
        for guild_id_str, guild_playlists in playlists_data.items():
            # Validate guild_id
            try:
                guild_id = int(guild_id_str)
            except (ValueError, TypeError):
                log.warning(
                    "Malformed guild_id key '%s': not a valid integer — skipping all playlists for this guild",
                    guild_id_str,
                )
                errors += 1
                continue

            # Validate guild_playlists is a dict
            if not isinstance(guild_playlists, dict):
                log.warning(
                    "Malformed playlists data for guild %s: not a JSON object — skipping",
                    guild_id_str,
                )
                errors += 1
                continue

            for playlist_name, playlist_data in guild_playlists.items():
                # Validate playlist_data is a dict
                if not isinstance(playlist_data, dict):
                    log.warning(
                        "Malformed playlist '%s' in guild %s: not a JSON object — skipping",
                        playlist_name,
                        guild_id_str,
                    )
                    errors += 1
                    continue

                # Validate name length (max 100 chars)
                if len(playlist_name) > 100:
                    log.warning(
                        "Playlist name too long (%d chars) in guild %s: '%s...' — skipping",
                        len(playlist_name),
                        guild_id_str,
                        playlist_name[:50],
                    )
                    errors += 1
                    continue

                # Extract tracks
                tracks = playlist_data.get("tracks", [])
                if not isinstance(tracks, list):
                    log.warning(
                        "Malformed tracks for playlist '%s' in guild %s: not a list — skipping",
                        playlist_name,
                        guild_id_str,
                    )
                    errors += 1
                    continue

                # Serialize tracks to JSON
                try:
                    tracks_json = json.dumps(tracks, ensure_ascii=False)
                except (TypeError, ValueError) as e:
                    log.warning(
                        "Cannot serialize tracks for playlist '%s' in guild %s: %s — skipping",
                        playlist_name,
                        guild_id_str,
                        e,
                    )
                    errors += 1
                    continue

                # Check tracks size (max 5 MB)
                if len(tracks_json.encode("utf-8")) > 5 * 1024 * 1024:
                    log.warning(
                        "Tracks too large for playlist '%s' in guild %s (>5MB) — skipping",
                        playlist_name,
                        guild_id_str,
                    )
                    errors += 1
                    continue

                # Parse created_at timestamp
                created_at = _parse_timestamp(playlist_data.get("created_at"))
                if created_at is None:
                    created_at = datetime.now(timezone.utc)

                try:
                    # The unique index is on (tenant_id, guild_id, lower(name)).
                    # PostgreSQL supports ON CONFLICT on expression indexes by
                    # specifying the expressions directly.
                    result = await conn.execute(
                        """
                        INSERT INTO playlists (tenant_id, playlist_id, guild_id, name, tracks, created_at, updated_at)
                        VALUES ($1, gen_random_uuid(), $2, $3, $4::jsonb, $5, now())
                        ON CONFLICT (tenant_id, guild_id, lower(name)) DO NOTHING
                        """,
                        tenant_uuid,
                        guild_id,
                        playlist_name,
                        tracks_json,
                        created_at,
                    )
                    if result == "INSERT 0 1":
                        migrated += 1
                    else:
                        skipped += 1
                        log.warning(
                            "Skipped existing playlist: guild=%s, name='%s'",
                            guild_id_str,
                            playlist_name,
                        )
                except asyncpg.UniqueViolationError:
                    # Fallback if ON CONFLICT doesn't match the index
                    skipped += 1
                    log.warning(
                        "Skipped existing playlist: guild=%s, name='%s'",
                        guild_id_str,
                        playlist_name,
                    )
                except Exception as e:
                    log.warning(
                        "Error inserting playlist '%s' in guild %s: %s — skipping",
                        playlist_name,
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
    playlists_path = Path(args.playlists_file)
    tenant_id = args.tenant_id

    log.info("Source: %s", playlists_path)
    log.info("Target: %s", pg_uri.split("@")[-1] if "@" in pg_uri else pg_uri)
    log.info("Tenant ID: %s", tenant_id)
    print()

    # Read playlists
    playlists_data = read_playlists(playlists_path)

    # Count total playlist entries for summary
    total_entries = sum(
        len(guild_pl) if isinstance(guild_pl, dict) else 0
        for guild_pl in playlists_data.values()
    )

    # Insert into PostgreSQL
    migrated, skipped, errors = await migrate_to_pg(pg_uri, playlists_data, tenant_id)

    # Summary
    print()
    log.info("=== Migration Summary (playlists) ===")
    log.info("  Source guilds read:     %d", len(playlists_data))
    log.info("  Source playlists total: %d", total_entries)
    log.info("  Migrated:              %d", migrated)
    log.info("  Skipped (conflict):    %d", skipped)
    log.info("  Skipped (errors):      %d", errors)

    if not playlists_data:
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

#!/usr/bin/env python3
"""
Rollback export script: re-export PostgreSQL data back to SQLite + JSON format.

Produces files in the same schema as the original sources:
  - hellodj.db (SQLite) with credentials table:
      key TEXT PRIMARY KEY, value BLOB NOT NULL, updated_at TEXT
  - data/sessions.json:
      { "<guild_id>": { session_data_dict } }
  - data/playlists.json:
      { "<guild_id>": { "<playlist_name>": { tracks, ... } } }

Usage:
    python scripts/rollback_export.py [--output-dir /path/to/output]

Environment:
    HELLODJ_PG_URI  - PostgreSQL connection URI
                      (default: postgresql://hellodj@postgresql-rw.postgresql-service.svc.cluster.local:5432/hellodj)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import sys
from pathlib import Path

import asyncpg


DEFAULT_PG_URI = (
    "postgresql://hellodj@postgresql-rw.postgresql-service.svc.cluster.local:5432/hellodj"
)


async def export_credentials(conn: asyncpg.Connection, output_dir: Path) -> int:
    """Export credentials table from PostgreSQL to SQLite.

    Returns the number of rows exported.
    """
    db_path = output_dir / "hellodj.db"

    rows = await conn.fetch(
        "SELECT key, value, updated_at FROM credentials ORDER BY key"
    )

    # Create SQLite database with the original schema
    sqlite_conn = sqlite3.connect(str(db_path))
    try:
        sqlite_conn.execute("PRAGMA journal_mode=WAL")
        sqlite_conn.execute(
            """
            CREATE TABLE IF NOT EXISTS credentials (
                key        TEXT PRIMARY KEY,
                value      BLOB NOT NULL,
                updated_at TEXT DEFAULT (datetime('now'))
            )
            """
        )
        # Clear any existing data to ensure a clean export
        sqlite_conn.execute("DELETE FROM credentials")

        for row in rows:
            key = row["key"]
            value = bytes(row["value"])
            # Convert timestamptz to SQLite text format (ISO without timezone for compat)
            updated_at = row["updated_at"]
            if updated_at is not None:
                updated_at_str = updated_at.strftime("%Y-%m-%d %H:%M:%S")
            else:
                updated_at_str = None

            sqlite_conn.execute(
                "INSERT INTO credentials (key, value, updated_at) VALUES (?, ?, ?)",
                (key, value, updated_at_str),
            )

        sqlite_conn.commit()
    finally:
        sqlite_conn.close()

    return len(rows)


async def export_sessions(conn: asyncpg.Connection, output_dir: Path) -> int:
    """Export sessions table from PostgreSQL to JSON.

    Reconstructs the original format: { "<guild_id>": { session_data } }
    Returns the number of rows exported.
    """
    data_dir = output_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    sessions_path = data_dir / "sessions.json"

    rows = await conn.fetch(
        "SELECT guild_id, channel_id, session_data, updated_at FROM sessions ORDER BY guild_id"
    )

    # The original format is keyed by guild_id as a string.
    # session_data is the full session dict. In the original JSON, each guild has
    # a single entry (the most recent channel's session state). If multiple
    # channel_ids exist for the same guild, we take the most recently updated one.
    sessions: dict[str, dict] = {}
    for row in rows:
        guild_id_str = str(row["guild_id"])
        session_data = row["session_data"]

        # If there's already an entry for this guild, keep the most recent
        if guild_id_str in sessions:
            existing_updated = sessions[guild_id_str].get("updated_at", "")
            new_updated = row["updated_at"].isoformat() if row["updated_at"] else ""
            if new_updated <= existing_updated:
                continue

        # The session_data JSONB is the full session dict as stored
        if isinstance(session_data, str):
            session_data = json.loads(session_data)

        sessions[guild_id_str] = session_data

    with open(sessions_path, "w", encoding="utf-8") as f:
        json.dump(sessions, f, ensure_ascii=False, indent=2)

    return len(rows)


async def export_playlists(conn: asyncpg.Connection, output_dir: Path) -> int:
    """Export playlists table from PostgreSQL to JSON.

    Reconstructs the original format:
    { "<guild_id>": { "<playlist_name>": { tracks, created_by, created_at, ... } } }
    Returns the number of rows exported.
    """
    data_dir = output_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    playlists_path = data_dir / "playlists.json"

    rows = await conn.fetch(
        "SELECT guild_id, name, tracks, created_at, updated_at FROM playlists ORDER BY guild_id, name"
    )

    # Reconstruct the nested dict structure
    playlists: dict[str, dict] = {}
    for row in rows:
        guild_id_str = str(row["guild_id"])
        name = row["name"]
        tracks = row["tracks"]

        if isinstance(tracks, str):
            tracks = json.loads(tracks)

        if guild_id_str not in playlists:
            playlists[guild_id_str] = {}

        # Reconstruct the playlist entry in the original format
        playlist_entry: dict = {"tracks": tracks}

        # The original format includes created_at as ISO string
        if row["created_at"] is not None:
            playlist_entry["created_at"] = row["created_at"].isoformat()

        # Preserve any extra fields stored in tracks JSONB if it's a full object
        # (some migration paths store the full original playlist object in tracks)
        playlists[guild_id_str][name] = playlist_entry

    with open(playlists_path, "w", encoding="utf-8") as f:
        json.dump(playlists, f, ensure_ascii=False, indent=2)

    return len(rows)


async def main(output_dir: Path) -> None:
    """Main rollback export entry point."""
    pg_uri = os.environ.get("HELLODJ_PG_URI", DEFAULT_PG_URI)

    print(f"[rollback_export] Connecting to: {pg_uri.split('@')[-1]}")
    print(f"[rollback_export] Output directory: {output_dir}")
    print()

    conn = await asyncpg.connect(pg_uri)
    try:
        # Export credentials → SQLite
        print("[rollback_export] Exporting credentials → hellodj.db ...")
        cred_count = await export_credentials(conn, output_dir)
        print(f"[rollback_export]   Exported {cred_count} credential(s)")

        # Export sessions → JSON
        print("[rollback_export] Exporting sessions → data/sessions.json ...")
        session_count = await export_sessions(conn, output_dir)
        print(f"[rollback_export]   Exported {session_count} session row(s)")

        # Export playlists → JSON
        print("[rollback_export] Exporting playlists → data/playlists.json ...")
        playlist_count = await export_playlists(conn, output_dir)
        print(f"[rollback_export]   Exported {playlist_count} playlist(s)")

    finally:
        await conn.close()

    # Summary
    print()
    print("[rollback_export] ═══════════════════════════════════════")
    print("[rollback_export] Rollback export complete.")
    print(f"[rollback_export]   Credentials: {cred_count} records → {output_dir / 'hellodj.db'}")
    print(f"[rollback_export]   Sessions:    {session_count} records → {output_dir / 'data' / 'sessions.json'}")
    print(f"[rollback_export]   Playlists:   {playlist_count} records → {output_dir / 'data' / 'playlists.json'}")
    print("[rollback_export] ═══════════════════════════════════════")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Re-export PostgreSQL data back to SQLite + JSON format for rollback."
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("."),
        help="Directory to write exported files (default: current directory)",
    )
    args = parser.parse_args()

    # Ensure output directory exists
    args.output_dir.mkdir(parents=True, exist_ok=True)

    try:
        asyncio.run(main(args.output_dir))
    except asyncpg.PostgresError as e:
        print(f"[rollback_export] ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    except ConnectionRefusedError:
        print(
            "[rollback_export] ERROR: Could not connect to PostgreSQL. "
            "Is the CNPG cluster running?",
            file=sys.stderr,
        )
        sys.exit(1)
    except Exception as e:
        print(f"[rollback_export] ERROR: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)

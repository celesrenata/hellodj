#!/usr/bin/env python3
"""
PostgreSQL schema migration script for HelloDJ SaaS platform.

Idempotently creates the `hellodj` database and user, then creates all tables,
constraints, and indexes in the CNPG cluster.

Usage:
    python scripts/migrate_schema.py

Environment:
    HELLODJ_PG_URI  - PostgreSQL connection URI (default: postgresql://postgres@postgresql-rw.postgresql-service.svc.cluster.local:5432/postgres)
    HELLODJ_DB_NAME - Target database name (default: hellodj)
    HELLODJ_DB_USER - Target database user (default: hellodj)
    HELLODJ_DB_PASS - Password for the hellodj user (default: auto-generated if creating)
"""

import asyncio
import os
import sys
import secrets

import asyncpg


# Default connection to the CNPG cluster's postgres database (admin context)
DEFAULT_PG_URI = (
    "postgresql://postgres@postgresql-rw.postgresql-service.svc.cluster.local:5432/postgres"
)

DB_NAME = os.environ.get("HELLODJ_DB_NAME", "hellodj")
DB_USER = os.environ.get("HELLODJ_DB_USER", "hellodj")
DB_PASS = os.environ.get("HELLODJ_DB_PASS", "")


# ---------------------------------------------------------------------------
# SQL Statements
# ---------------------------------------------------------------------------

TABLES_SQL = """
-- Existing credentials table (migrated from SQLite)
CREATE TABLE IF NOT EXISTS credentials (
    key         TEXT PRIMARY KEY,
    value       BYTEA NOT NULL,
    updated_at  TIMESTAMPTZ DEFAULT now()
);

-- Tenants
CREATE TABLE IF NOT EXISTS tenants (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    discord_user_id   BIGINT UNIQUE NOT NULL,
    discord_username  TEXT,
    email             TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Subscriptions
CREATE TABLE IF NOT EXISTS subscriptions (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   UUID NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    plan        TEXT NOT NULL,
    addons      TEXT[] DEFAULT '{}',
    status      TEXT NOT NULL,
    started_at  TIMESTAMPTZ NOT NULL,
    expires_at  TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Bot Instances
CREATE TABLE IF NOT EXISTS bot_instances (
    id                          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id                   UUID NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    discord_bot_token_encrypted BYTEA,
    guild_ids                   BIGINT[],
    status                      TEXT NOT NULL,
    node_name                   TEXT,
    pod_name                    TEXT,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Payments
CREATE TABLE IF NOT EXISTS payments (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    paypal_txn_id   TEXT UNIQUE,
    amount_cents    INTEGER NOT NULL,
    currency        TEXT NOT NULL DEFAULT 'USD',
    status          TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Trial Applications
CREATE TABLE IF NOT EXISTS trial_applications (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   UUID NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    status      TEXT NOT NULL,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    decided_at  TIMESTAMPTZ,
    decided_by  TEXT
);

-- Sessions (multi-tenant)
CREATE TABLE IF NOT EXISTS sessions (
    tenant_id       UUID NOT NULL,
    guild_id        BIGINT NOT NULL,
    channel_id      BIGINT NOT NULL,
    session_data    JSONB NOT NULL,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, guild_id, channel_id)
);

-- Playlists (multi-tenant)
CREATE TABLE IF NOT EXISTS playlists (
    tenant_id   UUID NOT NULL,
    playlist_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    guild_id    BIGINT NOT NULL,
    name        TEXT NOT NULL,
    tracks      JSONB NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- RBAC: Delegated tenant roles
CREATE TABLE IF NOT EXISTS tenant_roles (
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    discord_user_id BIGINT NOT NULL,
    role            TEXT NOT NULL CHECK (role IN ('admin', 'editor', 'viewer')),
    granted_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    granted_by      BIGINT NOT NULL,
    PRIMARY KEY (tenant_id, discord_user_id)
);
"""

# CHECK constraints added via DO blocks for idempotency (ALTER TABLE ... ADD CONSTRAINT IF NOT EXISTS
# is supported in PostgreSQL 12+ with DO $$ ... $$)
CONSTRAINTS_SQL = """
-- subscriptions.plan CHECK
DO $$ BEGIN
    ALTER TABLE subscriptions ADD CONSTRAINT subscriptions_plan_check
        CHECK (plan IN ('base', 'trial'));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- subscriptions.status CHECK
DO $$ BEGIN
    ALTER TABLE subscriptions ADD CONSTRAINT subscriptions_status_check
        CHECK (status IN ('active', 'past_due', 'cancelled', 'expired', 'pending_payment'));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- bot_instances.status CHECK
DO $$ BEGIN
    ALTER TABLE bot_instances ADD CONSTRAINT bot_instances_status_check
        CHECK (status IN ('provisioning', 'running', 'stopped', 'error', 'pending_resources', 'failed'));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- payments.amount_cents CHECK
DO $$ BEGIN
    ALTER TABLE payments ADD CONSTRAINT payments_amount_cents_check
        CHECK (amount_cents > 0);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- payments.status CHECK
DO $$ BEGIN
    ALTER TABLE payments ADD CONSTRAINT payments_status_check
        CHECK (status IN ('pending', 'completed', 'refunded', 'failed'));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- trial_applications.status CHECK
DO $$ BEGIN
    ALTER TABLE trial_applications ADD CONSTRAINT trial_applications_status_check
        CHECK (status IN ('pending', 'approved', 'rejected'));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- sessions.session_data size CHECK (max 1 MB)
DO $$ BEGIN
    ALTER TABLE sessions ADD CONSTRAINT session_data_size
        CHECK (octet_length(session_data::text) <= 1048576);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- playlists.name length CHECK
DO $$ BEGIN
    ALTER TABLE playlists ADD CONSTRAINT playlists_name_length_check
        CHECK (char_length(name) <= 100);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- playlists.tracks size CHECK (max 5 MB)
DO $$ BEGIN
    ALTER TABLE playlists ADD CONSTRAINT playlist_tracks_size
        CHECK (octet_length(tracks::text) <= 5242880);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- playlists unique name per tenant+guild (case-insensitive)
-- This uses a unique index instead of a table constraint for the expression
CREATE UNIQUE INDEX IF NOT EXISTS playlists_unique_name
    ON playlists (tenant_id, guild_id, lower(name));
"""

INDEXES_SQL = """
CREATE INDEX IF NOT EXISTS idx_subscriptions_tenant ON subscriptions(tenant_id);
CREATE INDEX IF NOT EXISTS idx_subscriptions_status ON subscriptions(status);
CREATE INDEX IF NOT EXISTS idx_bot_instances_tenant ON bot_instances(tenant_id);
CREATE INDEX IF NOT EXISTS idx_bot_instances_status ON bot_instances(status);
CREATE INDEX IF NOT EXISTS idx_payments_tenant ON payments(tenant_id);
CREATE INDEX IF NOT EXISTS idx_payments_created ON payments(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_trial_applications_status ON trial_applications(status);
CREATE INDEX IF NOT EXISTS idx_sessions_tenant ON sessions(tenant_id);
CREATE INDEX IF NOT EXISTS idx_playlists_tenant_guild ON playlists(tenant_id, guild_id);
CREATE INDEX IF NOT EXISTS idx_tenant_roles_user ON tenant_roles(discord_user_id);
CREATE INDEX IF NOT EXISTS idx_tenant_roles_tenant ON tenant_roles(tenant_id);
"""


async def ensure_database_and_user(admin_uri: str) -> str:
    """
    Idempotently create the hellodj database and user.
    Returns the connection URI for the hellodj database.
    """
    conn = await asyncpg.connect(admin_uri)
    try:
        # Create user if not exists
        user_exists = await conn.fetchval(
            "SELECT 1 FROM pg_roles WHERE rolname = $1", DB_USER
        )
        if not user_exists:
            password = DB_PASS or secrets.token_urlsafe(32)
            # Use quoted identifier for safety; password is escaped by asyncpg
            await conn.execute(
                f'CREATE ROLE "{DB_USER}" WITH LOGIN PASSWORD '
                f"'{password}'"
            )
            print(f"[migrate_schema] Created user '{DB_USER}'")
            if not DB_PASS:
                print(
                    f"[migrate_schema] Generated password for '{DB_USER}': {password}"
                )
                print(
                    "[migrate_schema] Store this in a Kubernetes Secret "
                    "(HELLODJ_PG_URI) for production use."
                )
        else:
            print(f"[migrate_schema] User '{DB_USER}' already exists")

        # Create database if not exists
        db_exists = await conn.fetchval(
            "SELECT 1 FROM pg_database WHERE datname = $1", DB_NAME
        )
        if not db_exists:
            # CREATE DATABASE cannot run inside a transaction in asyncpg
            # but asyncpg auto-manages transactions. We need to use execute
            # outside a transaction block.
            await conn.execute(
                f'CREATE DATABASE "{DB_NAME}" OWNER "{DB_USER}"'
            )
            print(f"[migrate_schema] Created database '{DB_NAME}'")
        else:
            print(f"[migrate_schema] Database '{DB_NAME}' already exists")

        # Grant privileges
        await conn.execute(
            f'GRANT ALL PRIVILEGES ON DATABASE "{DB_NAME}" TO "{DB_USER}"'
        )

    finally:
        await conn.close()

    # Build connection URI for the hellodj database
    # Parse the admin URI to replace the database name
    parts = admin_uri.rsplit("/", 1)
    hellodj_uri = f"{parts[0]}/{DB_NAME}"
    return hellodj_uri


async def apply_schema(db_uri: str) -> None:
    """Apply all tables, constraints, and indexes to the hellodj database."""
    conn = await asyncpg.connect(db_uri)
    try:
        # Create tables
        print("[migrate_schema] Creating tables...")
        await conn.execute(TABLES_SQL)
        print("[migrate_schema] Tables created (IF NOT EXISTS)")

        # Apply CHECK constraints and unique indexes
        print("[migrate_schema] Applying constraints...")
        await conn.execute(CONSTRAINTS_SQL)
        print("[migrate_schema] Constraints applied")

        # Create indexes
        print("[migrate_schema] Creating indexes...")
        await conn.execute(INDEXES_SQL)
        print("[migrate_schema] Indexes created (IF NOT EXISTS)")

    finally:
        await conn.close()


async def main() -> None:
    """Main migration entry point."""
    pg_uri = os.environ.get("HELLODJ_PG_URI", DEFAULT_PG_URI)

    print(f"[migrate_schema] Connecting to: {pg_uri.split('@')[-1]}")
    print(f"[migrate_schema] Target database: {DB_NAME}")
    print(f"[migrate_schema] Target user: {DB_USER}")
    print()

    # Step 1: Ensure database and user exist (connect to admin/postgres db)
    # If HELLODJ_PG_URI already points to the hellodj database, we need the
    # admin URI (postgres database) for CREATE DATABASE/ROLE operations.
    # Detect this by checking if the URI ends with the target DB name.
    if pg_uri.rstrip("/").endswith(f"/{DB_NAME}"):
        # Derive admin URI by replacing the DB name with 'postgres'
        admin_uri = pg_uri.rstrip("/").rsplit("/", 1)[0] + "/postgres"
    else:
        admin_uri = pg_uri

    try:
        hellodj_uri = await ensure_database_and_user(admin_uri)
    except asyncpg.InvalidCatalogNameError:
        # The admin URI database doesn't exist — try connecting to 'postgres' directly
        parts = pg_uri.rsplit("/", 1)
        admin_uri = f"{parts[0]}/postgres"
        hellodj_uri = await ensure_database_and_user(admin_uri)

    # Step 2: Apply schema to hellodj database
    print()
    await apply_schema(hellodj_uri)

    print()
    print("[migrate_schema] ✓ Schema migration complete.")
    print(
        f"[migrate_schema] Connection URI for services: "
        f"postgresql://{DB_USER}@.../{DB_NAME}"
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except asyncpg.PostgresError as e:
        print(f"[migrate_schema] ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    except ConnectionRefusedError:
        print(
            "[migrate_schema] ERROR: Could not connect to PostgreSQL. "
            "Is the CNPG cluster running?",
            file=sys.stderr,
        )
        sys.exit(1)
    except Exception as e:
        print(f"[migrate_schema] ERROR: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)

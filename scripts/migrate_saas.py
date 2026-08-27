#!/usr/bin/env python3
"""Minimal migration: create hellodj DB + tenants + tenant_roles tables."""
import asyncio, os, asyncpg

ADMIN_URI = os.environ.get("HELLODJ_PG_URI", "postgresql://celes:PSCh4ng3me!@postgresql-rw.postgresql-service.svc.cluster.local:5432/postgres")

async def main():
    # Connect to postgres admin DB
    conn = await asyncpg.connect(ADMIN_URI)
    db_exists = await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = 'hellodj'")
    if not db_exists:
        await conn.execute("CREATE DATABASE hellodj OWNER celes")
        print("Created database hellodj")
    else:
        print("Database hellodj already exists")
    await conn.close()

    # Connect to hellodj and create tables
    hellodj_uri = ADMIN_URI.rsplit("/", 1)[0] + "/hellodj"
    conn = await asyncpg.connect(hellodj_uri)

    await conn.execute("""
        CREATE TABLE IF NOT EXISTS tenants (
            id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            discord_user_id   BIGINT UNIQUE NOT NULL,
            discord_username  TEXT,
            email             TEXT,
            created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    print("tenants table ready")

    await conn.execute("""
        CREATE TABLE IF NOT EXISTS tenant_roles (
            tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            discord_user_id BIGINT NOT NULL,
            role            TEXT NOT NULL CHECK (role IN ('admin', 'editor', 'viewer')),
            granted_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            granted_by      BIGINT NOT NULL,
            PRIMARY KEY (tenant_id, discord_user_id)
        )
    """)
    print("tenant_roles table ready")

    await conn.execute("CREATE INDEX IF NOT EXISTS idx_tenant_roles_user ON tenant_roles(discord_user_id)")
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_tenant_roles_tenant ON tenant_roles(tenant_id)")
    print("Indexes ready")

    await conn.close()
    print("Migration complete!")

asyncio.run(main())

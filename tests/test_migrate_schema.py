"""
Tests for scripts/migrate_schema.py

Validates SQL statement structure, idempotency patterns, and script logic.
These tests don't require a live PostgreSQL connection — they verify the SQL
content and script behavior at the module level.
"""

import importlib.util
import re
import sys
from pathlib import Path

import pytest


# Load the migration module without executing main()
@pytest.fixture(scope="module")
def migrate_module():
    spec = importlib.util.spec_from_file_location(
        "migrate_schema",
        str(Path(__file__).resolve().parent.parent / "scripts" / "migrate_schema.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    # Temporarily prevent execution of __main__ block
    old_name = mod.__name__
    mod.__name__ = "migrate_schema"
    spec.loader.exec_module(mod)
    mod.__name__ = old_name
    return mod


class TestTableCreation:
    """Verify all required tables are defined with IF NOT EXISTS."""

    EXPECTED_TABLES = [
        "credentials",
        "tenants",
        "subscriptions",
        "bot_instances",
        "payments",
        "trial_applications",
        "sessions",
        "playlists",
    ]

    def test_all_tables_present(self, migrate_module):
        """All 8 tables are created in TABLES_SQL."""
        sql = migrate_module.TABLES_SQL
        for table in self.EXPECTED_TABLES:
            pattern = rf"CREATE TABLE IF NOT EXISTS {table}"
            assert re.search(pattern, sql, re.IGNORECASE), (
                f"Table '{table}' not found with IF NOT EXISTS pattern"
            )

    def test_tables_idempotent(self, migrate_module):
        """All CREATE TABLE statements use IF NOT EXISTS."""
        sql = migrate_module.TABLES_SQL
        # Find all CREATE TABLE statements
        creates = re.findall(r"CREATE TABLE\b[^;]+", sql, re.IGNORECASE)
        for stmt in creates:
            assert "IF NOT EXISTS" in stmt.upper(), (
                f"CREATE TABLE without IF NOT EXISTS: {stmt[:80]}"
            )


class TestConstraints:
    """Verify CHECK constraints are defined for all required columns."""

    def test_subscriptions_plan_check(self, migrate_module):
        sql = migrate_module.CONSTRAINTS_SQL
        assert "subscriptions_plan_check" in sql
        assert "'base'" in sql
        assert "'trial'" in sql

    def test_subscriptions_status_check(self, migrate_module):
        sql = migrate_module.CONSTRAINTS_SQL
        assert "subscriptions_status_check" in sql
        for status in ("active", "past_due", "cancelled", "expired", "pending_payment"):
            assert f"'{status}'" in sql

    def test_bot_instances_status_check(self, migrate_module):
        sql = migrate_module.CONSTRAINTS_SQL
        assert "bot_instances_status_check" in sql
        for status in ("provisioning", "running", "stopped", "error", "pending_resources", "failed"):
            assert f"'{status}'" in sql

    def test_payments_amount_cents_check(self, migrate_module):
        sql = migrate_module.CONSTRAINTS_SQL
        assert "payments_amount_cents_check" in sql
        assert "amount_cents > 0" in sql

    def test_payments_status_check(self, migrate_module):
        sql = migrate_module.CONSTRAINTS_SQL
        assert "payments_status_check" in sql
        for status in ("pending", "completed", "refunded", "failed"):
            assert f"'{status}'" in sql

    def test_trial_applications_status_check(self, migrate_module):
        sql = migrate_module.CONSTRAINTS_SQL
        assert "trial_applications_status_check" in sql
        for status in ("pending", "approved", "rejected"):
            assert f"'{status}'" in sql

    def test_session_data_size_check(self, migrate_module):
        sql = migrate_module.CONSTRAINTS_SQL
        assert "session_data_size" in sql
        assert "1048576" in sql  # 1 MB

    def test_playlist_tracks_size_check(self, migrate_module):
        sql = migrate_module.CONSTRAINTS_SQL
        assert "playlist_tracks_size" in sql
        assert "5242880" in sql  # 5 MB

    def test_playlists_name_length_check(self, migrate_module):
        sql = migrate_module.CONSTRAINTS_SQL
        assert "playlists_name_length_check" in sql
        assert "100" in sql

    def test_playlists_unique_name_index(self, migrate_module):
        sql = migrate_module.CONSTRAINTS_SQL
        assert "playlists_unique_name" in sql
        assert "lower(name)" in sql

    def test_constraints_idempotent(self, migrate_module):
        """All constraints use exception handling for idempotency."""
        sql = migrate_module.CONSTRAINTS_SQL
        # Each DO block should have EXCEPTION WHEN duplicate_object
        do_blocks = re.findall(r"DO \$\$.*?\$\$;", sql, re.DOTALL)
        for block in do_blocks:
            assert "EXCEPTION WHEN duplicate_object" in block, (
                f"DO block missing idempotency handler: {block[:80]}"
            )


class TestIndexes:
    """Verify all required indexes are defined."""

    EXPECTED_INDEXES = [
        "idx_subscriptions_tenant",
        "idx_subscriptions_status",
        "idx_bot_instances_tenant",
        "idx_bot_instances_status",
        "idx_payments_tenant",
        "idx_payments_created",
        "idx_trial_applications_status",
        "idx_sessions_tenant",
        "idx_playlists_tenant_guild",
    ]

    def test_all_indexes_present(self, migrate_module):
        sql = migrate_module.INDEXES_SQL
        for idx in self.EXPECTED_INDEXES:
            assert idx in sql, f"Index '{idx}' not found"

    def test_indexes_idempotent(self, migrate_module):
        """All CREATE INDEX statements use IF NOT EXISTS."""
        sql = migrate_module.INDEXES_SQL
        creates = re.findall(r"CREATE INDEX\b[^;]+", sql, re.IGNORECASE)
        for stmt in creates:
            assert "IF NOT EXISTS" in stmt.upper(), (
                f"CREATE INDEX without IF NOT EXISTS: {stmt[:80]}"
            )

    def test_payments_created_descending(self, migrate_module):
        """idx_payments_created should be DESC for recent-first queries."""
        sql = migrate_module.INDEXES_SQL
        assert "created_at DESC" in sql


class TestForeignKeys:
    """Verify foreign key constraints are defined with ON DELETE RESTRICT."""

    FK_TABLES = ["subscriptions", "bot_instances", "payments", "trial_applications"]

    def test_fk_references_tenants(self, migrate_module):
        """All tenant-scoped tables reference tenants(id)."""
        sql = migrate_module.TABLES_SQL
        for table in self.FK_TABLES:
            # Extract the CREATE TABLE block for this table
            pattern = rf"CREATE TABLE IF NOT EXISTS {table}\s*\(.*?\);"
            match = re.search(pattern, sql, re.DOTALL | re.IGNORECASE)
            assert match, f"Table '{table}' CREATE block not found"
            block = match.group(0)
            assert "REFERENCES tenants(id)" in block, (
                f"Table '{table}' missing FK to tenants(id)"
            )
            assert "ON DELETE RESTRICT" in block, (
                f"Table '{table}' missing ON DELETE RESTRICT"
            )


class TestColumnDefinitions:
    """Verify key columns are present in table definitions."""

    def test_credentials_columns(self, migrate_module):
        sql = migrate_module.TABLES_SQL
        assert "key         TEXT PRIMARY KEY" in sql
        assert "value       BYTEA NOT NULL" in sql

    def test_tenants_columns(self, migrate_module):
        sql = migrate_module.TABLES_SQL
        assert "discord_user_id   BIGINT UNIQUE NOT NULL" in sql

    def test_sessions_composite_pk(self, migrate_module):
        sql = migrate_module.TABLES_SQL
        assert "PRIMARY KEY (tenant_id, guild_id, channel_id)" in sql

    def test_playlists_has_playlist_id_pk(self, migrate_module):
        sql = migrate_module.TABLES_SQL
        assert "playlist_id UUID PRIMARY KEY DEFAULT gen_random_uuid()" in sql


class TestScriptConfig:
    """Verify script configuration and defaults."""

    def test_default_pg_uri(self, migrate_module):
        assert "postgresql-rw.postgresql-service.svc.cluster.local" in migrate_module.DEFAULT_PG_URI
        assert "5432" in migrate_module.DEFAULT_PG_URI

    def test_default_db_name(self, migrate_module):
        assert migrate_module.DB_NAME == "hellodj"

    def test_default_db_user(self, migrate_module):
        assert migrate_module.DB_USER == "hellodj"

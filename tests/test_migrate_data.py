"""
Tests for the data migration scripts:
  - scripts/migrate_credentials.py
  - scripts/migrate_sessions.py
  - scripts/migrate_playlists.py

Validates script logic for reading source files, handling missing/malformed data,
and correct behavior of the migration functions. Tests that don't require a live
PostgreSQL connection focus on the read/parse logic and error handling.
"""

import importlib.util
import json
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest


# ── Module loading fixtures ─────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def creds_module():
    """Load migrate_credentials module without executing main()."""
    spec = importlib.util.spec_from_file_location(
        "migrate_credentials",
        str(Path(__file__).resolve().parent.parent / "scripts" / "migrate_credentials.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    mod.__name__ = "migrate_credentials"
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def sessions_module():
    """Load migrate_sessions module without executing main()."""
    spec = importlib.util.spec_from_file_location(
        "migrate_sessions",
        str(Path(__file__).resolve().parent.parent / "scripts" / "migrate_sessions.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    mod.__name__ = "migrate_sessions"
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def playlists_module():
    """Load migrate_playlists module without executing main()."""
    spec = importlib.util.spec_from_file_location(
        "migrate_playlists",
        str(Path(__file__).resolve().parent.parent / "scripts" / "migrate_playlists.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    mod.__name__ = "migrate_playlists"
    spec.loader.exec_module(mod)
    return mod


# ── Credentials Migration Tests ─────────────────────────────────────────────────


class TestMigrateCredentialsRead:
    """Test reading credentials from SQLite."""

    def test_missing_file_returns_empty(self, creds_module, tmp_path):
        """Missing SQLite file returns empty list with warning."""
        nonexistent = tmp_path / "missing.db"
        result = creds_module.read_sqlite_credentials(nonexistent)
        assert result == []

    def test_reads_all_rows(self, creds_module, tmp_path):
        """Reads all rows from a valid SQLite credentials table."""
        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "CREATE TABLE credentials (key TEXT PRIMARY KEY, value BLOB NOT NULL, "
            "updated_at TEXT DEFAULT (datetime('now')))"
        )
        conn.execute(
            "INSERT INTO credentials (key, value) VALUES (?, ?)",
            ("test.key", b"encrypted_data"),
        )
        conn.execute(
            "INSERT INTO credentials (key, value) VALUES (?, ?)",
            ("another.key", b"more_encrypted"),
        )
        conn.commit()
        conn.close()

        result = creds_module.read_sqlite_credentials(db_path)
        assert len(result) == 2
        # Rows are (key, value, updated_at)
        keys = {r[0] for r in result}
        assert keys == {"test.key", "another.key"}

    def test_empty_table_returns_empty(self, creds_module, tmp_path):
        """Empty credentials table returns empty list."""
        db_path = tmp_path / "empty.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "CREATE TABLE credentials (key TEXT PRIMARY KEY, value BLOB NOT NULL, "
            "updated_at TEXT DEFAULT (datetime('now')))"
        )
        conn.commit()
        conn.close()

        result = creds_module.read_sqlite_credentials(db_path)
        assert result == []

    def test_no_credentials_table_returns_empty(self, creds_module, tmp_path):
        """Database without credentials table returns empty list."""
        db_path = tmp_path / "nocreds.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE other_table (id INTEGER)")
        conn.commit()
        conn.close()

        result = creds_module.read_sqlite_credentials(db_path)
        assert result == []

    def test_preserves_binary_values(self, creds_module, tmp_path):
        """Binary blob values are preserved byte-for-byte."""
        db_path = tmp_path / "binary.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "CREATE TABLE credentials (key TEXT PRIMARY KEY, value BLOB NOT NULL, "
            "updated_at TEXT DEFAULT (datetime('now')))"
        )
        # Insert a realistic Fernet-encrypted value (just random bytes for test)
        binary_value = b"\x80\x01\x02\x03\xff\xfe\xfd" * 20
        conn.execute(
            "INSERT INTO credentials (key, value) VALUES (?, ?)",
            ("binary.test", binary_value),
        )
        conn.commit()
        conn.close()

        result = creds_module.read_sqlite_credentials(db_path)
        assert len(result) == 1
        assert result[0][0] == "binary.test"
        assert result[0][1] == binary_value


# ── Sessions Migration Tests ─────────────────────────────────────────────────────


class TestMigrateSessionsRead:
    """Test reading sessions from JSON."""

    def test_missing_file_returns_empty(self, sessions_module, tmp_path):
        """Missing sessions.json returns empty dict."""
        result = sessions_module.read_sessions(tmp_path / "missing.json")
        assert result == {}

    def test_reads_valid_json(self, sessions_module, tmp_path):
        """Valid sessions.json is parsed correctly."""
        sessions_file = tmp_path / "sessions.json"
        data = {
            "123456789": {
                "voice_channel_id": 987654321,
                "text_channel_id": 111222333,
                "current": {"title": "Test Song", "url": "http://example.com"},
                "queue": [],
                "auto_resume": True,
            }
        }
        sessions_file.write_text(json.dumps(data))

        result = sessions_module.read_sessions(sessions_file)
        assert "123456789" in result
        assert result["123456789"]["voice_channel_id"] == 987654321

    def test_malformed_json_returns_empty(self, sessions_module, tmp_path):
        """Malformed JSON file returns empty dict with warning."""
        sessions_file = tmp_path / "bad.json"
        sessions_file.write_text("{not valid json")

        result = sessions_module.read_sessions(sessions_file)
        assert result == {}

    def test_empty_file_returns_empty(self, sessions_module, tmp_path):
        """Empty JSON object returns empty dict."""
        sessions_file = tmp_path / "empty.json"
        sessions_file.write_text("{}")

        result = sessions_module.read_sessions(sessions_file)
        assert result == {}

    def test_non_dict_toplevel_returns_empty(self, sessions_module, tmp_path):
        """Non-dict top-level (e.g., list) returns empty dict."""
        sessions_file = tmp_path / "array.json"
        sessions_file.write_text("[1, 2, 3]")

        result = sessions_module.read_sessions(sessions_file)
        assert result == {}


# ── Playlists Migration Tests ─────────────────────────────────────────────────────


class TestMigratePlaylistsRead:
    """Test reading playlists from JSON."""

    def test_missing_file_returns_empty(self, playlists_module, tmp_path):
        """Missing playlists.json returns empty dict."""
        result = playlists_module.read_playlists(tmp_path / "missing.json")
        assert result == {}

    def test_reads_valid_json(self, playlists_module, tmp_path):
        """Valid playlists.json is parsed correctly."""
        playlists_file = tmp_path / "playlists.json"
        data = {
            "123456789": {
                "My Playlist": {
                    "tracks": [{"title": "Song A", "url": "http://a.com"}],
                    "created_by": "111222333",
                    "created_at": "2024-01-01T00:00:00+00:00",
                    "visibility": "public",
                    "description": "Test playlist",
                }
            }
        }
        playlists_file.write_text(json.dumps(data))

        result = playlists_module.read_playlists(playlists_file)
        assert "123456789" in result
        assert "My Playlist" in result["123456789"]
        assert len(result["123456789"]["My Playlist"]["tracks"]) == 1

    def test_malformed_json_returns_empty(self, playlists_module, tmp_path):
        """Malformed JSON returns empty dict."""
        playlists_file = tmp_path / "bad.json"
        playlists_file.write_text("{{invalid}}")

        result = playlists_module.read_playlists(playlists_file)
        assert result == {}

    def test_non_dict_toplevel_returns_empty(self, playlists_module, tmp_path):
        """Non-dict top-level returns empty dict."""
        playlists_file = tmp_path / "array.json"
        playlists_file.write_text("[]")

        result = playlists_module.read_playlists(playlists_file)
        assert result == {}


class TestMigratePlaylistsTimestamp:
    """Test timestamp parsing utility."""

    def test_valid_iso_timestamp(self, playlists_module):
        """Valid ISO timestamp is parsed correctly."""
        dt = playlists_module._parse_timestamp("2024-06-15T10:30:00+00:00")
        assert dt is not None
        assert dt.year == 2024
        assert dt.month == 6

    def test_naive_timestamp_gets_utc(self, playlists_module):
        """Naive timestamp (no timezone) gets UTC applied."""
        dt = playlists_module._parse_timestamp("2024-06-15T10:30:00")
        assert dt is not None
        assert dt.tzinfo is not None

    def test_none_returns_none(self, playlists_module):
        """None input returns None."""
        assert playlists_module._parse_timestamp(None) is None

    def test_empty_string_returns_none(self, playlists_module):
        """Empty string returns None."""
        assert playlists_module._parse_timestamp("") is None

    def test_invalid_format_returns_none(self, playlists_module):
        """Invalid format returns None."""
        assert playlists_module._parse_timestamp("not-a-date") is None


class TestMigrateScriptDefaults:
    """Test default configuration values across migration scripts."""

    def test_credentials_default_pg_uri(self, creds_module):
        """Credentials script has correct default PG URI."""
        assert "postgresql-rw.postgresql-service.svc.cluster.local" in creds_module.DEFAULT_PG_URI
        assert "hellodj" in creds_module.DEFAULT_PG_URI

    def test_sessions_default_pg_uri(self, sessions_module):
        """Sessions script has correct default PG URI."""
        assert "postgresql-rw.postgresql-service.svc.cluster.local" in sessions_module.DEFAULT_PG_URI

    def test_playlists_default_pg_uri(self, playlists_module):
        """Playlists script has correct default PG URI."""
        assert "postgresql-rw.postgresql-service.svc.cluster.local" in playlists_module.DEFAULT_PG_URI

    def test_system_tenant_id_is_valid_uuid(self, sessions_module):
        """Default system tenant ID is a valid UUID."""
        import uuid
        uuid.UUID(sessions_module.SYSTEM_TENANT_ID)  # Raises if invalid

    def test_playlists_system_tenant_id_matches_sessions(self, sessions_module, playlists_module):
        """Both scripts use the same default system tenant ID."""
        assert sessions_module.SYSTEM_TENANT_ID == playlists_module.SYSTEM_TENANT_ID

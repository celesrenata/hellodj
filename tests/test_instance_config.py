"""Tests for bot/playback/instance_config.py — multi-instance credential helpers."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

# Ensure the bot directory is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bot"))

# Set a test encryption key before importing anything that touches creds
os.environ.setdefault("HELLODJ_DB_KEY", "test-key-for-instance-config-tests-only")


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    """Use a temporary database for each test."""
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("DATA_DIR", str(tmp_path))

    # Force re-import of credentials with the new DATA_DIR
    import credentials

    credentials.DB_PATH = db_path
    credentials.creds = credentials.CredentialStore(db_path)

    # Also reset the config module's cached creds reference
    import config

    config._creds = None

    yield


class TestGetInstanceCount:
    def test_default_zero_when_unset(self):
        from playback.instance_config import get_instance_count

        assert get_instance_count() == 0

    def test_reads_stored_value(self):
        from config import cfg
        from playback.instance_config import get_instance_count

        cfg.set("playback.instance_count", "3")
        assert get_instance_count() == 3

    def test_invalid_value_returns_zero(self):
        from config import cfg
        from playback.instance_config import get_instance_count

        cfg.set("playback.instance_count", "not-a-number")
        assert get_instance_count() == 0


class TestSetInstanceCount:
    def test_stores_valid_count(self):
        from config import cfg
        from playback.instance_config import set_instance_count

        set_instance_count(5)
        assert cfg("playback.instance_count") == "5"

    def test_stores_zero(self):
        from config import cfg
        from playback.instance_config import set_instance_count

        set_instance_count(0)
        assert cfg("playback.instance_count") == "0"

    def test_stores_ten(self):
        from config import cfg
        from playback.instance_config import set_instance_count

        set_instance_count(10)
        assert cfg("playback.instance_count") == "10"

    def test_rejects_negative(self):
        from playback.instance_config import set_instance_count

        with pytest.raises(ValueError, match="0–10"):
            set_instance_count(-1)

    def test_rejects_over_ten(self):
        from playback.instance_config import set_instance_count

        with pytest.raises(ValueError, match="0–10"):
            set_instance_count(11)


class TestGetInstanceCredentials:
    def test_returns_none_when_no_token(self):
        from playback.instance_config import get_instance_credentials

        assert get_instance_credentials(0) is None

    def test_returns_credentials_when_stored(self):
        from config import cfg
        from playback.instance_config import get_instance_credentials

        cfg.set("instance.0.token", "Bot FAKE_TOKEN")
        cfg.set("instance.0.app_id", "123456789")
        cfg.set("instance.0.name", "HelloDJ #2")

        cred = get_instance_credentials(0)
        assert cred is not None
        assert cred["token"] == "Bot FAKE_TOKEN"
        assert cred["app_id"] == "123456789"
        assert cred["name"] == "HelloDJ #2"

    def test_defaults_name_when_missing(self):
        from config import cfg
        from playback.instance_config import get_instance_credentials

        cfg.set("instance.2.token", "Bot TOKEN")
        cfg.set("instance.2.app_id", "999")

        cred = get_instance_credentials(2)
        assert cred is not None
        assert cred["name"] == "HelloDJ #4"  # index 2 → #4

    def test_defaults_app_id_to_empty_string(self):
        from config import cfg
        from playback.instance_config import get_instance_credentials

        cfg.set("instance.0.token", "Bot TOKEN")

        cred = get_instance_credentials(0)
        assert cred is not None
        assert cred["app_id"] == ""


class TestSetInstanceCredentials:
    def test_stores_all_fields(self):
        from config import cfg
        from playback.instance_config import set_instance_credentials

        set_instance_credentials(
            1, token="Bot ABC123", app_id="987654321", name="DJ Helper"
        )

        assert cfg("instance.1.token") == "Bot ABC123"
        assert cfg("instance.1.app_id") == "987654321"
        assert cfg("instance.1.name") == "DJ Helper"

    def test_rejects_empty_token(self):
        from playback.instance_config import set_instance_credentials

        with pytest.raises(ValueError, match="token"):
            set_instance_credentials(0, token="", app_id="123", name="Test")

    def test_rejects_empty_app_id(self):
        from playback.instance_config import set_instance_credentials

        with pytest.raises(ValueError, match="app_id"):
            set_instance_credentials(0, token="Bot X", app_id="", name="Test")

    def test_defaults_name_when_empty(self):
        from config import cfg
        from playback.instance_config import set_instance_credentials

        set_instance_credentials(3, token="Bot X", app_id="123", name="")

        assert cfg("instance.3.name") == "HelloDJ #5"  # index 3 → #5


class TestRemoveInstanceCredentials:
    def test_removes_all_keys(self):
        from config import cfg
        from playback.instance_config import (
            remove_instance_credentials,
            set_instance_credentials,
        )

        set_instance_credentials(0, token="Bot T", app_id="A", name="N")
        assert cfg("instance.0.token") == "Bot T"

        remove_instance_credentials(0)

        assert cfg("instance.0.token") is None
        assert cfg("instance.0.app_id") is None
        assert cfg("instance.0.name") is None

    def test_no_error_when_not_present(self):
        from playback.instance_config import remove_instance_credentials

        # Should not raise
        remove_instance_credentials(99)


class TestLegacyVideoEnabled:
    def test_default_true_when_unset(self):
        from playback.instance_config import is_legacy_video_enabled

        assert is_legacy_video_enabled() is True

    def test_reads_true(self):
        from config import cfg
        from playback.instance_config import is_legacy_video_enabled

        cfg.set("playback.legacy_video_enabled", "true")
        assert is_legacy_video_enabled() is True

    def test_reads_false(self):
        from config import cfg
        from playback.instance_config import is_legacy_video_enabled

        cfg.set("playback.legacy_video_enabled", "false")
        assert is_legacy_video_enabled() is False


class TestSetLegacyVideoEnabled:
    def test_set_true(self):
        from config import cfg
        from playback.instance_config import set_legacy_video_enabled

        set_legacy_video_enabled(True)
        assert cfg("playback.legacy_video_enabled") == "true"

    def test_set_false(self):
        from config import cfg
        from playback.instance_config import set_legacy_video_enabled

        set_legacy_video_enabled(False)
        assert cfg("playback.legacy_video_enabled") == "false"


class TestListAllInstances:
    def test_empty_when_count_zero(self):
        from playback.instance_config import list_all_instances

        assert list_all_instances() == []

    def test_returns_configured_instances(self):
        from config import cfg
        from playback.instance_config import list_all_instances, set_instance_credentials

        set_instance_credentials(0, token="Bot A", app_id="111", name="DJ A")
        set_instance_credentials(1, token="Bot B", app_id="222", name="DJ B")
        cfg.set("playback.instance_count", "2")

        instances = list_all_instances()
        assert len(instances) == 2
        assert instances[0]["name"] == "DJ A"
        assert instances[1]["name"] == "DJ B"

    def test_skips_unconfigured_indices(self):
        from config import cfg
        from playback.instance_config import list_all_instances, set_instance_credentials

        # Only set index 0, skip index 1
        set_instance_credentials(0, token="Bot A", app_id="111", name="DJ A")
        cfg.set("playback.instance_count", "2")

        instances = list_all_instances()
        assert len(instances) == 1
        assert instances[0]["name"] == "DJ A"

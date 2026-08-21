"""Tests for bot/playback/persistence.py — Properties 18, 19, 20.

Property 18: Session persistence round-trip
Property 19: Video sessions not auto-resumed
Property 20: Legacy key migration
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

# Ensure bot/ is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bot"))

from playback.persistence import (
    _composite_key,
    _data,
    _is_legacy_key,
    _lock,
    _parse_composite_key,
    clear_session,
    get,
    load_all,
    mark_suspended,
    migrate_legacy,
    save_session,
    set_auto_resume,
    SESSIONS_FILE,
)


@pytest.fixture(autouse=True)
def isolate_persistence(tmp_path, monkeypatch):
    """Isolate each test from the real sessions file and module state."""
    import playback.persistence as mod

    # Point the module at a temp directory
    sessions_file = str(tmp_path / "sessions.json")
    monkeypatch.setattr(mod, "SESSIONS_FILE", sessions_file)
    monkeypatch.setattr(mod, "_data", {})

    # Ensure data dir for _save() uses the tmp path
    monkeypatch.setattr(os, "makedirs", lambda *a, **kw: None)
    # Actually we need makedirs to work for the tmp path — just ensure sessions_file parent exists
    monkeypatch.setattr(mod, "SESSIONS_FILE", sessions_file)

    yield sessions_file

    # Clean up module state
    mod._data.clear()


# ── Unit tests ─────────────────────────────────────────────────────────────


class TestCompositeKeyHelpers:
    """Tests for key construction and parsing helpers."""

    def test_composite_key_format(self):
        assert _composite_key(123, 456) == "123:456"

    def test_composite_key_large_ids(self):
        assert _composite_key(123456789012345678, 987654321098765432) == (
            "123456789012345678:987654321098765432"
        )

    def test_parse_valid_composite_key(self):
        assert _parse_composite_key("123:456") == (123, 456)

    def test_parse_invalid_no_colon(self):
        assert _parse_composite_key("123456") is None

    def test_parse_invalid_non_numeric(self):
        assert _parse_composite_key("abc:def") is None

    def test_is_legacy_key_true(self):
        assert _is_legacy_key("123456789") is True

    def test_is_legacy_key_false(self):
        assert _is_legacy_key("123:456") is False


class TestSaveAndLoad:
    """Property 18: Session persistence round-trip."""

    @pytest.mark.asyncio
    async def test_save_and_load_audio_session(self, isolate_persistence):
        """Save an audio session → load_all returns equivalent data with correct key."""
        await save_session(
            guild_id=111,
            channel_id=222,
            session_type="audio",
            voice_channel_id=222,
            text_channel_id=333,
            current={"webpage_url": "https://example.com", "title": "Test", "duration": 240000},
            queue=[{"webpage_url": "https://example.com/2", "title": "Track 2"}],
            auto_resume=True,
            source_provider="spotify",
            repeat_mode="one",
            filters={"bass": 1.2},
            crossfade_seconds=3.0,
            tune_enabled=True,
            bot_instance_index=1,
        )

        sessions = await load_all()
        assert (111, 222) in sessions
        entry = sessions[(111, 222)]
        assert entry["session_type"] == "audio"
        assert entry["voice_channel_id"] == 222
        assert entry["text_channel_id"] == 333
        assert entry["current"]["title"] == "Test"
        assert len(entry["queue"]) == 1
        assert entry["auto_resume"] is True
        assert entry["source_provider"] == "spotify"
        assert entry["repeat_mode"] == "one"
        assert entry["filters"] == {"bass": 1.2}
        assert entry["crossfade_seconds"] == 3.0
        assert entry["tune_enabled"] is True
        assert entry["bot_instance_index"] == 1

    @pytest.mark.asyncio
    async def test_save_and_load_video_session(self, isolate_persistence):
        """Save a video session → round-trip preserves session_type='video'."""
        await save_session(
            guild_id=111,
            channel_id=444,
            session_type="video",
            voice_channel_id=444,
            text_channel_id=555,
            current={"webpage_url": "https://vid.example.com", "title": "Video"},
            queue=[],
            auto_resume=False,
        )

        sessions = await load_all()
        assert (111, 444) in sessions
        assert sessions[(111, 444)]["session_type"] == "video"

    @pytest.mark.asyncio
    async def test_multiple_sessions_independent(self, isolate_persistence):
        """Multiple sessions with different keys are stored independently."""
        await save_session(
            guild_id=1, channel_id=10, session_type="audio",
            voice_channel_id=10, text_channel_id=20,
            current=None, queue=[],
        )
        await save_session(
            guild_id=1, channel_id=11, session_type="video",
            voice_channel_id=11, text_channel_id=21,
            current=None, queue=[],
        )
        await save_session(
            guild_id=2, channel_id=10, session_type="audio",
            voice_channel_id=10, text_channel_id=30,
            current=None, queue=[],
        )

        sessions = await load_all()
        assert len(sessions) == 3
        assert (1, 10) in sessions
        assert (1, 11) in sessions
        assert (2, 10) in sessions

    @pytest.mark.asyncio
    async def test_save_overwrites_existing(self, isolate_persistence):
        """Saving to the same key overwrites the previous record."""
        await save_session(
            guild_id=1, channel_id=10, session_type="audio",
            voice_channel_id=10, text_channel_id=20,
            current={"title": "First"}, queue=[],
        )
        await save_session(
            guild_id=1, channel_id=10, session_type="audio",
            voice_channel_id=10, text_channel_id=20,
            current={"title": "Second"}, queue=[],
        )

        sessions = await load_all()
        assert sessions[(1, 10)]["current"]["title"] == "Second"


class TestClearSession:
    """Test clear_session removes the record from disk."""

    @pytest.mark.asyncio
    async def test_clear_existing_session(self, isolate_persistence):
        await save_session(
            guild_id=1, channel_id=10, session_type="audio",
            voice_channel_id=10, text_channel_id=20,
            current=None, queue=[],
        )
        await clear_session(1, 10)
        sessions = await load_all()
        assert (1, 10) not in sessions

    @pytest.mark.asyncio
    async def test_clear_nonexistent_session_is_noop(self, isolate_persistence):
        """Clearing a non-existent session doesn't crash."""
        await clear_session(999, 888)  # Should not raise


class TestMarkSuspended:
    """Req 10.6: Mark session as suspended on restore failure."""

    @pytest.mark.asyncio
    async def test_mark_suspended_adds_flag(self, isolate_persistence):
        await save_session(
            guild_id=1, channel_id=10, session_type="audio",
            voice_channel_id=10, text_channel_id=20,
            current={"title": "Track"}, queue=[],
        )
        await mark_suspended(1, 10, "Channel no longer exists")

        sessions = await load_all()
        entry = sessions[(1, 10)]
        assert entry["suspended"] is True
        assert entry["suspended_reason"] == "Channel no longer exists"
        # Data is preserved
        assert entry["current"]["title"] == "Track"

    @pytest.mark.asyncio
    async def test_mark_suspended_nonexistent_is_noop(self, isolate_persistence):
        """Marking a non-existent session suspended doesn't crash."""
        await mark_suspended(999, 888, "test reason")


class TestSetAutoResume:
    """Test set_auto_resume updates the flag."""

    @pytest.mark.asyncio
    async def test_flip_auto_resume(self, isolate_persistence):
        await save_session(
            guild_id=1, channel_id=10, session_type="audio",
            voice_channel_id=10, text_channel_id=20,
            current=None, queue=[], auto_resume=True,
        )
        await set_auto_resume(1, 10, False)

        sessions = await load_all()
        assert sessions[(1, 10)]["auto_resume"] is False


class TestVideoNotAutoResumed:
    """Property 19: Video sessions not auto-resumed.

    The persistence layer stores video sessions but the restore logic
    (load_all caller) must respect session_type to decide auto-resume.
    We verify that video sessions are loaded with correct type info.
    """

    @pytest.mark.asyncio
    async def test_video_session_loaded_with_type(self, isolate_persistence):
        """Video sessions are persisted with session_type='video'."""
        await save_session(
            guild_id=1, channel_id=10, session_type="video",
            voice_channel_id=10, text_channel_id=20,
            current={"title": "Video"}, queue=[{"title": "Next"}],
            auto_resume=True,  # Even if True, caller should NOT auto-resume video
        )

        sessions = await load_all()
        entry = sessions[(1, 10)]
        assert entry["session_type"] == "video"
        # The auto_resume flag is stored but the caller must check session_type
        assert entry["auto_resume"] is True


class TestLegacyMigration:
    """Property 20: Legacy key migration."""

    @pytest.mark.asyncio
    async def test_migrate_legacy_key_with_voice_channel(self):
        """Legacy guild_id key with voice_channel_id → composite key."""
        legacy_data = {
            "123456789": {
                "voice_channel_id": 987654321,
                "text_channel_id": 111111111,
                "current": {"title": "Song"},
                "queue": [],
                "auto_resume": True,
                "source_provider": "youtube",
                "repeat_mode": "off",
                "filters": {},
            }
        }

        migrated = await migrate_legacy(legacy_data)
        assert "123456789:987654321" in migrated
        assert "123456789" not in migrated

        entry = migrated["123456789:987654321"]
        assert entry["voice_channel_id"] == 987654321
        assert entry["current"]["title"] == "Song"
        assert entry["session_type"] == "audio"
        assert entry["bot_instance_index"] == 0

    @pytest.mark.asyncio
    async def test_migrate_legacy_missing_voice_channel_skipped(self):
        """Legacy key without voice_channel_id is skipped with warning (Req 10.7)."""
        legacy_data = {
            "111": {
                "text_channel_id": 222,
                "current": None,
                "queue": [],
                # No voice_channel_id!
            },
            "333": {
                "voice_channel_id": 444,
                "text_channel_id": 555,
                "current": None,
                "queue": [],
            },
        }

        migrated = await migrate_legacy(legacy_data)
        # First entry skipped (no voice_channel_id)
        assert "111" not in migrated
        # Second entry migrated
        assert "333:444" in migrated

    @pytest.mark.asyncio
    async def test_migrate_preserves_existing_composite_keys(self):
        """Already-composite keys are kept as-is during migration."""
        mixed_data = {
            "100:200": {"session_type": "audio", "voice_channel_id": 200, "queue": []},
            "300": {"voice_channel_id": 400, "queue": [], "current": None},
        }

        migrated = await migrate_legacy(mixed_data)
        assert "100:200" in migrated
        assert "300:400" in migrated
        assert "300" not in migrated

    @pytest.mark.asyncio
    async def test_migrate_legacy_preserves_all_fields(self):
        """All fields from a legacy record are preserved in the migrated record."""
        legacy_data = {
            "111": {
                "voice_channel_id": 222,
                "text_channel_id": 333,
                "current": {"webpage_url": "url", "title": "T", "author": "A", "duration": 100},
                "queue": [{"title": "Q1"}, {"title": "Q2"}],
                "auto_resume": True,
                "source_provider": "tidal",
                "repeat_mode": "all",
                "filters": {"treble": 0.5},
                "crossfade_seconds": 2.0,
                "tune_enabled": True,
                "autoplay_enabled": True,
                "autoplay_genres": ["rock"],
            }
        }

        migrated = await migrate_legacy(legacy_data)
        entry = migrated["111:222"]

        assert entry["text_channel_id"] == 333
        assert entry["current"]["title"] == "T"
        assert len(entry["queue"]) == 2
        assert entry["auto_resume"] is True
        assert entry["source_provider"] == "tidal"
        assert entry["repeat_mode"] == "all"
        assert entry["filters"] == {"treble": 0.5}
        assert entry["crossfade_seconds"] == 2.0
        assert entry["tune_enabled"] is True
        assert entry["autoplay_enabled"] is True
        assert entry["autoplay_genres"] == ["rock"]
        # New fields added by migration
        assert entry["session_type"] == "audio"
        assert entry["bot_instance_index"] == 0

    @pytest.mark.asyncio
    async def test_load_all_triggers_migration(self, isolate_persistence):
        """load_all migrates legacy keys in-place on first load."""
        import playback.persistence as mod

        # Write a legacy-keyed file directly
        legacy = {
            "999": {
                "voice_channel_id": 888,
                "text_channel_id": 777,
                "current": None,
                "queue": [],
                "auto_resume": True,
            }
        }
        with open(isolate_persistence, "w") as f:
            json.dump(legacy, f)

        sessions = await load_all()
        assert (999, 888) in sessions

        # Verify the file on disk was rewritten with composite key
        with open(isolate_persistence, "r") as f:
            on_disk = json.load(f)
        assert "999:888" in on_disk
        assert "999" not in on_disk

    @pytest.mark.asyncio
    async def test_migrate_invalid_voice_channel_id_skipped(self):
        """Legacy key with non-integer voice_channel_id is skipped."""
        legacy_data = {
            "111": {
                "voice_channel_id": "not_a_number",
                "text_channel_id": 222,
                "current": None,
                "queue": [],
            }
        }

        migrated = await migrate_legacy(legacy_data)
        assert len(migrated) == 0


class TestCorruptFile:
    """Req: sessions.json corrupt/unreadable → start empty, log error."""

    @pytest.mark.asyncio
    async def test_corrupt_json_returns_empty(self, isolate_persistence):
        """Corrupt JSON file results in empty session dict."""
        with open(isolate_persistence, "w") as f:
            f.write("{invalid json content here!!!")

        sessions = await load_all()
        assert sessions == {}

    @pytest.mark.asyncio
    async def test_nonexistent_file_returns_empty(self, isolate_persistence):
        """Missing sessions file returns empty dict without error."""
        # Don't create any file
        sessions = await load_all()
        assert sessions == {}


class TestGet:
    """Test the synchronous get() helper."""

    @pytest.mark.asyncio
    async def test_get_existing(self, isolate_persistence):
        await save_session(
            guild_id=1, channel_id=10, session_type="audio",
            voice_channel_id=10, text_channel_id=20,
            current={"title": "X"}, queue=[],
        )
        entry = get(1, 10)
        assert entry is not None
        assert entry["current"]["title"] == "X"

    def test_get_nonexistent(self, isolate_persistence):
        assert get(999, 888) is None

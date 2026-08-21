"""Tests for the track-start callback in player.py (Task 5.6).

Verifies the fire-and-forget callback pattern that wires player.py
track events to the VisualizerManager without breaking audio independence.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Ensure the bot directory is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bot"))

import player


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_callback():
    """Reset the module-level callback before and after each test."""
    player._on_track_start_callback = None
    yield
    player._on_track_start_callback = None


@pytest.fixture
def mock_track():
    """Create a mock wavelink.Playable track."""
    track = MagicMock()
    track.title = "Test Song"
    track.author = "Test Artist"
    track.length = 240000
    track.artwork = "https://example.com/art.jpg"
    return track


@pytest.fixture
def mock_player():
    """Create a mock wavelink.Player."""
    p = MagicMock()
    p.guild = MagicMock()
    p.guild.id = 12345
    return p


# ---------------------------------------------------------------------------
# Tests: set_on_track_start_callback
# ---------------------------------------------------------------------------


class TestSetOnTrackStartCallback:
    """Tests for the callback registration function."""

    def test_registers_callback(self):
        cb = AsyncMock()
        player.set_on_track_start_callback(cb)
        assert player._on_track_start_callback is cb

    def test_replaces_existing_callback(self):
        cb1 = AsyncMock()
        cb2 = AsyncMock()
        player.set_on_track_start_callback(cb1)
        player.set_on_track_start_callback(cb2)
        assert player._on_track_start_callback is cb2

    def test_can_set_to_none(self):
        player.set_on_track_start_callback(AsyncMock())
        player.set_on_track_start_callback(None)
        assert player._on_track_start_callback is None


# ---------------------------------------------------------------------------
# Tests: callback invocation in on_track_start
# ---------------------------------------------------------------------------


class TestTrackStartCallbackInvocation:
    """Tests for callback invocation within on_track_start."""

    @pytest.mark.asyncio
    async def test_callback_called_with_metadata(self, mock_track, mock_player):
        """Callback receives guild_id and metadata dict."""
        cb = AsyncMock()
        player.set_on_track_start_callback(cb)

        guild_id = 12345
        # Set up guild state with current track entry
        state = player.get_state(guild_id)
        state["current"] = {
            "title": "My Track",
            "author": "My Artist",
            "artwork_url": "https://art.example.com/cover.png",
            "duration": 180000,
        }

        with patch.object(player, "_send_now_playing", new_callable=AsyncMock), \
             patch.object(player, "_now_playing_updater", new_callable=AsyncMock), \
             patch.object(player, "persist"):
            await player.on_track_start(guild_id, mock_player, mock_track)

        cb.assert_awaited_once()
        call_args = cb.await_args[0]
        assert call_args[0] == guild_id
        metadata = call_args[1]
        assert metadata["title"] == "My Track"
        assert metadata["artist"] == "My Artist"
        assert metadata["artwork_url"] == "https://art.example.com/cover.png"
        assert metadata["duration_ms"] == 180000
        assert metadata["position_ms"] == 0

    @pytest.mark.asyncio
    async def test_callback_not_called_when_none(self, mock_track, mock_player):
        """No crash when callback is not set."""
        guild_id = 99999
        state = player.get_state(guild_id)
        state["current"] = {"title": "Track", "author": "Artist", "duration": 60000}

        with patch.object(player, "_send_now_playing", new_callable=AsyncMock), \
             patch.object(player, "_now_playing_updater", new_callable=AsyncMock), \
             patch.object(player, "persist"):
            # Should not raise
            await player.on_track_start(guild_id, mock_player, mock_track)

    @pytest.mark.asyncio
    async def test_callback_exception_swallowed(self, mock_track, mock_player):
        """Callback exceptions don't propagate — audio independence."""
        cb = AsyncMock(side_effect=RuntimeError("visualizer crash"))
        player.set_on_track_start_callback(cb)

        guild_id = 77777
        state = player.get_state(guild_id)
        state["current"] = {"title": "X", "author": "Y", "duration": 5000}

        with patch.object(player, "_send_now_playing", new_callable=AsyncMock), \
             patch.object(player, "_now_playing_updater", new_callable=AsyncMock), \
             patch.object(player, "persist"):
            # Should NOT raise despite callback crashing
            await player.on_track_start(guild_id, mock_player, mock_track)

        cb.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_metadata_fallback_to_track_attrs(self, mock_track, mock_player):
        """When state['current'] is empty, falls back to track attributes."""
        cb = AsyncMock()
        player.set_on_track_start_callback(cb)

        guild_id = 55555
        state = player.get_state(guild_id)
        state["current"] = {}  # empty entry

        with patch.object(player, "_send_now_playing", new_callable=AsyncMock), \
             patch.object(player, "_now_playing_updater", new_callable=AsyncMock), \
             patch.object(player, "persist"):
            await player.on_track_start(guild_id, mock_player, mock_track)

        metadata = cb.await_args[0][1]
        assert metadata["title"] == mock_track.title
        assert metadata["artist"] == mock_track.author
        assert metadata["artwork_url"] == mock_track.artwork
        assert metadata["duration_ms"] == mock_track.length
        assert metadata["position_ms"] == 0

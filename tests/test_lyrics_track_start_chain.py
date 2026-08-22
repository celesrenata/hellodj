"""Tests for LyricsService track-start callback chaining (Task 1.10).

Verifies that:
- register_track_start_callback chains the lyrics callback with any existing callback
- Original callback (e.g. VisualizerManager) is called first
- Lyrics callback is called second
- Original callback exceptions do not prevent lyrics callback from running
- Lyrics callback exceptions do not propagate to the caller
- When no original callback is registered, lyrics still runs fine
- Audio independence is maintained (Req 9.5)
"""

from __future__ import annotations

import asyncio
import inspect
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
def reset_state():
    """Reset player callback and lyrics_service module state."""
    player._on_track_start_callback = None
    yield
    player._on_track_start_callback = None


@pytest.fixture
def mock_ws_hub():
    """Create a mock WebSocketHub."""
    hub = MagicMock()
    hub.broadcast_from_bot = AsyncMock()
    return hub


# ---------------------------------------------------------------------------
# Tests: register_track_start_callback
# ---------------------------------------------------------------------------


class TestRegisterTrackStartCallback:
    """Tests for the callback chaining registration."""

    def test_registers_chained_callback(self, mock_ws_hub):
        """register_track_start_callback sets a callback on player."""
        from video.lyrics_service import init_lyrics_services, register_track_start_callback

        init_lyrics_services(mock_ws_hub)
        register_track_start_callback()

        assert player._on_track_start_callback is not None
        assert inspect.iscoroutinefunction(player._on_track_start_callback)

    def test_captures_existing_callback(self, mock_ws_hub):
        """Existing callback is captured and will be called by the chain."""
        from video.lyrics_service import init_lyrics_services, register_track_start_callback

        original = AsyncMock()
        player.set_on_track_start_callback(original)

        init_lyrics_services(mock_ws_hub)
        register_track_start_callback()

        # The registered callback is NOT the original (it's the chained wrapper)
        assert player._on_track_start_callback is not original

    @pytest.mark.asyncio
    async def test_original_called_first_then_lyrics(self, mock_ws_hub):
        """Original callback runs before lyrics callback."""
        from video.lyrics_service import (
            init_lyrics_services,
            register_track_start_callback,
            _instances,
        )

        call_order = []

        async def original_cb(guild_id, metadata):
            call_order.append("original")

        player.set_on_track_start_callback(original_cb)
        init_lyrics_services(mock_ws_hub)

        # Create a mock LyricsService for the guild
        mock_svc = MagicMock()

        async def mock_on_track_change(guild_id, metadata):
            call_order.append("lyrics")

        mock_svc.on_track_change = mock_on_track_change
        _instances[42] = mock_svc

        register_track_start_callback()

        await player._on_track_start_callback(42, {"title": "Test", "artist": "Artist"})

        assert call_order == ["original", "lyrics"]

        # Cleanup
        _instances.pop(42, None)

    @pytest.mark.asyncio
    async def test_original_exception_does_not_block_lyrics(self, mock_ws_hub):
        """Original callback crash does NOT prevent lyrics from running."""
        from video.lyrics_service import (
            init_lyrics_services,
            register_track_start_callback,
            _instances,
        )

        async def crashing_original(guild_id, metadata):
            raise RuntimeError("visualizer exploded")

        player.set_on_track_start_callback(crashing_original)
        init_lyrics_services(mock_ws_hub)

        lyrics_called = []
        mock_svc = MagicMock()

        async def mock_on_track_change(guild_id, metadata):
            lyrics_called.append(True)

        mock_svc.on_track_change = mock_on_track_change
        _instances[99] = mock_svc

        register_track_start_callback()

        # Should NOT raise
        await player._on_track_start_callback(99, {"title": "T", "artist": "A"})

        assert lyrics_called == [True]

        # Cleanup
        _instances.pop(99, None)

    @pytest.mark.asyncio
    async def test_lyrics_exception_does_not_propagate(self, mock_ws_hub):
        """Lyrics callback crash does NOT propagate to the caller."""
        from video.lyrics_service import (
            init_lyrics_services,
            register_track_start_callback,
            _instances,
        )

        original_called = []

        async def original_cb(guild_id, metadata):
            original_called.append(True)

        player.set_on_track_start_callback(original_cb)
        init_lyrics_services(mock_ws_hub)

        mock_svc = MagicMock()

        async def crashing_lyrics(guild_id, metadata):
            raise RuntimeError("lyrics service exploded")

        mock_svc.on_track_change = crashing_lyrics
        _instances[77] = mock_svc

        register_track_start_callback()

        # Should NOT raise
        await player._on_track_start_callback(77, {"title": "T", "artist": "A"})

        # Original still ran successfully
        assert original_called == [True]

        # Cleanup
        _instances.pop(77, None)

    @pytest.mark.asyncio
    async def test_no_original_callback_lyrics_still_runs(self, mock_ws_hub):
        """When no original callback is registered, lyrics still runs fine."""
        from video.lyrics_service import (
            init_lyrics_services,
            register_track_start_callback,
            _instances,
        )

        # No original callback set (player._on_track_start_callback is None)
        init_lyrics_services(mock_ws_hub)

        lyrics_called = []
        mock_svc = MagicMock()

        async def mock_on_track_change(guild_id, metadata):
            lyrics_called.append(True)

        mock_svc.on_track_change = mock_on_track_change
        _instances[55] = mock_svc

        register_track_start_callback()

        await player._on_track_start_callback(55, {"title": "T", "artist": "A"})

        assert lyrics_called == [True]

        # Cleanup
        _instances.pop(55, None)

    @pytest.mark.asyncio
    async def test_both_callbacks_crash_no_propagation(self, mock_ws_hub):
        """Both original and lyrics crash — nothing propagates to caller."""
        from video.lyrics_service import (
            init_lyrics_services,
            register_track_start_callback,
            _instances,
        )

        async def crashing_original(guild_id, metadata):
            raise ValueError("original boom")

        player.set_on_track_start_callback(crashing_original)
        init_lyrics_services(mock_ws_hub)

        mock_svc = MagicMock()

        async def crashing_lyrics(guild_id, metadata):
            raise TypeError("lyrics boom")

        mock_svc.on_track_change = crashing_lyrics
        _instances[33] = mock_svc

        register_track_start_callback()

        # Neither exception should propagate
        await player._on_track_start_callback(33, {"title": "T", "artist": "A"})

        # Cleanup
        _instances.pop(33, None)

    @pytest.mark.asyncio
    async def test_metadata_forwarded_to_both(self, mock_ws_hub):
        """Both callbacks receive the same guild_id and metadata."""
        from video.lyrics_service import (
            init_lyrics_services,
            register_track_start_callback,
            _instances,
        )

        original_args = []

        async def original_cb(guild_id, metadata):
            original_args.append((guild_id, metadata))

        player.set_on_track_start_callback(original_cb)
        init_lyrics_services(mock_ws_hub)

        lyrics_args = []
        mock_svc = MagicMock()

        async def mock_on_track_change(guild_id, metadata):
            lyrics_args.append((guild_id, metadata))

        mock_svc.on_track_change = mock_on_track_change
        _instances[101] = mock_svc

        register_track_start_callback()

        test_metadata = {
            "title": "Bohemian Rhapsody",
            "artist": "Queen",
            "duration_ms": 355000,
            "artwork_url": "https://example.com/art.jpg",
        }

        await player._on_track_start_callback(101, test_metadata)

        assert original_args == [(101, test_metadata)]
        assert lyrics_args == [(101, test_metadata)]

        # Cleanup
        _instances.pop(101, None)

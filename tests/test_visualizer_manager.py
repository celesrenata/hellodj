"""Tests for the VisualizerManager state machine.

Covers state transitions, event handlers, engine lifecycle, and
the suspension debounce mechanism.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Ensure the bot directory is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bot"))

from video.visualizer_manager import VisualizerManager, VisualizerState


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_ws_hub():
    """Create a mock WebSocketHub with required methods."""
    hub = MagicMock()
    hub.broadcast = AsyncMock()
    hub.viewer_count = MagicMock(return_value=0)
    return hub


@pytest.fixture
def manager(mock_ws_hub):
    """Create a VisualizerManager in default state (DISABLED)."""
    with patch("video.visualizer_manager.guild_settings") as mock_gs:
        mock_gs.get_visualizer_engine.return_value = "dvd"
        mock_gs.VALID_VISUALIZER_ENGINES = {
            "dvd", "projectm", "vgalizer", "varda", "fosfora",
            "audiovis", "native", "random", "off",
        }
        mock_gs.set_visualizer_engine = MagicMock()
        mgr = VisualizerManager(
            guild_id=12345,
            ws_hub=mock_ws_hub,
            bot_avatar_url="https://example.com/avatar.png",
        )
    # Patch guild_settings on the manager's module for set_engine calls
    mgr._guild_settings_mock = mock_gs
    return mgr


# ---------------------------------------------------------------------------
# Unit Tests — Initial State
# ---------------------------------------------------------------------------


class TestInitialState:
    """VisualizerManager starts in DISABLED state."""

    def test_starts_disabled(self, manager):
        assert manager.state == VisualizerState.DISABLED

    def test_stores_guild_id(self, manager):
        assert manager.guild_id == 12345

    def test_engine_type_loaded_from_settings(self, manager):
        assert manager._engine_type == "dvd"

    def test_no_engine_instance_initially(self, manager):
        assert manager._engine is None


# ---------------------------------------------------------------------------
# Unit Tests — on_video_start (ANY → DISABLED)
# ---------------------------------------------------------------------------


class TestOnVideoStart:
    """on_video_start transitions any state to DISABLED."""

    @pytest.mark.asyncio
    async def test_from_disabled(self, manager):
        manager.state = VisualizerState.DISABLED
        await manager.on_video_start()
        assert manager.state == VisualizerState.DISABLED

    @pytest.mark.asyncio
    async def test_from_idle(self, manager):
        manager.state = VisualizerState.IDLE_NO_VIEWERS
        await manager.on_video_start()
        assert manager.state == VisualizerState.DISABLED

    @pytest.mark.asyncio
    async def test_from_active(self, manager, mock_ws_hub):
        manager.state = VisualizerState.ACTIVE
        await manager.on_video_start()
        assert manager.state == VisualizerState.DISABLED

    @pytest.mark.asyncio
    async def test_from_starting(self, manager):
        manager.state = VisualizerState.STARTING
        await manager.on_video_start()
        assert manager.state == VisualizerState.DISABLED

    @pytest.mark.asyncio
    async def test_from_suspending(self, manager):
        manager.state = VisualizerState.SUSPENDING
        await manager.on_video_start()
        assert manager.state == VisualizerState.DISABLED

    @pytest.mark.asyncio
    async def test_from_error(self, manager):
        manager.state = VisualizerState.ERROR
        await manager.on_video_start()
        assert manager.state == VisualizerState.DISABLED

    @pytest.mark.asyncio
    async def test_stops_running_engine(self, manager):
        engine = AsyncMock()
        manager._engine = engine
        manager.state = VisualizerState.ACTIVE
        await manager.on_video_start()
        engine.stop.assert_awaited_once()
        assert manager._engine is None


# ---------------------------------------------------------------------------
# Unit Tests — on_video_end
# ---------------------------------------------------------------------------


class TestOnVideoEnd:
    """on_video_end transitions to IDLE_NO_VIEWERS when engine is not 'off'."""

    @pytest.mark.asyncio
    async def test_transitions_to_idle(self, manager):
        manager.state = VisualizerState.DISABLED
        manager._engine_type = "dvd"
        await manager.on_video_end()
        assert manager.state == VisualizerState.IDLE_NO_VIEWERS

    @pytest.mark.asyncio
    async def test_stays_disabled_when_off(self, manager):
        manager.state = VisualizerState.DISABLED
        manager._engine_type = "off"
        await manager.on_video_end()
        assert manager.state == VisualizerState.DISABLED


# ---------------------------------------------------------------------------
# Unit Tests — on_viewer_join
# ---------------------------------------------------------------------------


class TestOnViewerJoin:
    """on_viewer_join transitions from IDLE_NO_VIEWERS to ACTIVE."""

    @pytest.mark.asyncio
    async def test_idle_to_active(self, manager, mock_ws_hub):
        manager.state = VisualizerState.IDLE_NO_VIEWERS
        await manager.on_viewer_join()
        assert manager.state == VisualizerState.ACTIVE

    @pytest.mark.asyncio
    async def test_broadcasts_visualizer_state(self, manager, mock_ws_hub):
        manager.state = VisualizerState.IDLE_NO_VIEWERS
        await manager.on_viewer_join()
        mock_ws_hub.broadcast.assert_awaited()
        call_args = mock_ws_hub.broadcast.call_args
        message = call_args[0][1]
        assert message["type"] == "visualizer"
        assert message["state"] == "active"
        assert message["engine"] == "dvd"
        assert "config" in message

    @pytest.mark.asyncio
    async def test_suspending_to_active(self, manager):
        manager.state = VisualizerState.SUSPENDING
        await manager.on_viewer_join()
        assert manager.state == VisualizerState.ACTIVE

    @pytest.mark.asyncio
    async def test_no_action_when_disabled(self, manager):
        manager.state = VisualizerState.DISABLED
        await manager.on_viewer_join()
        assert manager.state == VisualizerState.DISABLED

    @pytest.mark.asyncio
    async def test_no_action_when_already_active(self, manager, mock_ws_hub):
        # Set up as already active with an engine
        manager.state = VisualizerState.ACTIVE
        engine = AsyncMock()
        manager._engine = engine
        await manager.on_viewer_join()
        assert manager.state == VisualizerState.ACTIVE


# ---------------------------------------------------------------------------
# Unit Tests — on_viewer_leave
# ---------------------------------------------------------------------------


class TestOnViewerLeave:
    """on_viewer_leave begins suspension when last viewer leaves."""

    @pytest.mark.asyncio
    async def test_active_to_suspending(self, manager, mock_ws_hub):
        manager.state = VisualizerState.ACTIVE
        mock_ws_hub.viewer_count.return_value = 0
        await manager.on_viewer_leave(viewer_count=0)
        assert manager.state == VisualizerState.SUSPENDING

    @pytest.mark.asyncio
    async def test_no_action_when_viewers_remain(self, manager):
        manager.state = VisualizerState.ACTIVE
        await manager.on_viewer_leave(viewer_count=1)
        assert manager.state == VisualizerState.ACTIVE

    @pytest.mark.asyncio
    async def test_starting_to_idle_when_all_leave(self, manager):
        manager.state = VisualizerState.STARTING
        await manager.on_viewer_leave(viewer_count=0)
        assert manager.state == VisualizerState.IDLE_NO_VIEWERS


# ---------------------------------------------------------------------------
# Unit Tests — on_track_change
# ---------------------------------------------------------------------------


class TestOnTrackChange:
    """on_track_change stores metadata and forwards to active engine."""

    @pytest.mark.asyncio
    async def test_stores_metadata(self, manager):
        metadata = {
            "title": "Test Song",
            "artist": "Test Artist",
            "artwork_url": "https://example.com/art.jpg",
            "duration_ms": 180000,
            "position_ms": 0,
        }
        await manager.on_track_change(metadata)
        assert manager._track_metadata is not None
        assert manager._track_metadata.title == "Test Song"
        assert manager._track_metadata.artist == "Test Artist"

    @pytest.mark.asyncio
    async def test_forwards_to_active_engine(self, manager, mock_ws_hub):
        engine = AsyncMock()
        engine.is_client_side = True
        engine.client_config = {"avatar_url": "test", "track": {}}
        manager._engine = engine
        manager.state = VisualizerState.ACTIVE

        metadata = {
            "title": "New Song",
            "artist": "Artist",
            "artwork_url": None,
            "duration_ms": 200000,
            "position_ms": 0,
        }
        await manager.on_track_change(metadata)
        engine.on_track_change.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_broadcasts_for_client_side_engine(self, manager, mock_ws_hub):
        engine = AsyncMock()
        engine.is_client_side = True
        engine.client_config = {"avatar_url": "test", "track": {"title": "X", "artist": "Y"}}
        manager._engine = engine
        manager.state = VisualizerState.ACTIVE

        metadata = {"title": "X", "artist": "Y", "artwork_url": None, "duration_ms": 100, "position_ms": 0}
        await manager.on_track_change(metadata)
        mock_ws_hub.broadcast.assert_awaited()

    @pytest.mark.asyncio
    async def test_no_broadcast_when_idle(self, manager, mock_ws_hub):
        manager.state = VisualizerState.IDLE_NO_VIEWERS
        metadata = {"title": "X", "artist": "Y", "artwork_url": None, "duration_ms": 100, "position_ms": 0}
        await manager.on_track_change(metadata)
        mock_ws_hub.broadcast.assert_not_awaited()


# ---------------------------------------------------------------------------
# Unit Tests — set_engine
# ---------------------------------------------------------------------------


class TestSetEngine:
    """set_engine changes the engine type and transitions state."""

    @pytest.mark.asyncio
    async def test_set_to_off_disables(self, manager):
        with patch("video.visualizer_manager.guild_settings") as mock_gs:
            mock_gs.VALID_VISUALIZER_ENGINES = {
                "dvd", "projectm", "vgalizer", "varda", "fosfora",
                "audiovis", "native", "random", "off",
            }
            mock_gs.set_visualizer_engine = MagicMock()
            manager.state = VisualizerState.IDLE_NO_VIEWERS
            await manager.set_engine("off")
            assert manager.state == VisualizerState.DISABLED
            assert manager._engine_type == "off"

    @pytest.mark.asyncio
    async def test_set_from_disabled_to_idle(self, manager):
        with patch("video.visualizer_manager.guild_settings") as mock_gs:
            mock_gs.VALID_VISUALIZER_ENGINES = {
                "dvd", "projectm", "vgalizer", "varda", "fosfora",
                "audiovis", "native", "random", "off",
            }
            mock_gs.set_visualizer_engine = MagicMock()
            manager.state = VisualizerState.DISABLED
            await manager.set_engine("dvd")
            assert manager.state == VisualizerState.IDLE_NO_VIEWERS

    @pytest.mark.asyncio
    async def test_invalid_engine_raises(self, manager):
        with patch("video.visualizer_manager.guild_settings") as mock_gs:
            mock_gs.VALID_VISUALIZER_ENGINES = {
                "dvd", "projectm", "vgalizer", "varda", "fosfora",
                "audiovis", "native", "random", "off",
            }
            with pytest.raises(ValueError, match="Invalid visualizer engine"):
                await manager.set_engine("nonexistent")


# ---------------------------------------------------------------------------
# Unit Tests — shutdown
# ---------------------------------------------------------------------------


class TestShutdown:
    """shutdown stops everything and transitions to DISABLED."""

    @pytest.mark.asyncio
    async def test_stops_engine_and_disables(self, manager):
        engine = AsyncMock()
        manager._engine = engine
        manager.state = VisualizerState.ACTIVE
        await manager.shutdown()
        engine.stop.assert_awaited_once()
        assert manager._engine is None
        assert manager.state == VisualizerState.DISABLED

    @pytest.mark.asyncio
    async def test_cancels_suspend_task(self, manager):
        task = MagicMock()
        task.done.return_value = False
        task.cancel = MagicMock()
        manager._suspend_task = task
        manager.state = VisualizerState.SUSPENDING
        await manager.shutdown()
        task.cancel.assert_called_once()
        assert manager.state == VisualizerState.DISABLED


# ---------------------------------------------------------------------------
# Unit Tests — Suspension Debounce
# ---------------------------------------------------------------------------


class TestSuspensionDebounce:
    """Suspension debounce transitions correctly (10s per Req 12 AC 3)."""

    @pytest.mark.asyncio
    async def test_debounce_completes_to_idle(self, manager, mock_ws_hub):
        """After 10s with no viewers, transitions to IDLE_NO_VIEWERS."""
        # Use a short debounce for test speed
        manager.SUSPENSION_DEBOUNCE_SECONDS = 0.1
        manager.state = VisualizerState.ACTIVE
        mock_ws_hub.viewer_count.return_value = 0

        await manager.on_viewer_leave(viewer_count=0)
        assert manager.state == VisualizerState.SUSPENDING

        # Wait for the debounce timer to complete
        await asyncio.sleep(0.15)
        assert manager.state == VisualizerState.IDLE_NO_VIEWERS

    @pytest.mark.asyncio
    async def test_viewer_reconnect_cancels_suspension(self, manager, mock_ws_hub):
        """Viewer rejoining during SUSPENDING cancels and returns to ACTIVE."""
        # Use a short debounce for test speed
        manager.SUSPENSION_DEBOUNCE_SECONDS = 0.1
        manager.state = VisualizerState.ACTIVE
        mock_ws_hub.viewer_count.return_value = 0

        await manager.on_viewer_leave(viewer_count=0)
        assert manager.state == VisualizerState.SUSPENDING

        # Viewer rejoins within debounce window
        await manager.on_viewer_join()
        assert manager.state == VisualizerState.ACTIVE

    def test_default_debounce_is_10_seconds(self, manager):
        """Default SUSPENSION_DEBOUNCE_SECONDS is 10.0 per Req 12 AC 3."""
        # Check the class-level default (not the instance override)
        assert VisualizerManager.SUSPENSION_DEBOUNCE_SECONDS == 10.0

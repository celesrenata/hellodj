"""Tests for VisualizerRegistry — wiring between WebSocketHub and VisualizerManager."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bot"))

from video.visualizer_manager import VisualizerState
from video.visualizer_registry import VisualizerRegistry


def _make_ws_hub() -> MagicMock:
    """Create a mock WebSocketHub with the expected interface."""
    hub = MagicMock()
    hub.set_viewer_count_callback = MagicMock()
    hub.broadcast = AsyncMock()
    hub.viewer_count = MagicMock(return_value=0)
    return hub


class TestRegistryInit:
    """Test VisualizerRegistry initialization."""

    def test_wires_callback_to_ws_hub(self):
        hub = _make_ws_hub()
        registry = VisualizerRegistry(ws_hub=hub, bot_avatar_url="https://example.com/avatar.png")

        hub.set_viewer_count_callback.assert_called_once()
        # The callback should be the registry's internal method
        callback = hub.set_viewer_count_callback.call_args[0][0]
        assert callable(callback)

    def test_starts_with_no_managers(self):
        hub = _make_ws_hub()
        registry = VisualizerRegistry(ws_hub=hub)
        assert registry.get(12345) is None


class TestGetOrCreate:
    """Test lazy manager creation."""

    def test_creates_manager_on_first_call(self):
        hub = _make_ws_hub()
        registry = VisualizerRegistry(ws_hub=hub, bot_avatar_url="https://example.com/av.png")

        manager = registry.get_or_create(111)
        assert manager is not None
        assert manager.guild_id == 111
        # Manager starts in IDLE_NO_VIEWERS when an engine is configured (not "off")
        assert manager.state == VisualizerState.IDLE_NO_VIEWERS

    def test_returns_same_manager_on_subsequent_calls(self):
        hub = _make_ws_hub()
        registry = VisualizerRegistry(ws_hub=hub)

        m1 = registry.get_or_create(222)
        m2 = registry.get_or_create(222)
        assert m1 is m2

    def test_different_guilds_get_different_managers(self):
        hub = _make_ws_hub()
        registry = VisualizerRegistry(ws_hub=hub)

        m1 = registry.get_or_create(111)
        m2 = registry.get_or_create(222)
        assert m1 is not m2
        assert m1.guild_id == 111
        assert m2.guild_id == 222


class TestViewerCountCallback:
    """Test viewer count change dispatching to managers."""

    @pytest.mark.asyncio
    async def test_first_viewer_calls_on_viewer_join(self):
        hub = _make_ws_hub()
        registry = VisualizerRegistry(ws_hub=hub)

        # Get the callback that was registered with ws_hub
        callback = hub.set_viewer_count_callback.call_args[0][0]

        # Simulate viewer count transition 0 → 1
        await callback(42, 0, 1)

        # A manager should now exist for guild 42
        manager = registry.get(42)
        assert manager is not None

    @pytest.mark.asyncio
    async def test_last_viewer_calls_on_viewer_leave(self):
        hub = _make_ws_hub()
        registry = VisualizerRegistry(ws_hub=hub)

        callback = hub.set_viewer_count_callback.call_args[0][0]

        # Pre-create a manager and set it to ACTIVE
        manager = registry.get_or_create(42)
        manager.state = VisualizerState.ACTIVE

        # Simulate viewer count transition 1 → 0
        await callback(42, 1, 0)

        # Manager should now be SUSPENDING (suspension debounce started)
        assert manager.state == VisualizerState.SUSPENDING

    @pytest.mark.asyncio
    async def test_non_boundary_changes_ignored(self):
        hub = _make_ws_hub()
        registry = VisualizerRegistry(ws_hub=hub)

        callback = hub.set_viewer_count_callback.call_args[0][0]

        # Pre-create a manager
        manager = registry.get_or_create(42)
        manager.on_viewer_join = AsyncMock()
        manager.on_viewer_leave = AsyncMock()

        # Transition 2 → 3 should NOT trigger anything
        await callback(42, 2, 3)
        manager.on_viewer_join.assert_not_called()
        manager.on_viewer_leave.assert_not_called()


class TestVideoLifecycleCallbacks:
    """Test video start/end dispatching."""

    @pytest.mark.asyncio
    async def test_on_video_start_creates_manager_and_disables(self):
        hub = _make_ws_hub()
        registry = VisualizerRegistry(ws_hub=hub)

        await registry.on_video_start(99)

        manager = registry.get(99)
        assert manager is not None
        assert manager.state == VisualizerState.DISABLED

    @pytest.mark.asyncio
    async def test_on_video_end_transitions_to_idle(self):
        hub = _make_ws_hub()
        registry = VisualizerRegistry(ws_hub=hub)

        # Start a video first (sets to DISABLED)
        await registry.on_video_start(99)
        manager = registry.get(99)
        assert manager.state == VisualizerState.DISABLED

        # End the video (transitions to IDLE_NO_VIEWERS)
        await registry.on_video_end(99)
        assert manager.state == VisualizerState.IDLE_NO_VIEWERS

    @pytest.mark.asyncio
    async def test_on_video_start_interrupts_active_visualizer(self):
        hub = _make_ws_hub()
        hub.viewer_count.return_value = 1  # Mock active viewers
        registry = VisualizerRegistry(ws_hub=hub)

        # Get a manager into ACTIVE state manually
        manager = registry.get_or_create(55)
        manager.state = VisualizerState.ACTIVE

        # Video starts — should disable the visualizer
        await registry.on_video_start(55)
        assert manager.state == VisualizerState.DISABLED


class TestShutdown:
    """Test registry shutdown."""

    @pytest.mark.asyncio
    async def test_shutdown_cleans_all_managers(self):
        hub = _make_ws_hub()
        registry = VisualizerRegistry(ws_hub=hub)

        # Create a few managers
        registry.get_or_create(1)
        registry.get_or_create(2)
        registry.get_or_create(3)

        await registry.shutdown()

        # All managers should be removed
        assert registry.get(1) is None
        assert registry.get(2) is None
        assert registry.get(3) is None

    @pytest.mark.asyncio
    async def test_remove_single_guild(self):
        hub = _make_ws_hub()
        registry = VisualizerRegistry(ws_hub=hub)

        registry.get_or_create(10)
        registry.get_or_create(20)

        await registry.remove(10)

        assert registry.get(10) is None
        assert registry.get(20) is not None


class TestActivityStreamerSessionStartCallback:
    """Test that ActivityStreamer calls on_session_start."""

    @pytest.mark.asyncio
    async def test_on_session_start_callback_accepted(self):
        """Verify ActivityStreamer accepts on_session_start parameter."""
        from video.activity_streamer import ActivityStreamer

        callback = AsyncMock()
        streamer = ActivityStreamer(
            guild_id=123,
            channel_id=456,
            ws_hub=None,
            on_session_end=None,
            on_session_start=callback,
        )
        # Verify it stored the callback
        assert streamer._on_session_start is callback

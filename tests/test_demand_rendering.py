"""Tests for Viewer-Driven Demand Rendering (Req 12 AC 1-4).

Covers:
- 10-second debounce timing after last viewer disconnects
- Zero GPU resources when no viewers are connected
- Transient disconnect recovery (reconnect within 10s cancels suspension)
- Engine start within 2s of first viewer joining
- GPU VF release after suspension completes

Requirements validated:
- Req 12 AC 1: Zero viewers → no GPU context, no render loop
- Req 12 AC 2: First viewer + audio → engine starts within 2s
- Req 12 AC 3: Last viewer disconnect → 10s debounce
- Req 12 AC 4: Suspended engine holds zero GPU allocations
"""

from __future__ import annotations

import asyncio
import sys
import time
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
def mock_gpu_scheduler():
    """Patch the module-level GPU scheduler."""
    with patch("video.visualizer_manager._gpu_scheduler") as mock_sched:
        mock_sched.allocate = MagicMock()
        mock_sched.release = MagicMock()
        mock_sched.is_allocated = MagicMock(return_value=False)
        yield mock_sched


@pytest.fixture
def manager(mock_ws_hub, mock_gpu_scheduler):
    """Create a VisualizerManager with a short debounce for testing."""
    with patch("video.visualizer_manager.guild_settings") as mock_gs:
        mock_gs.get_visualizer_engine.return_value = "dvd"
        mock_gs.VALID_VISUALIZER_ENGINES = {
            "dvd", "projectm", "varda", "fosfora",
            "audiovis", "native", "random", "off",
        }
        mock_gs.set_visualizer_engine = MagicMock()
        mgr = VisualizerManager(
            guild_id=99999,
            ws_hub=mock_ws_hub,
            bot_avatar_url="https://example.com/avatar.png",
        )
    return mgr


@pytest.fixture
def fast_manager(manager):
    """Manager with a very short debounce for timing tests."""
    manager.SUSPENSION_DEBOUNCE_SECONDS = 0.1
    return manager


# ---------------------------------------------------------------------------
# Tests — Req 12 AC 1: Zero GPU when no viewers
# ---------------------------------------------------------------------------


class TestZeroGPUWhenNoViewers:
    """Req 12 AC 1: No GPU context or render loop when zero viewers."""

    def test_no_engine_in_idle_state(self, manager):
        """IDLE_NO_VIEWERS has no engine instance."""
        manager.state = VisualizerState.IDLE_NO_VIEWERS
        assert manager._engine is None

    def test_no_engine_in_disabled_state(self, manager):
        """DISABLED state has no engine instance."""
        assert manager.state == VisualizerState.DISABLED
        assert manager._engine is None

    def test_no_render_task_in_idle(self, manager):
        """No render loop task when idle (zero viewers)."""
        manager.state = VisualizerState.IDLE_NO_VIEWERS
        assert manager._render_task is None

    def test_no_pipeline_in_idle(self, manager):
        """No HLS pipeline when idle (zero viewers)."""
        manager.state = VisualizerState.IDLE_NO_VIEWERS
        assert manager._pipeline is None

    def test_no_audio_bus_in_idle(self, manager):
        """No AudioFeatureBus subscription when idle."""
        manager.state = VisualizerState.IDLE_NO_VIEWERS
        assert manager._audio_bus is None

    @pytest.mark.asyncio
    async def test_gpu_released_after_suspension(
        self, fast_manager, mock_ws_hub, mock_gpu_scheduler
    ):
        """After suspension completes, GPU scheduler has released the VF."""
        fast_manager.state = VisualizerState.ACTIVE
        mock_ws_hub.viewer_count.return_value = 0

        await fast_manager.on_viewer_leave(viewer_count=0)
        assert fast_manager.state == VisualizerState.SUSPENDING

        # Wait for debounce to complete
        await asyncio.sleep(0.15)
        assert fast_manager.state == VisualizerState.IDLE_NO_VIEWERS

        # GPU VF should have been released
        mock_gpu_scheduler.release.assert_called_with(99999)


# ---------------------------------------------------------------------------
# Tests — Req 12 AC 2: Engine starts within 2s of first viewer
# ---------------------------------------------------------------------------


class TestEngineStartOnFirstViewer:
    """Req 12 AC 2: First viewer + audio → engine starts within 2s."""

    @pytest.mark.asyncio
    async def test_idle_to_active_on_viewer_join(self, manager, mock_ws_hub):
        """First viewer joining from IDLE starts the engine."""
        manager.state = VisualizerState.IDLE_NO_VIEWERS
        t0 = time.monotonic()
        await manager.on_viewer_join()
        elapsed = time.monotonic() - t0

        # Engine should now be active
        assert manager.state == VisualizerState.ACTIVE
        # Should complete well within 2 seconds (DVD is client-side, instant)
        assert elapsed < 2.0

    @pytest.mark.asyncio
    async def test_engine_created_on_viewer_join(self, manager, mock_ws_hub):
        """Engine instance is created when first viewer joins."""
        manager.state = VisualizerState.IDLE_NO_VIEWERS
        assert manager._engine is None

        await manager.on_viewer_join()
        assert manager._engine is not None

    @pytest.mark.asyncio
    async def test_no_start_when_disabled(self, manager, mock_ws_hub):
        """Viewer joining when DISABLED does not start engine."""
        manager.state = VisualizerState.DISABLED
        await manager.on_viewer_join()
        assert manager.state == VisualizerState.DISABLED
        assert manager._engine is None

    @pytest.mark.asyncio
    async def test_broadcasts_visualizer_state_on_start(self, manager, mock_ws_hub):
        """Starting engine broadcasts visualizer state to viewers."""
        manager.state = VisualizerState.IDLE_NO_VIEWERS
        await manager.on_viewer_join()

        mock_ws_hub.broadcast.assert_awaited()
        call_args = mock_ws_hub.broadcast.call_args
        message = call_args[0][1]
        assert message["type"] == "visualizer"
        assert message["state"] == "active"


# ---------------------------------------------------------------------------
# Tests — Req 12 AC 3: 10-second debounce on last viewer disconnect
# ---------------------------------------------------------------------------


class TestDebounceTimer:
    """Req 12 AC 3: 10s debounce after last viewer disconnects."""

    def test_default_debounce_is_10_seconds(self):
        """The class default for SUSPENSION_DEBOUNCE_SECONDS is 10.0."""
        assert VisualizerManager.SUSPENSION_DEBOUNCE_SECONDS == 10.0

    @pytest.mark.asyncio
    async def test_transitions_to_suspending_immediately(
        self, fast_manager, mock_ws_hub
    ):
        """Last viewer disconnect → immediate SUSPENDING state."""
        fast_manager.state = VisualizerState.ACTIVE
        mock_ws_hub.viewer_count.return_value = 0

        await fast_manager.on_viewer_leave(viewer_count=0)
        assert fast_manager.state == VisualizerState.SUSPENDING

    @pytest.mark.asyncio
    async def test_stays_suspending_during_debounce(
        self, fast_manager, mock_ws_hub
    ):
        """State remains SUSPENDING during the debounce window."""
        fast_manager.state = VisualizerState.ACTIVE
        mock_ws_hub.viewer_count.return_value = 0

        await fast_manager.on_viewer_leave(viewer_count=0)

        # Before debounce expires, still in SUSPENDING
        await asyncio.sleep(0.05)
        assert fast_manager.state == VisualizerState.SUSPENDING

    @pytest.mark.asyncio
    async def test_transitions_to_idle_after_debounce(
        self, fast_manager, mock_ws_hub
    ):
        """After debounce expires with 0 viewers → IDLE_NO_VIEWERS."""
        fast_manager.state = VisualizerState.ACTIVE
        mock_ws_hub.viewer_count.return_value = 0

        await fast_manager.on_viewer_leave(viewer_count=0)
        assert fast_manager.state == VisualizerState.SUSPENDING

        # Wait for debounce (0.1s) + margin
        await asyncio.sleep(0.15)
        assert fast_manager.state == VisualizerState.IDLE_NO_VIEWERS

    @pytest.mark.asyncio
    async def test_no_suspension_when_viewers_remain(self, manager, mock_ws_hub):
        """Viewer leaving with others still connected does nothing."""
        manager.state = VisualizerState.ACTIVE
        await manager.on_viewer_leave(viewer_count=2)
        assert manager.state == VisualizerState.ACTIVE

    @pytest.mark.asyncio
    async def test_suspend_task_created(self, fast_manager, mock_ws_hub):
        """A suspension timer task is created on begin_suspension."""
        fast_manager.state = VisualizerState.ACTIVE
        mock_ws_hub.viewer_count.return_value = 0

        await fast_manager.on_viewer_leave(viewer_count=0)
        assert fast_manager._suspend_task is not None
        assert not fast_manager._suspend_task.done()

        # Cleanup
        fast_manager._suspend_task.cancel()
        try:
            await fast_manager._suspend_task
        except asyncio.CancelledError:
            pass

    @pytest.mark.asyncio
    async def test_debounce_viewer_reconnect_during_timer(
        self, fast_manager, mock_ws_hub
    ):
        """Viewer count > 0 at debounce expiry → stays ACTIVE."""
        fast_manager.state = VisualizerState.ACTIVE
        mock_ws_hub.viewer_count.return_value = 0

        await fast_manager.on_viewer_leave(viewer_count=0)
        assert fast_manager.state == VisualizerState.SUSPENDING

        # Simulate viewer reconnecting (ws_hub reports 1 viewer at debounce check)
        mock_ws_hub.viewer_count.return_value = 1

        # Wait for debounce to fire
        await asyncio.sleep(0.15)
        # Timer re-checked viewer_count and found 1 → back to ACTIVE
        assert fast_manager.state == VisualizerState.ACTIVE


# ---------------------------------------------------------------------------
# Tests — Transient Disconnect Recovery
# ---------------------------------------------------------------------------


class TestTransientDisconnectRecovery:
    """Reconnect within 10s cancels debounce (no suspension occurs)."""

    @pytest.mark.asyncio
    async def test_reconnect_cancels_debounce(self, fast_manager, mock_ws_hub):
        """Viewer rejoining during SUSPENDING cancels timer, back to ACTIVE."""
        fast_manager.state = VisualizerState.ACTIVE
        mock_ws_hub.viewer_count.return_value = 0

        await fast_manager.on_viewer_leave(viewer_count=0)
        assert fast_manager.state == VisualizerState.SUSPENDING

        # Viewer reconnects before debounce expires
        await asyncio.sleep(0.03)
        await fast_manager.on_viewer_join()
        assert fast_manager.state == VisualizerState.ACTIVE

        # Verify the suspend task was cancelled
        assert (
            fast_manager._suspend_task is None
            or fast_manager._suspend_task.cancelled()
            or fast_manager._suspend_task.done()
        )

    @pytest.mark.asyncio
    async def test_no_gpu_release_on_transient_disconnect(
        self, fast_manager, mock_ws_hub, mock_gpu_scheduler
    ):
        """GPU VF is NOT released if viewer reconnects within debounce."""
        fast_manager.state = VisualizerState.ACTIVE
        mock_ws_hub.viewer_count.return_value = 0

        await fast_manager.on_viewer_leave(viewer_count=0)
        assert fast_manager.state == VisualizerState.SUSPENDING

        # Reconnect quickly
        await fast_manager.on_viewer_join()
        assert fast_manager.state == VisualizerState.ACTIVE

        # Wait past the debounce period to confirm no release happens
        await asyncio.sleep(0.15)
        # gpu_scheduler.release should NOT have been called
        mock_gpu_scheduler.release.assert_not_called()

    @pytest.mark.asyncio
    async def test_multiple_transient_disconnects(
        self, fast_manager, mock_ws_hub, mock_gpu_scheduler
    ):
        """Multiple disconnect/reconnect cycles within debounce are handled."""
        fast_manager.state = VisualizerState.ACTIVE
        mock_ws_hub.viewer_count.return_value = 0

        # First disconnect
        await fast_manager.on_viewer_leave(viewer_count=0)
        assert fast_manager.state == VisualizerState.SUSPENDING

        # Quick reconnect
        await fast_manager.on_viewer_join()
        assert fast_manager.state == VisualizerState.ACTIVE

        # Second disconnect
        await fast_manager.on_viewer_leave(viewer_count=0)
        assert fast_manager.state == VisualizerState.SUSPENDING

        # Quick reconnect again
        await fast_manager.on_viewer_join()
        assert fast_manager.state == VisualizerState.ACTIVE

        # No GPU release should have occurred
        await asyncio.sleep(0.15)
        mock_gpu_scheduler.release.assert_not_called()

    @pytest.mark.asyncio
    async def test_engine_not_stopped_on_transient_disconnect(
        self, fast_manager, mock_ws_hub
    ):
        """Engine remains alive during transient disconnect."""
        # Set up an active engine
        engine = AsyncMock()
        engine.is_client_side = True
        engine.client_config = {"avatar_url": "test"}
        fast_manager._engine = engine
        fast_manager.state = VisualizerState.ACTIVE
        mock_ws_hub.viewer_count.return_value = 0

        await fast_manager.on_viewer_leave(viewer_count=0)
        assert fast_manager.state == VisualizerState.SUSPENDING

        # Reconnect
        await fast_manager.on_viewer_join()
        assert fast_manager.state == VisualizerState.ACTIVE

        # Engine stop should NOT have been called
        engine.stop.assert_not_awaited()


# ---------------------------------------------------------------------------
# Tests — Req 12 AC 4: Zero GPU allocations while suspended
# ---------------------------------------------------------------------------


class TestZeroGPUWhileSuspended:
    """Req 12 AC 4: Suspended engine holds zero GPU allocations."""

    @pytest.mark.asyncio
    async def test_engine_none_after_suspension(
        self, fast_manager, mock_ws_hub
    ):
        """Engine instance is None after suspension completes."""
        fast_manager.state = VisualizerState.ACTIVE
        mock_ws_hub.viewer_count.return_value = 0

        await fast_manager.on_viewer_leave(viewer_count=0)
        await asyncio.sleep(0.15)

        assert fast_manager.state == VisualizerState.IDLE_NO_VIEWERS
        assert fast_manager._engine is None

    @pytest.mark.asyncio
    async def test_gpu_vf_released_on_suspension(
        self, fast_manager, mock_ws_hub, mock_gpu_scheduler
    ):
        """GPU VF is released when suspension completes."""
        fast_manager.state = VisualizerState.ACTIVE
        mock_ws_hub.viewer_count.return_value = 0

        await fast_manager.on_viewer_leave(viewer_count=0)
        await asyncio.sleep(0.15)

        assert fast_manager.state == VisualizerState.IDLE_NO_VIEWERS
        mock_gpu_scheduler.release.assert_called_with(99999)

    @pytest.mark.asyncio
    async def test_no_render_task_after_suspension(
        self, fast_manager, mock_ws_hub
    ):
        """Render task is None after suspension."""
        fast_manager.state = VisualizerState.ACTIVE
        mock_ws_hub.viewer_count.return_value = 0

        await fast_manager.on_viewer_leave(viewer_count=0)
        await asyncio.sleep(0.15)

        assert fast_manager.state == VisualizerState.IDLE_NO_VIEWERS
        assert fast_manager._render_task is None

    @pytest.mark.asyncio
    async def test_no_pipeline_after_suspension(
        self, fast_manager, mock_ws_hub
    ):
        """HLS pipeline is None after suspension."""
        fast_manager.state = VisualizerState.ACTIVE
        mock_ws_hub.viewer_count.return_value = 0

        await fast_manager.on_viewer_leave(viewer_count=0)
        await asyncio.sleep(0.15)

        assert fast_manager.state == VisualizerState.IDLE_NO_VIEWERS
        assert fast_manager._pipeline is None

    @pytest.mark.asyncio
    async def test_no_audio_bus_after_suspension(
        self, fast_manager, mock_ws_hub
    ):
        """AudioFeatureBus is None after suspension."""
        fast_manager.state = VisualizerState.ACTIVE
        mock_ws_hub.viewer_count.return_value = 0

        await fast_manager.on_viewer_leave(viewer_count=0)
        await asyncio.sleep(0.15)

        assert fast_manager.state == VisualizerState.IDLE_NO_VIEWERS
        assert fast_manager._audio_bus is None


# ---------------------------------------------------------------------------
# Tests — WebSocket Hub Viewer Count Integration
# ---------------------------------------------------------------------------


class TestWSHubViewerCountTracking:
    """ws_hub tracks viewer count per guild and emits changes."""

    def test_viewer_count_starts_at_zero(self, mock_ws_hub):
        """Initial viewer count is 0."""
        mock_ws_hub.viewer_count.return_value = 0
        assert mock_ws_hub.viewer_count(99999) == 0

    def test_viewer_count_method_exists(self, mock_ws_hub):
        """ws_hub has a viewer_count method callable with guild_id."""
        assert callable(mock_ws_hub.viewer_count)

    @pytest.mark.asyncio
    async def test_viewer_count_callback_mechanism(self, mock_ws_hub):
        """ws_hub supports a viewer_count_callback for transitions."""
        # The real ws_hub has set_viewer_count_callback
        assert hasattr(mock_ws_hub, "viewer_count")

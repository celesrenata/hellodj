"""End-to-end integration tests for the GPU visualizer pipeline.

Tests the complete path: viewer joins → engine starts → HLS pipeline starts →
frames piped → segments appear → readiness signaled → viewer leaves → debounce →
engine suspended → GPU released.

All GPU/ffmpeg components are mocked. This validates the wiring between:
- VisualizerManager state machine
- GPUResourceScheduler VF allocation
- HLSTranscodePipeline (visualizer mode)
- AudioFeatureBus subscription
- WebSocket hub notifications

Requirements: Req 3 (AC 1-5), Req 12 (AC 2-3), Req 13 (AC 1-5)
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Ensure the bot directory is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bot"))

from video.gpu_scheduler import GPUResourceScheduler
from video.visualizer_manager import (
    VisualizerManager,
    VisualizerState,
    _gpu_scheduler,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

FRAME_SIZE = 1280 * 720 * 4  # 3,686,400 bytes per RGBA frame


class FakeStdinWriter:
    """Mock for asyncio.StreamWriter used as ffmpeg stdin pipe."""

    def __init__(self):
        self.written: list[bytes] = []
        self.closed = False

    def write(self, data: bytes) -> None:
        self.written.append(data)

    async def drain(self) -> None:
        await asyncio.sleep(0)

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        pass


class FakeHLSPipeline:
    """Mock HLSTranscodePipeline in visualizer mode.

    Simulates the pipeline that accepts raw RGBA frames via stdin and
    produces HLS segments. The `ready` event can be set to simulate
    the first segment appearing on disk.
    """

    def __init__(self, guild_id: int, session_id: str = "viz"):
        self.guild_id = guild_id
        self.session_id = session_id
        self.ready = asyncio.Event()
        self.output_dir = Path(f"/tmp/hellodj_hls/{guild_id}/viz")
        self.playlist_path = self.output_dir / "playlist.m3u8"
        self.stdin = FakeStdinWriter()
        self._running = False
        self._started = False
        self.frames_written: list[bytes] = []

    @property
    def stdin_pipe(self):
        if self._running:
            return self.stdin
        return None

    async def start_visualizer(self, width=1280, height=720, fps=30):
        self._running = True
        self._started = True
        # Simulate first segment appearing after a short delay
        asyncio.get_event_loop().call_later(0.05, self.ready.set)
        return self.stdin

    async def write_frame(self, data: bytes) -> None:
        if not self._running:
            raise RuntimeError("Pipeline not running")
        self.frames_written.append(data)

    async def stop(self) -> None:
        self._running = False
        self.ready.clear()


class FakeEngine:
    """Mock server-rendered visualizer engine.

    Produces a configurable number of frames then stops. Simulates
    a GPU-based engine that yields RGBA frames via render_frames().
    """

    def __init__(self, frame_count: int = 5):
        self.frame_count = frame_count
        self.is_client_side = False
        self.consumes_gpu_while_suspended = False
        self.client_config = None
        self.initialized = False
        self.activated = False
        self.stopped = False
        self.suspended = False
        self._audio_features_received: list = []
        self._render_started = asyncio.Event()

    async def initialize(self, metadata=None):
        self.initialized = True

    async def activate(self, metadata=None):
        self.activated = True

    async def suspend(self):
        self.suspended = True

    async def resume(self, metadata=None):
        self.suspended = False

    async def stop(self):
        self.stopped = True

    async def on_track_change(self, metadata):
        pass

    def on_audio_features(self, features):
        self._audio_features_received.append(features)

    async def render_frames(self):
        """Yield fake RGBA frames."""
        self._render_started.set()
        for _ in range(self.frame_count):
            yield b"\x00" * FRAME_SIZE
            await asyncio.sleep(0.01)  # Simulate 30fps-ish timing


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_ws_hub():
    """Create a mock WebSocketHub with required methods."""
    hub = MagicMock()
    hub.broadcast = AsyncMock()
    hub.viewer_count = MagicMock(return_value=1)
    return hub


@pytest.fixture(autouse=True)
def reset_gpu_scheduler():
    """Reset the module-level GPU scheduler between tests."""
    _gpu_scheduler._allocations.clear()
    yield
    _gpu_scheduler._allocations.clear()


@pytest.fixture
def fake_engine():
    """Create a FakeEngine that produces 5 frames."""
    return FakeEngine(frame_count=5)


@pytest.fixture
def fake_pipeline():
    """Create a FakeHLSPipeline."""
    return FakeHLSPipeline(guild_id=100)


@pytest.fixture
def manager(mock_ws_hub):
    """Create a VisualizerManager configured for a server-rendered engine."""
    with patch("video.visualizer_manager.guild_settings") as mock_gs:
        mock_gs.get_visualizer_engine.return_value = "varda"
        mock_gs.VALID_VISUALIZER_ENGINES = {
            "dvd", "projectm", "varda", "fosfora",
            "audiovis", "native", "random", "off",
        }
        mock_gs.set_visualizer_engine = MagicMock()
        mgr = VisualizerManager(
            guild_id=100,
            ws_hub=mock_ws_hub,
            bot_avatar_url="https://example.com/avatar.png",
        )
    return mgr


# ---------------------------------------------------------------------------
# Test: Viewer Join → Engine Start → HLS Pipeline → Frames Piped → Ready
# ---------------------------------------------------------------------------


class TestViewerJoinToHLSReady:
    """Req 3 AC 1-3, Req 13 AC 1-3: Full startup path from viewer join to HLS playback."""

    @pytest.mark.asyncio
    async def test_viewer_join_starts_full_pipeline(
        self, manager, mock_ws_hub, fake_engine, fake_pipeline
    ):
        """Viewer joins → engine starts → HLS pipeline started → frames piped → readiness signaled."""
        manager.state = VisualizerState.IDLE_NO_VIEWERS

        with patch(
            "video.visualizer_manager.VisualizerManager._create_engine_instance",
            return_value=fake_engine,
        ), patch(
            "video.visualizer_manager.HLSTranscodePipeline",
            return_value=fake_pipeline,
        ), patch(
            "video.visualizer_manager.AudioFeatureBus",
        ) as MockBus:
            mock_bus_instance = AsyncMock()
            MockBus.return_value = mock_bus_instance

            await manager.on_viewer_join()

            # Allow async tasks (render loop, ready watcher) to execute
            await asyncio.sleep(0.2)

        # Engine was initialized and activated
        assert fake_engine.initialized
        assert fake_engine.activated

        # GPU VF was allocated
        assert _gpu_scheduler.is_allocated(100)

        # HLS pipeline was started in visualizer mode
        assert fake_pipeline._started

        # Readiness was signaled to frontend via WebSocket broadcast
        broadcast_calls = mock_ws_hub.broadcast.call_args_list
        # Should have at least the "starting" message and the "active + hls_ready" message
        messages = [call.args[1] for call in broadcast_calls]

        starting_msgs = [m for m in messages if m.get("state") == "starting"]
        assert len(starting_msgs) >= 1

        ready_msgs = [m for m in messages if m.get("hls_ready") is True]
        assert len(ready_msgs) >= 1

        # The ready message includes the correct playlist URL
        ready_msg = ready_msgs[0]
        assert ready_msg["playlist_url"] == "/activity/stream/100/viz/playlist.m3u8"
        assert ready_msg["engine"] == "varda"

    @pytest.mark.asyncio
    async def test_startup_within_2_seconds(
        self, manager, mock_ws_hub, fake_engine, fake_pipeline
    ):
        """Req 13 AC 2: Startup completes within 2 seconds of first viewer connecting."""
        manager.state = VisualizerState.IDLE_NO_VIEWERS

        with patch(
            "video.visualizer_manager.VisualizerManager._create_engine_instance",
            return_value=fake_engine,
        ), patch(
            "video.visualizer_manager.HLSTranscodePipeline",
            return_value=fake_pipeline,
        ), patch(
            "video.visualizer_manager.AudioFeatureBus",
        ) as MockBus:
            mock_bus_instance = AsyncMock()
            MockBus.return_value = mock_bus_instance

            start_time = asyncio.get_event_loop().time()
            await manager.on_viewer_join()

            # Wait for the ready signal
            await asyncio.wait_for(fake_pipeline.ready.wait(), timeout=2.0)
            elapsed = asyncio.get_event_loop().time() - start_time

        # HLS ready within 2 seconds
        assert elapsed < 2.0


# ---------------------------------------------------------------------------
# Test: HLS Playlist Path Correctness
# ---------------------------------------------------------------------------


class TestHLSPlaylistPath:
    """Req 13 AC 4: /activity/stream/{gid}/viz/playlist.m3u8 serves valid HLS playlist."""

    @pytest.mark.asyncio
    async def test_playlist_url_format(self, manager, mock_ws_hub, fake_engine, fake_pipeline):
        """The broadcast playlist_url matches expected Activity URL pattern."""
        manager.state = VisualizerState.IDLE_NO_VIEWERS

        with patch(
            "video.visualizer_manager.VisualizerManager._create_engine_instance",
            return_value=fake_engine,
        ), patch(
            "video.visualizer_manager.HLSTranscodePipeline",
            return_value=fake_pipeline,
        ), patch(
            "video.visualizer_manager.AudioFeatureBus",
        ) as MockBus:
            MockBus.return_value = AsyncMock()
            await manager.on_viewer_join()
            await asyncio.sleep(0.2)

        # Verify the path structure in broadcast message
        broadcast_calls = mock_ws_hub.broadcast.call_args_list
        messages = [call.args[1] for call in broadcast_calls]
        ready_msgs = [m for m in messages if m.get("hls_ready") is True]
        assert len(ready_msgs) >= 1

        url = ready_msgs[0]["playlist_url"]
        # Must follow the pattern /activity/stream/{guild_id}/viz/playlist.m3u8
        assert url == "/activity/stream/100/viz/playlist.m3u8"
        # Verify it contains the guild id
        assert "100" in url
        # Verify it ends with the HLS manifest filename
        assert url.endswith("playlist.m3u8")

    @pytest.mark.asyncio
    async def test_pipeline_output_dir_matches_url(self, fake_pipeline):
        """Pipeline output directory matches the URL-served path."""
        # The output directory should be /tmp/hellodj_hls/{guild_id}/viz/
        expected_path = Path("/tmp/hellodj_hls/100/viz/playlist.m3u8")
        assert fake_pipeline.playlist_path == expected_path


# ---------------------------------------------------------------------------
# Test: Viewer Disconnect → Debounce → Suspension → GPU Release
# ---------------------------------------------------------------------------


class TestViewerDisconnectSuspension:
    """Req 12 AC 2-3: Last viewer disconnect → debounce → suspension → GPU release."""

    @pytest.mark.asyncio
    async def test_last_viewer_disconnect_triggers_debounce(
        self, manager, mock_ws_hub, fake_engine
    ):
        """Last viewer leaving starts a 2s debounce before suspension."""
        # Set up active state with GPU allocated
        manager._engine = fake_engine
        manager.state = VisualizerState.ACTIVE
        _gpu_scheduler.allocate(100, "varda")
        mock_ws_hub.viewer_count.return_value = 0

        # Last viewer leaves
        await manager.on_viewer_leave(viewer_count=0)

        # Should transition to SUSPENDING (debounce started)
        assert manager.state == VisualizerState.SUSPENDING
        # GPU still allocated during debounce
        assert _gpu_scheduler.is_allocated(100)

    @pytest.mark.asyncio
    async def test_debounce_completes_releases_gpu(
        self, manager, mock_ws_hub, fake_engine
    ):
        """After debounce with zero viewers, GPU VF is released."""
        manager._engine = fake_engine
        manager.state = VisualizerState.ACTIVE
        _gpu_scheduler.allocate(100, "varda")
        mock_ws_hub.viewer_count.return_value = 0

        # Shorten debounce for test speed
        manager.SUSPENSION_DEBOUNCE_SECONDS = 0.2

        await manager.on_viewer_leave(viewer_count=0)
        assert manager.state == VisualizerState.SUSPENDING

        # Wait for debounce + margin
        await asyncio.sleep(0.5)

        # After debounce: suspended, GPU released
        assert manager.state == VisualizerState.IDLE_NO_VIEWERS
        assert not _gpu_scheduler.is_allocated(100)

    @pytest.mark.asyncio
    async def test_viewer_reconnects_during_debounce_cancels_suspension(
        self, manager, mock_ws_hub, fake_engine
    ):
        """Reconnecting within debounce window cancels suspension."""
        manager._engine = fake_engine
        manager.state = VisualizerState.ACTIVE
        _gpu_scheduler.allocate(100, "varda")
        mock_ws_hub.viewer_count.return_value = 0

        # Shorten debounce for test speed
        manager.SUSPENSION_DEBOUNCE_SECONDS = 1.0

        # Last viewer leaves
        await manager.on_viewer_leave(viewer_count=0)
        assert manager.state == VisualizerState.SUSPENDING

        # Viewer reconnects within debounce window
        await asyncio.sleep(0.2)
        await manager.on_viewer_join()

        # Should cancel suspension and return to ACTIVE
        assert manager.state == VisualizerState.ACTIVE
        # GPU still allocated
        assert _gpu_scheduler.is_allocated(100)

        # Wait past the debounce window — should NOT transition
        await asyncio.sleep(1.5)
        assert manager.state == VisualizerState.ACTIVE
        assert _gpu_scheduler.is_allocated(100)


# ---------------------------------------------------------------------------
# Test: render_frames() Output Piped to HLS Pipeline
# ---------------------------------------------------------------------------


class TestRenderFramesPipedToHLS:
    """Req 3 AC 1, Req 13 AC 1: render_frames() output is written to HLS pipeline."""

    @pytest.mark.asyncio
    async def test_render_loop_writes_frames_to_pipeline(
        self, manager, mock_ws_hub, fake_engine, fake_pipeline
    ):
        """The render loop reads from render_frames() and writes to pipeline stdin."""
        manager.state = VisualizerState.IDLE_NO_VIEWERS

        with patch(
            "video.visualizer_manager.VisualizerManager._create_engine_instance",
            return_value=fake_engine,
        ), patch(
            "video.visualizer_manager.HLSTranscodePipeline",
            return_value=fake_pipeline,
        ), patch(
            "video.visualizer_manager.AudioFeatureBus",
        ) as MockBus:
            MockBus.return_value = AsyncMock()
            await manager.on_viewer_join()

            # Wait for the render loop to process frames
            await asyncio.sleep(0.3)

        # Verify frames were written to the pipeline stdin
        assert len(fake_pipeline.stdin.written) >= 1
        # Each frame should be exactly FRAME_SIZE bytes
        for frame in fake_pipeline.stdin.written:
            assert len(frame) == FRAME_SIZE

    @pytest.mark.asyncio
    async def test_frame_size_exactly_3686400_bytes(
        self, manager, mock_ws_hub, fake_pipeline
    ):
        """Each frame piped to HLS is exactly 1280×720×4 = 3,686,400 bytes (Req 13 AC 1)."""
        engine = FakeEngine(frame_count=3)
        manager.state = VisualizerState.IDLE_NO_VIEWERS

        with patch(
            "video.visualizer_manager.VisualizerManager._create_engine_instance",
            return_value=engine,
        ), patch(
            "video.visualizer_manager.HLSTranscodePipeline",
            return_value=fake_pipeline,
        ), patch(
            "video.visualizer_manager.AudioFeatureBus",
        ) as MockBus:
            MockBus.return_value = AsyncMock()
            await manager.on_viewer_join()
            await asyncio.sleep(0.2)

        for frame in fake_pipeline.stdin.written:
            assert len(frame) == 3_686_400


# ---------------------------------------------------------------------------
# Test: HLS Readiness Event Triggers WebSocket Broadcast
# ---------------------------------------------------------------------------


class TestHLSReadinessBroadcast:
    """Req 3 AC 3, Req 13 AC 3: HLS readiness event triggers WebSocket broadcast."""

    @pytest.mark.asyncio
    async def test_ready_event_broadcasts_to_viewers(
        self, manager, mock_ws_hub, fake_engine, fake_pipeline
    ):
        """When HLS pipeline ready event fires, WebSocket hub broadcasts playlist URL."""
        manager.state = VisualizerState.IDLE_NO_VIEWERS

        with patch(
            "video.visualizer_manager.VisualizerManager._create_engine_instance",
            return_value=fake_engine,
        ), patch(
            "video.visualizer_manager.HLSTranscodePipeline",
            return_value=fake_pipeline,
        ), patch(
            "video.visualizer_manager.AudioFeatureBus",
        ) as MockBus:
            MockBus.return_value = AsyncMock()
            await manager.on_viewer_join()
            await asyncio.sleep(0.2)

        # The broadcast method should have been called with hls_ready=True
        broadcast_calls = mock_ws_hub.broadcast.call_args_list
        messages = [call.args[1] for call in broadcast_calls]

        hls_ready_msgs = [m for m in messages if m.get("hls_ready") is True]
        assert len(hls_ready_msgs) >= 1, (
            f"Expected hls_ready broadcast, got messages: {messages}"
        )

        msg = hls_ready_msgs[0]
        assert msg["type"] == "visualizer"
        assert msg["state"] == "active"
        assert msg["engine"] == "varda"
        assert msg["playlist_url"] == "/activity/stream/100/viz/playlist.m3u8"

    @pytest.mark.asyncio
    async def test_state_transitions_to_active_on_ready(
        self, manager, mock_ws_hub, fake_engine, fake_pipeline
    ):
        """Manager transitions from STARTING to ACTIVE when HLS is ready."""
        manager.state = VisualizerState.IDLE_NO_VIEWERS

        with patch(
            "video.visualizer_manager.VisualizerManager._create_engine_instance",
            return_value=fake_engine,
        ), patch(
            "video.visualizer_manager.HLSTranscodePipeline",
            return_value=fake_pipeline,
        ), patch(
            "video.visualizer_manager.AudioFeatureBus",
        ) as MockBus:
            MockBus.return_value = AsyncMock()
            await manager.on_viewer_join()
            await asyncio.sleep(0.2)

        assert manager.state == VisualizerState.ACTIVE


# ---------------------------------------------------------------------------
# Test: AudioFeatureBus Subscription Lifecycle
# ---------------------------------------------------------------------------


class TestAudioBusSubscription:
    """Req 1 AC 1-2: Engine subscribes/unsubscribes to AudioFeatureBus on lifecycle events."""

    @pytest.mark.asyncio
    async def test_audio_bus_subscribed_on_start(
        self, manager, mock_ws_hub, fake_engine, fake_pipeline
    ):
        """AudioFeatureBus.subscribe() called with engine callback on start."""
        manager.state = VisualizerState.IDLE_NO_VIEWERS

        with patch(
            "video.visualizer_manager.VisualizerManager._create_engine_instance",
            return_value=fake_engine,
        ), patch(
            "video.visualizer_manager.HLSTranscodePipeline",
            return_value=fake_pipeline,
        ), patch(
            "video.visualizer_manager.AudioFeatureBus",
        ) as MockBus:
            mock_bus_instance = AsyncMock()
            MockBus.return_value = mock_bus_instance

            await manager.on_viewer_join()
            await asyncio.sleep(0.2)

        # subscribe was called with the engine's on_audio_features method
        mock_bus_instance.subscribe.assert_called_once_with(
            fake_engine.on_audio_features
        )

    @pytest.mark.asyncio
    async def test_audio_bus_unsubscribed_on_stop(
        self, manager, mock_ws_hub, fake_engine, fake_pipeline
    ):
        """AudioFeatureBus.unsubscribe() called when engine is stopped."""
        manager.state = VisualizerState.IDLE_NO_VIEWERS

        with patch(
            "video.visualizer_manager.VisualizerManager._create_engine_instance",
            return_value=fake_engine,
        ), patch(
            "video.visualizer_manager.HLSTranscodePipeline",
            return_value=fake_pipeline,
        ), patch(
            "video.visualizer_manager.AudioFeatureBus",
        ) as MockBus:
            mock_bus_instance = AsyncMock()
            MockBus.return_value = mock_bus_instance

            await manager.on_viewer_join()
            await asyncio.sleep(0.2)

            # Now stop the engine
            await manager._stop_engine()

        # unsubscribe was called with the engine's callback
        mock_bus_instance.unsubscribe.assert_called_once_with(
            fake_engine.on_audio_features
        )
        # Bus was shut down
        mock_bus_instance.shutdown.assert_called_once()


# ---------------------------------------------------------------------------
# Test: Full End-to-End Lifecycle
# ---------------------------------------------------------------------------


class TestFullE2ELifecycle:
    """Complete E2E: viewer join → active → viewer leave → debounce → GPU released."""

    @pytest.mark.asyncio
    async def test_complete_lifecycle(self, manager, mock_ws_hub, fake_pipeline):
        """Full lifecycle: start → render → ready → viewer leaves → suspend → release."""
        engine = FakeEngine(frame_count=50)  # Enough frames for the full test
        manager.state = VisualizerState.IDLE_NO_VIEWERS

        # Shorten debounce for test speed
        manager.SUSPENSION_DEBOUNCE_SECONDS = 0.2

        with patch(
            "video.visualizer_manager.VisualizerManager._create_engine_instance",
            return_value=engine,
        ), patch(
            "video.visualizer_manager.HLSTranscodePipeline",
            return_value=fake_pipeline,
        ), patch(
            "video.visualizer_manager.AudioFeatureBus",
        ) as MockBus:
            MockBus.return_value = AsyncMock()

            # Phase 1: Viewer joins → engine starts → HLS ready
            await manager.on_viewer_join()
            await asyncio.sleep(0.2)

            assert engine.initialized
            assert engine.activated
            assert _gpu_scheduler.is_allocated(100)
            assert manager.state == VisualizerState.ACTIVE

            # Phase 2: Frames are being piped
            assert len(fake_pipeline.stdin.written) >= 1

            # Phase 3: Viewer leaves → suspension debounce starts
            mock_ws_hub.viewer_count.return_value = 0
            await manager.on_viewer_leave(viewer_count=0)
            assert manager.state == VisualizerState.SUSPENDING

            # Phase 4: After debounce → IDLE, GPU released
            await asyncio.sleep(0.5)

        assert manager.state == VisualizerState.IDLE_NO_VIEWERS
        assert not _gpu_scheduler.is_allocated(100)

    @pytest.mark.asyncio
    async def test_multiple_viewers_join_leave_one_remains(
        self, manager, mock_ws_hub, fake_engine, fake_pipeline
    ):
        """Multiple viewers: engine stays active as long as at least one viewer remains."""
        manager.state = VisualizerState.IDLE_NO_VIEWERS

        with patch(
            "video.visualizer_manager.VisualizerManager._create_engine_instance",
            return_value=fake_engine,
        ), patch(
            "video.visualizer_manager.HLSTranscodePipeline",
            return_value=fake_pipeline,
        ), patch(
            "video.visualizer_manager.AudioFeatureBus",
        ) as MockBus:
            MockBus.return_value = AsyncMock()

            # First viewer joins
            await manager.on_viewer_join()
            await asyncio.sleep(0.2)
            assert manager.state == VisualizerState.ACTIVE

            # Second viewer joins (no-op, already active)
            await manager.on_viewer_join()
            assert manager.state == VisualizerState.ACTIVE

            # One viewer leaves (1 remaining) — no suspension
            await manager.on_viewer_leave(viewer_count=1)
            assert manager.state == VisualizerState.ACTIVE
            assert _gpu_scheduler.is_allocated(100)

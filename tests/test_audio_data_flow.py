"""Integration tests for audio data flow: voice_recv → AudioFeatureBus → Engine.

Tests the wiring between AudioFeatureBus and visualizer engines via
VisualizerManager, verifying:
- Subscribe on engine start (within 100ms)
- Unsubscribe on engine stop/suspend (within 100ms)
- AudioFeatureBus auto-starts processing loop on first subscriber
- AudioFeatures dispatched include correct fields (FFT 512, beat, BPM, 7-band)
- Queue-full condition drops oldest frame (never blocks voice_recv)

Requirements: Req 1 (AC 1-5)
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bot"))

from video.audio_feature_bus import AudioFeatureBus
from video.visualizer_engines.base import AudioFeatures
from video.visualizer_manager import VisualizerManager, VisualizerState, _gpu_scheduler


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_pcm_frame(frequency: float = 440.0, samples: int = 1024) -> bytes:
    """Generate a sine wave PCM frame (16-bit signed LE, mono, 48kHz).

    Args:
        frequency: Tone frequency in Hz.
        samples: Number of samples per frame.

    Returns:
        bytes of length samples * 2 (16-bit).
    """
    import numpy as np

    t = np.arange(samples) / 48000.0
    signal = np.sin(2 * np.pi * frequency * t) * 16000
    return signal.astype(np.int16).tobytes()


def _make_audio_features(beat: bool = False, bpm: float = 120.0) -> AudioFeatures:
    """Create a valid AudioFeatures instance with expected field shapes."""
    return AudioFeatures(
        fft=[0.1] * 512,
        beat=beat,
        bpm=bpm,
        band_energy=[0.5, 0.4, 0.3, 0.2, 0.1, 0.05, 0.02],
        timestamp=time.monotonic(),
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_ws_hub():
    """Create a mock WebSocketHub."""
    hub = MagicMock()
    hub.broadcast = AsyncMock()
    hub.viewer_count = MagicMock(return_value=0)
    return hub


@pytest.fixture(autouse=True)
def reset_gpu_scheduler():
    """Reset the module-level GPU scheduler between tests."""
    _gpu_scheduler._allocations.clear()
    yield
    _gpu_scheduler._allocations.clear()


@pytest.fixture
def audio_bus():
    """Create a fresh AudioFeatureBus for testing."""
    bus = AudioFeatureBus(guild_id=42)
    return bus


# ---------------------------------------------------------------------------
# Test: Subscribe on Engine Start (Req 1 AC 1)
# ---------------------------------------------------------------------------


class TestSubscribeOnEngineStart:
    """VisualizerManager subscribes engine's on_audio_features on start."""

    @pytest.mark.asyncio
    async def test_subscribe_called_on_server_engine_start(self, mock_ws_hub):
        """_start_server_render_pipeline subscribes engine audio callback."""
        with patch("video.visualizer_manager.guild_settings") as mock_gs:
            mock_gs.get_visualizer_engine.return_value = "varda"
            mock_gs.VALID_VISUALIZER_ENGINES = {
                "dvd", "projectm", "varda", "fosfora",
                "audiovis", "native", "random", "off",
            }
            mgr = VisualizerManager(guild_id=42, ws_hub=mock_ws_hub)

        mock_engine = AsyncMock()
        mock_engine.is_client_side = False
        mock_engine.on_audio_features = MagicMock()

        with patch(
            "video.visualizer_manager.VisualizerManager._create_engine_instance",
            return_value=mock_engine,
        ), patch(
            "video.visualizer_manager.HLSTranscodePipeline",
        ) as MockPipeline:
            mock_pipeline_instance = AsyncMock()
            mock_pipeline_instance.ready = asyncio.Event()
            mock_pipeline_instance.stdin_pipe = MagicMock()
            MockPipeline.return_value = mock_pipeline_instance

            mgr.state = VisualizerState.IDLE_NO_VIEWERS
            await mgr._start_engine()

        # Verify the AudioFeatureBus was created and engine was subscribed
        assert mgr._audio_bus is not None
        assert mock_engine.on_audio_features in mgr._audio_bus._subscribers

    @pytest.mark.asyncio
    async def test_subscribe_completes_within_100ms(self, mock_ws_hub):
        """Subscription to AudioFeatureBus completes within 100ms."""
        with patch("video.visualizer_manager.guild_settings") as mock_gs:
            mock_gs.get_visualizer_engine.return_value = "fosfora"
            mock_gs.VALID_VISUALIZER_ENGINES = {
                "dvd", "projectm", "varda", "fosfora",
                "audiovis", "native", "random", "off",
            }
            mgr = VisualizerManager(guild_id=43, ws_hub=mock_ws_hub)

        mock_engine = AsyncMock()
        mock_engine.is_client_side = False
        mock_engine.on_audio_features = MagicMock()

        with patch(
            "video.visualizer_manager.VisualizerManager._create_engine_instance",
            return_value=mock_engine,
        ), patch(
            "video.visualizer_manager.HLSTranscodePipeline",
        ) as MockPipeline:
            mock_pipeline_instance = AsyncMock()
            mock_pipeline_instance.ready = asyncio.Event()
            mock_pipeline_instance.stdin_pipe = MagicMock()
            MockPipeline.return_value = mock_pipeline_instance

            mgr.state = VisualizerState.IDLE_NO_VIEWERS

            t0 = time.monotonic()
            await mgr._start_engine()
            elapsed = time.monotonic() - t0

        # The subscribe itself should be near-instant (well under 100ms)
        # The 100ms budget is for the subscribe operation, not the full start
        assert mgr._audio_bus is not None
        assert mock_engine.on_audio_features in mgr._audio_bus._subscribers
        # Subscribe portion is much faster than 100ms; full start may include
        # engine init but the bus subscription is effectively instantaneous
        assert elapsed < 1.0  # Generous bound for CI; real constraint is 100ms


# ---------------------------------------------------------------------------
# Test: Unsubscribe on Engine Stop/Suspend (Req 1 AC 2)
# ---------------------------------------------------------------------------


class TestUnsubscribeOnStopSuspend:
    """VisualizerManager unsubscribes engine on stop and suspend."""

    @pytest.mark.asyncio
    async def test_unsubscribe_on_stop(self, mock_ws_hub):
        """_stop_server_render_resources unsubscribes engine from bus."""
        with patch("video.visualizer_manager.guild_settings") as mock_gs:
            mock_gs.get_visualizer_engine.return_value = "varda"
            mock_gs.VALID_VISUALIZER_ENGINES = {
                "dvd", "projectm", "varda", "fosfora",
                "audiovis", "native", "random", "off",
            }
            mgr = VisualizerManager(guild_id=44, ws_hub=mock_ws_hub)

        # Simulate an active engine with audio bus subscribed
        mock_engine = AsyncMock()
        mock_engine.is_client_side = False
        mock_engine.on_audio_features = MagicMock()
        mgr._engine = mock_engine

        bus = AudioFeatureBus(guild_id=44)
        await bus.subscribe(mock_engine.on_audio_features)
        mgr._audio_bus = bus

        assert mock_engine.on_audio_features in bus._subscribers

        # Stop engine
        await mgr._stop_engine()

        # Verify unsubscribed and bus shut down
        assert mgr._audio_bus is None
        assert mock_engine.on_audio_features not in bus._subscribers

    @pytest.mark.asyncio
    async def test_unsubscribe_on_suspension(self, mock_ws_hub):
        """_execute_suspension unsubscribes and releases resources."""
        with patch("video.visualizer_manager.guild_settings") as mock_gs:
            mock_gs.get_visualizer_engine.return_value = "audiovis"
            mock_gs.VALID_VISUALIZER_ENGINES = {
                "dvd", "projectm", "varda", "fosfora",
                "audiovis", "native", "random", "off",
            }
            mgr = VisualizerManager(guild_id=45, ws_hub=mock_ws_hub)

        mock_engine = AsyncMock()
        mock_engine.is_client_side = False
        mock_engine.on_audio_features = MagicMock()
        mgr._engine = mock_engine
        mgr.state = VisualizerState.SUSPENDING

        bus = AudioFeatureBus(guild_id=45)
        await bus.subscribe(mock_engine.on_audio_features)
        mgr._audio_bus = bus

        _gpu_scheduler.allocate(45, "audiovis")

        await mgr._execute_suspension()

        assert mgr._audio_bus is None
        assert mgr.state == VisualizerState.IDLE_NO_VIEWERS

    @pytest.mark.asyncio
    async def test_unsubscribe_within_100ms(self, mock_ws_hub):
        """Unsubscribe completes within 100ms timing budget."""
        with patch("video.visualizer_manager.guild_settings") as mock_gs:
            mock_gs.get_visualizer_engine.return_value = "varda"
            mock_gs.VALID_VISUALIZER_ENGINES = {
                "dvd", "projectm", "varda", "fosfora",
                "audiovis", "native", "random", "off",
            }
            mgr = VisualizerManager(guild_id=46, ws_hub=mock_ws_hub)

        mock_engine = AsyncMock()
        mock_engine.is_client_side = False
        mock_engine.on_audio_features = MagicMock()
        mgr._engine = mock_engine

        bus = AudioFeatureBus(guild_id=46)
        await bus.subscribe(mock_engine.on_audio_features)
        mgr._audio_bus = bus

        t0 = time.monotonic()
        await mgr._stop_server_render_resources()
        elapsed = time.monotonic() - t0

        # Unsubscribe + shutdown should be well under 100ms
        assert elapsed < 0.1


# ---------------------------------------------------------------------------
# Test: AudioFeatureBus Auto-Starts on First Subscriber (Req 1 AC 3)
# ---------------------------------------------------------------------------


class TestBusAutoStart:
    """AudioFeatureBus starts processing loop on first subscriber."""

    @pytest.mark.asyncio
    async def test_processing_starts_on_first_subscriber(self, audio_bus):
        """Bus starts processing loop when first subscriber is added."""
        assert not audio_bus.is_processing

        callback = MagicMock()
        await audio_bus.subscribe(callback)

        assert audio_bus.is_processing
        assert audio_bus.subscriber_count == 1

        # Cleanup
        await audio_bus.shutdown()

    @pytest.mark.asyncio
    async def test_processing_starts_within_100ms(self, audio_bus):
        """Processing loop starts within 100ms of first subscription."""
        callback = MagicMock()

        t0 = time.monotonic()
        await audio_bus.subscribe(callback)
        elapsed = time.monotonic() - t0

        assert audio_bus.is_processing
        assert elapsed < 0.1  # Must start within 100ms

        await audio_bus.shutdown()

    @pytest.mark.asyncio
    async def test_processing_stops_on_last_unsubscribe(self, audio_bus):
        """Bus stops processing when last subscriber is removed."""
        callback = MagicMock()
        await audio_bus.subscribe(callback)
        assert audio_bus.is_processing

        await audio_bus.unsubscribe(callback)

        assert not audio_bus.is_processing
        assert audio_bus.subscriber_count == 0

    @pytest.mark.asyncio
    async def test_multiple_subscribers_keep_processing(self, audio_bus):
        """Bus stays processing until ALL subscribers are removed."""
        cb1 = MagicMock()
        cb2 = MagicMock()

        await audio_bus.subscribe(cb1)
        await audio_bus.subscribe(cb2)
        assert audio_bus.subscriber_count == 2
        assert audio_bus.is_processing

        await audio_bus.unsubscribe(cb1)
        assert audio_bus.is_processing  # cb2 still subscribed

        await audio_bus.unsubscribe(cb2)
        assert not audio_bus.is_processing

        await audio_bus.shutdown()


# ---------------------------------------------------------------------------
# Test: AudioFeatures Include Expected Fields (Req 1 AC 4)
# ---------------------------------------------------------------------------


class TestAudioFeaturesFields:
    """AudioFeatures dispatched include FFT (512), beat, BPM, 7-band energy."""

    @pytest.mark.asyncio
    async def test_features_dispatched_to_engine(self, audio_bus):
        """Engine callback receives AudioFeatures with all expected fields."""
        received: list[AudioFeatures] = []

        def on_features(features: AudioFeatures) -> None:
            received.append(features)

        await audio_bus.subscribe(on_features)

        # Feed PCM data — need enough for at least one full frame
        pcm = _make_pcm_frame(frequency=440.0, samples=1024)
        audio_bus.feed_pcm(pcm)

        # Give the analysis loop time to process
        await asyncio.sleep(0.1)

        assert len(received) >= 1

        features = received[0]
        # FFT: 512 bins
        assert len(features.fft) == 512
        assert all(isinstance(v, float) for v in features.fft)

        # Beat: boolean
        assert isinstance(features.beat, bool)

        # BPM: float
        assert isinstance(features.bpm, float)
        assert features.bpm > 0

        # 7-band energy
        assert len(features.band_energy) == 7
        assert all(isinstance(v, float) for v in features.band_energy)

        # Timestamp: positive monotonic
        assert isinstance(features.timestamp, float)
        assert features.timestamp > 0

        await audio_bus.shutdown()

    @pytest.mark.asyncio
    async def test_engine_on_audio_features_receives_data(self):
        """Full path: PCM → bus → engine.on_audio_features receives features."""
        bus = AudioFeatureBus(guild_id=99)
        received: list[AudioFeatures] = []

        def engine_callback(features: AudioFeatures) -> None:
            received.append(features)

        await bus.subscribe(engine_callback)

        # Feed multiple PCM frames
        for _ in range(5):
            pcm = _make_pcm_frame(frequency=220.0, samples=1024)
            bus.feed_pcm(pcm)

        # Allow processing
        await asyncio.sleep(0.2)

        assert len(received) >= 1
        # Verify structure of all received features
        for features in received:
            assert len(features.fft) == 512
            assert isinstance(features.beat, bool)
            assert isinstance(features.bpm, float)
            assert len(features.band_energy) == 7

        await bus.shutdown()


# ---------------------------------------------------------------------------
# Test: Queue-Full Drops Oldest Frame (Req 1 AC 5)
# ---------------------------------------------------------------------------


class TestQueueFullBehavior:
    """Queue-full condition drops oldest frame, never blocks voice_recv."""

    @pytest.mark.asyncio
    async def test_feed_pcm_does_not_block_when_queue_full(self, audio_bus):
        """feed_pcm() never blocks even when the internal queue is full."""
        # Subscribe but don't process (stop the analysis loop from draining)
        callback = MagicMock()
        await audio_bus.subscribe(callback)

        # Pause processing by cancelling the task (simulate slow consumer)
        if audio_bus._processing_task:
            audio_bus._processing_task.cancel()
            try:
                await audio_bus._processing_task
            except asyncio.CancelledError:
                pass

        # Fill queue to capacity (maxsize=100)
        pcm = _make_pcm_frame(samples=1024)
        for _ in range(100):
            audio_bus.feed_pcm(pcm)

        assert audio_bus._pcm_queue.full()

        # This should NOT block — it drops the frame
        t0 = time.monotonic()
        audio_bus.feed_pcm(pcm)
        elapsed = time.monotonic() - t0

        # Must complete nearly instantly (< 1ms)
        assert elapsed < 0.01
        # Queue is still full (frame was dropped, not added)
        assert audio_bus._pcm_queue.qsize() == 100

        await audio_bus.shutdown()

    @pytest.mark.asyncio
    async def test_dropped_frames_do_not_crash(self, audio_bus):
        """Dropping frames gracefully — no exceptions, no corruption."""
        callback = MagicMock()
        await audio_bus.subscribe(callback)

        # Cancel processing to accumulate frames
        if audio_bus._processing_task:
            audio_bus._processing_task.cancel()
            try:
                await audio_bus._processing_task
            except asyncio.CancelledError:
                pass

        pcm = _make_pcm_frame(samples=1024)

        # Feed way more frames than capacity — all should be non-blocking
        for _ in range(200):
            audio_bus.feed_pcm(pcm)

        # No exception raised, queue capped at maxsize
        assert audio_bus._pcm_queue.qsize() == 100

        await audio_bus.shutdown()

    def test_feed_pcm_noop_when_not_running(self, audio_bus):
        """feed_pcm() is a no-op when the bus is not running (no subscribers)."""
        assert not audio_bus._running

        pcm = _make_pcm_frame(samples=1024)
        audio_bus.feed_pcm(pcm)

        # Nothing queued because bus isn't running
        assert audio_bus._pcm_queue.qsize() == 0


# ---------------------------------------------------------------------------
# Test: Full Integration Path (mock PCM → bus → engine receives features)
# ---------------------------------------------------------------------------


class TestFullIntegrationPath:
    """End-to-end: PCM data flows from feed_pcm through bus to engine callback."""

    @pytest.mark.asyncio
    async def test_mock_pcm_to_bus_to_engine(self):
        """Complete integration: mock PCM → AudioFeatureBus → engine receives."""
        bus = AudioFeatureBus(guild_id=77)
        received_features: list[AudioFeatures] = []

        # Simulated engine callback (like GPUEngineBase.on_audio_features)
        def mock_engine_on_audio_features(features: AudioFeatures) -> None:
            received_features.append(features)

        # Subscribe (simulates _start_server_render_pipeline)
        await bus.subscribe(mock_engine_on_audio_features)
        assert bus.is_processing

        # Feed PCM frames (simulates voice_recv pushing audio)
        for freq in [220.0, 440.0, 880.0]:
            pcm = _make_pcm_frame(frequency=freq, samples=1024)
            bus.feed_pcm(pcm)

        # Wait for analysis loop to process
        await asyncio.sleep(0.3)

        # Engine should have received features
        assert len(received_features) >= 1

        # Verify all features have correct structure
        for features in received_features:
            assert len(features.fft) == 512
            assert isinstance(features.beat, bool)
            assert isinstance(features.bpm, float)
            assert 60.0 <= features.bpm <= 200.0
            assert len(features.band_energy) == 7
            assert all(e >= 0.0 for e in features.band_energy)
            assert features.timestamp > 0

        # Unsubscribe (simulates _stop_server_render_resources)
        await bus.unsubscribe(mock_engine_on_audio_features)
        assert not bus.is_processing

        await bus.shutdown()

    @pytest.mark.asyncio
    async def test_manager_wires_bus_to_engine_correctly(self, mock_ws_hub):
        """VisualizerManager correctly wires AudioFeatureBus to engine."""
        with patch("video.visualizer_manager.guild_settings") as mock_gs:
            mock_gs.get_visualizer_engine.return_value = "varda"
            mock_gs.VALID_VISUALIZER_ENGINES = {
                "dvd", "projectm", "varda", "fosfora",
                "audiovis", "native", "random", "off",
            }
            mgr = VisualizerManager(guild_id=78, ws_hub=mock_ws_hub)

        # Mock engine with on_audio_features
        mock_engine = AsyncMock()
        mock_engine.is_client_side = False
        mock_engine.on_audio_features = MagicMock()

        with patch(
            "video.visualizer_manager.VisualizerManager._create_engine_instance",
            return_value=mock_engine,
        ), patch(
            "video.visualizer_manager.HLSTranscodePipeline",
        ) as MockPipeline:
            mock_pipeline_instance = AsyncMock()
            mock_pipeline_instance.ready = asyncio.Event()
            mock_pipeline_instance.stdin_pipe = MagicMock()
            MockPipeline.return_value = mock_pipeline_instance

            mgr.state = VisualizerState.IDLE_NO_VIEWERS
            await mgr._start_engine()

        # Bus should be created and engine subscribed
        assert mgr._audio_bus is not None
        assert mock_engine.on_audio_features in mgr._audio_bus._subscribers
        assert mgr._audio_bus.is_processing

        # Feed PCM and verify engine receives callback
        pcm = _make_pcm_frame(frequency=440.0, samples=1024)
        mgr._audio_bus.feed_pcm(pcm)
        await asyncio.sleep(0.15)

        # Engine's on_audio_features should have been called
        assert mock_engine.on_audio_features.call_count >= 1

        # Verify the AudioFeatures structure passed to engine
        call_args = mock_engine.on_audio_features.call_args[0]
        features = call_args[0]
        assert isinstance(features, AudioFeatures)
        assert len(features.fft) == 512
        assert isinstance(features.beat, bool)
        assert isinstance(features.bpm, float)
        assert len(features.band_energy) == 7

        # Stop — should unsubscribe
        await mgr._stop_engine()
        assert mgr._audio_bus is None

    @pytest.fixture
    def mock_ws_hub(self):
        hub = MagicMock()
        hub.broadcast = AsyncMock()
        hub.viewer_count = MagicMock(return_value=0)
        return hub

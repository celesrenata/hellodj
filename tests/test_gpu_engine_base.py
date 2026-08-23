"""Tests for the GPU Engine Base Class with mocked EGL context.

Covers: properties, activate/suspend/resume/stop lifecycle,
on_audio_features atomic reference swap, render_frames at 30fps,
and consumes_gpu_while_suspended invariant.

Requirements: Req 2 (AC 1-5), Req 11 (AC 4), Req 12 (AC 4)
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from video.visualizer_engines.base import AudioFeatures, TrackMetadata
from video.visualizer_engines.gpu_engine_base import GPUEngineBase


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------

FAKE_FRAME = b"\x00" * (1280 * 720 * 4)  # 3,686,400 bytes RGBA


class ConcreteGPUEngine(GPUEngineBase):
    """Minimal concrete subclass for testing the base class."""

    def __init__(self) -> None:
        super().__init__()
        self.gl_ready_called = False
        self.gl_ready_metadata: TrackMetadata | None = None
        self.render_count = 0

    async def _on_gl_ready(self, metadata: TrackMetadata | None) -> None:
        self.gl_ready_called = True
        self.gl_ready_metadata = metadata

    def _render_gl_frame(self, features: AudioFeatures | None) -> None:
        self.render_count += 1


def _make_mock_egl():
    """Create a mock EGLHeadlessContext."""
    mock = MagicMock()
    mock.create.return_value = None
    mock.make_current.return_value = None
    mock.read_pixels.return_value = FAKE_FRAME
    mock.destroy.return_value = None
    mock.is_valid = True
    return mock


def _make_features(beat: bool = False, bpm: float = 120.0) -> AudioFeatures:
    """Create a sample AudioFeatures instance."""
    return AudioFeatures(
        fft=[0.0] * 512,
        beat=beat,
        bpm=bpm,
        band_energy=[0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7],
        timestamp=time.monotonic(),
    )


def _make_metadata() -> TrackMetadata:
    """Create a sample TrackMetadata instance."""
    return TrackMetadata(
        title="Test Song",
        artist="Test Artist",
        artwork_url=None,
        duration_ms=180000,
        position_ms=0,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def engine() -> ConcreteGPUEngine:
    """Create a ConcreteGPUEngine instance."""
    return ConcreteGPUEngine()


@pytest.fixture
def mock_egl_class():
    """Patch EGLHeadlessContext to return a mock."""
    mock_ctx = _make_mock_egl()
    with patch(
        "video.visualizer_engines.gpu_engine_base.EGLHeadlessContext",
        return_value=mock_ctx,
    ) as mock_cls:
        mock_cls._instance = mock_ctx
        yield mock_cls


# ---------------------------------------------------------------------------
# Unit Tests — Properties
# ---------------------------------------------------------------------------


class TestProperties:
    """GPUEngineBase properties return correct values."""

    def test_is_client_side_is_false(self, engine):
        assert engine.is_client_side is False

    def test_consumes_gpu_while_suspended_is_false(self, engine):
        assert engine.consumes_gpu_while_suspended is False

    def test_client_config_is_none(self, engine):
        assert engine.client_config is None

    def test_target_fps(self, engine):
        assert engine.TARGET_FPS == 30

    def test_frame_interval(self, engine):
        assert abs(engine.FRAME_INTERVAL - 1.0 / 30) < 1e-9


# ---------------------------------------------------------------------------
# Unit Tests — activate()
# ---------------------------------------------------------------------------


class TestActivate:
    """activate() creates EGL context and calls _on_gl_ready."""

    @pytest.mark.asyncio
    async def test_creates_egl_context(self, engine, mock_egl_class):
        await engine.activate()
        mock_egl_class.assert_called_once()

    @pytest.mark.asyncio
    async def test_calls_egl_create(self, engine, mock_egl_class):
        await engine.activate()
        mock_egl_class._instance.create.assert_called_once()

    @pytest.mark.asyncio
    async def test_sets_running_true(self, engine, mock_egl_class):
        await engine.activate()
        assert engine._running is True

    @pytest.mark.asyncio
    async def test_calls_on_gl_ready(self, engine, mock_egl_class):
        await engine.activate()
        assert engine.gl_ready_called is True

    @pytest.mark.asyncio
    async def test_passes_metadata_to_on_gl_ready(self, engine, mock_egl_class):
        metadata = _make_metadata()
        await engine.activate(metadata)
        assert engine.gl_ready_metadata is metadata

    @pytest.mark.asyncio
    async def test_stores_egl_context(self, engine, mock_egl_class):
        await engine.activate()
        assert engine._egl_ctx is mock_egl_class._instance


# ---------------------------------------------------------------------------
# Unit Tests — suspend()
# ---------------------------------------------------------------------------


class TestSuspend:
    """suspend() destroys EGL context and sets it to None."""

    @pytest.mark.asyncio
    async def test_sets_running_false(self, engine, mock_egl_class):
        await engine.activate()
        await engine.suspend()
        assert engine._running is False

    @pytest.mark.asyncio
    async def test_destroys_egl_context(self, engine, mock_egl_class):
        await engine.activate()
        await engine.suspend()
        mock_egl_class._instance.destroy.assert_called_once()

    @pytest.mark.asyncio
    async def test_sets_egl_ctx_to_none(self, engine, mock_egl_class):
        await engine.activate()
        await engine.suspend()
        assert engine._egl_ctx is None

    @pytest.mark.asyncio
    async def test_suspend_without_activate_is_safe(self, engine):
        # Should not raise
        await engine.suspend()
        assert engine._running is False
        assert engine._egl_ctx is None


# ---------------------------------------------------------------------------
# Unit Tests — resume()
# ---------------------------------------------------------------------------


class TestResume:
    """resume() re-creates context via activate()."""

    @pytest.mark.asyncio
    async def test_recreates_egl_context(self, engine, mock_egl_class):
        await engine.activate()
        await engine.suspend()
        # Reset the mock to track the second creation
        mock_egl_class.reset_mock()
        mock_egl_class.return_value = _make_mock_egl()
        await engine.resume()
        mock_egl_class.assert_called_once()

    @pytest.mark.asyncio
    async def test_sets_running_true_again(self, engine, mock_egl_class):
        await engine.activate()
        await engine.suspend()
        assert engine._running is False
        mock_egl_class.return_value = _make_mock_egl()
        await engine.resume()
        assert engine._running is True

    @pytest.mark.asyncio
    async def test_calls_on_gl_ready_again(self, engine, mock_egl_class):
        await engine.activate()
        engine.gl_ready_called = False
        await engine.suspend()
        mock_egl_class.return_value = _make_mock_egl()
        await engine.resume()
        assert engine.gl_ready_called is True

    @pytest.mark.asyncio
    async def test_passes_metadata(self, engine, mock_egl_class):
        metadata = _make_metadata()
        await engine.activate()
        await engine.suspend()
        mock_egl_class.return_value = _make_mock_egl()
        await engine.resume(metadata)
        assert engine.gl_ready_metadata is metadata


# ---------------------------------------------------------------------------
# Unit Tests — stop()
# ---------------------------------------------------------------------------


class TestStop:
    """stop() destroys context and all resources."""

    @pytest.mark.asyncio
    async def test_sets_running_false(self, engine, mock_egl_class):
        await engine.activate()
        await engine.stop()
        assert engine._running is False

    @pytest.mark.asyncio
    async def test_destroys_egl_context(self, engine, mock_egl_class):
        await engine.activate()
        await engine.stop()
        mock_egl_class._instance.destroy.assert_called_once()

    @pytest.mark.asyncio
    async def test_sets_egl_ctx_to_none(self, engine, mock_egl_class):
        await engine.activate()
        await engine.stop()
        assert engine._egl_ctx is None

    @pytest.mark.asyncio
    async def test_stop_without_activate_is_safe(self, engine):
        await engine.stop()
        assert engine._running is False
        assert engine._egl_ctx is None


# ---------------------------------------------------------------------------
# Unit Tests — on_audio_features()
# ---------------------------------------------------------------------------


class TestOnAudioFeatures:
    """on_audio_features() stores features atomically."""

    def test_stores_features(self, engine):
        features = _make_features()
        engine.on_audio_features(features)
        assert engine._latest_features is features

    def test_overwrites_previous_features(self, engine):
        features1 = _make_features(beat=False)
        features2 = _make_features(beat=True)
        engine.on_audio_features(features1)
        engine.on_audio_features(features2)
        assert engine._latest_features is features2

    def test_initial_features_is_none(self, engine):
        assert engine._latest_features is None

    def test_reference_swap_is_atomic(self, engine):
        """The assignment is a single reference swap — non-blocking."""
        features = _make_features(bpm=140.0)
        engine.on_audio_features(features)
        # Verify the exact object (not a copy) is stored
        assert engine._latest_features is features
        assert engine._latest_features.bpm == 140.0


# ---------------------------------------------------------------------------
# Unit Tests — render_frames()
# ---------------------------------------------------------------------------


class TestRenderFrames:
    """render_frames() yields RGBA frames at 30fps with sleep timing."""

    @pytest.mark.asyncio
    async def test_yields_frames(self, engine, mock_egl_class):
        await engine.activate()
        frames = []
        count = 0
        async for frame in engine.render_frames():
            frames.append(frame)
            count += 1
            if count >= 3:
                engine._running = False
        assert len(frames) == 3

    @pytest.mark.asyncio
    async def test_frame_size_is_correct(self, engine, mock_egl_class):
        await engine.activate()
        async for frame in engine.render_frames():
            assert len(frame) == 1280 * 720 * 4
            engine._running = False

    @pytest.mark.asyncio
    async def test_calls_make_current(self, engine, mock_egl_class):
        await engine.activate()
        async for _ in engine.render_frames():
            engine._running = False
        mock_egl_class._instance.make_current.assert_called()

    @pytest.mark.asyncio
    async def test_calls_read_pixels(self, engine, mock_egl_class):
        await engine.activate()
        async for _ in engine.render_frames():
            engine._running = False
        mock_egl_class._instance.read_pixels.assert_called()

    @pytest.mark.asyncio
    async def test_calls_render_gl_frame(self, engine, mock_egl_class):
        await engine.activate()
        async for _ in engine.render_frames():
            engine._running = False
        assert engine.render_count == 1

    @pytest.mark.asyncio
    async def test_passes_latest_features_to_render(self, engine, mock_egl_class):
        features = _make_features(beat=True)
        engine.on_audio_features(features)
        await engine.activate()

        received_features = []
        original_render = engine._render_gl_frame

        def capture_render(f):
            received_features.append(f)
            original_render(f)

        engine._render_gl_frame = capture_render

        async for _ in engine.render_frames():
            engine._running = False

        assert received_features[0] is features

    @pytest.mark.asyncio
    async def test_stops_when_running_false(self, engine, mock_egl_class):
        await engine.activate()
        engine._running = False
        frames = []
        async for frame in engine.render_frames():
            frames.append(frame)
        assert len(frames) == 0

    @pytest.mark.asyncio
    async def test_frame_timing_uses_sleep(self, engine, mock_egl_class):
        """Verify asyncio.sleep is called for frame pacing."""
        await engine.activate()
        with patch("video.visualizer_engines.gpu_engine_base.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            count = 0
            async for _ in engine.render_frames():
                count += 1
                if count >= 2:
                    engine._running = False
            # Sleep should have been called (rendering is fast with mocks)
            assert mock_sleep.call_count >= 1


# ---------------------------------------------------------------------------
# Unit Tests — consumes_gpu_while_suspended invariant
# ---------------------------------------------------------------------------


class TestGPUConsumption:
    """consumes_gpu_while_suspended is always False regardless of state."""

    def test_before_activate(self, engine):
        assert engine.consumes_gpu_while_suspended is False

    @pytest.mark.asyncio
    async def test_while_active(self, engine, mock_egl_class):
        await engine.activate()
        assert engine.consumes_gpu_while_suspended is False

    @pytest.mark.asyncio
    async def test_after_suspend(self, engine, mock_egl_class):
        await engine.activate()
        await engine.suspend()
        assert engine.consumes_gpu_while_suspended is False

    @pytest.mark.asyncio
    async def test_after_resume(self, engine, mock_egl_class):
        await engine.activate()
        await engine.suspend()
        mock_egl_class.return_value = _make_mock_egl()
        await engine.resume()
        assert engine.consumes_gpu_while_suspended is False

    @pytest.mark.asyncio
    async def test_after_stop(self, engine, mock_egl_class):
        await engine.activate()
        await engine.stop()
        assert engine.consumes_gpu_while_suspended is False


# ---------------------------------------------------------------------------
# Unit Tests — Subclass hooks
# ---------------------------------------------------------------------------


class TestSubclassHooks:
    """Subclass hooks raise NotImplementedError if not overridden."""

    @pytest.mark.asyncio
    async def test_on_gl_ready_raises_if_not_implemented(self):
        """A bare GPUEngineBase (without overrides) raises NotImplementedError."""

        class BareEngine(GPUEngineBase):
            def _render_gl_frame(self, features):
                pass

        engine = BareEngine()
        with patch(
            "video.visualizer_engines.gpu_engine_base.EGLHeadlessContext",
            return_value=_make_mock_egl(),
        ):
            with pytest.raises(NotImplementedError):
                await engine.activate()

    def test_render_gl_frame_raises_if_not_implemented(self):
        """A bare GPUEngineBase (without overrides) raises NotImplementedError."""

        class BareEngine(GPUEngineBase):
            async def _on_gl_ready(self, metadata):
                pass

        engine = BareEngine()
        with pytest.raises(NotImplementedError):
            engine._render_gl_frame(None)

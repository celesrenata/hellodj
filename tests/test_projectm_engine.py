"""Tests for the projectM Engine with mocked libprojectM ctypes bindings.

Covers: library loading, instance creation/destruction, configuration,
audio feed, frame rendering, track change blend, preset path resolution,
suspend/resume lifecycle, and category listing.

Requirements: Req 5 (AC 1-6), Req 17 (AC 1-3)
"""

from __future__ import annotations

import ctypes
import time
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from video.visualizer_engines.base import AudioFeatures, TrackMetadata
from video.visualizer_engines.projectm import (
    BLEND_DURATION,
    DEFAULT_PRESET_DURATION,
    DEFAULT_SENSITIVITY,
    HEIGHT,
    PRESET_DIR,
    WIDTH,
    ProjectMEngine,
)


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------

FAKE_FRAME = b"\x00" * (1280 * 720 * 4)


def _make_features(beat: bool = False, bpm: float = 120.0) -> AudioFeatures:
    """Create a sample AudioFeatures instance."""
    return AudioFeatures(
        fft=[float(i) / 512.0 for i in range(512)],
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


def _make_mock_egl():
    """Create a mock EGLHeadlessContext."""
    mock = MagicMock()
    mock.create.return_value = None
    mock.make_current.return_value = None
    mock.read_pixels.return_value = FAKE_FRAME
    mock.destroy.return_value = None
    mock.is_valid = True
    return mock


def _make_mock_lib():
    """Create a mock libprojectM CDLL with all required function stubs."""
    lib = MagicMock()

    # projectm_create returns a fake handle (non-null pointer value)
    lib.projectm_create.return_value = 0xDEADBEEF

    # All other functions return None (void)
    lib.projectm_destroy.return_value = None
    lib.projectm_set_window_size.return_value = None
    lib.projectm_set_preset_duration.return_value = None
    lib.projectm_set_soft_cut_duration.return_value = None
    lib.projectm_set_beat_sensitivity.return_value = None
    lib.projectm_set_preset_path.return_value = None
    lib.projectm_set_shuffle_enabled.return_value = None
    lib.projectm_pcm_add_float.return_value = None
    lib.projectm_opengl_render_frame.return_value = None
    lib.projectm_select_random_preset.return_value = None

    return lib


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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


@pytest.fixture
def mock_lib():
    """Patch ctypes.CDLL to return a mock libprojectM."""
    lib = _make_mock_lib()
    with patch(
        "video.visualizer_engines.projectm.ctypes.CDLL",
        return_value=lib,
    ):
        yield lib


@pytest.fixture
def engine():
    """Create a ProjectMEngine instance with default config."""
    return ProjectMEngine()


@pytest.fixture
def configured_engine():
    """Create a ProjectMEngine with custom config."""
    return ProjectMEngine(
        preset_category="Abstract",
        blend_duration=5.0,
        preset_duration=60.0,
        brightness=1.5,
        sensitivity=1.8,
    )


# ---------------------------------------------------------------------------
# Unit Tests — Properties & Inheritance
# ---------------------------------------------------------------------------


class TestProperties:
    """ProjectMEngine inherits correct properties from GPUEngineBase."""

    def test_is_client_side_false(self, engine):
        assert engine.is_client_side is False

    def test_consumes_gpu_while_suspended_false(self, engine):
        assert engine.consumes_gpu_while_suspended is False

    def test_client_config_none(self, engine):
        assert engine.client_config is None

    def test_inherits_from_gpu_engine_base(self, engine):
        from video.visualizer_engines.gpu_engine_base import GPUEngineBase
        assert isinstance(engine, GPUEngineBase)


# ---------------------------------------------------------------------------
# Unit Tests — Constructor / Config
# ---------------------------------------------------------------------------


class TestConstructor:
    """Constructor stores configurable parameters."""

    def test_default_preset_category(self, engine):
        assert engine._preset_category == "all"

    def test_default_blend_duration(self, engine):
        assert engine._blend_duration == BLEND_DURATION

    def test_default_preset_duration(self, engine):
        assert engine._preset_duration == DEFAULT_PRESET_DURATION

    def test_default_sensitivity(self, engine):
        assert engine._sensitivity == DEFAULT_SENSITIVITY

    def test_custom_config(self, configured_engine):
        assert configured_engine._preset_category == "Abstract"
        assert configured_engine._blend_duration == 5.0
        assert configured_engine._preset_duration == 60.0
        assert configured_engine._brightness == 1.5
        assert configured_engine._sensitivity == 1.8

    def test_pm_handle_initially_none(self, engine):
        assert engine._pm_handle is None

    def test_lib_initially_none(self, engine):
        assert engine._lib is None


# ---------------------------------------------------------------------------
# Unit Tests — _on_gl_ready (library load + instance creation)
# ---------------------------------------------------------------------------


class TestOnGlReady:
    """_on_gl_ready loads libprojectM and creates configured instance."""

    @pytest.mark.asyncio
    async def test_loads_library(self, engine, mock_egl_class, mock_lib):
        await engine.activate()
        assert engine._lib is not None

    @pytest.mark.asyncio
    async def test_creates_projectm_instance(self, engine, mock_egl_class, mock_lib):
        await engine.activate()
        mock_lib.projectm_create.assert_called_once()
        assert engine._pm_handle is not None

    @pytest.mark.asyncio
    async def test_sets_window_size(self, engine, mock_egl_class, mock_lib):
        await engine.activate()
        mock_lib.projectm_set_window_size.assert_called_once_with(
            engine._pm_handle, WIDTH, HEIGHT
        )

    @pytest.mark.asyncio
    async def test_sets_preset_duration(self, engine, mock_egl_class, mock_lib):
        await engine.activate()
        call_args = mock_lib.projectm_set_preset_duration.call_args
        assert call_args[0][0] == engine._pm_handle
        # c_double wraps the value
        assert float(call_args[0][1].value) == DEFAULT_PRESET_DURATION

    @pytest.mark.asyncio
    async def test_sets_blend_duration(self, engine, mock_egl_class, mock_lib):
        await engine.activate()
        call_args = mock_lib.projectm_set_soft_cut_duration.call_args
        assert call_args[0][0] == engine._pm_handle
        assert float(call_args[0][1].value) == BLEND_DURATION

    @pytest.mark.asyncio
    async def test_sets_beat_sensitivity(self, engine, mock_egl_class, mock_lib):
        await engine.activate()
        call_args = mock_lib.projectm_set_beat_sensitivity.call_args
        assert call_args[0][0] == engine._pm_handle
        assert float(call_args[0][1].value) == DEFAULT_SENSITIVITY

    @pytest.mark.asyncio
    async def test_sets_preset_path(self, engine, mock_egl_class, mock_lib):
        await engine.activate()
        call_args = mock_lib.projectm_set_preset_path.call_args
        assert call_args[0][0] == engine._pm_handle
        # Default category "all" → base PRESET_DIR
        assert call_args[0][1] == PRESET_DIR.encode("utf-8")

    @pytest.mark.asyncio
    async def test_enables_shuffle(self, engine, mock_egl_class, mock_lib):
        await engine.activate()
        call_args = mock_lib.projectm_set_shuffle_enabled.call_args
        assert call_args[0][0] == engine._pm_handle

    @pytest.mark.asyncio
    async def test_stores_metadata(self, engine, mock_egl_class, mock_lib):
        metadata = _make_metadata()
        await engine.activate(metadata)
        assert engine._metadata is metadata

    @pytest.mark.asyncio
    async def test_null_handle_raises(self, engine, mock_egl_class, mock_lib):
        mock_lib.projectm_create.return_value = None
        with pytest.raises(RuntimeError, match="projectm_create.*NULL"):
            await engine.activate()


# ---------------------------------------------------------------------------
# Unit Tests — _render_gl_frame (audio feed + render)
# ---------------------------------------------------------------------------


class TestRenderGlFrame:
    """_render_gl_frame feeds audio data and renders a frame."""

    @pytest.mark.asyncio
    async def test_renders_frame_without_features(self, engine, mock_egl_class, mock_lib):
        await engine.activate()
        engine._render_gl_frame(None)
        mock_lib.projectm_opengl_render_frame.assert_called_once_with(
            engine._pm_handle
        )

    @pytest.mark.asyncio
    async def test_feeds_fft_data(self, engine, mock_egl_class, mock_lib):
        await engine.activate()
        features = _make_features()
        engine._render_gl_frame(features)
        mock_lib.projectm_pcm_add_float.assert_called_once()
        call_args = mock_lib.projectm_pcm_add_float.call_args
        assert call_args[0][0] == engine._pm_handle
        # num_samples=512, channels=1
        assert call_args[0][2] == 512
        assert call_args[0][3] == 1

    @pytest.mark.asyncio
    async def test_renders_after_feeding_audio(self, engine, mock_egl_class, mock_lib):
        await engine.activate()
        features = _make_features()
        engine._render_gl_frame(features)
        # Both pcm_add_float and render_frame should be called
        mock_lib.projectm_pcm_add_float.assert_called_once()
        mock_lib.projectm_opengl_render_frame.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_op_without_handle(self, engine, mock_egl_class, mock_lib):
        # Don't activate — handle is None
        engine._render_gl_frame(_make_features())
        mock_lib.projectm_opengl_render_frame.assert_not_called()
        mock_lib.projectm_pcm_add_float.assert_not_called()


# ---------------------------------------------------------------------------
# Unit Tests — on_track_change (Req 5 AC 4)
# ---------------------------------------------------------------------------


class TestTrackChange:
    """on_track_change triggers random preset with smooth blend."""

    @pytest.mark.asyncio
    async def test_selects_random_preset(self, engine, mock_egl_class, mock_lib):
        await engine.activate()
        metadata = _make_metadata()
        await engine.on_track_change(metadata)
        mock_lib.projectm_select_random_preset.assert_called_once()

    @pytest.mark.asyncio
    async def test_uses_soft_cut(self, engine, mock_egl_class, mock_lib):
        await engine.activate()
        # Reset call count from _configure_instance
        mock_lib.projectm_set_soft_cut_duration.reset_mock()

        metadata = _make_metadata()
        await engine.on_track_change(metadata)

        # Should set soft cut duration before selecting preset
        mock_lib.projectm_set_soft_cut_duration.assert_called_once()
        call_args = mock_lib.projectm_set_soft_cut_duration.call_args
        assert float(call_args[0][1].value) == BLEND_DURATION

    @pytest.mark.asyncio
    async def test_hard_cut_is_false(self, engine, mock_egl_class, mock_lib):
        await engine.activate()
        metadata = _make_metadata()
        await engine.on_track_change(metadata)
        call_args = mock_lib.projectm_select_random_preset.call_args
        # hard_cut should be False for smooth blend
        assert call_args[0][1].value is False

    @pytest.mark.asyncio
    async def test_updates_metadata(self, engine, mock_egl_class, mock_lib):
        await engine.activate()
        metadata = _make_metadata()
        await engine.on_track_change(metadata)
        assert engine._metadata is metadata

    @pytest.mark.asyncio
    async def test_no_op_without_handle(self, engine, mock_egl_class, mock_lib):
        # Don't activate
        metadata = _make_metadata()
        await engine.on_track_change(metadata)
        mock_lib.projectm_select_random_preset.assert_not_called()


# ---------------------------------------------------------------------------
# Unit Tests — suspend / resume (Req 5 AC 5)
# ---------------------------------------------------------------------------


class TestSuspendResume:
    """Suspend destroys projectM + EGL; resume recreates both."""

    @pytest.mark.asyncio
    async def test_suspend_destroys_projectm(self, engine, mock_egl_class, mock_lib):
        await engine.activate()
        await engine.suspend()
        mock_lib.projectm_destroy.assert_called_once_with(engine._pm_handle or mock_lib.projectm_create.return_value)

    @pytest.mark.asyncio
    async def test_suspend_nulls_handle(self, engine, mock_egl_class, mock_lib):
        await engine.activate()
        await engine.suspend()
        assert engine._pm_handle is None

    @pytest.mark.asyncio
    async def test_suspend_destroys_egl(self, engine, mock_egl_class, mock_lib):
        await engine.activate()
        await engine.suspend()
        mock_egl_class._instance.destroy.assert_called_once()
        assert engine._egl_ctx is None

    @pytest.mark.asyncio
    async def test_resume_recreates_instance(self, engine, mock_egl_class, mock_lib):
        await engine.activate()
        await engine.suspend()
        mock_lib.projectm_create.reset_mock()
        mock_egl_class.return_value = _make_mock_egl()
        await engine.resume()
        mock_lib.projectm_create.assert_called_once()
        assert engine._pm_handle is not None

    @pytest.mark.asyncio
    async def test_resume_preserves_metadata(self, engine, mock_egl_class, mock_lib):
        metadata = _make_metadata()
        await engine.activate(metadata)
        await engine.suspend()
        mock_egl_class.return_value = _make_mock_egl()
        await engine.resume()
        assert engine._metadata is metadata


# ---------------------------------------------------------------------------
# Unit Tests — stop (full shutdown)
# ---------------------------------------------------------------------------


class TestStop:
    """stop() destroys projectM and EGL context."""

    @pytest.mark.asyncio
    async def test_stop_destroys_projectm(self, engine, mock_egl_class, mock_lib):
        await engine.activate()
        await engine.stop()
        mock_lib.projectm_destroy.assert_called_once()

    @pytest.mark.asyncio
    async def test_stop_nulls_handle(self, engine, mock_egl_class, mock_lib):
        await engine.activate()
        await engine.stop()
        assert engine._pm_handle is None

    @pytest.mark.asyncio
    async def test_stop_destroys_egl(self, engine, mock_egl_class, mock_lib):
        await engine.activate()
        await engine.stop()
        mock_egl_class._instance.destroy.assert_called_once()

    @pytest.mark.asyncio
    async def test_stop_without_activate_safe(self, engine, mock_lib):
        await engine.stop()
        mock_lib.projectm_destroy.assert_not_called()


# ---------------------------------------------------------------------------
# Unit Tests — _resolve_preset_path (Req 17 AC 1-3)
# ---------------------------------------------------------------------------


class TestResolvePresetPath:
    """_resolve_preset_path resolves category subfolders."""

    def test_all_category_returns_base_dir(self, engine):
        engine._preset_category = "all"
        result = engine._resolve_preset_path()
        assert result == PRESET_DIR

    def test_existing_category_returns_subfolder(self, engine, tmp_path):
        # Create a fake category directory
        with patch(
            "video.visualizer_engines.projectm.PRESET_DIR",
            str(tmp_path),
        ):
            category_dir = tmp_path / "Abstract"
            category_dir.mkdir()
            engine._preset_category = "Abstract"
            result = engine._resolve_preset_path()
            assert result == str(category_dir)

    def test_nonexistent_category_falls_back_to_base(self, engine, tmp_path):
        with patch(
            "video.visualizer_engines.projectm.PRESET_DIR",
            str(tmp_path),
        ):
            engine._preset_category = "NonExistent"
            result = engine._resolve_preset_path()
            assert result == str(tmp_path)


# ---------------------------------------------------------------------------
# Unit Tests — get_available_categories (Req 17 AC 4)
# ---------------------------------------------------------------------------


class TestGetAvailableCategories:
    """get_available_categories lists categories with preset counts."""

    def test_returns_categories_with_counts(self, tmp_path):
        with patch(
            "video.visualizer_engines.projectm.PRESET_DIR",
            str(tmp_path),
        ):
            # Create category dirs with preset files
            abstract = tmp_path / "Abstract"
            abstract.mkdir()
            (abstract / "preset1.milk").touch()
            (abstract / "preset2.milk").touch()
            (abstract / "readme.txt").touch()  # Non-preset file

            space = tmp_path / "Space"
            space.mkdir()
            (space / "stars.prjm").touch()

            categories = ProjectMEngine.get_available_categories()
            assert categories == {"Abstract": 2, "Space": 1}

    def test_excludes_empty_directories(self, tmp_path):
        with patch(
            "video.visualizer_engines.projectm.PRESET_DIR",
            str(tmp_path),
        ):
            empty = tmp_path / "Empty"
            empty.mkdir()

            categories = ProjectMEngine.get_available_categories()
            assert categories == {}

    def test_nonexistent_base_dir_returns_empty(self):
        with patch(
            "video.visualizer_engines.projectm.PRESET_DIR",
            "/nonexistent/path",
        ):
            categories = ProjectMEngine.get_available_categories()
            assert categories == {}


# ---------------------------------------------------------------------------
# Unit Tests — Library loading
# ---------------------------------------------------------------------------


class TestLibraryLoading:
    """Library loading tries alternate SO names."""

    @pytest.mark.asyncio
    async def test_tries_alternate_name_on_failure(self, engine, mock_egl_class):
        call_count = 0
        lib_mock = _make_mock_lib()

        def fake_cdll(name):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise OSError("not found")
            return lib_mock

        with patch("video.visualizer_engines.projectm.ctypes.CDLL", side_effect=fake_cdll):
            await engine.activate()
            assert call_count == 2  # Tried both names
            assert engine._lib is lib_mock

    @pytest.mark.asyncio
    async def test_raises_if_both_names_fail(self, engine, mock_egl_class):
        with patch(
            "video.visualizer_engines.projectm.ctypes.CDLL",
            side_effect=OSError("not found"),
        ):
            with pytest.raises(OSError, match="not found"):
                await engine.activate()


# ---------------------------------------------------------------------------
# Unit Tests — render_frames integration (via GPUEngineBase)
# ---------------------------------------------------------------------------


class TestRenderFramesIntegration:
    """Full render loop produces frames via mocked infrastructure."""

    @pytest.mark.asyncio
    async def test_yields_frames(self, engine, mock_egl_class, mock_lib):
        await engine.activate()
        frames = []
        count = 0
        async for frame in engine.render_frames():
            frames.append(frame)
            count += 1
            if count >= 3:
                engine._running = False
        assert len(frames) == 3
        assert all(len(f) == 1280 * 720 * 4 for f in frames)

    @pytest.mark.asyncio
    async def test_feeds_audio_each_frame(self, engine, mock_egl_class, mock_lib):
        await engine.activate()
        features = _make_features(beat=True, bpm=140.0)
        engine.on_audio_features(features)

        count = 0
        async for _ in engine.render_frames():
            count += 1
            if count >= 2:
                engine._running = False

        # projectm_pcm_add_float called for each frame with features
        assert mock_lib.projectm_pcm_add_float.call_count == 2

    @pytest.mark.asyncio
    async def test_renders_each_frame(self, engine, mock_egl_class, mock_lib):
        await engine.activate()
        count = 0
        async for _ in engine.render_frames():
            count += 1
            if count >= 2:
                engine._running = False

        assert mock_lib.projectm_opengl_render_frame.call_count == 2


# ---------------------------------------------------------------------------
# Unit Tests — Custom blend duration for track changes
# ---------------------------------------------------------------------------


class TestCustomBlendDuration:
    """Custom blend_duration config is used in track changes."""

    @pytest.mark.asyncio
    async def test_custom_blend_on_track_change(self, mock_egl_class, mock_lib):
        engine = ProjectMEngine(blend_duration=5.0)
        await engine.activate()
        mock_lib.projectm_set_soft_cut_duration.reset_mock()

        await engine.on_track_change(_make_metadata())

        call_args = mock_lib.projectm_set_soft_cut_duration.call_args
        assert float(call_args[0][1].value) == 5.0

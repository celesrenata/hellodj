"""Tests for the AudioVis Engine with mocked GL context.

Covers: shader loading, compilation, FFT texture upload, uniform setting,
beat pulse decay, track metadata handling, style selection, and configuration.

Requirements: Req 6 (AC 1-5)
"""

from __future__ import annotations

import ctypes
import time
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

from video.visualizer_engines.audiovis import (
    AudioVisEngine,
    BEAT_DECAY_PER_FRAME,
    DEFAULT_BG_OPACITY,
    DEFAULT_FFT_BINS,
    DEFAULT_GLOW_INTENSITY,
    DEFAULT_STYLE,
    SHADER_DIR,
    STYLE_REGISTRY,
    get_valid_styles,
)
from video.visualizer_engines.base import AudioFeatures, TrackMetadata


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

FAKE_FRAME = b"\x00" * (1280 * 720 * 4)


def _make_features(
    beat: bool = False, bpm: float = 120.0, fft: list[float] | None = None
) -> AudioFeatures:
    """Create a sample AudioFeatures instance."""
    return AudioFeatures(
        fft=fft if fft is not None else [0.1] * 512,
        beat=beat,
        bpm=bpm,
        band_energy=[0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7],
        timestamp=time.monotonic(),
    )


def _make_metadata(
    title: str = "Test Song", artist: str = "Test Artist"
) -> TrackMetadata:
    return TrackMetadata(
        title=title,
        artist=artist,
        artwork_url=None,
        duration_ms=180000,
        position_ms=0,
    )


def _make_mock_gl():
    """Create a mock GL library (ctypes CDLL mock)."""
    gl = MagicMock()
    # Shader compilation returns valid shader/program IDs
    gl.glCreateShader.return_value = 1
    gl.glCreateProgram.return_value = 100

    # Compile/link status checks return success
    def get_shader_iv(shader, pname, params):
        params._obj.value = 1  # GL_TRUE

    def get_program_iv(program, pname, params):
        params._obj.value = 1  # GL_TRUE

    gl.glGetShaderiv.side_effect = get_shader_iv
    gl.glGetProgramiv.side_effect = get_program_iv

    # Uniform locations
    _uniform_counter = [0]

    def get_uniform_location(program, name):
        _uniform_counter[0] += 1
        return _uniform_counter[0]

    gl.glGetUniformLocation.side_effect = get_uniform_location

    # Texture/VAO generation
    def gen_textures(n, ptr):
        ptr._obj.value = 42

    def gen_vertex_arrays(n, ptr):
        ptr._obj.value = 99

    gl.glGenTextures.side_effect = gen_textures
    gl.glGenVertexArrays.side_effect = gen_vertex_arrays

    return gl


def _make_mock_egl(gl=None):
    """Create a mock EGLHeadlessContext."""
    mock = MagicMock()
    mock.create.return_value = None
    mock.make_current.return_value = None
    mock.read_pixels.return_value = FAKE_FRAME
    mock.destroy.return_value = None
    mock.is_valid = True
    mock._gl = gl or _make_mock_gl()
    return mock


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_gl():
    """Provide a mock GL library."""
    return _make_mock_gl()


@pytest.fixture
def mock_egl_ctx(mock_gl):
    """Provide a mock EGL context with mock GL."""
    return _make_mock_egl(mock_gl)


@pytest.fixture
def engine():
    """Create a default AudioVisEngine."""
    return AudioVisEngine()


@pytest.fixture
def mock_egl_class(mock_egl_ctx):
    """Patch EGLHeadlessContext to return a mock."""
    with patch(
        "video.visualizer_engines.gpu_engine_base.EGLHeadlessContext",
        return_value=mock_egl_ctx,
    ) as mock_cls:
        mock_cls._instance = mock_egl_ctx
        yield mock_cls


# ---------------------------------------------------------------------------
# Tests — Initialization & Configuration
# ---------------------------------------------------------------------------


class TestConfiguration:
    """AudioVisEngine accepts and validates configuration."""

    def test_default_style(self):
        engine = AudioVisEngine()
        assert engine.style == "bars"

    def test_custom_style(self):
        engine = AudioVisEngine(style="waveform")
        assert engine.style == "waveform"

    def test_invalid_style_falls_back_to_default(self):
        engine = AudioVisEngine(style="invalid")
        assert engine.style == DEFAULT_STYLE

    def test_all_valid_styles(self):
        for style in get_valid_styles():
            engine = AudioVisEngine(style=style)
            assert engine.style == style

    def test_default_fft_bins(self):
        engine = AudioVisEngine()
        assert engine.fft_bins == DEFAULT_FFT_BINS

    def test_custom_fft_bins(self):
        engine = AudioVisEngine(fft_bins=128)
        assert engine.fft_bins == 128

    def test_default_glow_intensity(self):
        engine = AudioVisEngine()
        assert engine.glow_intensity == DEFAULT_GLOW_INTENSITY

    def test_custom_glow_intensity(self):
        engine = AudioVisEngine(glow_intensity=1.5)
        assert engine.glow_intensity == 1.5

    def test_default_bg_opacity(self):
        engine = AudioVisEngine()
        assert engine.background_opacity == DEFAULT_BG_OPACITY

    def test_custom_bg_opacity(self):
        engine = AudioVisEngine(background_opacity=0.5)
        assert engine.background_opacity == 0.5

    def test_color_scheme(self):
        engine = AudioVisEngine(color_scheme="warm")
        assert engine.color_scheme == "warm"


# ---------------------------------------------------------------------------
# Tests — Properties (GPUEngineBase interface)
# ---------------------------------------------------------------------------


class TestProperties:
    """AudioVisEngine properties conform to GPUEngineBase contract."""

    def test_is_client_side_false(self, engine):
        assert engine.is_client_side is False

    def test_consumes_gpu_while_suspended_false(self, engine):
        assert engine.consumes_gpu_while_suspended is False

    def test_client_config_none(self, engine):
        assert engine.client_config is None


# ---------------------------------------------------------------------------
# Tests — _on_gl_ready (shader compilation)
# ---------------------------------------------------------------------------


class TestOnGlReady:
    """_on_gl_ready compiles shaders and sets up GL state."""

    @pytest.mark.asyncio
    async def test_creates_shader_program(self, engine, mock_egl_class, mock_egl_ctx):
        await engine.activate()
        mock_egl_ctx._gl.glCreateProgram.assert_called_once()
        assert engine._shader_program == 100

    @pytest.mark.asyncio
    async def test_compiles_vertex_shader(self, engine, mock_egl_class, mock_egl_ctx):
        await engine.activate()
        # glCreateShader called twice (vertex + fragment)
        assert mock_egl_ctx._gl.glCreateShader.call_count == 2

    @pytest.mark.asyncio
    async def test_links_program(self, engine, mock_egl_class, mock_egl_ctx):
        await engine.activate()
        mock_egl_ctx._gl.glLinkProgram.assert_called_once_with(100)

    @pytest.mark.asyncio
    async def test_creates_fft_texture(self, engine, mock_egl_class, mock_egl_ctx):
        await engine.activate()
        mock_egl_ctx._gl.glGenTextures.assert_called()
        assert engine._fft_texture == 42

    @pytest.mark.asyncio
    async def test_creates_vao(self, engine, mock_egl_class, mock_egl_ctx):
        await engine.activate()
        mock_egl_ctx._gl.glGenVertexArrays.assert_called()
        assert engine._vao == 99

    @pytest.mark.asyncio
    async def test_caches_uniform_locations(self, engine, mock_egl_class, mock_egl_ctx):
        await engine.activate()
        # Should look up all 9 uniforms
        assert mock_egl_ctx._gl.glGetUniformLocation.call_count == 9

    @pytest.mark.asyncio
    async def test_stores_metadata_on_activate(self, mock_egl_class, mock_egl_ctx):
        engine = AudioVisEngine()
        metadata = _make_metadata("My Song", "My Artist")
        await engine.activate(metadata)
        assert engine._track_title == "My Song"
        assert engine._track_artist == "My Artist"

    @pytest.mark.asyncio
    async def test_records_start_time(self, engine, mock_egl_class, mock_egl_ctx):
        t_before = time.monotonic()
        await engine.activate()
        t_after = time.monotonic()
        assert t_before <= engine._start_time <= t_after

    @pytest.mark.asyncio
    async def test_style_selects_fragment_shader(self, mock_egl_class, mock_egl_ctx):
        """Each style loads its specific fragment shader."""
        # Only test styles whose shader files exist (classic styles already have shaders)
        existing_styles = [s for s in get_valid_styles() if (SHADER_DIR / STYLE_REGISTRY[s]["file"]).exists()]
        for style in existing_styles:
            engine = AudioVisEngine(style=style)
            await engine.activate()
            # Verify the shader source was loaded (glShaderSource called)
            mock_egl_ctx._gl.glShaderSource.assert_called()
            mock_egl_ctx._gl.reset_mock()
            # Re-setup side effects after reset
            mock_egl_ctx._gl.glCreateShader.return_value = 1
            mock_egl_ctx._gl.glCreateProgram.return_value = 100

            def get_shader_iv(shader, pname, params):
                params._obj.value = 1

            def get_program_iv(program, pname, params):
                params._obj.value = 1

            mock_egl_ctx._gl.glGetShaderiv.side_effect = get_shader_iv
            mock_egl_ctx._gl.glGetProgramiv.side_effect = get_program_iv
            counter = [0]

            def get_uniform_location(program, name):
                counter[0] += 1
                return counter[0]

            mock_egl_ctx._gl.glGetUniformLocation.side_effect = get_uniform_location

            def gen_textures(n, ptr):
                ptr._obj.value = 42

            def gen_vertex_arrays(n, ptr):
                ptr._obj.value = 99

            mock_egl_ctx._gl.glGenTextures.side_effect = gen_textures
            mock_egl_ctx._gl.glGenVertexArrays.side_effect = gen_vertex_arrays


# ---------------------------------------------------------------------------
# Tests — Shader file loading
# ---------------------------------------------------------------------------


class TestShaderLoading:
    """Shader files exist and are loadable."""

    def test_shader_dir_exists(self):
        assert SHADER_DIR.exists()

    def test_vertex_shader_exists(self):
        assert (SHADER_DIR / "audiovis_vert.glsl").exists()

    def test_all_style_shaders_exist(self):
        """Classic styles (already implemented) have shader files on disk."""
        classic_styles = [s for s, m in STYLE_REGISTRY.items() if m["category"] == "classic"]
        for style in classic_styles:
            path = SHADER_DIR / STYLE_REGISTRY[style]["file"]
            assert path.exists(), f"Missing shader: {path}"

    def test_shader_files_are_nonempty(self):
        classic_styles = [s for s, m in STYLE_REGISTRY.items() if m["category"] == "classic"]
        for style in classic_styles:
            path = SHADER_DIR / STYLE_REGISTRY[style]["file"]
            content = path.read_text()
            assert len(content) > 100, f"Shader too small: {path}"

    def test_vertex_shader_has_version(self):
        content = (SHADER_DIR / "audiovis_vert.glsl").read_text()
        assert "#version 330 core" in content

    def test_fragment_shaders_have_version(self):
        classic_styles = [s for s, m in STYLE_REGISTRY.items() if m["category"] == "classic"]
        for style in classic_styles:
            content = (SHADER_DIR / STYLE_REGISTRY[style]["file"]).read_text()
            assert "#version 330 core" in content

    def test_fragment_shaders_declare_uniforms(self):
        """All fragment shaders declare the required uniforms."""
        required_uniforms = ["iTime", "iResolution", "iBeat", "iBPM", "iFFT"]
        classic_styles = [s for s, m in STYLE_REGISTRY.items() if m["category"] == "classic"]
        for style in classic_styles:
            content = (SHADER_DIR / STYLE_REGISTRY[style]["file"]).read_text()
            for uniform in required_uniforms:
                assert uniform in content, (
                    f"Missing uniform '{uniform}' in {STYLE_REGISTRY[style]['file']}"
                )

    def test_load_shader_source_raises_on_missing(self, engine):
        with pytest.raises(FileNotFoundError):
            engine._load_shader_source("nonexistent.glsl")


# ---------------------------------------------------------------------------
# Tests — _render_gl_frame
# ---------------------------------------------------------------------------


class TestRenderGlFrame:
    """_render_gl_frame uploads FFT and sets uniforms correctly."""

    @pytest.mark.asyncio
    async def test_clears_framebuffer(self, engine, mock_egl_class, mock_egl_ctx):
        await engine.activate()
        features = _make_features()
        engine._render_gl_frame(features)
        mock_egl_ctx._gl.glClear.assert_called()

    @pytest.mark.asyncio
    async def test_uses_shader_program(self, engine, mock_egl_class, mock_egl_ctx):
        await engine.activate()
        features = _make_features()
        engine._render_gl_frame(features)
        mock_egl_ctx._gl.glUseProgram.assert_called_with(100)

    @pytest.mark.asyncio
    async def test_uploads_fft_data(self, engine, mock_egl_class, mock_egl_ctx):
        await engine.activate()
        features = _make_features()
        engine._render_gl_frame(features)
        mock_egl_ctx._gl.glTexSubImage1D.assert_called()

    @pytest.mark.asyncio
    async def test_sets_time_uniform(self, engine, mock_egl_class, mock_egl_ctx):
        await engine.activate()
        features = _make_features()
        engine._render_gl_frame(features)
        mock_egl_ctx._gl.glUniform1f.assert_called()

    @pytest.mark.asyncio
    async def test_sets_resolution_uniform(self, engine, mock_egl_class, mock_egl_ctx):
        await engine.activate()
        features = _make_features()
        engine._render_gl_frame(features)
        mock_egl_ctx._gl.glUniform2f.assert_called()

    @pytest.mark.asyncio
    async def test_sets_band_energy_uniform(self, engine, mock_egl_class, mock_egl_ctx):
        await engine.activate()
        features = _make_features()
        engine._render_gl_frame(features)
        mock_egl_ctx._gl.glUniform1fv.assert_called()

    @pytest.mark.asyncio
    async def test_draws_fullscreen_quad(self, engine, mock_egl_class, mock_egl_ctx):
        await engine.activate()
        features = _make_features()
        engine._render_gl_frame(features)
        mock_egl_ctx._gl.glDrawArrays.assert_called()
        # Triangle strip with 4 vertices
        args = mock_egl_ctx._gl.glDrawArrays.call_args
        assert args[0][2] == 4  # vertex count

    @pytest.mark.asyncio
    async def test_binds_fft_texture(self, engine, mock_egl_class, mock_egl_ctx):
        await engine.activate()
        features = _make_features()
        engine._render_gl_frame(features)
        mock_egl_ctx._gl.glActiveTexture.assert_called()
        mock_egl_ctx._gl.glBindTexture.assert_called()

    @pytest.mark.asyncio
    async def test_handles_none_features(self, engine, mock_egl_class, mock_egl_ctx):
        """Rendering with None features should not crash."""
        await engine.activate()
        engine._render_gl_frame(None)
        # Should still draw
        mock_egl_ctx._gl.glDrawArrays.assert_called()

    @pytest.mark.asyncio
    async def test_no_fft_upload_when_no_features(
        self, engine, mock_egl_class, mock_egl_ctx
    ):
        await engine.activate()
        engine._render_gl_frame(None)
        # glTexSubImage1D should NOT be called if no features
        mock_egl_ctx._gl.glTexSubImage1D.assert_not_called()


# ---------------------------------------------------------------------------
# Tests — Beat Pulse (Req 6 AC 4)
# ---------------------------------------------------------------------------


class TestBeatPulse:
    """Beat pulse triggers brightness boost and decays over 200ms."""

    @pytest.mark.asyncio
    async def test_beat_sets_pulse_to_1(self, engine, mock_egl_class, mock_egl_ctx):
        await engine.activate()
        features = _make_features(beat=True)
        engine._render_gl_frame(features)
        assert engine._beat_pulse == 1.0

    @pytest.mark.asyncio
    async def test_pulse_decays_each_frame(self, engine, mock_egl_class, mock_egl_ctx):
        await engine.activate()
        # Set beat
        engine._render_gl_frame(_make_features(beat=True))
        assert engine._beat_pulse == 1.0

        # Next frame without beat
        engine._render_gl_frame(_make_features(beat=False))
        expected = 1.0 - BEAT_DECAY_PER_FRAME
        assert abs(engine._beat_pulse - expected) < 1e-6

    @pytest.mark.asyncio
    async def test_pulse_decays_to_zero(self, engine, mock_egl_class, mock_egl_ctx):
        await engine.activate()
        engine._render_gl_frame(_make_features(beat=True))

        # Run enough frames for full decay (200ms at 30fps = 6 frames)
        for _ in range(7):
            engine._render_gl_frame(_make_features(beat=False))

        assert engine._beat_pulse == 0.0

    @pytest.mark.asyncio
    async def test_pulse_never_negative(self, engine, mock_egl_class, mock_egl_ctx):
        await engine.activate()
        engine._render_gl_frame(_make_features(beat=True))
        for _ in range(20):  # Way more than needed
            engine._render_gl_frame(_make_features(beat=False))
        assert engine._beat_pulse >= 0.0

    @pytest.mark.asyncio
    async def test_consecutive_beats_reset_pulse(
        self, engine, mock_egl_class, mock_egl_ctx
    ):
        await engine.activate()
        engine._render_gl_frame(_make_features(beat=True))
        # Decay a bit
        engine._render_gl_frame(_make_features(beat=False))
        engine._render_gl_frame(_make_features(beat=False))
        assert engine._beat_pulse < 1.0
        # New beat resets to 1.0
        engine._render_gl_frame(_make_features(beat=True))
        assert engine._beat_pulse == 1.0

    def test_decay_rate_is_correct(self):
        """200ms decay at 30fps: 6 frames to reach 0."""
        # BEAT_DECAY_PER_FRAME = (1/30) / 0.2 = 1/6
        assert abs(BEAT_DECAY_PER_FRAME - 1.0 / 6.0) < 1e-9


# ---------------------------------------------------------------------------
# Tests — Track metadata (Req 6 AC 5)
# ---------------------------------------------------------------------------


class TestTrackMetadata:
    """Track title/artist is stored for text overlay."""

    @pytest.mark.asyncio
    async def test_on_track_change_updates_title(
        self, engine, mock_egl_class, mock_egl_ctx
    ):
        await engine.activate()
        metadata = _make_metadata("New Title", "New Artist")
        await engine.on_track_change(metadata)
        assert engine._track_title == "New Title"
        assert engine._track_artist == "New Artist"

    @pytest.mark.asyncio
    async def test_metadata_from_activate(self, mock_egl_class, mock_egl_ctx):
        engine = AudioVisEngine()
        metadata = _make_metadata("Initial", "Band")
        await engine.activate(metadata)
        assert engine._track_title == "Initial"
        assert engine._track_artist == "Band"

    @pytest.mark.asyncio
    async def test_no_metadata_leaves_empty(self, engine, mock_egl_class, mock_egl_ctx):
        await engine.activate()
        assert engine._track_title == ""
        assert engine._track_artist == ""


# ---------------------------------------------------------------------------
# Tests — Lifecycle (inherited from GPUEngineBase)
# ---------------------------------------------------------------------------


class TestLifecycle:
    """Engine lifecycle through activate/suspend/stop."""

    @pytest.mark.asyncio
    async def test_activate_sets_running(self, engine, mock_egl_class, mock_egl_ctx):
        await engine.activate()
        assert engine._running is True

    @pytest.mark.asyncio
    async def test_suspend_clears_context(self, engine, mock_egl_class, mock_egl_ctx):
        await engine.activate()
        await engine.suspend()
        assert engine._egl_ctx is None
        assert engine._running is False

    @pytest.mark.asyncio
    async def test_stop_cleans_up_gl_resources(
        self, engine, mock_egl_class, mock_egl_ctx
    ):
        await engine.activate()
        await engine.stop()
        # Should have called glDeleteProgram
        mock_egl_ctx._gl.glDeleteProgram.assert_called_with(100)
        assert engine._shader_program == 0
        assert engine._egl_ctx is None

    @pytest.mark.asyncio
    async def test_stop_deletes_texture(self, engine, mock_egl_class, mock_egl_ctx):
        await engine.activate()
        await engine.stop()
        mock_egl_ctx._gl.glDeleteTextures.assert_called()

    @pytest.mark.asyncio
    async def test_stop_deletes_vao(self, engine, mock_egl_class, mock_egl_ctx):
        await engine.activate()
        await engine.stop()
        mock_egl_ctx._gl.glDeleteVertexArrays.assert_called()

    @pytest.mark.asyncio
    async def test_render_frames_yields_correct_size(
        self, engine, mock_egl_class, mock_egl_ctx
    ):
        await engine.activate()
        async for frame in engine.render_frames():
            assert len(frame) == 1280 * 720 * 4
            engine._running = False


# ---------------------------------------------------------------------------
# Tests — Audio features callback
# ---------------------------------------------------------------------------


class TestAudioFeatures:
    """on_audio_features stores latest features atomically."""

    def test_stores_features(self, engine):
        features = _make_features(beat=True, bpm=140.0)
        engine.on_audio_features(features)
        assert engine._latest_features is features

    def test_overwrites_previous(self, engine):
        f1 = _make_features(beat=False)
        f2 = _make_features(beat=True)
        engine.on_audio_features(f1)
        engine.on_audio_features(f2)
        assert engine._latest_features is f2

"""Tests for the Varda Engine (GPU fragment shader visualizer) with mocked GL.

Covers: shader loading, fallback on compile failure, uniform setting,
audio texture upload, beat pulse decay, and track change crossfade.

Requirements: Req 8 (AC 1-6)
"""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from video.visualizer_engines.base import AudioFeatures, TrackMetadata
from video.visualizer_engines.varda import (
    BEAT_DECAY_PER_FRAME,
    CROSSFADE_DURATION,
    FALLBACK_SHADER,
    GL_COMPILE_STATUS,
    GL_FRAGMENT_SHADER,
    GL_LINK_STATUS,
    GL_TRIANGLES,
    GL_TRUE,
    GL_VERTEX_SHADER,
    SHADER_DIR,
    VardaEngine,
)


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

FAKE_FRAME = b"\x00" * (1280 * 720 * 4)

VERTEX_SHADER_SRC = "#version 330 core\nvoid main() {}\n"
FRAGMENT_SHADER_SRC = "#version 330 core\nout vec4 c;\nvoid main() { c = vec4(1.0); }\n"


def _make_features(beat: bool = False, bpm: float = 120.0) -> AudioFeatures:
    return AudioFeatures(
        fft=[0.5] * 512,
        beat=beat,
        bpm=bpm,
        band_energy=[0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7],
        timestamp=time.monotonic(),
    )


def _make_metadata() -> TrackMetadata:
    return TrackMetadata(
        title="Test Track",
        artist="Test Artist",
        artwork_url=None,
        duration_ms=200000,
        position_ms=0,
    )


def _make_mock_gl():
    """Create a mock GL library with shader compilation succeeding."""
    gl = MagicMock()

    # Shader compilation succeeds by default
    def get_shaderiv(shader, pname, params):
        params._obj.value = GL_TRUE

    gl.glGetShaderiv.side_effect = get_shaderiv

    # Program link succeeds
    def get_programiv(program, pname, params):
        params._obj.value = GL_TRUE

    gl.glGetProgramiv.side_effect = get_programiv

    # glCreateShader returns incrementing IDs
    gl.glCreateShader.side_effect = [1, 2, 3, 4, 5, 6, 7, 8]
    gl.glCreateProgram.return_value = 100

    # Uniform locations
    gl.glGetUniformLocation.return_value = 0

    # Texture gen
    def gen_textures(n, ptr):
        ptr._obj.value = 10

    gl.glGenTextures.side_effect = gen_textures

    # VAO gen
    def gen_vaos(n, ptr):
        ptr._obj.value = 20

    gl.glGenVertexArrays.side_effect = gen_vaos

    # Read pixels
    gl.glReadPixels.return_value = None

    return gl


def _make_mock_egl(gl=None):
    """Create a mock EGLHeadlessContext."""
    mock = MagicMock()
    mock.create.return_value = None
    mock.make_current.return_value = None
    mock.read_pixels.return_value = FAKE_FRAME
    mock.destroy.return_value = None
    mock.is_valid = True
    mock.width = 1280
    mock.height = 720
    mock._gl = gl or _make_mock_gl()
    return mock


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_gl():
    return _make_mock_gl()


@pytest.fixture
def mock_egl(mock_gl):
    return _make_mock_egl(mock_gl)


@pytest.fixture
def engine():
    return VardaEngine(shader_name="plasma", color_intensity=1.0, speed=1.0)


@pytest.fixture
def shader_dir(tmp_path):
    """Create a temp shader directory with vertex and varda fragment shaders."""
    shader_path = tmp_path / "shaders"
    shader_path.mkdir()
    (shader_path / "varda_vertex.glsl").write_text(VERTEX_SHADER_SRC)
    (shader_path / "plasma.glsl").write_text(FRAGMENT_SHADER_SRC)
    (shader_path / "varda_tunnel.glsl").write_text(FRAGMENT_SHADER_SRC)
    (shader_path / "varda_star_field.glsl").write_text(FRAGMENT_SHADER_SRC)
    return shader_path


@pytest.fixture
def patched_engine(engine, mock_egl, shader_dir):
    """Engine with mocked EGL and shader directory."""
    engine._egl_ctx = mock_egl
    with patch("video.visualizer_engines.varda.SHADER_DIR", shader_dir):
        yield engine


# ---------------------------------------------------------------------------
# Tests — Shader Loading (Req 8 AC 1)
# ---------------------------------------------------------------------------


class TestShaderLoading:
    """Varda loads vertex + fragment shaders on _on_gl_ready."""

    @pytest.mark.asyncio
    async def test_loads_shader_program(self, engine, mock_egl, shader_dir):
        """_on_gl_ready compiles a shader program successfully."""
        engine._egl_ctx = mock_egl
        with patch("video.visualizer_engines.varda.SHADER_DIR", shader_dir):
            await engine._on_gl_ready(None)
        assert engine._shader_program == 100

    @pytest.mark.asyncio
    async def test_creates_audio_texture(self, engine, mock_egl, shader_dir):
        """_on_gl_ready creates a 512×2 audio texture."""
        engine._egl_ctx = mock_egl
        with patch("video.visualizer_engines.varda.SHADER_DIR", shader_dir):
            await engine._on_gl_ready(None)
        assert engine._audio_texture == 10
        mock_egl._gl.glGenTextures.assert_called_once()

    @pytest.mark.asyncio
    async def test_creates_vao(self, engine, mock_egl, shader_dir):
        """_on_gl_ready creates an empty VAO for fullscreen triangle."""
        engine._egl_ctx = mock_egl
        with patch("video.visualizer_engines.varda.SHADER_DIR", shader_dir):
            await engine._on_gl_ready(None)
        assert engine._vao == 20
        mock_egl._gl.glGenVertexArrays.assert_called_once()

    @pytest.mark.asyncio
    async def test_discovers_shader_pool(self, engine, mock_egl, shader_dir):
        """_on_gl_ready populates the shader pool from directory."""
        engine._egl_ctx = mock_egl
        with patch("video.visualizer_engines.varda.SHADER_DIR", shader_dir):
            await engine._on_gl_ready(None)
        # Should find plasma, varda_tunnel, varda_star_field (vertex excluded)
        assert "plasma" in engine._shader_pool
        assert "varda_tunnel" in engine._shader_pool
        assert "varda_star_field" in engine._shader_pool
        assert "varda_vertex" not in engine._shader_pool

    @pytest.mark.asyncio
    async def test_compiles_vertex_and_fragment(self, engine, mock_egl, shader_dir):
        """Both vertex and fragment shaders are compiled."""
        engine._egl_ctx = mock_egl
        with patch("video.visualizer_engines.varda.SHADER_DIR", shader_dir):
            await engine._on_gl_ready(None)
        gl = mock_egl._gl
        # glCreateShader called twice (vertex + fragment)
        assert gl.glCreateShader.call_count == 2
        calls = gl.glCreateShader.call_args_list
        assert calls[0][0][0] == GL_VERTEX_SHADER
        assert calls[1][0][0] == GL_FRAGMENT_SHADER


# ---------------------------------------------------------------------------
# Tests — Shader Fallback (Req 8 AC 6)
# ---------------------------------------------------------------------------


class TestShaderFallback:
    """When a shader fails to compile, Varda falls back to plasma.glsl."""

    @pytest.mark.asyncio
    async def test_fallback_on_missing_shader(self, mock_egl, shader_dir):
        """If the configured shader doesn't exist, falls back to plasma."""
        engine = VardaEngine(shader_name="nonexistent")
        engine._egl_ctx = mock_egl
        with patch("video.visualizer_engines.varda.SHADER_DIR", shader_dir):
            await engine._on_gl_ready(None)
        # Should still get a valid program (fallback compiled)
        assert engine._shader_program == 100

    @pytest.mark.asyncio
    async def test_fallback_on_compile_failure(self, mock_egl, shader_dir):
        """If fragment shader compile fails, falls back to plasma.glsl."""
        engine = VardaEngine(shader_name="tunnel")
        engine._egl_ctx = mock_egl
        gl = mock_egl._gl

        # First fragment compile fails, second (fallback) succeeds
        compile_calls = [0]

        def shader_compile_effect(shader, pname, params):
            compile_calls[0] += 1
            # Fail the second glGetShaderiv call (first fragment shader)
            if compile_calls[0] == 2:
                params._obj.value = 0  # GL_FALSE
            else:
                params._obj.value = GL_TRUE

        gl.glGetShaderiv.side_effect = shader_compile_effect
        # Need enough shader IDs for the retry
        gl.glCreateShader.side_effect = [1, 2, 3, 4, 5, 6]
        gl.glGetShaderInfoLog.return_value = None

        with patch("video.visualizer_engines.varda.SHADER_DIR", shader_dir):
            await engine._on_gl_ready(None)

        # Should still produce a program (via fallback)
        assert engine._shader_program == 100


# ---------------------------------------------------------------------------
# Tests — Uniform Setting (Req 8 AC 2)
# ---------------------------------------------------------------------------


class TestUniformSetting:
    """_render_gl_frame sets the correct uniforms for shader playback."""

    @pytest.mark.asyncio
    async def test_sets_itime_uniform(self, engine, mock_egl, shader_dir):
        """iTime uniform is set with elapsed time * speed."""
        engine._egl_ctx = mock_egl
        with patch("video.visualizer_engines.varda.SHADER_DIR", shader_dir):
            await engine._on_gl_ready(None)

        features = _make_features()
        engine._render_gl_frame(features)

        gl = mock_egl._gl
        gl.glGetUniformLocation.assert_any_call(100, b"iTime")

    @pytest.mark.asyncio
    async def test_sets_iresolution_uniform(self, engine, mock_egl, shader_dir):
        """iResolution uniform is set with width and height."""
        engine._egl_ctx = mock_egl
        with patch("video.visualizer_engines.varda.SHADER_DIR", shader_dir):
            await engine._on_gl_ready(None)

        engine._render_gl_frame(_make_features())

        gl = mock_egl._gl
        gl.glGetUniformLocation.assert_any_call(100, b"iResolution")

    @pytest.mark.asyncio
    async def test_sets_ibeat_uniform(self, engine, mock_egl, shader_dir):
        """iBeat uniform is set with beat pulse value."""
        engine._egl_ctx = mock_egl
        with patch("video.visualizer_engines.varda.SHADER_DIR", shader_dir):
            await engine._on_gl_ready(None)

        engine._render_gl_frame(_make_features(beat=True))

        gl = mock_egl._gl
        gl.glGetUniformLocation.assert_any_call(100, b"iBeat")

    @pytest.mark.asyncio
    async def test_sets_ibpm_uniform(self, engine, mock_egl, shader_dir):
        """iBPM uniform is set with current BPM."""
        engine._egl_ctx = mock_egl
        with patch("video.visualizer_engines.varda.SHADER_DIR", shader_dir):
            await engine._on_gl_ready(None)

        engine._render_gl_frame(_make_features(bpm=140.0))

        gl = mock_egl._gl
        gl.glGetUniformLocation.assert_any_call(100, b"iBPM")

    @pytest.mark.asyncio
    async def test_sets_ibandenergy_uniforms(self, engine, mock_egl, shader_dir):
        """iBandEnergy[0..6] uniforms are set."""
        engine._egl_ctx = mock_egl
        with patch("video.visualizer_engines.varda.SHADER_DIR", shader_dir):
            await engine._on_gl_ready(None)

        engine._render_gl_frame(_make_features())

        gl = mock_egl._gl
        for i in range(7):
            gl.glGetUniformLocation.assert_any_call(100, f"iBandEnergy[{i}]".encode())

    @pytest.mark.asyncio
    async def test_sets_ichannel0_uniform(self, engine, mock_egl, shader_dir):
        """iChannel0 (audio texture) uniform is bound to texture unit 0."""
        engine._egl_ctx = mock_egl
        with patch("video.visualizer_engines.varda.SHADER_DIR", shader_dir):
            await engine._on_gl_ready(None)

        engine._render_gl_frame(_make_features())

        gl = mock_egl._gl
        gl.glGetUniformLocation.assert_any_call(100, b"iChannel0")

    @pytest.mark.asyncio
    async def test_draws_fullscreen_triangle(self, engine, mock_egl, shader_dir):
        """Draws 3 vertices as a triangle for fullscreen coverage."""
        engine._egl_ctx = mock_egl
        with patch("video.visualizer_engines.varda.SHADER_DIR", shader_dir):
            await engine._on_gl_ready(None)

        engine._render_gl_frame(_make_features())

        gl = mock_egl._gl
        gl.glDrawArrays.assert_called_with(GL_TRIANGLES, 0, 3)


# ---------------------------------------------------------------------------
# Tests — Beat Pulse Decay (Req 8 AC 2)
# ---------------------------------------------------------------------------


class TestBeatPulse:
    """Beat pulse decays from 1.0 → 0.0 over ~300ms (9 frames at 30fps)."""

    @pytest.mark.asyncio
    async def test_beat_sets_pulse_to_one(self, engine, mock_egl, shader_dir):
        """When beat is True, pulse is set to 1.0."""
        engine._egl_ctx = mock_egl
        with patch("video.visualizer_engines.varda.SHADER_DIR", shader_dir):
            await engine._on_gl_ready(None)

        engine._render_gl_frame(_make_features(beat=True))
        assert engine._beat_pulse == 1.0

    @pytest.mark.asyncio
    async def test_pulse_decays_per_frame(self, engine, mock_egl, shader_dir):
        """Pulse decays by BEAT_DECAY_PER_FRAME each non-beat frame."""
        engine._egl_ctx = mock_egl
        with patch("video.visualizer_engines.varda.SHADER_DIR", shader_dir):
            await engine._on_gl_ready(None)

        # Set pulse to 1.0
        engine._render_gl_frame(_make_features(beat=True))
        assert engine._beat_pulse == 1.0

        # Decay one frame
        engine._render_gl_frame(_make_features(beat=False))
        expected = 1.0 - BEAT_DECAY_PER_FRAME
        assert abs(engine._beat_pulse - expected) < 1e-6

    @pytest.mark.asyncio
    async def test_pulse_reaches_zero(self, engine, mock_egl, shader_dir):
        """After ~9 frames (300ms), pulse reaches 0.0."""
        engine._egl_ctx = mock_egl
        with patch("video.visualizer_engines.varda.SHADER_DIR", shader_dir):
            await engine._on_gl_ready(None)

        engine._render_gl_frame(_make_features(beat=True))
        # Render 10 non-beat frames (should reach 0)
        for _ in range(10):
            engine._render_gl_frame(_make_features(beat=False))
        assert engine._beat_pulse == 0.0

    @pytest.mark.asyncio
    async def test_pulse_never_goes_negative(self, engine, mock_egl, shader_dir):
        """Pulse is clamped to 0.0 minimum."""
        engine._egl_ctx = mock_egl
        with patch("video.visualizer_engines.varda.SHADER_DIR", shader_dir):
            await engine._on_gl_ready(None)

        # Many frames without beat
        for _ in range(50):
            engine._render_gl_frame(_make_features(beat=False))
        assert engine._beat_pulse == 0.0


# ---------------------------------------------------------------------------
# Tests — Track Change / Crossfade (Req 8 AC 4)
# ---------------------------------------------------------------------------


class TestTrackChange:
    """on_track_change selects a new shader and starts crossfade."""

    @pytest.mark.asyncio
    async def test_selects_new_shader(self, engine, mock_egl, shader_dir):
        """Track change selects a different shader from the pool."""
        engine._egl_ctx = mock_egl
        gl = mock_egl._gl
        # Reset create shader/program for multiple compiles
        gl.glCreateShader.side_effect = list(range(1, 20))
        gl.glCreateProgram.side_effect = [100, 200, 300]

        with patch("video.visualizer_engines.varda.SHADER_DIR", shader_dir):
            await engine._on_gl_ready(None)
            original_shader = engine._shader_name
            await engine.on_track_change(_make_metadata())

        # Shader name should have changed
        assert engine._shader_name != original_shader or len(engine._shader_pool) == 1

    @pytest.mark.asyncio
    async def test_starts_crossfade(self, engine, mock_egl, shader_dir):
        """Track change initiates crossfade state."""
        engine._egl_ctx = mock_egl
        gl = mock_egl._gl
        gl.glCreateShader.side_effect = list(range(1, 20))
        gl.glCreateProgram.side_effect = [100, 200, 300]

        with patch("video.visualizer_engines.varda.SHADER_DIR", shader_dir):
            await engine._on_gl_ready(None)
            await engine.on_track_change(_make_metadata())

        assert engine._crossfading is True
        assert engine._prev_program == 100  # Old program stored
        assert engine._shader_program == 200  # New program active

    @pytest.mark.asyncio
    async def test_crossfade_completes_after_duration(self, engine, mock_egl, shader_dir):
        """Crossfade completes after CROSSFADE_DURATION seconds."""
        engine._egl_ctx = mock_egl
        gl = mock_egl._gl
        gl.glCreateShader.side_effect = list(range(1, 20))
        gl.glCreateProgram.side_effect = [100, 200, 300]

        with patch("video.visualizer_engines.varda.SHADER_DIR", shader_dir):
            await engine._on_gl_ready(None)
            await engine.on_track_change(_make_metadata())

        assert engine._crossfading is True

        # Simulate time passing beyond crossfade duration
        engine._crossfade_start = time.monotonic() - CROSSFADE_DURATION - 0.1
        engine._render_gl_frame(_make_features())

        assert engine._crossfading is False

    @pytest.mark.asyncio
    async def test_no_op_without_egl_context(self, engine):
        """Track change is a no-op if no EGL context exists."""
        engine._egl_ctx = None
        await engine.on_track_change(_make_metadata())
        # Should not raise


# ---------------------------------------------------------------------------
# Tests — Audio Texture Upload (Req 8 AC 2)
# ---------------------------------------------------------------------------


class TestAudioTexture:
    """Audio features are uploaded as a 512×2 texture."""

    @pytest.mark.asyncio
    async def test_uploads_fft_data(self, engine, mock_egl, shader_dir):
        """FFT data is uploaded via glTexSubImage2D."""
        engine._egl_ctx = mock_egl
        with patch("video.visualizer_engines.varda.SHADER_DIR", shader_dir):
            await engine._on_gl_ready(None)

        features = _make_features()
        engine._render_gl_frame(features)

        gl = mock_egl._gl
        # Two rows uploaded (row 0 + row 1)
        assert gl.glTexSubImage2D.call_count == 2

    @pytest.mark.asyncio
    async def test_no_upload_without_features(self, engine, mock_egl, shader_dir):
        """No texture upload when features is None."""
        engine._egl_ctx = mock_egl
        with patch("video.visualizer_engines.varda.SHADER_DIR", shader_dir):
            await engine._on_gl_ready(None)

        engine._render_gl_frame(None)

        gl = mock_egl._gl
        gl.glTexSubImage2D.assert_not_called()


# ---------------------------------------------------------------------------
# Tests — Configuration
# ---------------------------------------------------------------------------


class TestConfiguration:
    """Engine respects configuration parameters."""

    def test_default_shader_name(self):
        engine = VardaEngine()
        assert engine._shader_name == "plasma"

    def test_custom_shader_name(self):
        engine = VardaEngine(shader_name="tunnel")
        assert engine._shader_name == "tunnel"

    def test_default_speed(self):
        engine = VardaEngine()
        assert engine._speed == 1.0

    def test_custom_speed(self):
        engine = VardaEngine(speed=2.0)
        assert engine._speed == 2.0

    def test_default_color_intensity(self):
        engine = VardaEngine()
        assert engine._color_intensity == 1.0

    def test_custom_color_intensity(self):
        engine = VardaEngine(color_intensity=1.5)
        assert engine._color_intensity == 1.5

    def test_default_complexity(self):
        engine = VardaEngine()
        assert engine._complexity == "medium"

    def test_custom_complexity(self):
        engine = VardaEngine(complexity="high")
        assert engine._complexity == "high"


# ---------------------------------------------------------------------------
# Tests — Inheritance
# ---------------------------------------------------------------------------


class TestInheritance:
    """VardaEngine inherits from GPUEngineBase correctly."""

    def test_is_not_client_side(self):
        engine = VardaEngine()
        assert engine.is_client_side is False

    def test_does_not_consume_gpu_while_suspended(self):
        engine = VardaEngine()
        assert engine.consumes_gpu_while_suspended is False

    def test_client_config_is_none(self):
        engine = VardaEngine()
        assert engine.client_config is None

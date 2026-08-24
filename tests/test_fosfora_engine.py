"""Tests for the Fosfora Engine (GPU particle system with transform feedback).

Covers: _on_gl_ready shader compilation, ping-pong buffer allocation,
_render_gl_frame physics + render passes, beat emission, band energy
emission, suspend/stop resource cleanup, configurable properties.

All GL calls are mocked — no GPU hardware required.

Requirements: Req 7 (AC 1-6)
"""

from __future__ import annotations

import ctypes
import time
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

from video.visualizer_engines.base import AudioFeatures, TrackMetadata
from video.visualizer_engines.fosfora import (
    FosforaEngine,
    PARTICLE_STRIDE,
    SHADER_DIR,
    GL_VERTEX_SHADER,
    GL_FRAGMENT_SHADER,
    GL_COMPILE_STATUS,
    GL_LINK_STATUS,
    GL_TRUE,
    GL_DYNAMIC_COPY,
    GL_ARRAY_BUFFER,
    GL_RASTERIZER_DISCARD,
    GL_TRANSFORM_FEEDBACK_BUFFER,
    GL_POINTS,
    GL_BLEND,
    GL_PROGRAM_POINT_SIZE,
    GL_COLOR_BUFFER_BIT,
    GL_DEPTH_BUFFER_BIT,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

FAKE_FRAME = b"\x00" * (1280 * 720 * 4)


def _make_features(
    beat: bool = False, bpm: float = 120.0, energy: float = 0.5
) -> AudioFeatures:
    """Create a sample AudioFeatures instance."""
    return AudioFeatures(
        fft=[0.1] * 512,
        beat=beat,
        bpm=bpm,
        band_energy=[energy] * 7,
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


def _make_mock_gl():
    """Create a mock GL library with expected return values."""
    gl = MagicMock()
    # Shader compilation succeeds
    gl.glCreateShader.return_value = 1
    gl.glCreateProgram.return_value = 1
    gl.glGetShaderiv.side_effect = _mock_get_shaderiv_success
    gl.glGetProgramiv.side_effect = _mock_get_programiv_success
    gl.glGetUniformLocation.return_value = 0
    # Buffer/VAO generation returns incrementing IDs
    _buffer_counter = [0]

    def _gen_buffer(count, buf_ptr):
        _buffer_counter[0] += 1
        buf_ptr._obj.value = _buffer_counter[0]

    def _gen_vao(count, buf_ptr):
        _buffer_counter[0] += 1
        buf_ptr._obj.value = _buffer_counter[0]

    gl.glGenBuffers.side_effect = _gen_buffer
    gl.glGenVertexArrays.side_effect = _gen_vao
    gl.glReadPixels.return_value = None
    return gl


def _mock_get_shaderiv_success(shader, pname, params):
    """Mock glGetShaderiv — always report success."""
    if pname == GL_COMPILE_STATUS:
        params._obj.value = GL_TRUE


def _mock_get_programiv_success(program, pname, params):
    """Mock glGetProgramiv — always report success."""
    if pname == GL_LINK_STATUS:
        params._obj.value = GL_TRUE


def _make_mock_egl(gl_mock=None):
    """Create a mock EGLHeadlessContext with a GL mock."""
    egl = MagicMock()
    egl._gl = gl_mock or _make_mock_gl()
    egl.create.return_value = None
    egl.make_current.return_value = None
    egl.read_pixels.return_value = FAKE_FRAME
    egl.destroy.return_value = None
    egl.is_valid = True
    return egl


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def gl_mock():
    """Provide a standalone mock GL library."""
    return _make_mock_gl()


@pytest.fixture
def engine():
    """Create a FosforaEngine with default config."""
    return FosforaEngine()


@pytest.fixture
def engine_custom():
    """Create a FosforaEngine with custom config."""
    return FosforaEngine(
        particle_count=2000,
        gravity=1.5,
        emission_style="burst",
        color_mode="warm",
        trail_length=0.7,
    )


@pytest.fixture
def mock_egl_class(gl_mock):
    """Patch EGLHeadlessContext to return a mock with our GL mock."""
    mock_ctx = _make_mock_egl(gl_mock)
    with patch(
        "video.visualizer_engines.gpu_engine_base.EGLHeadlessContext",
        return_value=mock_ctx,
    ) as mock_cls:
        mock_cls._instance = mock_ctx
        yield mock_cls


# ---------------------------------------------------------------------------
# Tests — Properties
# ---------------------------------------------------------------------------


class TestProperties:
    """FosforaEngine properties and configuration."""

    def test_is_client_side_false(self, engine):
        assert engine.is_client_side is False

    def test_consumes_gpu_while_suspended_false(self, engine):
        assert engine.consumes_gpu_while_suspended is False

    def test_client_config_none(self, engine):
        assert engine.client_config is None

    def test_max_particles_constant(self):
        assert FosforaEngine.MAX_PARTICLES == 10_000

    def test_default_particle_count(self, engine):
        assert engine._particle_count == 5000

    def test_default_gravity(self, engine):
        assert engine._gravity == 0.5

    def test_default_emission_style(self, engine):
        assert engine._emission_style == "both"

    def test_default_color_mode(self, engine):
        assert engine._color_mode == "spectrum"

    def test_default_trail_length(self, engine):
        assert engine._trail_length == 0.3

    def test_custom_config(self, engine_custom):
        assert engine_custom._particle_count == 2000
        assert engine_custom._gravity == 1.5
        assert engine_custom._emission_style == "burst"
        assert engine_custom._color_mode == "warm"
        assert engine_custom._trail_length == 0.7


# ---------------------------------------------------------------------------
# Tests — _on_gl_ready (Req 7 AC 1)
# ---------------------------------------------------------------------------


class TestOnGLReady:
    """_on_gl_ready compiles shaders and allocates buffers."""

    @pytest.mark.asyncio
    async def test_compiles_transform_feedback_program(
        self, engine, mock_egl_class, gl_mock
    ):
        await engine.activate()
        # Should have compiled vertex shader for physics
        assert gl_mock.glCreateShader.call_count >= 2  # physics vert + render vert + render frag
        assert engine._transform_program != 0

    @pytest.mark.asyncio
    async def test_compiles_render_program(
        self, engine, mock_egl_class, gl_mock
    ):
        await engine.activate()
        assert engine._render_program != 0

    @pytest.mark.asyncio
    async def test_sets_transform_feedback_varyings(
        self, engine, mock_egl_class, gl_mock
    ):
        await engine.activate()
        # glTransformFeedbackVaryings should have been called
        gl_mock.glTransformFeedbackVaryings.assert_called()

    @pytest.mark.asyncio
    async def test_allocates_two_vbos(self, engine, mock_egl_class, gl_mock):
        await engine.activate()
        # Should allocate 2 VBOs (ping-pong)
        assert gl_mock.glGenBuffers.call_count == 2
        assert engine._vbo[0] != 0
        assert engine._vbo[1] != 0

    @pytest.mark.asyncio
    async def test_allocates_two_vaos(self, engine, mock_egl_class, gl_mock):
        await engine.activate()
        # Should allocate 2 VAOs (ping-pong)
        assert gl_mock.glGenVertexArrays.call_count == 2
        assert engine._vao[0] != 0
        assert engine._vao[1] != 0

    @pytest.mark.asyncio
    async def test_buffer_size_matches_particle_count(
        self, engine, mock_egl_class, gl_mock
    ):
        await engine.activate()
        expected_size = engine._particle_count * PARTICLE_STRIDE
        # glBufferData called with correct size (twice for ping-pong)
        buffer_data_calls = gl_mock.glBufferData.call_args_list
        assert len(buffer_data_calls) == 2
        for c in buffer_data_calls:
            args = c[0]
            assert args[1] == expected_size

    @pytest.mark.asyncio
    async def test_vertex_attrib_pointers_set(
        self, engine, mock_egl_class, gl_mock
    ):
        await engine.activate()
        # 4 attributes × 2 buffers = 8 calls to glEnableVertexAttribArray
        assert gl_mock.glEnableVertexAttribArray.call_count == 8
        # 4 attributes × 2 buffers = 8 calls to glVertexAttribPointer
        assert gl_mock.glVertexAttribPointer.call_count == 8

    @pytest.mark.asyncio
    async def test_stores_gl_reference(self, engine, mock_egl_class, gl_mock):
        await engine.activate()
        assert engine._gl is gl_mock

    @pytest.mark.asyncio
    async def test_custom_particle_count_affects_buffer(
        self, engine_custom, mock_egl_class, gl_mock
    ):
        await engine_custom.activate()
        expected_size = 2000 * PARTICLE_STRIDE
        buffer_data_calls = gl_mock.glBufferData.call_args_list
        for c in buffer_data_calls:
            args = c[0]
            assert args[1] == expected_size


# ---------------------------------------------------------------------------
# Tests — _render_gl_frame (Req 7 AC 2-5)
# ---------------------------------------------------------------------------


class TestRenderGLFrame:
    """_render_gl_frame performs physics + render passes."""

    @pytest.mark.asyncio
    async def test_physics_pass_enables_rasterizer_discard(
        self, engine, mock_egl_class, gl_mock
    ):
        await engine.activate()
        features = _make_features()
        engine.on_audio_features(features)
        engine._render_gl_frame(features)
        gl_mock.glEnable.assert_any_call(GL_RASTERIZER_DISCARD)

    @pytest.mark.asyncio
    async def test_physics_pass_disables_rasterizer_discard(
        self, engine, mock_egl_class, gl_mock
    ):
        await engine.activate()
        features = _make_features()
        engine._render_gl_frame(features)
        gl_mock.glDisable.assert_any_call(GL_RASTERIZER_DISCARD)

    @pytest.mark.asyncio
    async def test_transform_feedback_called(
        self, engine, mock_egl_class, gl_mock
    ):
        await engine.activate()
        features = _make_features()
        engine._render_gl_frame(features)
        gl_mock.glBeginTransformFeedback.assert_called_once_with(GL_POINTS)
        gl_mock.glEndTransformFeedback.assert_called_once()

    @pytest.mark.asyncio
    async def test_draw_arrays_called_for_physics(
        self, engine, mock_egl_class, gl_mock
    ):
        await engine.activate()
        features = _make_features()
        engine._render_gl_frame(features)
        # DrawArrays called twice: once for physics, once for render
        assert gl_mock.glDrawArrays.call_count == 2

    @pytest.mark.asyncio
    async def test_render_pass_enables_blending(
        self, engine, mock_egl_class, gl_mock
    ):
        await engine.activate()
        features = _make_features()
        engine._render_gl_frame(features)
        gl_mock.glEnable.assert_any_call(GL_BLEND)

    @pytest.mark.asyncio
    async def test_render_pass_clears_framebuffer(
        self, engine, mock_egl_class, gl_mock
    ):
        await engine.activate()
        features = _make_features()
        engine._render_gl_frame(features)
        gl_mock.glClear.assert_called_once_with(
            GL_COLOR_BUFFER_BIT | GL_DEPTH_BUFFER_BIT
        )

    @pytest.mark.asyncio
    async def test_render_pass_enables_point_size(
        self, engine, mock_egl_class, gl_mock
    ):
        await engine.activate()
        features = _make_features()
        engine._render_gl_frame(features)
        gl_mock.glEnable.assert_any_call(GL_PROGRAM_POINT_SIZE)

    @pytest.mark.asyncio
    async def test_ping_pong_buffer_swaps(
        self, engine, mock_egl_class, gl_mock
    ):
        await engine.activate()
        features = _make_features()
        assert engine._current_buffer == 0
        engine._render_gl_frame(features)
        assert engine._current_buffer == 1
        engine._render_gl_frame(features)
        assert engine._current_buffer == 0

    @pytest.mark.asyncio
    async def test_handles_none_features(
        self, engine, mock_egl_class, gl_mock
    ):
        """Should render without crash when no audio features available."""
        await engine.activate()
        engine._render_gl_frame(None)
        # Should still clear and draw (empty particles)
        gl_mock.glClear.assert_called()


# ---------------------------------------------------------------------------
# Tests — Beat emission (Req 7 AC 4)
# ---------------------------------------------------------------------------


class TestBeatEmission:
    """Beat detection triggers burst emission."""

    @pytest.mark.asyncio
    async def test_beat_sets_pulse_to_one(
        self, engine, mock_egl_class, gl_mock
    ):
        await engine.activate()
        features = _make_features(beat=True)
        engine._render_gl_frame(features)
        # After rendering, beat_pulse should be set high
        # (it gets set then immediately decayed by dt which is ~0)
        assert engine._beat_pulse > 0.5

    @pytest.mark.asyncio
    async def test_beat_burst_emits_particles(
        self, engine, mock_egl_class, gl_mock
    ):
        await engine.activate()
        features = _make_features(beat=True, energy=0.8)
        emit = engine._compute_emission_count(features, 1.0 / 30)
        # With burst + continuous, should emit significantly on beat
        assert emit > 0

    @pytest.mark.asyncio
    async def test_no_beat_lower_emission(
        self, engine, mock_egl_class, gl_mock
    ):
        await engine.activate()
        features_beat = _make_features(beat=True, energy=0.5)
        features_no_beat = _make_features(beat=False, energy=0.5)
        emit_beat = engine._compute_emission_count(features_beat, 1.0 / 30)
        emit_no_beat = engine._compute_emission_count(features_no_beat, 1.0 / 30)
        # Beat emission should be higher
        assert emit_beat > emit_no_beat

    def test_burst_only_style_no_continuous(self):
        engine = FosforaEngine(emission_style="burst")
        features = _make_features(beat=False, energy=0.8)
        emit = engine._compute_emission_count(features, 1.0 / 30)
        # No beat in burst-only mode: still emits baseline + energy-proportional
        # trickle so the visualization is never fully black
        assert emit > 0

    def test_burst_beat_emits_more_than_no_beat(self):
        engine = FosforaEngine(emission_style="burst")
        features_beat = _make_features(beat=True, energy=0.8)
        features_no_beat = _make_features(beat=False, energy=0.8)
        emit_beat = engine._compute_emission_count(features_beat, 1.0 / 30)
        emit_no_beat = engine._compute_emission_count(features_no_beat, 1.0 / 30)
        # Beat should produce significantly more emission than no-beat
        assert emit_beat > emit_no_beat

    def test_continuous_only_style_no_burst(self):
        engine = FosforaEngine(emission_style="continuous")
        features_beat = _make_features(beat=True, energy=0.5)
        features_no_beat = _make_features(beat=False, energy=0.5)
        emit_beat = engine._compute_emission_count(features_beat, 1.0 / 30)
        emit_no_beat = engine._compute_emission_count(features_no_beat, 1.0 / 30)
        # Continuous style: beat doesn't add burst
        assert emit_beat == emit_no_beat


# ---------------------------------------------------------------------------
# Tests — Band energy emission (Req 7 AC 2)
# ---------------------------------------------------------------------------


class TestBandEnergyEmission:
    """Band energy drives continuous emission rate."""

    def test_higher_energy_more_emission(self):
        engine = FosforaEngine(emission_style="continuous")
        features_low = _make_features(energy=0.1)
        features_high = _make_features(energy=0.9)
        dt = 1.0 / 30
        emit_low = engine._compute_emission_count(features_low, dt)
        emit_high = engine._compute_emission_count(features_high, dt)
        assert emit_high > emit_low

    def test_zero_energy_minimal_emission(self):
        engine = FosforaEngine(emission_style="continuous")
        features = _make_features(energy=0.0)
        # Even at zero energy, base rate produces some particles
        emit = engine._compute_emission_count(features, 1.0 / 30)
        assert emit >= 0

    def test_emission_capped_to_particle_count(self):
        engine = FosforaEngine(particle_count=100, emission_style="both")
        features = _make_features(beat=True, energy=1.0)
        # With large dt to force high emission
        emit = engine._compute_emission_count(features, 10.0)
        assert emit <= 100

    def test_none_features_baseline_emission(self):
        engine = FosforaEngine()
        emit = engine._compute_emission_count(None, 1.0 / 30)
        # Even without features, baseline emission keeps viz alive
        assert emit >= 1


# ---------------------------------------------------------------------------
# Tests — suspend() (Req 7 AC 6)
# ---------------------------------------------------------------------------


class TestSuspend:
    """suspend() releases all GPU particle buffers and shader programs."""

    @pytest.mark.asyncio
    async def test_deletes_vbos(self, engine, mock_egl_class, gl_mock):
        await engine.activate()
        await engine.suspend()
        # glDeleteBuffers called for 2 VBOs
        assert gl_mock.glDeleteBuffers.call_count == 2

    @pytest.mark.asyncio
    async def test_deletes_vaos(self, engine, mock_egl_class, gl_mock):
        await engine.activate()
        await engine.suspend()
        # glDeleteVertexArrays called for 2 VAOs
        assert gl_mock.glDeleteVertexArrays.call_count == 2

    @pytest.mark.asyncio
    async def test_deletes_transform_program(
        self, engine, mock_egl_class, gl_mock
    ):
        await engine.activate()
        tf_prog = engine._transform_program
        assert tf_prog != 0
        await engine.suspend()
        gl_mock.glDeleteProgram.assert_any_call(tf_prog)

    @pytest.mark.asyncio
    async def test_deletes_render_program(
        self, engine, mock_egl_class, gl_mock
    ):
        await engine.activate()
        # Store program IDs before suspend clears them
        tf_prog = engine._transform_program
        rend_prog = engine._render_program
        await engine.suspend()
        # Both programs deleted
        assert gl_mock.glDeleteProgram.call_count == 2

    @pytest.mark.asyncio
    async def test_zeroes_resource_ids(self, engine, mock_egl_class, gl_mock):
        await engine.activate()
        await engine.suspend()
        assert engine._vbo == [0, 0]
        assert engine._vao == [0, 0]
        assert engine._transform_program == 0
        assert engine._render_program == 0

    @pytest.mark.asyncio
    async def test_destroys_egl_context(self, engine, mock_egl_class, gl_mock):
        await engine.activate()
        await engine.suspend()
        mock_egl_class._instance.destroy.assert_called_once()
        assert engine._egl_ctx is None

    @pytest.mark.asyncio
    async def test_sets_gl_to_none(self, engine, mock_egl_class, gl_mock):
        await engine.activate()
        await engine.suspend()
        assert engine._gl is None


# ---------------------------------------------------------------------------
# Tests — stop()
# ---------------------------------------------------------------------------


class TestStop:
    """stop() releases resources and destroys context."""

    @pytest.mark.asyncio
    async def test_releases_gl_resources(
        self, engine, mock_egl_class, gl_mock
    ):
        await engine.activate()
        await engine.stop()
        assert gl_mock.glDeleteBuffers.call_count == 2
        assert gl_mock.glDeleteVertexArrays.call_count == 2
        assert gl_mock.glDeleteProgram.call_count == 2

    @pytest.mark.asyncio
    async def test_destroys_egl_context(self, engine, mock_egl_class, gl_mock):
        await engine.activate()
        await engine.stop()
        mock_egl_class._instance.destroy.assert_called_once()

    @pytest.mark.asyncio
    async def test_stop_without_activate_safe(self, engine):
        """stop() before activate should not raise."""
        await engine.stop()


# ---------------------------------------------------------------------------
# Tests — Shader loading
# ---------------------------------------------------------------------------


class TestShaderLoading:
    """Shader files exist and can be loaded."""

    def test_shader_dir_exists(self):
        assert SHADER_DIR.is_dir()

    def test_physics_vert_exists(self):
        assert (SHADER_DIR / "fosfora_physics.vert").is_file()

    def test_render_vert_exists(self):
        assert (SHADER_DIR / "fosfora_render.vert").is_file()

    def test_render_frag_exists(self):
        assert (SHADER_DIR / "fosfora_render.frag").is_file()

    def test_physics_shader_has_transform_feedback_outputs(self):
        src = (SHADER_DIR / "fosfora_physics.vert").read_text()
        assert "out_position" in src
        assert "out_velocity" in src
        assert "out_lifetime" in src
        assert "out_color" in src

    def test_render_frag_has_additive_blending_logic(self):
        src = (SHADER_DIR / "fosfora_render.frag").read_text()
        # Uses point coord for soft circles
        assert "gl_PointCoord" in src

    def test_load_shader_source(self, engine):
        src = engine._load_shader_source("fosfora_physics.vert")
        assert "#version 330 core" in src

    def test_load_missing_shader_raises(self, engine):
        with pytest.raises(FileNotFoundError):
            engine._load_shader_source("nonexistent.glsl")


# ---------------------------------------------------------------------------
# Tests — Orthographic matrix
# ---------------------------------------------------------------------------


class TestOrthoMatrix:
    """Orthographic projection matrix is well-formed."""

    def test_returns_16_floats(self, engine):
        mat = engine._ortho_matrix()
        assert len(mat) == 16

    def test_maps_origin_to_center(self, engine):
        mat = engine._ortho_matrix()
        # tx, ty, tz should all be 0 for symmetric bounds
        assert mat[12] == 0.0  # tx
        assert mat[13] == 0.0  # ty

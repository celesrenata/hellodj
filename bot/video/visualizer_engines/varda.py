"""Varda Engine — Shadertoy-compatible GLSL fragment shader runner.

Renders full-screen audio-reactive fragment shaders at 30fps using
the GPUEngineBase EGL context. Audio data is uploaded as a 512×2 texture
(row 0: waveform/FFT magnitudes, row 1: FFT spectrum). Uniforms follow
the Shadertoy convention plus custom audio extensions.

Requirements: Req 8 (AC 1-6)
"""

from __future__ import annotations

import ctypes
import logging
import os
import time
from pathlib import Path

from video.visualizer_engines.base import AudioFeatures, TrackMetadata
from video.visualizer_engines.gpu_engine_base import GPUEngineBase

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# OpenGL constants (ctypes, no dependency)
# ---------------------------------------------------------------------------
GL_VERTEX_SHADER = 0x8B31
GL_FRAGMENT_SHADER = 0x8B30
GL_COMPILE_STATUS = 0x8B81
GL_LINK_STATUS = 0x8B82
GL_TRUE = 1
GL_FALSE = 0
GL_TEXTURE_2D = 0x0DE1
GL_TEXTURE0 = 0x84C0
GL_TEXTURE_MIN_FILTER = 0x2801
GL_TEXTURE_MAG_FILTER = 0x2800
GL_NEAREST = 0x2600
GL_RGBA = 0x1908
GL_FLOAT = 0x1406
GL_UNSIGNED_BYTE = 0x1401
GL_RGBA32F = 0x8814
GL_TRIANGLES = 0x0004
GL_COLOR_BUFFER_BIT = 0x00004000

# Shader directory (bundled in bot container)
SHADER_DIR = Path(__file__).parent / "shaders"
FALLBACK_SHADER = "plasma"

# Beat pulse decay: 1.0 → 0.0 over ~300ms at 30fps
BEAT_DECAY_PER_FRAME = (1.0 / 30) / 0.3  # ≈ 0.1111 per frame

# Crossfade duration in seconds
CROSSFADE_DURATION = 2.0


class VardaEngine(GPUEngineBase):
    """Shadertoy-compatible GLSL fragment shader runner.

    Renders a single full-screen fragment shader with audio-reactive
    uniforms. Supports shader hot-swap on track change with crossfade.

    Configurable settings:
        - shader_name: GLSL shader file (without .glsl extension)
        - color_intensity: Color multiplier (0.5-2.0)
        - speed: Time multiplier (0.25-4.0)
        - complexity: Shader quality hint ("low", "medium", "high")
    """

    def __init__(self, **kwargs) -> None:
        super().__init__()
        # Configuration
        self._shader_name: str = kwargs.get("shader_name", "plasma")
        self._color_intensity: float = kwargs.get("color_intensity", 1.0)
        self._speed: float = kwargs.get("speed", 1.0)
        self._complexity: str = kwargs.get("complexity", "medium")

        # GL state
        self._shader_program: int = 0
        self._audio_texture: int = 0
        self._vao: int = 0
        self._start_time: float = 0.0
        self._beat_pulse: float = 0.0

        # Crossfade state
        self._prev_program: int = 0
        self._crossfade_start: float = 0.0
        self._crossfading: bool = False

        # Shader pool (populated on _on_gl_ready)
        self._shader_pool: list[str] = []
        self._shader_index: int = 0

    # ------------------------------------------------------------------
    # GPUEngineBase hooks
    # ------------------------------------------------------------------

    async def _on_gl_ready(self, metadata: TrackMetadata | None) -> None:
        """Compile vertex + fragment shader, create audio texture and VAO."""
        gl = self._egl_ctx._gl
        self._start_time = time.monotonic()

        # Discover available shaders
        self._shader_pool = self._discover_shaders()

        # Compile shader program
        self._shader_program = self._compile_shader_program(
            gl, self._shader_name
        )

        # Create 512×2 audio texture
        self._audio_texture = self._create_audio_texture(gl)

        # Create an empty VAO for the fullscreen triangle (uses gl_VertexID)
        self._vao = self._create_empty_vao(gl)

    def _render_gl_frame(self, features: AudioFeatures | None) -> None:
        """Render one frame: upload audio, set uniforms, draw triangle."""
        gl = self._egl_ctx._gl

        # Update beat pulse
        if features and features.beat:
            self._beat_pulse = 1.0
        else:
            self._beat_pulse = max(0.0, self._beat_pulse - BEAT_DECAY_PER_FRAME)

        # Clear
        gl.glClear(GL_COLOR_BUFFER_BIT)

        # Determine crossfade mix
        crossfade_mix = 0.0
        if self._crossfading:
            elapsed = time.monotonic() - self._crossfade_start
            crossfade_mix = min(1.0, elapsed / CROSSFADE_DURATION)
            if crossfade_mix >= 1.0:
                # Crossfade complete — discard old program
                self._crossfading = False
                if self._prev_program:
                    gl.glDeleteProgram(self._prev_program)
                    self._prev_program = 0

        # Upload audio texture data
        self._upload_audio_data(gl, features)

        # Render current shader (or both if crossfading)
        if self._crossfading and self._prev_program:
            # Render old shader
            self._render_with_program(gl, self._prev_program, features)
            # TODO: Real crossfade would use blending; for now we just
            # render the new one on top with alpha = crossfade_mix.
            # Simple approach: render the new one (it overwrites).
            # A proper implementation would use two FBOs and blend.
            # For simplicity, just render the new shader:
            pass

        self._render_with_program(gl, self._shader_program, features)

    async def on_track_change(self, metadata: TrackMetadata) -> None:
        """Select a new shader from the pool and trigger crossfade."""
        if not self._egl_ctx or not self._shader_pool:
            return

        gl = self._egl_ctx._gl

        # Pick next shader (sequential, wrap around)
        self._shader_index = (self._shader_index + 1) % len(self._shader_pool)
        new_shader_name = self._shader_pool[self._shader_index]

        # Avoid reloading same shader
        if new_shader_name == self._shader_name:
            self._shader_index = (self._shader_index + 1) % len(self._shader_pool)
            new_shader_name = self._shader_pool[self._shader_index]

        # Compile new shader
        new_program = self._compile_shader_program(gl, new_shader_name)
        if new_program:
            # Start crossfade
            self._prev_program = self._shader_program
            self._shader_program = new_program
            self._shader_name = new_shader_name
            self._crossfade_start = time.monotonic()
            self._crossfading = True
            log.info("Varda: crossfading to shader '%s'", new_shader_name)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _discover_shaders(self) -> list[str]:
        """List available .glsl fragment shaders (excluding vertex shader)."""
        shaders = []
        shader_dir = SHADER_DIR
        if shader_dir.is_dir():
            for f in sorted(shader_dir.iterdir()):
                if f.suffix == ".glsl" and "vertex" not in f.stem and "vert" not in f.stem:
                    shaders.append(f.stem)
        # Also check the data presets directory
        data_dir = Path("/app/data/presets/varda")
        if data_dir.is_dir():
            for f in sorted(data_dir.iterdir()):
                if f.suffix == ".glsl":
                    shaders.append(f.stem)
        if not shaders:
            shaders = [FALLBACK_SHADER]
        return shaders

    def _compile_shader_program(self, gl, shader_name: str) -> int:
        """Compile vertex + fragment shader into a program.

        If fragment shader compilation fails, falls back to plasma.glsl.
        Returns the GL program ID (0 on total failure).
        """
        # Load vertex shader source
        vert_src = self._load_shader_source("varda_vertex")
        if vert_src is None:
            log.error("Varda: failed to load vertex shader")
            return 0

        # Load fragment shader source
        frag_src = self._load_shader_source(shader_name)
        if frag_src is None:
            log.warning(
                "Varda: shader '%s' not found, falling back to '%s'",
                shader_name, FALLBACK_SHADER,
            )
            frag_src = self._load_shader_source(FALLBACK_SHADER)
            if frag_src is None:
                log.error("Varda: fallback shader '%s' also not found", FALLBACK_SHADER)
                return 0

        # Compile vertex shader
        vs = self._compile_shader(gl, GL_VERTEX_SHADER, vert_src)
        if not vs:
            return 0

        # Compile fragment shader
        fs = self._compile_shader(gl, GL_FRAGMENT_SHADER, frag_src)
        if not fs:
            # Fallback on fragment compile failure (Req 8 AC 6)
            log.warning(
                "Varda: shader '%s' failed to compile, trying fallback '%s'",
                shader_name, FALLBACK_SHADER,
            )
            gl.glDeleteShader(vs)
            fallback_src = self._load_shader_source(FALLBACK_SHADER)
            if fallback_src is None:
                return 0
            # Re-compile vertex
            vs = self._compile_shader(gl, GL_VERTEX_SHADER, vert_src)
            if not vs:
                return 0
            fs = self._compile_shader(gl, GL_FRAGMENT_SHADER, fallback_src)
            if not fs:
                gl.glDeleteShader(vs)
                return 0

        # Link program
        program = gl.glCreateProgram()
        gl.glAttachShader(program, vs)
        gl.glAttachShader(program, fs)
        gl.glLinkProgram(program)

        # Check link status
        link_status = ctypes.c_int()
        gl.glGetProgramiv(program, GL_LINK_STATUS, ctypes.byref(link_status))
        if link_status.value != GL_TRUE:
            info_log = (ctypes.c_char * 1024)()
            gl.glGetProgramInfoLog(program, 1024, None, info_log)
            log.error("Varda: program link failed: %s", info_log.value.decode())
            gl.glDeleteProgram(program)
            gl.glDeleteShader(vs)
            gl.glDeleteShader(fs)
            return 0

        # Cleanup individual shaders (attached to program now)
        gl.glDeleteShader(vs)
        gl.glDeleteShader(fs)

        return program

    def _compile_shader(self, gl, shader_type: int, source: str) -> int:
        """Compile a single shader. Returns shader ID or 0 on failure."""
        shader = gl.glCreateShader(shader_type)
        src_bytes = source.encode("utf-8")
        src_ptr = ctypes.c_char_p(src_bytes)
        length = ctypes.c_int(len(src_bytes))
        gl.glShaderSource(shader, 1, ctypes.byref(src_ptr), ctypes.byref(length))
        gl.glCompileShader(shader)

        # Check compile status
        status = ctypes.c_int()
        gl.glGetShaderiv(shader, GL_COMPILE_STATUS, ctypes.byref(status))
        if status.value != GL_TRUE:
            info_log = (ctypes.c_char * 1024)()
            gl.glGetShaderInfoLog(shader, 1024, None, info_log)
            log.error(
                "Varda: shader compile error (%s): %s",
                "vertex" if shader_type == GL_VERTEX_SHADER else "fragment",
                info_log.value.decode(),
            )
            gl.glDeleteShader(shader)
            return 0

        return shader

    def _load_shader_source(self, name: str) -> str | None:
        """Load shader source from the shaders directory or data presets."""
        # Check local shaders directory first
        local_path = SHADER_DIR / f"{name}.glsl"
        if local_path.is_file():
            return local_path.read_text()

        # Check data presets directory
        data_path = Path("/app/data/presets/varda") / f"{name}.glsl"
        if data_path.is_file():
            return data_path.read_text()

        return None

    def _create_audio_texture(self, gl) -> int:
        """Create a 512×2 RGBA float texture for audio data."""
        tex_id = ctypes.c_uint()
        gl.glGenTextures(1, ctypes.byref(tex_id))
        gl.glBindTexture(GL_TEXTURE_2D, tex_id)
        gl.glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_NEAREST)
        gl.glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_NEAREST)
        # Allocate 512×2 RGBA32F
        gl.glTexImage2D(
            GL_TEXTURE_2D, 0, GL_RGBA32F,
            512, 2, 0,
            GL_RGBA, GL_FLOAT, None,
        )
        return tex_id.value

    def _create_empty_vao(self, gl) -> int:
        """Create an empty VAO for drawing with gl_VertexID."""
        vao = ctypes.c_uint()
        gl.glGenVertexArrays(1, ctypes.byref(vao))
        return vao.value

    def _upload_audio_data(self, gl, features: AudioFeatures | None) -> None:
        """Upload audio features into the 512×2 texture.

        Row 0: waveform/FFT magnitudes (R channel, 512 values)
        Row 1: FFT spectrum (R channel, 512 values)
        """
        gl.glActiveTexture(GL_TEXTURE0)
        gl.glBindTexture(GL_TEXTURE_2D, self._audio_texture)

        if features and features.fft:
            # Row 0: waveform (we use FFT magnitudes for both in absence of raw PCM)
            fft_data = features.fft[:512]
            # Pad to 512 if shorter
            while len(fft_data) < 512:
                fft_data.append(0.0)

            # Build RGBA float data for row 0 (waveform) — 512 pixels × 4 floats
            row0 = []
            for val in fft_data:
                row0.extend([val, 0.0, 0.0, 1.0])  # R=value, G=0, B=0, A=1
            row0_array = (ctypes.c_float * len(row0))(*row0)
            gl.glTexSubImage2D(
                GL_TEXTURE_2D, 0,
                0, 0, 512, 1,
                GL_RGBA, GL_FLOAT, row0_array,
            )

            # Row 1: FFT spectrum
            row1 = []
            for val in fft_data:
                row1.extend([val, 0.0, 0.0, 1.0])
            row1_array = (ctypes.c_float * len(row1))(*row1)
            gl.glTexSubImage2D(
                GL_TEXTURE_2D, 0,
                0, 1, 512, 1,
                GL_RGBA, GL_FLOAT, row1_array,
            )

    def _render_with_program(
        self, gl, program: int, features: AudioFeatures | None
    ) -> None:
        """Bind program, set uniforms, and draw fullscreen triangle."""
        if not program:
            return

        gl.glUseProgram(program)

        # Set uniforms
        elapsed = (time.monotonic() - self._start_time) * self._speed
        self._set_uniform_float(gl, program, "iTime", elapsed)
        self._set_uniform_vec2(
            gl, program, "iResolution",
            float(self._egl_ctx.width), float(self._egl_ctx.height),
        )
        self._set_uniform_float(gl, program, "iBeat", self._beat_pulse)

        if features:
            self._set_uniform_float(gl, program, "iBPM", features.bpm)
            # Band energy array
            for i, energy in enumerate(features.band_energy[:7]):
                self._set_uniform_float(
                    gl, program, f"iBandEnergy[{i}]", energy
                )
        else:
            self._set_uniform_float(gl, program, "iBPM", 120.0)
            for i in range(7):
                self._set_uniform_float(gl, program, f"iBandEnergy[{i}]", 0.0)

        # Bind audio texture to unit 0
        self._set_uniform_int(gl, program, "iChannel0", 0)

        # Draw fullscreen triangle (3 vertices, no VBO needed)
        gl.glBindVertexArray(self._vao)
        gl.glDrawArrays(GL_TRIANGLES, 0, 3)

    def _set_uniform_float(self, gl, program: int, name: str, value: float) -> None:
        """Set a float uniform by name."""
        loc = gl.glGetUniformLocation(program, name.encode("utf-8"))
        if loc >= 0:
            gl.glUniform1f(loc, ctypes.c_float(value))

    def _set_uniform_vec2(
        self, gl, program: int, name: str, x: float, y: float
    ) -> None:
        """Set a vec2 uniform by name."""
        loc = gl.glGetUniformLocation(program, name.encode("utf-8"))
        if loc >= 0:
            gl.glUniform2f(loc, ctypes.c_float(x), ctypes.c_float(y))

    def _set_uniform_int(self, gl, program: int, name: str, value: int) -> None:
        """Set an int uniform by name."""
        loc = gl.glGetUniformLocation(program, name.encode("utf-8"))
        if loc >= 0:
            gl.glUniform1i(loc, value)

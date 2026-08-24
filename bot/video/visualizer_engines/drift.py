"""Drift Engine — multipass feedback visualizer rivaling Milkdrop.

Renders audio-reactive visuals using an iterative frame feedback loop:
each frame warps the previous frame through a vertex-displaced mesh,
applies decay, composites new visual elements (waveform, spectrum ring,
particles), and post-processes with bloom.

This produces organic, evolving trails impossible with single-pass shaders.

Architecture:
    Frame N:
    1. Warp pass: 48×36 mesh samples FBO_prev with displaced UVs → FBO_current
    2. Composite pass: new shapes drawn additively onto FBO_current
    3. Bloom pass: FBO_current → half-res blur → additive blend
    4. Final pass: FBO_current + bloom → output FBO (read by GPUEngineBase)
    5. Swap: FBO_current becomes FBO_prev for next frame
"""

from __future__ import annotations

import ctypes
import logging
import math
import time
from pathlib import Path

import numpy as np

from video.visualizer_engines.base import AudioFeatures, TrackMetadata
from video.visualizer_engines.gpu_engine_base import GPUEngineBase

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# GL constants
# ---------------------------------------------------------------------------
GL_TRUE = 1
GL_FALSE = 0
GL_FLOAT = 0x1406
GL_UNSIGNED_BYTE = 0x1401
GL_ARRAY_BUFFER = 0x8892
GL_ELEMENT_ARRAY_BUFFER = 0x8893
GL_STATIC_DRAW = 0x88E4
GL_DYNAMIC_DRAW = 0x88E8
GL_VERTEX_SHADER = 0x8B31
GL_FRAGMENT_SHADER = 0x8B30
GL_COMPILE_STATUS = 0x8B81
GL_LINK_STATUS = 0x8B82
GL_INFO_LOG_LENGTH = 0x8B84
GL_TEXTURE_2D = 0x0DE1
GL_TEXTURE_1D = 0x0DE1 - 1  # 0x0DE0
GL_TEXTURE0 = 0x84C0
GL_TEXTURE1 = 0x84C1
GL_TEXTURE2 = 0x84C2
GL_TEXTURE3 = 0x84C3
GL_RGBA = 0x1908
GL_RGBA8 = 0x8058
GL_RED = 0x1903
GL_R32F = 0x822E
GL_FRAMEBUFFER = 0x8D40
GL_COLOR_ATTACHMENT0 = 0x8CE0
GL_FRAMEBUFFER_COMPLETE = 0x8CD5
GL_COLOR_BUFFER_BIT = 0x00004000
GL_BLEND = 0x0BE2
GL_ONE = 1
GL_SRC_ALPHA = 0x0302
GL_TRIANGLES = 0x0004
GL_TRIANGLE_STRIP = 0x0005
GL_TEXTURE_MIN_FILTER = 0x2801
GL_TEXTURE_MAG_FILTER = 0x2800
GL_LINEAR = 0x2601
GL_CLAMP_TO_EDGE = 0x812F
GL_TEXTURE_WRAP_S = 0x2802
GL_TEXTURE_WRAP_T = 0x2803
GL_RENDERBUFFER = 0x8D41
GL_DEPTH_BUFFER_BIT = 0x00000100
GL_PROGRAM_POINT_SIZE = 0x8642

# Warp mesh dimensions
MESH_W = 48
MESH_H = 36

# Shader directory
SHADER_DIR = Path(__file__).parent / "shaders"

# Default preset
DEFAULT_PRESET = {
    "name": "Cosmic Drift",
    "warp": {
        "zoom_base": 1.005,
        "zoom_bass": 0.02,
        "zoom_beat": 0.04,
        "rot_base": 0.003,
        "rot_mids": 0.008,
        "warp_x_freq": 2.0,
        "warp_y_freq": 3.0,
        "warp_amplitude": 0.006,
    },
    "decay": 0.965,
    "composite": {
        "wave_enabled": True,
        "wave_thickness": 3.0,
        "wave_color": [0.2, 0.7, 1.0],
        "ring_enabled": True,
        "ring_radius": 0.25,
        "ring_glow": 1.2,
        "particles_enabled": True,
        "particle_count": 40,
        "particle_size": 5.0,
    },
    "bloom": {
        "intensity": 0.35,
    },
}


class DriftEngine(GPUEngineBase):
    """Multipass feedback visualizer with warp mesh, compositing, and bloom.

    Configurable:
        preset: dict — full preset configuration (warp, decay, composite, bloom)
    """

    def __init__(self, **kwargs) -> None:
        super().__init__()
        self._preset: dict = kwargs.get("preset", DEFAULT_PRESET)

        # GL resources
        self._gl: ctypes.CDLL | None = None

        # Programs
        self._warp_program: int = 0
        self._composite_program: int = 0
        self._bloom_h_program: int = 0
        self._bloom_v_program: int = 0
        self._final_program: int = 0

        # FBOs: ping-pong for feedback
        self._fbo_a: int = 0  # "current" frame
        self._fbo_b: int = 0  # "previous" frame
        self._tex_a: int = 0
        self._tex_b: int = 0
        self._current_is_a: bool = True

        # Bloom FBOs (half resolution)
        self._bloom_fbo_h: int = 0
        self._bloom_fbo_v: int = 0
        self._bloom_tex_h: int = 0
        self._bloom_tex_v: int = 0

        # Warp mesh
        self._warp_vao: int = 0
        self._warp_vbo: int = 0
        self._warp_ebo: int = 0
        self._warp_index_count: int = 0

        # Composite fullscreen triangle VAO
        self._fs_vao: int = 0

        # Audio textures
        self._fft_texture: int = 0
        self._wave_texture: int = 0

        # Timing
        self._start_time: float = 0.0
        self._beat_pulse: float = 0.0

        # Smoothed audio values (interpolated for buttery motion)
        self._smooth_bass: float = 0.0
        self._smooth_mids: float = 0.0
        self._smooth_highs: float = 0.0

    # ------------------------------------------------------------------
    # GPUEngineBase hooks
    # ------------------------------------------------------------------

    async def _on_gl_ready(self, metadata: TrackMetadata | None) -> None:
        """Set up all shaders, FBOs, mesh, and textures."""
        self._gl = self._egl_ctx._gl
        self._start_time = time.monotonic()
        gl = self._gl
        width = self._egl_ctx.width
        height = self._egl_ctx.height

        # Compile shader programs
        self._warp_program = self._compile_program(
            "drift_warp.vert", "drift_warp.frag"
        )
        self._composite_program = self._compile_program(
            "drift_composite.vert", "drift_composite.frag"
        )
        self._bloom_h_program = self._compile_program(
            "drift_composite.vert", "drift_bloom_h.frag"
        )
        self._bloom_v_program = self._compile_program(
            "drift_composite.vert", "drift_bloom_v.frag"
        )
        self._final_program = self._compile_program(
            "drift_composite.vert", "drift_final.frag"
        )

        # Create ping-pong FBOs (full resolution)
        self._fbo_a, self._tex_a = self._create_feedback_fbo(width, height)
        self._fbo_b, self._tex_b = self._create_feedback_fbo(width, height)

        # Create bloom FBOs (half resolution)
        hw, hh = width // 2, height // 2
        self._bloom_fbo_h, self._bloom_tex_h = self._create_feedback_fbo(hw, hh)
        self._bloom_fbo_v, self._bloom_tex_v = self._create_feedback_fbo(hw, hh)

        # Create warp mesh
        self._create_warp_mesh()

        # Create fullscreen triangle VAO (empty — uses gl_VertexID)
        vao = ctypes.c_uint()
        gl.glGenVertexArrays(1, ctypes.byref(vao))
        self._fs_vao = vao.value

        # Create audio data textures
        self._fft_texture = self._create_1d_texture(64)
        self._wave_texture = self._create_1d_texture(512)

        # Clear both feedback FBOs to black
        for fbo in (self._fbo_a, self._fbo_b):
            gl.glBindFramebuffer(GL_FRAMEBUFFER, fbo)
            gl.glClearColor(
                ctypes.c_float(0.0), ctypes.c_float(0.0),
                ctypes.c_float(0.0), ctypes.c_float(1.0),
            )
            gl.glClear(GL_COLOR_BUFFER_BIT)

        # Bind back to the main output FBO
        gl.glBindFramebuffer(GL_FRAMEBUFFER, self._egl_ctx._fbo)

        log.info("Drift engine ready: %dx%d, mesh %dx%d", width, height, MESH_W, MESH_H)

    def _render_gl_frame(self, features: AudioFeatures | None) -> None:
        """Execute the full multipass render pipeline."""
        gl = self._gl
        if gl is None:
            return

        now = time.monotonic()
        dt = 1.0 / 30.0  # Fixed timestep
        elapsed = now - self._start_time

        # Update smoothed audio values
        self._update_smoothed_audio(features, dt)

        # Update beat pulse
        if features and features.beat:
            self._beat_pulse = 1.0
        else:
            self._beat_pulse = max(0.0, self._beat_pulse - dt / 0.25)

        # Upload audio data textures
        self._upload_audio_textures(features)

        # Determine current/previous FBOs
        if self._current_is_a:
            fbo_current, tex_current = self._fbo_a, self._tex_a
            fbo_prev, tex_prev = self._fbo_b, self._tex_b
        else:
            fbo_current, tex_current = self._fbo_b, self._tex_b
            fbo_prev, tex_prev = self._fbo_a, self._tex_a

        width = self._egl_ctx.width
        height = self._egl_ctx.height

        # --- Pass 1: Warp (render prev frame warped into current FBO) ---
        gl.glBindFramebuffer(GL_FRAMEBUFFER, fbo_current)
        gl.glViewport(0, 0, width, height)
        gl.glClear(GL_COLOR_BUFFER_BIT)

        gl.glUseProgram(self._warp_program)
        self._set_warp_uniforms(elapsed)

        # Bind previous frame as texture
        gl.glActiveTexture(GL_TEXTURE0)
        gl.glBindTexture(GL_TEXTURE_2D, tex_prev)
        loc = gl.glGetUniformLocation(self._warp_program, b"u_prev_frame")
        if loc >= 0:
            gl.glUniform1i(loc, 0)

        # Draw warp mesh
        gl.glBindVertexArray(self._warp_vao)
        gl.glDrawElements(GL_TRIANGLES, self._warp_index_count, 0x1405, None)  # GL_UNSIGNED_INT
        gl.glBindVertexArray(0)

        # --- Pass 2: Composite (draw new shapes additively onto current FBO) ---
        gl.glEnable(GL_BLEND)
        gl.glBlendFunc(GL_SRC_ALPHA, GL_ONE)  # Additive

        gl.glUseProgram(self._composite_program)
        self._set_composite_uniforms(elapsed, width, height)

        # Bind FFT and waveform textures
        gl.glActiveTexture(GL_TEXTURE0)
        gl.glBindTexture(GL_TEXTURE_1D, self._fft_texture)
        loc = gl.glGetUniformLocation(self._composite_program, b"u_fft")
        if loc >= 0:
            gl.glUniform1i(loc, 0)

        gl.glActiveTexture(GL_TEXTURE1)
        gl.glBindTexture(GL_TEXTURE_1D, self._wave_texture)
        loc = gl.glGetUniformLocation(self._composite_program, b"u_waveform")
        if loc >= 0:
            gl.glUniform1i(loc, 1)

        # Draw fullscreen triangle
        gl.glBindVertexArray(self._fs_vao)
        gl.glDrawArrays(GL_TRIANGLES, 0, 3)
        gl.glBindVertexArray(0)

        gl.glDisable(GL_BLEND)

        # --- Pass 3: Bloom (downsample + horizontal blur) ---
        hw, hh = width // 2, height // 2

        # Horizontal blur: current FBO → bloom_fbo_h
        gl.glBindFramebuffer(GL_FRAMEBUFFER, self._bloom_fbo_h)
        gl.glViewport(0, 0, hw, hh)
        gl.glClear(GL_COLOR_BUFFER_BIT)

        gl.glUseProgram(self._bloom_h_program)
        gl.glActiveTexture(GL_TEXTURE0)
        gl.glBindTexture(GL_TEXTURE_2D, tex_current)
        loc = gl.glGetUniformLocation(self._bloom_h_program, b"u_source")
        if loc >= 0:
            gl.glUniform1i(loc, 0)
        loc = gl.glGetUniformLocation(self._bloom_h_program, b"u_texel_size")
        if loc >= 0:
            gl.glUniform1f(loc, ctypes.c_float(1.0 / hw))

        gl.glBindVertexArray(self._fs_vao)
        gl.glDrawArrays(GL_TRIANGLES, 0, 3)

        # Vertical blur: bloom_fbo_h → bloom_fbo_v
        gl.glBindFramebuffer(GL_FRAMEBUFFER, self._bloom_fbo_v)
        gl.glClear(GL_COLOR_BUFFER_BIT)

        gl.glUseProgram(self._bloom_v_program)
        gl.glActiveTexture(GL_TEXTURE0)
        gl.glBindTexture(GL_TEXTURE_2D, self._bloom_tex_h)
        loc = gl.glGetUniformLocation(self._bloom_v_program, b"u_source")
        if loc >= 0:
            gl.glUniform1i(loc, 0)
        loc = gl.glGetUniformLocation(self._bloom_v_program, b"u_texel_size")
        if loc >= 0:
            gl.glUniform1f(loc, ctypes.c_float(1.0 / hh))

        gl.glDrawArrays(GL_TRIANGLES, 0, 3)
        gl.glBindVertexArray(0)

        # --- Pass 4: Final composite (main + bloom → output FBO) ---
        gl.glBindFramebuffer(GL_FRAMEBUFFER, self._egl_ctx._fbo)
        gl.glViewport(0, 0, width, height)
        gl.glClear(GL_COLOR_BUFFER_BIT)

        gl.glUseProgram(self._final_program)

        gl.glActiveTexture(GL_TEXTURE0)
        gl.glBindTexture(GL_TEXTURE_2D, tex_current)
        loc = gl.glGetUniformLocation(self._final_program, b"u_main")
        if loc >= 0:
            gl.glUniform1i(loc, 0)

        gl.glActiveTexture(GL_TEXTURE1)
        gl.glBindTexture(GL_TEXTURE_2D, self._bloom_tex_v)
        loc = gl.glGetUniformLocation(self._final_program, b"u_bloom")
        if loc >= 0:
            gl.glUniform1i(loc, 1)

        bloom_cfg = self._preset.get("bloom", {})
        loc = gl.glGetUniformLocation(self._final_program, b"u_bloom_intensity")
        if loc >= 0:
            gl.glUniform1f(loc, ctypes.c_float(bloom_cfg.get("intensity", 0.3)))

        gl.glBindVertexArray(self._fs_vao)
        gl.glDrawArrays(GL_TRIANGLES, 0, 3)
        gl.glBindVertexArray(0)

        # --- Swap feedback buffers ---
        self._current_is_a = not self._current_is_a

    async def stop(self) -> None:
        """Release all GL resources."""
        self._release_gl_resources()
        await super().stop()

    async def suspend(self) -> None:
        """Release GL resources then EGL context."""
        self._release_gl_resources()
        await super().suspend()

    # ------------------------------------------------------------------
    # Internal: shader compilation
    # ------------------------------------------------------------------

    def _compile_program(self, vert_file: str, frag_file: str) -> int:
        """Compile and link a shader program from files."""
        gl = self._gl
        vert_src = (SHADER_DIR / vert_file).read_text()
        frag_src = (SHADER_DIR / frag_file).read_text()

        vert = self._compile_shader(vert_src, GL_VERTEX_SHADER)
        frag = self._compile_shader(frag_src, GL_FRAGMENT_SHADER)

        program = gl.glCreateProgram()
        gl.glAttachShader(program, vert)
        gl.glAttachShader(program, frag)
        gl.glLinkProgram(program)

        status = ctypes.c_int()
        gl.glGetProgramiv(program, GL_LINK_STATUS, ctypes.byref(status))
        if status.value != GL_TRUE:
            log_len = ctypes.c_int()
            gl.glGetProgramiv(program, GL_INFO_LOG_LENGTH, ctypes.byref(log_len))
            buf = ctypes.create_string_buffer(max(log_len.value, 1))
            gl.glGetProgramInfoLog(program, log_len.value, None, buf)
            msg = buf.value.decode("utf-8", errors="replace")
            gl.glDeleteProgram(program)
            raise RuntimeError(f"Link failed ({vert_file}+{frag_file}): {msg}")

        gl.glDetachShader(program, vert)
        gl.glDetachShader(program, frag)
        gl.glDeleteShader(vert)
        gl.glDeleteShader(frag)
        return program

    def _compile_shader(self, source: str, shader_type: int) -> int:
        """Compile a single shader."""
        gl = self._gl
        shader = gl.glCreateShader(shader_type)
        src_bytes = source.encode("utf-8")
        src_ptr = ctypes.c_char_p(src_bytes)
        length = ctypes.c_int(len(src_bytes))
        gl.glShaderSource(shader, 1, ctypes.byref(src_ptr), ctypes.byref(length))
        gl.glCompileShader(shader)

        status = ctypes.c_int()
        gl.glGetShaderiv(shader, GL_COMPILE_STATUS, ctypes.byref(status))
        if status.value != GL_TRUE:
            log_len = ctypes.c_int()
            gl.glGetShaderiv(shader, GL_INFO_LOG_LENGTH, ctypes.byref(log_len))
            buf = ctypes.create_string_buffer(max(log_len.value, 1))
            gl.glGetShaderInfoLog(shader, log_len.value, None, buf)
            msg = buf.value.decode("utf-8", errors="replace")
            gl.glDeleteShader(shader)
            raise RuntimeError(f"Shader compile failed: {msg}")
        return shader

    # ------------------------------------------------------------------
    # Internal: FBO creation
    # ------------------------------------------------------------------

    def _create_feedback_fbo(self, width: int, height: int) -> tuple[int, int]:
        """Create an FBO with an RGBA8 texture attachment. Returns (fbo, tex)."""
        gl = self._gl

        # Create texture
        tex = ctypes.c_uint()
        gl.glGenTextures(1, ctypes.byref(tex))
        gl.glBindTexture(GL_TEXTURE_2D, tex)
        gl.glTexImage2D(
            GL_TEXTURE_2D, 0, GL_RGBA8, width, height, 0,
            GL_RGBA, GL_UNSIGNED_BYTE, None,
        )
        gl.glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR)
        gl.glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR)
        gl.glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE)
        gl.glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE)

        # Create FBO
        fbo = ctypes.c_uint()
        gl.glGenFramebuffers(1, ctypes.byref(fbo))
        gl.glBindFramebuffer(GL_FRAMEBUFFER, fbo)
        gl.glFramebufferTexture2D(
            GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D, tex, 0,
        )

        status = gl.glCheckFramebufferStatus(GL_FRAMEBUFFER)
        if status != GL_FRAMEBUFFER_COMPLETE:
            raise RuntimeError(f"FBO incomplete: 0x{status:04X}")

        gl.glBindFramebuffer(GL_FRAMEBUFFER, 0)
        return fbo.value, tex.value

    # ------------------------------------------------------------------
    # Internal: warp mesh
    # ------------------------------------------------------------------

    def _create_warp_mesh(self) -> None:
        """Create a MESH_W × MESH_H vertex grid for the warp pass."""
        gl = self._gl

        # Generate vertices: position (x,y) + uv (u,v) = 4 floats per vertex
        vertices = []
        for j in range(MESH_H + 1):
            for i in range(MESH_W + 1):
                x = (i / MESH_W) * 2.0 - 1.0  # [-1, 1]
                y = (j / MESH_H) * 2.0 - 1.0
                u = i / MESH_W  # [0, 1]
                v = j / MESH_H
                vertices.extend([x, y, u, v])

        # Generate indices (two triangles per quad)
        indices = []
        for j in range(MESH_H):
            for i in range(MESH_W):
                tl = j * (MESH_W + 1) + i
                tr = tl + 1
                bl = (j + 1) * (MESH_W + 1) + i
                br = bl + 1
                indices.extend([tl, bl, tr, tr, bl, br])

        self._warp_index_count = len(indices)

        vert_data = (ctypes.c_float * len(vertices))(*vertices)
        idx_data = (ctypes.c_uint * len(indices))(*indices)

        # Create VAO
        vao = ctypes.c_uint()
        gl.glGenVertexArrays(1, ctypes.byref(vao))
        gl.glBindVertexArray(vao)
        self._warp_vao = vao.value

        # Create VBO
        vbo = ctypes.c_uint()
        gl.glGenBuffers(1, ctypes.byref(vbo))
        gl.glBindBuffer(GL_ARRAY_BUFFER, vbo)
        gl.glBufferData(GL_ARRAY_BUFFER, ctypes.sizeof(vert_data), vert_data, GL_STATIC_DRAW)
        self._warp_vbo = vbo.value

        # Create EBO
        ebo = ctypes.c_uint()
        gl.glGenBuffers(1, ctypes.byref(ebo))
        gl.glBindBuffer(GL_ELEMENT_ARRAY_BUFFER, ebo)
        gl.glBufferData(GL_ELEMENT_ARRAY_BUFFER, ctypes.sizeof(idx_data), idx_data, GL_STATIC_DRAW)
        self._warp_ebo = ebo.value

        # Vertex attribs: location 0 = position (2 floats), location 1 = uv (2 floats)
        stride = 4 * 4  # 4 floats × 4 bytes
        gl.glEnableVertexAttribArray(0)
        gl.glVertexAttribPointer(0, 2, GL_FLOAT, GL_FALSE, stride, ctypes.c_void_p(0))
        gl.glEnableVertexAttribArray(1)
        gl.glVertexAttribPointer(1, 2, GL_FLOAT, GL_FALSE, stride, ctypes.c_void_p(8))

        gl.glBindVertexArray(0)

    # ------------------------------------------------------------------
    # Internal: audio textures
    # ------------------------------------------------------------------

    def _create_1d_texture(self, size: int) -> int:
        """Create a 1D R32F texture for audio data."""
        gl = self._gl
        tex = ctypes.c_uint()
        gl.glGenTextures(1, ctypes.byref(tex))
        gl.glBindTexture(GL_TEXTURE_1D, tex)
        gl.glTexParameteri(GL_TEXTURE_1D, GL_TEXTURE_MIN_FILTER, GL_LINEAR)
        gl.glTexParameteri(GL_TEXTURE_1D, GL_TEXTURE_MAG_FILTER, GL_LINEAR)
        gl.glTexParameteri(GL_TEXTURE_1D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE)
        zeros = (ctypes.c_float * size)(*([0.0] * size))
        gl.glTexImage1D(GL_TEXTURE_1D, 0, GL_R32F, size, 0, GL_RED, GL_FLOAT, zeros)
        return tex.value

    def _upload_audio_textures(self, features: AudioFeatures | None) -> None:
        """Upload FFT and waveform data to GPU textures."""
        gl = self._gl
        if features is None:
            return

        # Upload FFT (first 64 bins)
        fft_data = features.fft[:64] if len(features.fft) >= 64 else features.fft
        n = len(fft_data)
        fft_arr = (ctypes.c_float * 64)(*fft_data[:64])
        gl.glBindTexture(GL_TEXTURE_1D, self._fft_texture)
        gl.glTexSubImage1D(GL_TEXTURE_1D, 0, 0, 64, GL_RED, GL_FLOAT, fft_arr)

        # Upload waveform (use FFT as proxy if no raw waveform available)
        # AudioFeatures has .fft but not raw waveform — synthesize one from band energy
        wave_data = [0.0] * 512
        if features.band_energy:
            for i in range(512):
                t = i / 512.0
                val = 0.0
                for b, energy in enumerate(features.band_energy[:7]):
                    freq = (b + 1) * 2.0
                    val += energy * math.sin(t * math.pi * 2.0 * freq + self._smooth_bass * 3.0)
                wave_data[i] = val * 0.3
        wave_arr = (ctypes.c_float * 512)(*wave_data)
        gl.glBindTexture(GL_TEXTURE_1D, self._wave_texture)
        gl.glTexSubImage1D(GL_TEXTURE_1D, 0, 0, 512, GL_RED, GL_FLOAT, wave_arr)

    # ------------------------------------------------------------------
    # Internal: uniform setting
    # ------------------------------------------------------------------

    def _set_warp_uniforms(self, elapsed: float) -> None:
        """Set uniforms for the warp pass."""
        gl = self._gl
        prog = self._warp_program
        warp_cfg = self._preset.get("warp", {})

        self._uf(prog, "u_time", elapsed)
        self._uf(prog, "u_bass", self._smooth_bass)
        self._uf(prog, "u_mids", self._smooth_mids)
        self._uf(prog, "u_highs", self._smooth_highs)
        self._uf(prog, "u_beat", self._beat_pulse)
        self._uf(prog, "u_decay", self._preset.get("decay", 0.965))

        self._uf(prog, "u_zoom_base", warp_cfg.get("zoom_base", 1.005))
        self._uf(prog, "u_zoom_bass", warp_cfg.get("zoom_bass", 0.02))
        self._uf(prog, "u_zoom_beat", warp_cfg.get("zoom_beat", 0.04))
        self._uf(prog, "u_rot_base", warp_cfg.get("rot_base", 0.003))
        self._uf(prog, "u_rot_mids", warp_cfg.get("rot_mids", 0.008))
        self._uf(prog, "u_warp_x_freq", warp_cfg.get("warp_x_freq", 2.0))
        self._uf(prog, "u_warp_y_freq", warp_cfg.get("warp_y_freq", 3.0))
        self._uf(prog, "u_warp_amplitude", warp_cfg.get("warp_amplitude", 0.006))

    def _set_composite_uniforms(self, elapsed: float, width: int, height: int) -> None:
        """Set uniforms for the composite pass."""
        gl = self._gl
        prog = self._composite_program
        comp_cfg = self._preset.get("composite", {})

        self._uf(prog, "u_time", elapsed)
        loc = gl.glGetUniformLocation(prog, b"u_resolution")
        if loc >= 0:
            gl.glUniform2f(loc, ctypes.c_float(float(width)), ctypes.c_float(float(height)))
        self._uf(prog, "u_beat", self._beat_pulse)
        self._uf(prog, "u_bass", self._smooth_bass)
        self._uf(prog, "u_mids", self._smooth_mids)
        self._uf(prog, "u_highs", self._smooth_highs)
        self._uf(prog, "u_bpm", 120.0)  # TODO: use features.bpm

        # Wave
        self._uf(prog, "u_wave_enabled", 1.0 if comp_cfg.get("wave_enabled", True) else 0.0)
        self._uf(prog, "u_wave_thickness", comp_cfg.get("wave_thickness", 3.0))
        wc = comp_cfg.get("wave_color", [0.2, 0.7, 1.0])
        loc = gl.glGetUniformLocation(prog, b"u_wave_color")
        if loc >= 0:
            gl.glUniform3f(loc, ctypes.c_float(wc[0]), ctypes.c_float(wc[1]), ctypes.c_float(wc[2]))

        # Ring
        self._uf(prog, "u_ring_enabled", 1.0 if comp_cfg.get("ring_enabled", True) else 0.0)
        self._uf(prog, "u_ring_radius", comp_cfg.get("ring_radius", 0.25))
        self._uf(prog, "u_ring_glow", comp_cfg.get("ring_glow", 1.2))

        # Particles
        self._uf(prog, "u_particles_enabled", 1.0 if comp_cfg.get("particles_enabled", True) else 0.0)
        self._uf(prog, "u_particle_count", comp_cfg.get("particle_count", 40.0))
        self._uf(prog, "u_particle_size", comp_cfg.get("particle_size", 5.0))

    def _uf(self, program: int, name: str, value: float) -> None:
        """Set a float uniform by name."""
        gl = self._gl
        loc = gl.glGetUniformLocation(program, name.encode("utf-8"))
        if loc >= 0:
            gl.glUniform1f(loc, ctypes.c_float(value))

    # ------------------------------------------------------------------
    # Internal: audio smoothing
    # ------------------------------------------------------------------

    def _update_smoothed_audio(self, features: AudioFeatures | None, dt: float) -> None:
        """Exponential smoothing of audio values for buttery motion."""
        # Smoothing factor — higher = more responsive, lower = smoother
        alpha = min(1.0, dt * 8.0)  # ~8Hz response

        if features and features.band_energy:
            target_bass = (features.band_energy[0] + features.band_energy[1]) * 0.5
            target_mids = sum(features.band_energy[2:5]) / 3.0
            target_highs = (features.band_energy[5] + features.band_energy[6]) * 0.5
        else:
            target_bass = 0.0
            target_mids = 0.0
            target_highs = 0.0

        self._smooth_bass += (target_bass - self._smooth_bass) * alpha
        self._smooth_mids += (target_mids - self._smooth_mids) * alpha
        self._smooth_highs += (target_highs - self._smooth_highs) * alpha

    # ------------------------------------------------------------------
    # Internal: cleanup
    # ------------------------------------------------------------------

    def _release_gl_resources(self) -> None:
        """Delete all GPU resources."""
        gl = self._gl
        if gl is None:
            return

        # Delete programs
        for prog in (self._warp_program, self._composite_program,
                     self._bloom_h_program, self._bloom_v_program,
                     self._final_program):
            if prog:
                gl.glDeleteProgram(prog)

        # Delete FBOs and textures
        for fbo_id in (self._fbo_a, self._fbo_b, self._bloom_fbo_h, self._bloom_fbo_v):
            if fbo_id:
                fbo_c = ctypes.c_uint(fbo_id)
                gl.glDeleteFramebuffers(1, ctypes.byref(fbo_c))

        for tex_id in (self._tex_a, self._tex_b, self._bloom_tex_h, self._bloom_tex_v,
                       self._fft_texture, self._wave_texture):
            if tex_id:
                tex_c = ctypes.c_uint(tex_id)
                gl.glDeleteTextures(1, ctypes.byref(tex_c))

        # Delete mesh
        if self._warp_vao:
            vao_c = ctypes.c_uint(self._warp_vao)
            gl.glDeleteVertexArrays(1, ctypes.byref(vao_c))
        if self._warp_vbo:
            vbo_c = ctypes.c_uint(self._warp_vbo)
            gl.glDeleteBuffers(1, ctypes.byref(vbo_c))
        if self._warp_ebo:
            ebo_c = ctypes.c_uint(self._warp_ebo)
            gl.glDeleteBuffers(1, ctypes.byref(ebo_c))
        if self._fs_vao:
            vao_c = ctypes.c_uint(self._fs_vao)
            gl.glDeleteVertexArrays(1, ctypes.byref(vao_c))

        self._gl = None

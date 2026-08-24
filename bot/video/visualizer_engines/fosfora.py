"""Fosfora Engine — GPU particle system with transform feedback.

Renders audio-reactive particle animations using OpenGL transform feedback
for GPU-side physics simulation. Particles react to beat detection (burst
emission) and band energy (continuous emission rate).

Particle data layout (44 bytes/particle):
    vec3 position  (12B)
    vec3 velocity  (12B)
    float lifetime (4B)
    vec4 color     (16B)

Uses ping-pong VBO pattern: two VAO/VBO pairs swapped each frame.
Physics pass uses transform feedback with rasterizer discard.
Render pass draws point sprites with additive blending.

Requirements: Req 7 (AC 1-6)
"""

from __future__ import annotations

import ctypes
import logging
import math
import time
from pathlib import Path

from video.visualizer_engines.base import AudioFeatures, TrackMetadata
from video.visualizer_engines.gpu_engine_base import GPUEngineBase

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# OpenGL constants (core 3.3)
# ---------------------------------------------------------------------------

GL_TRUE = 1
GL_FALSE = 0
GL_FLOAT = 0x1406
GL_ARRAY_BUFFER = 0x8892
GL_TRANSFORM_FEEDBACK_BUFFER = 0x8C8E
GL_STATIC_DRAW = 0x88E4
GL_DYNAMIC_COPY = 0x88EA
GL_VERTEX_SHADER = 0x8B31
GL_FRAGMENT_SHADER = 0x8B30
GL_COMPILE_STATUS = 0x8B81
GL_LINK_STATUS = 0x8B82
GL_INTERLEAVED_ATTRIBS = 0x8C8C
GL_RASTERIZER_DISCARD = 0x8C89
GL_TRANSFORM_FEEDBACK = 0x8E22
GL_POINTS = 0x0000
GL_BLEND = 0x0BE2
GL_ONE = 1
GL_ONE_MINUS_SRC_ALPHA = 0x0303
GL_SRC_ALPHA = 0x0302
GL_COLOR_BUFFER_BIT = 0x00004000
GL_DEPTH_BUFFER_BIT = 0x00000100
GL_PROGRAM_POINT_SIZE = 0x8642
GL_POINT_SPRITE = 0x8861
GL_VERTEX_PROGRAM_POINT_SIZE = 0x8642
GL_INFO_LOG_LENGTH = 0x8B84

# Particle struct: vec3 + vec3 + float + vec4 = 44 bytes
PARTICLE_STRIDE = 44
POSITION_OFFSET = 0
VELOCITY_OFFSET = 12
LIFETIME_OFFSET = 24
COLOR_OFFSET = 28

# Shader directory relative to this file
SHADER_DIR = Path(__file__).parent / "shaders"


class FosforaEngine(GPUEngineBase):
    """GPU particle system driven by audio features.

    Transform feedback for physics, additive blending for rendering.
    Beat detection triggers burst emission; band energy drives continuous rate.

    Configurable:
        particle_count: Max particles (100-10000, default 5000)
        gravity: Downward force (0.0-5.0, default 0.5)
        emission_style: "burst", "continuous", or "both" (default "both")
        color_mode: "spectrum", "warm", "cool", "mono" (default "spectrum")
        trail_length: Visual trail factor (0.0-1.0, default 0.3)
    """

    MAX_PARTICLES = 10_000

    def __init__(self, **kwargs) -> None:
        super().__init__()
        # Configuration
        self._particle_count: int = kwargs.get("particle_count", 5000)
        self._gravity: float = kwargs.get("gravity", 0.5)
        self._emission_style: str = kwargs.get("emission_style", "both")
        self._color_mode: str = kwargs.get("color_mode", "spectrum")
        self._trail_length: float = kwargs.get("trail_length", 0.3)

        # GL resources (allocated in _on_gl_ready)
        self._vao: list[int] = [0, 0]
        self._vbo: list[int] = [0, 0]
        self._transform_program: int = 0
        self._render_program: int = 0
        self._current_buffer: int = 0

        # Timing
        self._start_time: float = 0.0
        self._last_frame_time: float = 0.0
        self._beat_pulse: float = 0.0

        # GL library reference (from EGL context)
        self._gl: ctypes.CDLL | None = None

    # ------------------------------------------------------------------
    # GPUEngineBase hooks
    # ------------------------------------------------------------------

    async def _on_gl_ready(self, metadata: TrackMetadata | None) -> None:
        """Compile shaders, allocate ping-pong particle buffers."""
        self._gl = self._egl_ctx._gl
        self._start_time = time.monotonic()
        self._last_frame_time = self._start_time

        # Compile transform feedback program
        physics_vert_src = self._load_shader_source("fosfora_physics.vert")
        self._transform_program = self._compile_transform_feedback_program(
            physics_vert_src,
            varyings=["out_position", "out_velocity", "out_lifetime", "out_color"],
        )

        # Compile render program
        render_vert_src = self._load_shader_source("fosfora_render.vert")
        render_frag_src = self._load_shader_source("fosfora_render.frag")
        self._render_program = self._compile_shader_program(
            render_vert_src, render_frag_src
        )

        # Allocate ping-pong particle buffers
        self._allocate_particle_buffers()

        log.info(
            "Fosfora GL ready: %d particles, gravity=%.2f, style=%s",
            self._particle_count,
            self._gravity,
            self._emission_style,
        )

    def _render_gl_frame(self, features: AudioFeatures | None) -> None:
        """Render one frame: physics pass + render pass."""
        gl = self._gl
        if gl is None:
            return

        now = time.monotonic()
        dt = min(now - self._last_frame_time, 0.1)  # Cap to avoid spiral
        self._last_frame_time = now
        elapsed = now - self._start_time

        # Update beat pulse
        if features and features.beat:
            self._beat_pulse = 1.0
        else:
            # Decay over ~300ms
            self._beat_pulse = max(0.0, self._beat_pulse - dt / 0.3)

        # Determine emission count this frame
        emit_count = self._compute_emission_count(features, dt)

        # --- Physics pass (transform feedback) ---
        self._physics_pass(dt, elapsed, features, emit_count)

        # Swap ping-pong buffers
        self._current_buffer = 1 - self._current_buffer

        # --- Render pass ---
        self._render_pass(elapsed)

    # ------------------------------------------------------------------
    # suspend() — release all GPU resources (Req 7 AC 6)
    # ------------------------------------------------------------------

    async def suspend(self) -> None:
        """Release all GPU particle buffers and shader programs, then EGL."""
        self._release_gl_resources()
        await super().suspend()

    async def stop(self) -> None:
        """Full shutdown: release GL resources then destroy EGL context."""
        self._release_gl_resources()
        await super().stop()

    # ------------------------------------------------------------------
    # Internal: shader compilation
    # ------------------------------------------------------------------

    def _load_shader_source(self, filename: str) -> str:
        """Load shader source from the shaders directory."""
        path = SHADER_DIR / filename
        if not path.exists():
            raise FileNotFoundError(f"Shader not found: {path}")
        return path.read_text()

    def _compile_shader(self, source: str, shader_type: int) -> int:
        """Compile a single shader, return shader ID."""
        gl = self._gl
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
            log_len = ctypes.c_int()
            gl.glGetShaderiv(shader, GL_INFO_LOG_LENGTH, ctypes.byref(log_len))
            if log_len.value > 0:
                buf = ctypes.create_string_buffer(log_len.value)
                gl.glGetShaderInfoLog(shader, log_len.value, None, buf)
                error_msg = buf.value.decode("utf-8", errors="replace")
            else:
                error_msg = "Unknown shader compilation error"
            gl.glDeleteShader(shader)
            raise RuntimeError(f"Shader compile failed: {error_msg}")
        return shader

    def _compile_shader_program(self, vert_src: str, frag_src: str) -> int:
        """Compile and link a vertex+fragment shader program."""
        gl = self._gl
        vert = self._compile_shader(vert_src, GL_VERTEX_SHADER)
        frag = self._compile_shader(frag_src, GL_FRAGMENT_SHADER)

        program = gl.glCreateProgram()
        gl.glAttachShader(program, vert)
        gl.glAttachShader(program, frag)
        gl.glLinkProgram(program)

        # Check link status
        status = ctypes.c_int()
        gl.glGetProgramiv(program, GL_LINK_STATUS, ctypes.byref(status))
        if status.value != GL_TRUE:
            log_len = ctypes.c_int()
            gl.glGetProgramiv(program, GL_INFO_LOG_LENGTH, ctypes.byref(log_len))
            if log_len.value > 0:
                buf = ctypes.create_string_buffer(log_len.value)
                gl.glGetProgramInfoLog(program, log_len.value, None, buf)
                error_msg = buf.value.decode("utf-8", errors="replace")
            else:
                error_msg = "Unknown link error"
            gl.glDeleteProgram(program)
            raise RuntimeError(f"Program link failed: {error_msg}")

        # Detach and delete shaders (linked into program now)
        gl.glDetachShader(program, vert)
        gl.glDetachShader(program, frag)
        gl.glDeleteShader(vert)
        gl.glDeleteShader(frag)
        return program

    def _compile_transform_feedback_program(
        self, vert_src: str, varyings: list[str]
    ) -> int:
        """Compile a vertex-only program with transform feedback varyings."""
        gl = self._gl
        vert = self._compile_shader(vert_src, GL_VERTEX_SHADER)

        program = gl.glCreateProgram()
        gl.glAttachShader(program, vert)

        # Set transform feedback varyings before linking
        c_varyings = (ctypes.c_char_p * len(varyings))(
            *(v.encode("utf-8") for v in varyings)
        )
        gl.glTransformFeedbackVaryings(
            program, len(varyings), c_varyings, GL_INTERLEAVED_ATTRIBS
        )
        gl.glLinkProgram(program)

        # Check link status
        status = ctypes.c_int()
        gl.glGetProgramiv(program, GL_LINK_STATUS, ctypes.byref(status))
        if status.value != GL_TRUE:
            log_len = ctypes.c_int()
            gl.glGetProgramiv(program, GL_INFO_LOG_LENGTH, ctypes.byref(log_len))
            if log_len.value > 0:
                buf = ctypes.create_string_buffer(log_len.value)
                gl.glGetProgramInfoLog(program, log_len.value, None, buf)
                error_msg = buf.value.decode("utf-8", errors="replace")
            else:
                error_msg = "Unknown link error"
            gl.glDeleteProgram(program)
            raise RuntimeError(f"TF program link failed: {error_msg}")

        gl.glDetachShader(program, vert)
        gl.glDeleteShader(vert)
        return program

    # ------------------------------------------------------------------
    # Internal: buffer allocation
    # ------------------------------------------------------------------

    def _allocate_particle_buffers(self) -> None:
        """Allocate ping-pong VAO/VBO pairs for particle data."""
        gl = self._gl
        particle_count = min(self._particle_count, self.MAX_PARTICLES)
        buffer_size = particle_count * PARTICLE_STRIDE

        for i in range(2):
            # Create VAO
            vao = ctypes.c_uint()
            gl.glGenVertexArrays(1, ctypes.byref(vao))
            self._vao[i] = vao.value

            # Create VBO
            vbo = ctypes.c_uint()
            gl.glGenBuffers(1, ctypes.byref(vbo))
            self._vbo[i] = vbo.value

            # Allocate buffer with zeroed data (all particles dead initially)
            gl.glBindVertexArray(vao)
            gl.glBindBuffer(GL_ARRAY_BUFFER, vbo)
            gl.glBufferData(
                GL_ARRAY_BUFFER,
                buffer_size,
                None,  # Zero-initialized
                GL_DYNAMIC_COPY,
            )

            # Set up vertex attribute pointers
            # location 0: in_position (vec3, offset 0)
            gl.glEnableVertexAttribArray(0)
            gl.glVertexAttribPointer(
                0, 3, GL_FLOAT, GL_FALSE, PARTICLE_STRIDE,
                ctypes.c_void_p(POSITION_OFFSET),
            )
            # location 1: in_velocity (vec3, offset 12)
            gl.glEnableVertexAttribArray(1)
            gl.glVertexAttribPointer(
                1, 3, GL_FLOAT, GL_FALSE, PARTICLE_STRIDE,
                ctypes.c_void_p(VELOCITY_OFFSET),
            )
            # location 2: in_lifetime (float, offset 24)
            gl.glEnableVertexAttribArray(2)
            gl.glVertexAttribPointer(
                2, 1, GL_FLOAT, GL_FALSE, PARTICLE_STRIDE,
                ctypes.c_void_p(LIFETIME_OFFSET),
            )
            # location 3: in_color (vec4, offset 28)
            gl.glEnableVertexAttribArray(3)
            gl.glVertexAttribPointer(
                3, 4, GL_FLOAT, GL_FALSE, PARTICLE_STRIDE,
                ctypes.c_void_p(COLOR_OFFSET),
            )

        # Unbind
        gl.glBindVertexArray(0)
        gl.glBindBuffer(GL_ARRAY_BUFFER, 0)

    # ------------------------------------------------------------------
    # Internal: physics pass (transform feedback)
    # ------------------------------------------------------------------

    def _physics_pass(
        self,
        dt: float,
        elapsed: float,
        features: AudioFeatures | None,
        emit_count: int,
    ) -> None:
        """Run transform feedback to simulate particle physics on the GPU."""
        gl = self._gl
        src = self._current_buffer
        dst = 1 - src
        particle_count = min(self._particle_count, self.MAX_PARTICLES)

        gl.glUseProgram(self._transform_program)

        # Set uniforms
        self._set_uniform_float("u_dt", dt, self._transform_program)
        self._set_uniform_float("u_gravity", self._gravity, self._transform_program)
        self._set_uniform_float("u_drag", 0.3, self._transform_program)
        self._set_uniform_float("u_beat", self._beat_pulse, self._transform_program)
        bpm = features.bpm if features else 120.0
        self._set_uniform_float("u_bpm", bpm, self._transform_program)
        self._set_uniform_float("u_time", elapsed, self._transform_program)
        self._set_uniform_int("u_emit_count", emit_count, self._transform_program)
        self._set_uniform_float(
            "u_emit_speed", 3.0 + self._beat_pulse * 4.0, self._transform_program
        )

        # Set band energy uniform array
        if features:
            for i, energy in enumerate(features.band_energy[:7]):
                self._set_uniform_float(
                    f"u_band_energy[{i}]", energy, self._transform_program
                )

        # Bind source VAO (read from)
        gl.glBindVertexArray(self._vao[src])

        # Bind destination VBO as transform feedback buffer (write to)
        gl.glBindBufferBase(GL_TRANSFORM_FEEDBACK_BUFFER, 0, self._vbo[dst])

        # Disable rasterization (physics-only pass)
        gl.glEnable(GL_RASTERIZER_DISCARD)

        # Begin transform feedback
        gl.glBeginTransformFeedback(GL_POINTS)
        gl.glDrawArrays(GL_POINTS, 0, particle_count)
        gl.glEndTransformFeedback()

        # Re-enable rasterization
        gl.glDisable(GL_RASTERIZER_DISCARD)

        # Unbind
        gl.glBindBufferBase(GL_TRANSFORM_FEEDBACK_BUFFER, 0, 0)
        gl.glBindVertexArray(0)

    # ------------------------------------------------------------------
    # Internal: render pass
    # ------------------------------------------------------------------

    def _render_pass(self, elapsed: float) -> None:
        """Render particles as additive-blended point sprites."""
        gl = self._gl
        dst = self._current_buffer  # After swap, this is the updated buffer
        particle_count = min(self._particle_count, self.MAX_PARTICLES)

        # Clear framebuffer (black background)
        gl.glClearColor(
            ctypes.c_float(0.0),
            ctypes.c_float(0.0),
            ctypes.c_float(0.0),
            ctypes.c_float(1.0),
        )
        gl.glClear(GL_COLOR_BUFFER_BIT | GL_DEPTH_BUFFER_BIT)

        # Enable additive blending
        gl.glEnable(GL_BLEND)
        gl.glBlendFunc(GL_SRC_ALPHA, GL_ONE)

        # Enable point sprites
        gl.glEnable(GL_PROGRAM_POINT_SIZE)

        gl.glUseProgram(self._render_program)

        # Set render uniforms
        # Simple orthographic projection: map [-5, 5] to [-1, 1]
        # For simplicity, pass an identity-like projection
        self._set_uniform_mat4("u_projection", self._ortho_matrix())
        self._set_uniform_float("u_point_size_base", 16.0, self._render_program)
        self._set_uniform_float(
            "u_trail_length", self._trail_length, self._render_program
        )

        # Bind destination VAO (the one just updated by TF)
        # After swap: current_buffer points to the updated data
        gl.glBindVertexArray(self._vao[dst])
        gl.glDrawArrays(GL_POINTS, 0, particle_count)
        gl.glBindVertexArray(0)

        # Cleanup
        gl.glDisable(GL_BLEND)
        gl.glDisable(GL_PROGRAM_POINT_SIZE)

    # ------------------------------------------------------------------
    # Internal: emission logic
    # ------------------------------------------------------------------

    def _compute_emission_count(
        self, features: AudioFeatures | None, dt: float
    ) -> int:
        """Compute how many particles to emit this frame.

        Always emits a baseline stream so the visualization is never fully
        black — even before audio features arrive or between beats.
        """
        emit = 0
        particle_count = min(self._particle_count, self.MAX_PARTICLES)

        # Baseline emission: always emit a trickle so the viz is never blank.
        # ~50 particles/sec regardless of audio state.
        baseline_rate = 50.0
        emit += int(baseline_rate * dt)

        if features is None:
            return min(emit, particle_count)

        # Beat burst emission
        if self._emission_style in ("burst", "both"):
            if features.beat:
                # Burst: emit 5-20% of capacity proportional to intensity
                intensity = sum(features.band_energy) / 7.0
                burst = int(particle_count * 0.05 * (1.0 + intensity * 3.0))
                emit += burst

        # Continuous emission driven by band energy
        if self._emission_style in ("continuous", "both"):
            avg_energy = sum(features.band_energy) / 7.0
            # Base rate: ~100 particles/sec scaled by energy
            rate = 100.0 * (0.2 + avg_energy * 2.0)
            emit += int(rate * dt)
        elif self._emission_style == "burst":
            # Even in burst mode, add energy-proportional emission so the
            # viz isn't dead between beats. Lower rate than "continuous".
            avg_energy = sum(features.band_energy) / 7.0
            rate = 40.0 * (0.1 + avg_energy * 1.5)
            emit += int(rate * dt)

        # Cap to particle count
        return min(emit, particle_count)

    # ------------------------------------------------------------------
    # Internal: uniform setters
    # ------------------------------------------------------------------

    def _set_uniform_float(self, name: str, value: float, program: int) -> None:
        """Set a float uniform on the current program."""
        gl = self._gl
        loc = gl.glGetUniformLocation(program, name.encode("utf-8"))
        if loc >= 0:
            gl.glUniform1f(loc, ctypes.c_float(value))

    def _set_uniform_int(self, name: str, value: int, program: int) -> None:
        """Set an int uniform on the current program."""
        gl = self._gl
        loc = gl.glGetUniformLocation(program, name.encode("utf-8"))
        if loc >= 0:
            gl.glUniform1i(loc, value)

    def _set_uniform_mat4(self, name: str, matrix: list[float]) -> None:
        """Set a 4x4 matrix uniform on the current render program."""
        gl = self._gl
        loc = gl.glGetUniformLocation(
            self._render_program, name.encode("utf-8")
        )
        if loc >= 0:
            mat = (ctypes.c_float * 16)(*matrix)
            gl.glUniformMatrix4fv(loc, 1, GL_FALSE, mat)

    # ------------------------------------------------------------------
    # Internal: projection matrix
    # ------------------------------------------------------------------

    def _ortho_matrix(self) -> list[float]:
        """Return a simple orthographic projection matrix.

        Maps world coordinates [-5, 5] in X/Y to clip space [-1, 1].
        Z range [-5, 5].
        """
        left, right = -5.0, 5.0
        bottom, top_ = -5.0, 5.0
        near, far = -5.0, 5.0

        # Column-major 4x4 orthographic matrix
        sx = 2.0 / (right - left)
        sy = 2.0 / (top_ - bottom)
        sz = -2.0 / (far - near)
        tx = -(right + left) / (right - left)
        ty = -(top_ + bottom) / (top_ - bottom)
        tz = -(far + near) / (far - near)

        return [
            sx,  0.0, 0.0, 0.0,
            0.0, sy,  0.0, 0.0,
            0.0, 0.0, sz,  0.0,
            tx,  ty,  tz,  1.0,
        ]

    # ------------------------------------------------------------------
    # Internal: resource cleanup
    # ------------------------------------------------------------------

    def _release_gl_resources(self) -> None:
        """Delete all GPU particle buffers and shader programs."""
        gl = self._gl
        if gl is None:
            return

        # Delete VBOs
        for i in range(2):
            if self._vbo[i]:
                vbo_id = ctypes.c_uint(self._vbo[i])
                gl.glDeleteBuffers(1, ctypes.byref(vbo_id))
                self._vbo[i] = 0

        # Delete VAOs
        for i in range(2):
            if self._vao[i]:
                vao_id = ctypes.c_uint(self._vao[i])
                gl.glDeleteVertexArrays(1, ctypes.byref(vao_id))
                self._vao[i] = 0

        # Delete shader programs
        if self._transform_program:
            gl.glDeleteProgram(self._transform_program)
            self._transform_program = 0

        if self._render_program:
            gl.glDeleteProgram(self._render_program)
            self._render_program = 0

        self._gl = None

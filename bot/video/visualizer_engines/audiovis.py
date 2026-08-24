"""AudioVis GPU-accelerated spectrum/waveform/waterfall visualizer engine.

Renders audio-reactive spectrum bars, waveform, waterfall, or circular
visualizations using GLSL fragment shaders on the EGL headless context.
FFT data is uploaded as a 1D texture each frame.

Requirements: Req 6 (AC 1-5)
"""

from __future__ import annotations

import ctypes
import logging
import time
from pathlib import Path

from video.visualizer_engines.base import AudioFeatures, TrackMetadata
from video.visualizer_engines.gpu_engine_base import GPUEngineBase

log = logging.getLogger(__name__)

# Shader directory
SHADER_DIR = Path(__file__).parent / "shaders"

# GL constants (OpenGL 3.3 Core)
GL_VERTEX_SHADER = 0x8B31
GL_FRAGMENT_SHADER = 0x8B30
GL_COMPILE_STATUS = 0x8B81
GL_LINK_STATUS = 0x8B82
GL_TRUE = 1
GL_FALSE = 0
GL_TEXTURE_1D = 0x0DE0
GL_TEXTURE0 = 0x84C0
GL_TEXTURE_MIN_FILTER = 0x2801
GL_TEXTURE_MAG_FILTER = 0x2800
GL_LINEAR = 0x2601
GL_CLAMP_TO_EDGE = 0x812F
GL_TEXTURE_WRAP_S = 0x2802
GL_RED = 0x1903
GL_R32F = 0x822E
GL_FLOAT = 0x1406
GL_TRIANGLE_STRIP = 0x0005
GL_COLOR_BUFFER_BIT = 0x00004000
GL_ARRAY_BUFFER = 0x8892
GL_STATIC_DRAW = 0x88E4

# Beat pulse decay: 200ms at 30fps → decay rate per frame = 1/30 / 0.2 = 1/6
BEAT_DECAY_PER_FRAME = (1.0 / 30.0) / 0.2  # ~0.1667 per frame

# Valid visualization styles
STYLES = ("bars", "waveform", "waterfall", "circular")

# Default configuration
DEFAULT_STYLE = "bars"
DEFAULT_COLOR_SCHEME = "neon"
DEFAULT_FFT_BINS = 64
DEFAULT_GLOW_INTENSITY = 0.5
DEFAULT_BG_OPACITY = 0.9


class AudioVisEngine(GPUEngineBase):
    """GPU-accelerated spectrum/waveform/waterfall/circular visualizer.

    Renders audio-reactive visualizations using GLSL fragment shaders.
    FFT data is uploaded as a 1D texture each frame. Supports multiple
    visualization styles selectable via configuration.

    Configuration:
        style: "bars" | "waveform" | "waterfall" | "circular"
        color_scheme: str (currently affects shader color palette)
        fft_bins: int (display bins: 7, 32, 64, 128, or 512)
        glow_intensity: float (0.0 - 2.0)
        background_opacity: float (0.0 - 1.0)
    """

    def __init__(
        self,
        style: str = DEFAULT_STYLE,
        color_scheme: str = DEFAULT_COLOR_SCHEME,
        fft_bins: int = DEFAULT_FFT_BINS,
        glow_intensity: float = DEFAULT_GLOW_INTENSITY,
        background_opacity: float = DEFAULT_BG_OPACITY,
    ) -> None:
        super().__init__()
        self._style: str = style if style in STYLES else DEFAULT_STYLE
        self._color_scheme: str = color_scheme
        self._fft_bins: int = fft_bins
        self._glow_intensity: float = glow_intensity
        self._bg_opacity: float = background_opacity

        # GL state (populated in _on_gl_ready)
        self._shader_program: int = 0
        self._vao: int = 0
        self._fft_texture: int = 0
        self._start_time: float = 0.0
        self._beat_pulse: float = 0.0

        # Track metadata for text overlay
        self._track_title: str = ""
        self._track_artist: str = ""

        # Uniform locations (cached after linking)
        self._u_time: int = -1
        self._u_resolution: int = -1
        self._u_beat: int = -1
        self._u_bpm: int = -1
        self._u_band_energy: int = -1
        self._u_fft: int = -1
        self._u_fft_bins: int = -1
        self._u_glow_intensity: int = -1
        self._u_bg_opacity: int = -1

    # ------------------------------------------------------------------
    # Properties (config access)
    # ------------------------------------------------------------------

    @property
    def style(self) -> str:
        return self._style

    @property
    def color_scheme(self) -> str:
        return self._color_scheme

    @property
    def fft_bins(self) -> int:
        return self._fft_bins

    @property
    def glow_intensity(self) -> float:
        return self._glow_intensity

    @property
    def background_opacity(self) -> float:
        return self._bg_opacity

    # ------------------------------------------------------------------
    # Lifecycle hooks
    # ------------------------------------------------------------------

    async def _on_gl_ready(self, metadata: TrackMetadata | None) -> None:
        """Compile shaders and set up GL state for rendering."""
        self._start_time = time.monotonic()
        self._beat_pulse = 0.0

        if metadata:
            self._track_title = metadata.title
            self._track_artist = metadata.artist

        gl = self._egl_ctx._gl

        # Load and compile shaders
        vert_src = self._load_shader_source("audiovis_vert.glsl")
        frag_file = f"audiovis_{self._style}.glsl"
        frag_src = self._load_shader_source(frag_file)

        self._shader_program = self._compile_program(gl, vert_src, frag_src)

        # Cache uniform locations
        self._cache_uniform_locations(gl)

        # Create VAO for fullscreen quad (attribute-less rendering)
        self._vao = self._create_vao(gl)

        # Create 1D texture for FFT data
        self._fft_texture = self._create_fft_texture(gl)

    async def on_track_change(self, metadata: TrackMetadata) -> None:
        """Update track title/artist for text overlay."""
        self._track_title = metadata.title
        self._track_artist = metadata.artist

    async def stop(self) -> None:
        """Clean up GL resources before destroying context."""
        if self._egl_ctx and self._egl_ctx.is_valid:
            gl = self._egl_ctx._gl
            if self._shader_program:
                gl.glDeleteProgram(self._shader_program)
                self._shader_program = 0
            if self._fft_texture:
                tex_id = ctypes.c_uint(self._fft_texture)
                gl.glDeleteTextures(1, ctypes.byref(tex_id))
                self._fft_texture = 0
            if self._vao:
                vao_id = ctypes.c_uint(self._vao)
                gl.glDeleteVertexArrays(1, ctypes.byref(vao_id))
                self._vao = 0
        await super().stop()

    # ------------------------------------------------------------------
    # Render
    # ------------------------------------------------------------------

    def _render_gl_frame(self, features: AudioFeatures | None) -> None:
        """Render one frame: upload FFT, set uniforms, draw fullscreen quad."""
        gl = self._egl_ctx._gl

        # Update beat pulse with 200ms decay (Req 6 AC 4)
        if features and features.beat:
            self._beat_pulse = 1.0
        else:
            self._beat_pulse = max(0.0, self._beat_pulse - BEAT_DECAY_PER_FRAME)

        # Clear framebuffer
        gl.glClear(GL_COLOR_BUFFER_BIT)

        # Use shader program
        gl.glUseProgram(self._shader_program)

        # Upload FFT data as 1D texture
        if features:
            self._upload_fft_data(gl, features.fft)

        # Set uniforms
        elapsed = time.monotonic() - self._start_time
        gl.glUniform1f(self._u_time, ctypes.c_float(elapsed))
        gl.glUniform2f(
            self._u_resolution,
            ctypes.c_float(1280.0),
            ctypes.c_float(720.0),
        )
        gl.glUniform1f(self._u_beat, ctypes.c_float(self._beat_pulse))
        gl.glUniform1f(
            self._u_bpm,
            ctypes.c_float(features.bpm if features else 120.0),
        )

        # Band energy (7 floats)
        if features and features.band_energy:
            band_arr = (ctypes.c_float * 7)(*features.band_energy[:7])
            gl.glUniform1fv(self._u_band_energy, 7, band_arr)
        else:
            band_arr = (ctypes.c_float * 7)(*([0.0] * 7))
            gl.glUniform1fv(self._u_band_energy, 7, band_arr)

        # FFT texture bound to unit 0
        gl.glActiveTexture(GL_TEXTURE0)
        gl.glBindTexture(GL_TEXTURE_1D, self._fft_texture)
        gl.glUniform1i(self._u_fft, 0)

        gl.glUniform1i(self._u_fft_bins, self._fft_bins)
        gl.glUniform1f(self._u_glow_intensity, ctypes.c_float(self._glow_intensity))
        gl.glUniform1f(self._u_bg_opacity, ctypes.c_float(self._bg_opacity))

        # Draw fullscreen quad (4 vertices, triangle strip)
        gl.glBindVertexArray(self._vao)
        gl.glDrawArrays(GL_TRIANGLE_STRIP, 0, 4)
        gl.glBindVertexArray(0)

        # TODO: Render track title/artist as text overlay (bitmap font)
        # self._render_text_overlay(gl)

    # ------------------------------------------------------------------
    # Shader compilation helpers
    # ------------------------------------------------------------------

    def _load_shader_source(self, filename: str) -> str:
        """Load GLSL shader source from the shaders/ directory."""
        path = SHADER_DIR / filename
        if not path.exists():
            raise FileNotFoundError(f"Shader file not found: {path}")
        return path.read_text()

    def _compile_program(self, gl, vert_src: str, frag_src: str) -> int:
        """Compile and link a vertex + fragment shader program."""
        vert = self._compile_shader(gl, GL_VERTEX_SHADER, vert_src)
        frag = self._compile_shader(gl, GL_FRAGMENT_SHADER, frag_src)

        program = gl.glCreateProgram()
        gl.glAttachShader(program, vert)
        gl.glAttachShader(program, frag)
        gl.glLinkProgram(program)

        # Check link status
        status = ctypes.c_int()
        gl.glGetProgramiv(program, GL_LINK_STATUS, ctypes.byref(status))
        if status.value != GL_TRUE:
            log_len = ctypes.c_int()
            gl.glGetProgramiv(program, 0x8B84, ctypes.byref(log_len))  # GL_INFO_LOG_LENGTH
            if log_len.value > 0:
                log_buf = ctypes.create_string_buffer(log_len.value)
                gl.glGetProgramInfoLog(program, log_len.value, None, log_buf)
                log.error("Shader link error: %s", log_buf.value.decode())
            gl.glDeleteProgram(program)
            raise RuntimeError(f"Shader program link failed for style '{self._style}'")

        # Detach and delete individual shaders (they're linked into the program)
        gl.glDetachShader(program, vert)
        gl.glDetachShader(program, frag)
        gl.glDeleteShader(vert)
        gl.glDeleteShader(frag)

        return program

    def _compile_shader(self, gl, shader_type: int, source: str) -> int:
        """Compile a single shader stage."""
        shader = gl.glCreateShader(shader_type)
        src_bytes = source.encode("utf-8")
        src_ptr = ctypes.c_char_p(src_bytes)
        src_len = ctypes.c_int(len(src_bytes))
        gl.glShaderSource(shader, 1, ctypes.byref(src_ptr), ctypes.byref(src_len))
        gl.glCompileShader(shader)

        # Check compile status
        status = ctypes.c_int()
        gl.glGetShaderiv(shader, GL_COMPILE_STATUS, ctypes.byref(status))
        if status.value != GL_TRUE:
            log_len = ctypes.c_int()
            gl.glGetShaderiv(shader, 0x8B84, ctypes.byref(log_len))  # GL_INFO_LOG_LENGTH
            if log_len.value > 0:
                log_buf = ctypes.create_string_buffer(log_len.value)
                gl.glGetShaderInfoLog(shader, log_len.value, None, log_buf)
                log.error("Shader compile error: %s", log_buf.value.decode())
            gl.glDeleteShader(shader)
            type_name = "vertex" if shader_type == GL_VERTEX_SHADER else "fragment"
            raise RuntimeError(f"{type_name} shader compilation failed")

        return shader

    # ------------------------------------------------------------------
    # GL resource creation helpers
    # ------------------------------------------------------------------

    def _cache_uniform_locations(self, gl) -> None:
        """Look up and cache all uniform locations."""
        def loc(name: str) -> int:
            return gl.glGetUniformLocation(self._shader_program, name.encode())

        self._u_time = loc("iTime")
        self._u_resolution = loc("iResolution")
        self._u_beat = loc("iBeat")
        self._u_bpm = loc("iBPM")
        self._u_band_energy = loc("iBandEnergy")
        self._u_fft = loc("iFFT")
        self._u_fft_bins = loc("iFFTBins")
        self._u_glow_intensity = loc("iGlowIntensity")
        self._u_bg_opacity = loc("iBgOpacity")

    def _create_vao(self, gl) -> int:
        """Create a VAO with a dummy vertex buffer for fullscreen quad rendering.

        Mesa iris (Intel Meteor Lake) requires at least one enabled vertex
        attribute for glDrawArrays to produce primitives, even when the shader
        only uses gl_VertexID. Bind a simple 4-vertex position buffer.
        """
        vao = ctypes.c_uint()
        gl.glGenVertexArrays(1, ctypes.byref(vao))
        gl.glBindVertexArray(vao)

        # Create VBO with fullscreen quad positions (matches audiovis_vert.glsl)
        positions = (ctypes.c_float * 8)(
            -1.0, -1.0,  # BL
             1.0, -1.0,  # BR
            -1.0,  1.0,  # TL
             1.0,  1.0,  # TR
        )
        vbo = ctypes.c_uint()
        gl.glGenBuffers(1, ctypes.byref(vbo))
        gl.glBindBuffer(GL_ARRAY_BUFFER, vbo)
        gl.glBufferData(GL_ARRAY_BUFFER, ctypes.sizeof(positions), positions, GL_STATIC_DRAW)

        # Enable attribute 0 (position) — even though the shader ignores it,
        # having an enabled attribute makes Mesa iris emit primitives.
        gl.glEnableVertexAttribArray(0)
        gl.glVertexAttribPointer = gl.glVertexAttribPointer
        gl.glVertexAttribPointer.argtypes = [
            ctypes.c_uint, ctypes.c_int, ctypes.c_uint,
            ctypes.c_ubyte, ctypes.c_int, ctypes.c_void_p,
        ]
        gl.glVertexAttribPointer(0, 2, GL_FLOAT, 0, 0, None)

        gl.glBindVertexArray(0)
        return vao.value

    def _create_fft_texture(self, gl) -> int:
        """Create a 1D texture for FFT data upload."""
        tex = ctypes.c_uint()
        gl.glGenTextures(1, ctypes.byref(tex))
        gl.glBindTexture(GL_TEXTURE_1D, tex)
        gl.glTexParameteri(GL_TEXTURE_1D, GL_TEXTURE_MIN_FILTER, GL_LINEAR)
        gl.glTexParameteri(GL_TEXTURE_1D, GL_TEXTURE_MAG_FILTER, GL_LINEAR)
        gl.glTexParameteri(GL_TEXTURE_1D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE)

        # Initialize with zeros (512 bins)
        zeros = (ctypes.c_float * 512)(*([0.0] * 512))
        gl.glTexImage1D(GL_TEXTURE_1D, 0, GL_R32F, 512, 0, GL_RED, GL_FLOAT, zeros)

        return tex.value

    def _upload_fft_data(self, gl, fft: list[float]) -> None:
        """Upload current FFT data to the 1D texture."""
        n = min(len(fft), 512)
        data = (ctypes.c_float * 512)(*fft[:n])
        gl.glBindTexture(GL_TEXTURE_1D, self._fft_texture)
        gl.glTexSubImage1D(GL_TEXTURE_1D, 0, 0, 512, GL_RED, GL_FLOAT, data)

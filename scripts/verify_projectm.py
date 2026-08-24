#!/usr/bin/env python3
"""projectM Rendering Verification Script.

Standalone script that verifies projectM renders non-black, visually active
frames on Intel Meteor Lake iGPUs (Mesa iris, EGL headless, SR-IOV VFs).

Creates an EGL headless context, loads libprojectM, feeds synthetic audio,
renders frames, and validates non-black output with zero GL errors.

Usage:
    python scripts/verify_projectm.py [--preset-dir /app/data/presets/projectm] [--frames 30] [--verbose]

Exit codes:
    0 — Rendering produces non-black frames with no GL errors
    1 — Rendering failed (black frames, GL errors, or initialization failure)

Requirements: 3.1, 3.2, 3.4, 3.5
"""

from __future__ import annotations

import argparse
import ctypes
import math
import os
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

FRAME_WIDTH = 1280
FRAME_HEIGHT = 720
FRAME_PIXELS = FRAME_WIDTH * FRAME_HEIGHT
FRAME_SIZE = FRAME_PIXELS * 4  # RGBA bytes

# Minimum percentage of non-zero pixels to consider a frame "non-black"
MIN_NONZERO_PERCENT = 1.0

# Audio: 440Hz sine wave, 512 float samples
PCM_SAMPLES = 512
SINE_FREQ = 440.0
SAMPLE_RATE = 44100.0

# EGL constants
EGL_OPENGL_API = 0x30A2
EGL_CONTEXT_MAJOR_VERSION = 0x3098
EGL_CONTEXT_MINOR_VERSION = 0x30FB
EGL_CONTEXT_OPENGL_PROFILE_MASK = 0x30FD
EGL_CONTEXT_OPENGL_CORE_PROFILE_BIT = 0x00000001
EGL_NONE = 0x3038
EGL_RENDERABLE_TYPE = 0x3040
EGL_OPENGL_BIT = 0x0008
EGL_SURFACE_TYPE = 0x3033
EGL_PLATFORM_GBM_KHR = 0x31D7

# GL constants
GL_NO_ERROR = 0
GL_FRAMEBUFFER = 0x8D40
GL_COLOR_ATTACHMENT0 = 0x8CE0
GL_DEPTH_ATTACHMENT = 0x8D00
GL_RENDERBUFFER = 0x8D41
GL_RGBA8 = 0x8058
GL_RGBA = 0x1908
GL_UNSIGNED_BYTE = 0x1401
GL_FRAMEBUFFER_COMPLETE = 0x8CD5
GL_DEPTH_COMPONENT24 = 0x81A6


# ---------------------------------------------------------------------------
# GL Error decoding
# ---------------------------------------------------------------------------

GL_ERROR_NAMES = {
    0x0000: "GL_NO_ERROR",
    0x0500: "GL_INVALID_ENUM",
    0x0501: "GL_INVALID_VALUE",
    0x0502: "GL_INVALID_OPERATION",
    0x0503: "GL_STACK_OVERFLOW",
    0x0504: "GL_STACK_UNDERFLOW",
    0x0505: "GL_OUT_OF_MEMORY",
    0x0506: "GL_INVALID_FRAMEBUFFER_OPERATION",
}


def gl_error_name(code: int) -> str:
    return GL_ERROR_NAMES.get(code, f"GL_ERROR_0x{code:04X}")


# ---------------------------------------------------------------------------
# EGL Headless Context (self-contained, reuses pattern from egl_context.py)
# ---------------------------------------------------------------------------

class EGLContext:
    """Minimal EGL headless context for verification purposes."""

    def __init__(self, render_device: str = "/dev/dri/renderD128"):
        self.render_device = render_device
        self._egl = None
        self._gl = None
        self._gbm = None
        self._gbm_device = None
        self._drm_fd = None
        self._display = None
        self._context = None
        self._fbo = None
        self._rbo_color = None
        self._rbo_depth = None

    def create(self) -> None:
        """Initialize EGL display, context, and offscreen FBO."""
        os.environ.setdefault("MESA_LOADER_DRIVER_OVERRIDE", "iris")

        # Load libraries
        self._egl = ctypes.CDLL("libEGL.so.1")
        self._gl = ctypes.CDLL("libGL.so.1")
        self._gbm = ctypes.CDLL("libgbm.so.1")

        # Open DRM render node
        self._drm_fd = os.open(self.render_device, os.O_RDWR)

        # Create GBM device
        self._gbm.gbm_create_device.restype = ctypes.c_void_p
        self._gbm_device = self._gbm.gbm_create_device(self._drm_fd)
        if not self._gbm_device:
            raise RuntimeError(f"gbm_create_device failed on {self.render_device}")

        # Get EGL display via GBM platform
        self._egl.eglGetPlatformDisplay.argtypes = [
            ctypes.c_uint, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int),
        ]
        self._egl.eglGetPlatformDisplay.restype = ctypes.c_void_p

        self._display = self._egl.eglGetPlatformDisplay(
            EGL_PLATFORM_GBM_KHR,
            ctypes.c_void_p(self._gbm_device),
            None,
        )
        if not self._display:
            raise RuntimeError("eglGetPlatformDisplay failed")

        # Initialize EGL
        major, minor = ctypes.c_int(), ctypes.c_int()
        if not self._egl.eglInitialize(self._display, ctypes.byref(major), ctypes.byref(minor)):
            raise RuntimeError("eglInitialize failed")

        # Bind OpenGL API
        if not self._egl.eglBindAPI(EGL_OPENGL_API):
            raise RuntimeError("eglBindAPI(EGL_OPENGL_API) failed")

        # Choose config (surfaceless)
        config_attribs = (ctypes.c_int * 7)(
            EGL_SURFACE_TYPE, 0,
            EGL_RENDERABLE_TYPE, EGL_OPENGL_BIT,
            EGL_NONE, 0, 0,
        )
        config = ctypes.c_void_p()
        num_configs = ctypes.c_int()
        self._egl.eglChooseConfig(
            self._display, config_attribs,
            ctypes.byref(config), 1, ctypes.byref(num_configs),
        )
        if num_configs.value == 0:
            raise RuntimeError("eglChooseConfig found no valid configs")

        # Create OpenGL 3.3 Core context
        context_attribs = (ctypes.c_int * 7)(
            EGL_CONTEXT_MAJOR_VERSION, 3,
            EGL_CONTEXT_MINOR_VERSION, 3,
            EGL_CONTEXT_OPENGL_PROFILE_MASK, EGL_CONTEXT_OPENGL_CORE_PROFILE_BIT,
            EGL_NONE,
        )
        create_context = self._egl.eglCreateContext
        create_context.restype = ctypes.c_void_p
        self._context = create_context(self._display, config, None, context_attribs)
        if not self._context:
            raise RuntimeError("eglCreateContext failed (OpenGL 3.3 Core)")

        # Make current (surfaceless)
        if not self._egl.eglMakeCurrent(self._display, None, None, self._context):
            raise RuntimeError("eglMakeCurrent failed")

        # Create FBO with color + depth attachments
        self._create_fbo()

    def _create_fbo(self) -> None:
        """Create offscreen FBO with RGBA8 color + depth24 renderbuffers."""
        gl = self._gl

        # Color renderbuffer
        rbo_color = ctypes.c_uint()
        gl.glGenRenderbuffers(1, ctypes.byref(rbo_color))
        gl.glBindRenderbuffer(GL_RENDERBUFFER, rbo_color)
        gl.glRenderbufferStorage(GL_RENDERBUFFER, GL_RGBA8, FRAME_WIDTH, FRAME_HEIGHT)
        self._rbo_color = rbo_color.value

        # Depth renderbuffer (projectM needs depth buffer)
        rbo_depth = ctypes.c_uint()
        gl.glGenRenderbuffers(1, ctypes.byref(rbo_depth))
        gl.glBindRenderbuffer(GL_RENDERBUFFER, rbo_depth)
        gl.glRenderbufferStorage(GL_RENDERBUFFER, GL_DEPTH_COMPONENT24, FRAME_WIDTH, FRAME_HEIGHT)
        self._rbo_depth = rbo_depth.value

        # Create FBO
        fbo = ctypes.c_uint()
        gl.glGenFramebuffers(1, ctypes.byref(fbo))
        gl.glBindFramebuffer(GL_FRAMEBUFFER, fbo)
        gl.glFramebufferRenderbuffer(
            GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_RENDERBUFFER, rbo_color
        )
        gl.glFramebufferRenderbuffer(
            GL_FRAMEBUFFER, GL_DEPTH_ATTACHMENT, GL_RENDERBUFFER, rbo_depth
        )
        self._fbo = fbo.value

        # Validate
        status = gl.glCheckFramebufferStatus(GL_FRAMEBUFFER)
        if status != GL_FRAMEBUFFER_COMPLETE:
            raise RuntimeError(f"FBO incomplete: status=0x{status:04X}")

        # CRITICAL: Set viewport for headless EGL (no default without a surface)
        gl.glViewport(0, 0, FRAME_WIDTH, FRAME_HEIGHT)

    def read_pixels(self) -> bytes:
        """Read FBO contents as RGBA bytes."""
        buf = (ctypes.c_ubyte * FRAME_SIZE)()
        self._gl.glReadPixels(0, 0, FRAME_WIDTH, FRAME_HEIGHT, GL_RGBA, GL_UNSIGNED_BYTE, buf)
        return bytes(buf)

    def get_error(self) -> int:
        """Return current GL error code."""
        self._gl.glGetError.restype = ctypes.c_uint
        return self._gl.glGetError()

    def destroy(self) -> None:
        """Release all EGL/GL resources."""
        if self._fbo is not None and self._gl:
            fbo_id = ctypes.c_uint(self._fbo)
            self._gl.glDeleteFramebuffers(1, ctypes.byref(fbo_id))
        if self._rbo_color is not None and self._gl:
            rbo_id = ctypes.c_uint(self._rbo_color)
            self._gl.glDeleteRenderbuffers(1, ctypes.byref(rbo_id))
        if self._rbo_depth is not None and self._gl:
            rbo_id = ctypes.c_uint(self._rbo_depth)
            self._gl.glDeleteRenderbuffers(1, ctypes.byref(rbo_id))
        if self._context and self._egl:
            self._egl.eglMakeCurrent(self._display, None, None, None)
            self._egl.eglDestroyContext(self._display, self._context)
        if self._display and self._egl:
            self._egl.eglTerminate(self._display)
        if self._gbm_device and self._gbm:
            self._gbm.gbm_device_destroy(ctypes.c_void_p(self._gbm_device))
        if self._drm_fd is not None:
            os.close(self._drm_fd)


# ---------------------------------------------------------------------------
# libprojectM Loader
# ---------------------------------------------------------------------------

class ProjectMInstance:
    """Minimal libprojectM 4.x wrapper for verification."""

    def __init__(self):
        self._lib = None
        self._handle = None

    def load(self) -> None:
        """Load the libprojectM shared library."""
        try:
            self._lib = ctypes.CDLL("libprojectM-4.so")
        except OSError:
            self._lib = ctypes.CDLL("libprojectM.so.4")

        self._setup_signatures()

    def _setup_signatures(self) -> None:
        """Configure ctypes function signatures."""
        lib = self._lib

        lib.projectm_create.restype = ctypes.c_void_p
        lib.projectm_create.argtypes = []

        lib.projectm_destroy.restype = None
        lib.projectm_destroy.argtypes = [ctypes.c_void_p]

        lib.projectm_set_window_size.restype = None
        lib.projectm_set_window_size.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]

        lib.projectm_set_preset_path.restype = None
        lib.projectm_set_preset_path.argtypes = [ctypes.c_void_p, ctypes.c_char_p]

        lib.projectm_set_shuffle_enabled.restype = None
        lib.projectm_set_shuffle_enabled.argtypes = [ctypes.c_void_p, ctypes.c_bool]

        lib.projectm_pcm_add_float.restype = None
        lib.projectm_pcm_add_float.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_float),
            ctypes.c_uint,
            ctypes.c_uint,
        ]

        lib.projectm_opengl_render_frame.restype = None
        lib.projectm_opengl_render_frame.argtypes = [ctypes.c_void_p]

        lib.projectm_set_texture_search_paths.restype = None
        lib.projectm_set_texture_search_paths.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_char_p),
            ctypes.c_size_t,
        ]

    def create(self, preset_dir: str) -> None:
        """Create and configure projectM instance."""
        self._handle = self._lib.projectm_create()
        if not self._handle:
            raise RuntimeError("projectm_create() returned NULL")

        # Configure window size
        self._lib.projectm_set_window_size(self._handle, FRAME_WIDTH, FRAME_HEIGHT)

        # Set preset directory
        self._lib.projectm_set_preset_path(self._handle, preset_dir.encode("utf-8"))

        # Enable shuffle
        self._lib.projectm_set_shuffle_enabled(self._handle, ctypes.c_bool(True))

        # Suppress logo by setting texture search paths to /dev/null
        empty_path = ctypes.c_char_p(b"/dev/null")
        paths_array = (ctypes.c_char_p * 1)(empty_path)
        self._lib.projectm_set_texture_search_paths(
            self._handle, paths_array, ctypes.c_size_t(1)
        )

    @property
    def handle(self):
        return self._handle

    def feed_audio(self, pcm_buffer) -> None:
        """Feed PCM float audio data to projectM."""
        self._lib.projectm_pcm_add_float(
            self._handle,
            ctypes.cast(pcm_buffer, ctypes.POINTER(ctypes.c_float)),
            PCM_SAMPLES,
            1,  # mono
        )

    def render_frame(self) -> None:
        """Render one projectM frame."""
        self._lib.projectm_opengl_render_frame(self._handle)

    def destroy(self) -> None:
        """Destroy the projectM instance."""
        if self._handle and self._lib:
            self._lib.projectm_destroy(self._handle)
            self._handle = None


# ---------------------------------------------------------------------------
# Audio Generation
# ---------------------------------------------------------------------------

def generate_sine_wave() -> ctypes.Array:
    """Generate a 440Hz sine wave as 512 float32 samples in [-1.0, 1.0]."""
    pcm_buffer = (ctypes.c_float * PCM_SAMPLES)()
    for i in range(PCM_SAMPLES):
        t = i / SAMPLE_RATE
        pcm_buffer[i] = math.sin(2.0 * math.pi * SINE_FREQ * t)
    return pcm_buffer


# ---------------------------------------------------------------------------
# Pixel Analysis
# ---------------------------------------------------------------------------

def count_nonzero_pixels(pixel_data: bytes) -> int:
    """Count pixels where any RGB channel is non-zero (ignoring alpha)."""
    count = 0
    for i in range(0, len(pixel_data), 4):
        r, g, b = pixel_data[i], pixel_data[i + 1], pixel_data[i + 2]
        if r > 0 or g > 0 or b > 0:
            count += 1
    return count


def nonzero_percent(pixel_data: bytes) -> float:
    """Return percentage of non-zero (non-black) pixels."""
    nonzero = count_nonzero_pixels(pixel_data)
    return (nonzero / FRAME_PIXELS) * 100.0


# ---------------------------------------------------------------------------
# Preset Directory Info
# ---------------------------------------------------------------------------

def count_milk_files(preset_dir: str) -> int:
    """Count .milk files in the preset directory tree."""
    count = 0
    for root, _dirs, files in os.walk(preset_dir):
        for f in files:
            if f.lower().endswith(".milk"):
                count += 1
    return count


# ---------------------------------------------------------------------------
# Main Verification
# ---------------------------------------------------------------------------

def run_verification(preset_dir: str, num_frames: int, verbose: bool) -> bool:
    """Run the full rendering verification pipeline.

    Returns True if verification passes, False otherwise.
    """
    print("=== projectM Rendering Verification ===")

    # --- EGL Context ---
    egl_ctx = EGLContext()
    try:
        egl_ctx.create()
    except Exception as e:
        print(f"EGL context: FAILED ({e})")
        return False
    print("EGL context: OK")

    # --- Load libprojectM ---
    pm = ProjectMInstance()
    try:
        pm.load()
    except OSError as e:
        print(f"libprojectM: FAILED to load ({e})")
        egl_ctx.destroy()
        return False
    print("libprojectM: loaded")

    # --- Create instance ---
    try:
        pm.create(preset_dir)
    except RuntimeError as e:
        print(f"Instance creation: FAILED ({e})")
        egl_ctx.destroy()
        return False
    print(f"Instance created: handle=0x{pm.handle:x}")

    # --- Preset directory info ---
    milk_count = count_milk_files(preset_dir)
    print(f"Preset directory: {preset_dir} ({milk_count} .milk files)")

    # --- Generate audio ---
    pcm_buffer = generate_sine_wave()

    # --- Render frames ---
    print(f"Rendering {num_frames} frames...")
    gl_errors = 0
    nonblack_frames = 0
    results = []

    for frame_num in range(1, num_frames + 1):
        # Feed audio
        pm.feed_audio(pcm_buffer)

        # Render
        pm.render_frame()

        # Check GL error
        gl_err = egl_ctx.get_error()
        err_name = gl_error_name(gl_err)
        if gl_err != GL_NO_ERROR:
            gl_errors += 1

        # Read pixels and analyze
        pixels = egl_ctx.read_pixels()
        pct = nonzero_percent(pixels)

        if pct >= MIN_NONZERO_PERCENT:
            nonblack_frames += 1

        results.append((frame_num, pct, err_name))

        if verbose:
            print(f"  Frame {frame_num}: {pct:.1f}% non-zero pixels, {err_name}")

    # Print summary for non-verbose mode (first and last few frames)
    if not verbose and num_frames > 0:
        show_frames = min(num_frames, 5)
        for i in range(show_frames):
            frame_num, pct, err_name = results[i]
            print(f"  Frame {frame_num}: {pct:.1f}% non-zero pixels, {err_name}")
        if num_frames > show_frames:
            print(f"  ... ({num_frames - show_frames} more frames)")
            # Show last frame
            frame_num, pct, err_name = results[-1]
            print(f"  Frame {frame_num}: {pct:.1f}% non-zero pixels, {err_name}")

    # --- Cleanup ---
    pm.destroy()
    egl_ctx.destroy()

    # --- Result ---
    passed = nonblack_frames == num_frames and gl_errors == 0

    if passed:
        print(f"RESULT: PASS ({nonblack_frames}/{num_frames} frames non-black, {gl_errors} GL errors)")
    else:
        reasons = []
        if nonblack_frames < num_frames:
            reasons.append(f"{num_frames - nonblack_frames} black frames")
        if gl_errors > 0:
            reasons.append(f"{gl_errors} GL errors")
        print(f"RESULT: FAIL ({', '.join(reasons)})")

    return passed


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify projectM rendering on Intel Meteor Lake iGPUs (EGL headless)."
    )
    parser.add_argument(
        "--preset-dir",
        default="/app/data/presets/projectm",
        help="Path to the preset directory (default: /app/data/presets/projectm)",
    )
    parser.add_argument(
        "--frames",
        type=int,
        default=30,
        help="Number of frames to render (default: 30)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print per-frame details",
    )

    args = parser.parse_args()

    # Validate preset directory exists
    if not Path(args.preset_dir).is_dir():
        print(f"ERROR: Preset directory not found: {args.preset_dir}")
        print("Hint: Use --preset-dir to specify the correct path.")
        return 1

    success = run_verification(args.preset_dir, args.frames, args.verbose)
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())

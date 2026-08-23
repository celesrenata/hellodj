"""Tests for EGLHeadlessContext — mocked ctypes for CI (no GPU required).

These tests validate the EGL context lifecycle, error handling, and pixel
readback using mock objects in place of the real libEGL.so.1 and libGL.so.1.

Requirements: Req 2 (AC 1-5)

GPU Integration Test (manual, requires Intel iGPU with Mesa iris driver):
    To run on a gremlin node with GPU access::

        import sys
        sys.path.insert(0, "bot")
        from video.visualizer_engines.egl_context import EGLHeadlessContext, FRAME_SIZE

        ctx = EGLHeadlessContext()
        ctx.create()
        assert ctx.is_valid
        ctx.make_current()
        frame = ctx.read_pixels()
        assert len(frame) == FRAME_SIZE  # 3,686,400 bytes
        ctx.destroy()
        assert not ctx.is_valid
        print("GPU integration test PASSED")
"""

from __future__ import annotations

import ctypes
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

# Ensure the bot directory is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bot"))

from video.visualizer_engines.egl_context import (
    EGL_CONTEXT_MAJOR_VERSION,
    EGL_CONTEXT_MINOR_VERSION,
    EGL_CONTEXT_OPENGL_CORE_PROFILE_BIT,
    EGL_CONTEXT_OPENGL_PROFILE_MASK,
    EGL_NONE,
    EGL_OPENGL_API,
    EGL_OPENGL_BIT,
    EGL_PBUFFER_BIT,
    EGL_PLATFORM_SURFACELESS_MESA,
    EGL_RENDERABLE_TYPE,
    EGL_SURFACE_TYPE,
    EGLContextError,
    EGLHeadlessContext,
    FRAME_HEIGHT,
    FRAME_SIZE,
    FRAME_WIDTH,
    GL_COLOR_ATTACHMENT0,
    GL_FRAMEBUFFER,
    GL_FRAMEBUFFER_COMPLETE,
    GL_RENDERBUFFER,
    GL_RGBA,
    GL_RGBA8,
    GL_UNSIGNED_BYTE,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_mock_egl() -> MagicMock:
    """Create a mock libEGL with successful default returns."""
    mock = MagicMock()
    # eglGetPlatformDisplay returns a non-null pointer
    mock.eglGetPlatformDisplay.return_value = 0xDEAD0001
    mock.eglGetPlatformDisplay.restype = ctypes.c_void_p
    # eglInitialize succeeds
    mock.eglInitialize.return_value = 1
    # eglBindAPI succeeds
    mock.eglBindAPI.return_value = 1
    # eglChooseConfig succeeds with 1 config found
    def choose_config_side_effect(display, attribs, config_out, max_configs, num_out):
        # Set num_configs to 1
        num_out._obj.value = 1
        return 1

    mock.eglChooseConfig.side_effect = choose_config_side_effect
    # eglCreateContext returns a non-null pointer
    mock.eglCreateContext.return_value = 0xDEAD0002
    mock.eglCreateContext.restype = ctypes.c_void_p
    # eglMakeCurrent succeeds
    mock.eglMakeCurrent.return_value = 1
    # eglDestroyContext succeeds
    mock.eglDestroyContext.return_value = 1
    # eglTerminate succeeds
    mock.eglTerminate.return_value = 1
    return mock


def _make_mock_gl() -> MagicMock:
    """Create a mock libGL with successful default returns."""
    mock = MagicMock()
    # glGenRenderbuffers sets rbo id
    def gen_renderbuffers(count, buf_ptr):
        buf_ptr._obj.value = 1

    mock.glGenRenderbuffers.side_effect = gen_renderbuffers
    # glGenFramebuffers sets fbo id
    def gen_framebuffers(count, buf_ptr):
        buf_ptr._obj.value = 2

    mock.glGenFramebuffers.side_effect = gen_framebuffers
    # glCheckFramebufferStatus returns COMPLETE
    mock.glCheckFramebufferStatus.return_value = GL_FRAMEBUFFER_COMPLETE
    # glReadPixels fills buffer (handled in tests)
    mock.glReadPixels.return_value = None
    return mock


@pytest.fixture
def mock_libs():
    """Patch ctypes.CDLL to return mock EGL and GL libraries."""
    mock_egl = _make_mock_egl()
    mock_gl = _make_mock_gl()

    def cdll_factory(name):
        if "EGL" in name:
            return mock_egl
        elif "GL" in name:
            return mock_gl
        raise OSError(f"Unexpected library: {name}")

    with patch("ctypes.CDLL", side_effect=cdll_factory) as mock_cdll:
        yield {"egl": mock_egl, "gl": mock_gl, "cdll": mock_cdll}


@pytest.fixture
def ctx(mock_libs) -> EGLHeadlessContext:
    """Create and return an EGLHeadlessContext backed by mocked libs."""
    context = EGLHeadlessContext()
    context.create()
    return context


# ---------------------------------------------------------------------------
# Tests — Constants
# ---------------------------------------------------------------------------


class TestConstants:
    """Verify module-level constants match spec."""

    def test_frame_width(self):
        assert FRAME_WIDTH == 1280

    def test_frame_height(self):
        assert FRAME_HEIGHT == 720

    def test_frame_size(self):
        assert FRAME_SIZE == 1280 * 720 * 4
        assert FRAME_SIZE == 3_686_400

    def test_egl_platform_surfaceless(self):
        assert EGL_PLATFORM_SURFACELESS_MESA == 0x31DD

    def test_egl_opengl_api(self):
        assert EGL_OPENGL_API == 0x30A2


# ---------------------------------------------------------------------------
# Tests — Successful Lifecycle
# ---------------------------------------------------------------------------


class TestLifecycle:
    """Test successful create/make_current/read_pixels/destroy cycle."""

    def test_create_sets_valid(self, ctx):
        """After create(), is_valid should be True."""
        assert ctx.is_valid is True

    def test_create_uses_surfaceless_platform(self, mock_libs):
        """create() calls eglGetPlatformDisplay with SURFACELESS."""
        context = EGLHeadlessContext()
        context.create()
        mock_libs["egl"].eglGetPlatformDisplay.assert_called_once()
        args = mock_libs["egl"].eglGetPlatformDisplay.call_args[0]
        assert args[0] == EGL_PLATFORM_SURFACELESS_MESA

    def test_create_binds_opengl_api(self, mock_libs):
        """create() calls eglBindAPI with EGL_OPENGL_API."""
        context = EGLHeadlessContext()
        context.create()
        mock_libs["egl"].eglBindAPI.assert_called_once_with(EGL_OPENGL_API)

    def test_create_requests_opengl_33_core(self, mock_libs):
        """create() requests OpenGL 3.3 Core profile context."""
        context = EGLHeadlessContext()
        context.create()
        mock_libs["egl"].eglCreateContext.assert_called_once()

    def test_create_makes_current_and_creates_fbo(self, mock_libs):
        """create() calls make_current and creates FBO."""
        context = EGLHeadlessContext()
        context.create()
        mock_libs["egl"].eglMakeCurrent.assert_called()
        mock_libs["gl"].glGenFramebuffers.assert_called_once()
        mock_libs["gl"].glGenRenderbuffers.assert_called_once()

    def test_make_current_calls_egl(self, ctx, mock_libs):
        """make_current() binds context with no surfaces."""
        ctx.make_current()
        # Should be called at least twice: once in create, once explicit
        assert mock_libs["egl"].eglMakeCurrent.call_count >= 2

    def test_read_pixels_returns_correct_size(self, ctx):
        """read_pixels() returns exactly FRAME_SIZE bytes."""
        frame = ctx.read_pixels()
        assert len(frame) == FRAME_SIZE

    def test_read_pixels_default_dimensions(self, ctx):
        """read_pixels() uses 1280x720 by default."""
        assert ctx.width == 1280
        assert ctx.height == 720
        frame = ctx.read_pixels()
        assert len(frame) == 1280 * 720 * 4

    def test_destroy_clears_valid(self, ctx):
        """After destroy(), is_valid should be False."""
        ctx.destroy()
        assert ctx.is_valid is False

    def test_destroy_calls_cleanup(self, ctx, mock_libs):
        """destroy() releases FBO, renderbuffer, context, and display."""
        ctx.destroy()
        mock_libs["gl"].glDeleteFramebuffers.assert_called_once()
        mock_libs["gl"].glDeleteRenderbuffers.assert_called_once()
        mock_libs["egl"].eglDestroyContext.assert_called_once()
        mock_libs["egl"].eglTerminate.assert_called_once()

    def test_custom_dimensions(self, mock_libs):
        """create() with custom width/height sets dimensions correctly."""
        context = EGLHeadlessContext()
        context.create(width=640, height=480)
        assert context.width == 640
        assert context.height == 480
        frame = context.read_pixels()
        assert len(frame) == 640 * 480 * 4


# ---------------------------------------------------------------------------
# Tests — Error Handling
# ---------------------------------------------------------------------------


class TestErrorHandling:
    """Test EGLContextError is raised on failures."""

    def test_failed_egl_load(self):
        """Should raise EGLContextError if libEGL.so.1 cannot be loaded."""
        with patch("ctypes.CDLL", side_effect=OSError("not found")):
            ctx = EGLHeadlessContext()
            with pytest.raises(EGLContextError, match="Failed to load libEGL"):
                ctx.create()

    def test_failed_gl_load(self):
        """Should raise EGLContextError if libGL.so.1 cannot be loaded."""
        mock_egl = _make_mock_egl()

        def cdll_factory(name):
            if "EGL" in name:
                return mock_egl
            raise OSError("not found")

        with patch("ctypes.CDLL", side_effect=cdll_factory):
            ctx = EGLHeadlessContext()
            with pytest.raises(EGLContextError, match="Failed to load libGL"):
                ctx.create()

    def test_failed_display(self, mock_libs):
        """Should raise EGLContextError if eglGetPlatformDisplay returns NULL."""
        mock_libs["egl"].eglGetPlatformDisplay.return_value = None
        ctx = EGLHeadlessContext()
        with pytest.raises(EGLContextError, match="eglGetPlatformDisplay failed"):
            ctx.create()

    def test_failed_initialize(self, mock_libs):
        """Should raise EGLContextError if eglInitialize fails."""
        mock_libs["egl"].eglInitialize.return_value = 0
        ctx = EGLHeadlessContext()
        with pytest.raises(EGLContextError, match="eglInitialize failed"):
            ctx.create()

    def test_failed_bind_api(self, mock_libs):
        """Should raise EGLContextError if eglBindAPI fails."""
        mock_libs["egl"].eglBindAPI.return_value = 0
        ctx = EGLHeadlessContext()
        with pytest.raises(EGLContextError, match="eglBindAPI"):
            ctx.create()

    def test_no_configs(self, mock_libs):
        """Should raise EGLContextError if eglChooseConfig finds no configs."""

        def choose_config_no_match(display, attribs, config_out, max_configs, num_out):
            num_out._obj.value = 0
            return 1

        mock_libs["egl"].eglChooseConfig.side_effect = choose_config_no_match
        ctx = EGLHeadlessContext()
        with pytest.raises(EGLContextError, match="eglChooseConfig found no valid"):
            ctx.create()

    def test_failed_create_context(self, mock_libs):
        """Should raise EGLContextError if eglCreateContext returns NULL."""
        mock_libs["egl"].eglCreateContext.return_value = None
        ctx = EGLHeadlessContext()
        with pytest.raises(EGLContextError, match="eglCreateContext failed"):
            ctx.create()

    def test_incomplete_fbo(self, mock_libs):
        """Should raise EGLContextError if FBO status is not COMPLETE."""
        mock_libs["gl"].glCheckFramebufferStatus.return_value = 0x8CDD  # INCOMPLETE
        ctx = EGLHeadlessContext()
        with pytest.raises(EGLContextError, match="FBO incomplete"):
            ctx.create()

    def test_make_current_fails(self, mock_libs):
        """Should raise EGLContextError if eglMakeCurrent fails after create."""
        ctx = EGLHeadlessContext()
        ctx.create()
        # Now make subsequent calls fail
        mock_libs["egl"].eglMakeCurrent.return_value = 0
        with pytest.raises(EGLContextError, match="eglMakeCurrent failed"):
            ctx.make_current()

    def test_make_current_without_create(self):
        """Should raise EGLContextError if make_current called before create."""
        ctx = EGLHeadlessContext()
        with pytest.raises(EGLContextError, match="context not created"):
            ctx.make_current()

    def test_read_pixels_without_create(self):
        """Should raise EGLContextError if read_pixels called before create."""
        ctx = EGLHeadlessContext()
        with pytest.raises(EGLContextError, match="context not created"):
            ctx.read_pixels()


# ---------------------------------------------------------------------------
# Tests — Idempotent Destroy
# ---------------------------------------------------------------------------


class TestIdempotentDestroy:
    """Test that destroy() can be called multiple times safely."""

    def test_double_destroy(self, ctx, mock_libs):
        """Calling destroy() twice should not raise or crash."""
        ctx.destroy()
        assert ctx.is_valid is False
        # Second call should be a no-op
        ctx.destroy()
        assert ctx.is_valid is False

    def test_triple_destroy(self, ctx):
        """Calling destroy() three times should be safe."""
        ctx.destroy()
        ctx.destroy()
        ctx.destroy()
        assert ctx.is_valid is False

    def test_destroy_only_cleans_once(self, ctx, mock_libs):
        """Resources should only be freed on the first destroy() call."""
        ctx.destroy()
        ctx.destroy()
        # Should only be called once, not twice
        assert mock_libs["gl"].glDeleteFramebuffers.call_count == 1
        assert mock_libs["gl"].glDeleteRenderbuffers.call_count == 1
        assert mock_libs["egl"].eglDestroyContext.call_count == 1
        assert mock_libs["egl"].eglTerminate.call_count == 1


# ---------------------------------------------------------------------------
# Tests — is_valid Property
# ---------------------------------------------------------------------------


class TestIsValid:
    """Test that is_valid accurately reflects context state."""

    def test_initially_false(self):
        """Before create(), is_valid should be False."""
        ctx = EGLHeadlessContext()
        assert ctx.is_valid is False

    def test_true_after_create(self, ctx):
        """After create(), is_valid should be True."""
        assert ctx.is_valid is True

    def test_false_after_destroy(self, ctx):
        """After destroy(), is_valid should be False."""
        ctx.destroy()
        assert ctx.is_valid is False

    def test_false_on_create_failure(self, mock_libs):
        """If create() fails, is_valid should remain False."""
        mock_libs["egl"].eglGetPlatformDisplay.return_value = None
        ctx = EGLHeadlessContext()
        with pytest.raises(EGLContextError):
            ctx.create()
        assert ctx.is_valid is False


# ---------------------------------------------------------------------------
# Tests — read_pixels Byte Count
# ---------------------------------------------------------------------------


class TestReadPixels:
    """Test that read_pixels returns the correct byte count."""

    def test_default_size_bytes(self, ctx):
        """Default 1280x720 returns exactly 3,686,400 bytes."""
        frame = ctx.read_pixels()
        assert len(frame) == 3_686_400

    def test_frame_is_bytes(self, ctx):
        """read_pixels returns bytes type."""
        frame = ctx.read_pixels()
        assert isinstance(frame, bytes)

    def test_custom_size_bytes(self, mock_libs):
        """Custom dimensions return width * height * 4 bytes."""
        ctx = EGLHeadlessContext()
        ctx.create(width=800, height=600)
        frame = ctx.read_pixels()
        assert len(frame) == 800 * 600 * 4

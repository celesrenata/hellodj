"""EGL headless rendering context for GPU-accelerated visualizer engines.

Creates an OpenGL 3.3 Core context on a DRM render node using the
EGL surfaceless platform. No X11/Wayland required.

Platform requirements:
- Render node: /dev/dri/renderD128 (discovered by GPUProbe)
- Mesa iris driver for OpenGL 3.3 Core on Intel Meteor Lake
- EGL extensions: EGL_MESA_platform_surfaceless, EGL_KHR_create_context
"""

from __future__ import annotations

import ctypes
import logging

log = logging.getLogger(__name__)

# Frame constants
FRAME_WIDTH = 1280
FRAME_HEIGHT = 720
FRAME_SIZE = FRAME_WIDTH * FRAME_HEIGHT * 4  # RGBA = 3,686,400 bytes

# EGL constants
EGL_PLATFORM_SURFACELESS_MESA = 0x31DD
EGL_OPENGL_API = 0x30A2
EGL_CONTEXT_MAJOR_VERSION = 0x3098
EGL_CONTEXT_MINOR_VERSION = 0x30FB
EGL_CONTEXT_OPENGL_PROFILE_MASK = 0x30FD
EGL_CONTEXT_OPENGL_CORE_PROFILE_BIT = 0x00000001
EGL_NONE = 0x3038
EGL_RENDERABLE_TYPE = 0x3040
EGL_OPENGL_BIT = 0x0008
EGL_SURFACE_TYPE = 0x3033
EGL_PBUFFER_BIT = 0x0001

# GL constants
GL_FRAMEBUFFER = 0x8D40
GL_COLOR_ATTACHMENT0 = 0x8CE0
GL_RENDERBUFFER = 0x8D41
GL_RGBA8 = 0x8058
GL_RGBA = 0x1908
GL_UNSIGNED_BYTE = 0x1401
GL_FRAMEBUFFER_COMPLETE = 0x8CD5


class EGLContextError(Exception):
    """Raised when EGL context creation or operation fails."""


class EGLHeadlessContext:
    """Headless EGL/OpenGL context on a DRM render node.

    Lifecycle::

        ctx = EGLHeadlessContext()
        ctx.create(width=1280, height=720)
        ctx.make_current()
        # ... OpenGL rendering via ctypes ...
        frame = ctx.read_pixels()  # RGBA bytes, 1280x720
        ctx.destroy()

    Uses EGL_MESA_platform_surfaceless — no X11/Wayland dependency.
    """

    def __init__(self, render_device: str = "/dev/dri/renderD128") -> None:
        self.render_device = render_device
        self.width = FRAME_WIDTH
        self.height = FRAME_HEIGHT
        self._egl: ctypes.CDLL | None = None
        self._gl: ctypes.CDLL | None = None
        self._gbm: ctypes.CDLL | None = None
        self._gbm_device: int | None = None
        self._drm_fd: int | None = None
        self._display: ctypes.c_void_p | None = None
        self._context: ctypes.c_void_p | None = None
        self._fbo: int | None = None
        self._rbo: int | None = None
        self._created: bool = False

    @property
    def is_valid(self) -> bool:
        """True if the EGL context and FBO are created and usable."""
        return self._created

    def create(self, width: int = FRAME_WIDTH, height: int = FRAME_HEIGHT) -> None:
        """Initialize EGL display, context, and offscreen FBO.

        Args:
            width: Framebuffer width in pixels. Defaults to 1280.
            height: Framebuffer height in pixels. Defaults to 720.

        Raises:
            EGLContextError: If any EGL/GL operation fails.
        """
        self.width = width
        self.height = height

        # Force Mesa as the EGL vendor (bypasses libglvnd dispatch issues
        # in containers without /usr/share/glvnd/egl_vendor.d/ config).
        import os
        os.environ.setdefault(
            "__EGL_VENDOR_LIBRARY_FILENAMES",
            "/usr/lib/x86_64-linux-gnu/libEGL_mesa.so.0",
        )
        # Hint Mesa to use the iris driver for our Intel Meteor Lake iGPU
        os.environ.setdefault("MESA_LOADER_DRIVER_OVERRIDE", "iris")

        # Load shared libraries
        try:
            self._egl = ctypes.CDLL("libEGL.so.1")
        except OSError as exc:
            raise EGLContextError(f"Failed to load libEGL.so.1: {exc}") from exc
        try:
            self._gl = ctypes.CDLL("libGL.so.1")
        except OSError as exc:
            raise EGLContextError(f"Failed to load libGL.so.1: {exc}") from exc

        # Open the DRM render node and create a GBM device for headless EGL.
        # GBM platform binds EGL to a specific GPU device (the SR-IOV VF).
        import os

        try:
            self._gbm = ctypes.CDLL("libgbm.so.1")
        except OSError as exc:
            raise EGLContextError(f"Failed to load libgbm.so.1: {exc}") from exc

        try:
            self._drm_fd = os.open(self.render_device, os.O_RDWR)
        except OSError as exc:
            raise EGLContextError(
                f"Failed to open render device {self.render_device}: {exc}"
            ) from exc

        self._gbm.gbm_create_device.restype = ctypes.c_void_p
        self._gbm_device = self._gbm.gbm_create_device(self._drm_fd)
        if not self._gbm_device:
            os.close(self._drm_fd)
            raise EGLContextError(
                f"gbm_create_device failed on {self.render_device}"
            )

        # Get eglGetPlatformDisplayEXT via eglGetProcAddress.
        # Debian trixie's libglvnd 1.7.0 doesn't route eglGetPlatformDisplay
        # for GBM platform correctly, but eglGetProcAddress works through the
        # glvnd dispatch table to reach Mesa's implementation.
        self._egl.eglGetProcAddress.restype = ctypes.c_void_p
        self._egl.eglGetProcAddress.argtypes = [ctypes.c_char_p]
        _fn = self._egl.eglGetProcAddress(b"eglGetPlatformDisplayEXT")
        if not _fn:
            self._gbm.gbm_device_destroy(ctypes.c_void_p(self._gbm_device))
            os.close(self._drm_fd)
            raise EGLContextError("eglGetProcAddress(eglGetPlatformDisplayEXT) returned NULL")

        _get_platform_display_ext = ctypes.cast(
            _fn,
            ctypes.CFUNCTYPE(
                ctypes.c_void_p,   # return EGLDisplay
                ctypes.c_uint,     # platform
                ctypes.c_void_p,   # native_display
                ctypes.POINTER(ctypes.c_int),  # attrib_list
            ),
        )

        EGL_PLATFORM_GBM_KHR = 0x31D7
        self._display = _get_platform_display_ext(
            EGL_PLATFORM_GBM_KHR,
            ctypes.c_void_p(self._gbm_device),
            None,
        )
        if not self._display:
            self._gbm.gbm_device_destroy(ctypes.c_void_p(self._gbm_device))
            os.close(self._drm_fd)
            raise EGLContextError("eglGetPlatformDisplayEXT failed (GBM)")

        # Initialize EGL
        major, minor = ctypes.c_int(), ctypes.c_int()
        if not self._egl.eglInitialize(
            self._display, ctypes.byref(major), ctypes.byref(minor)
        ):
            self._gbm.gbm_device_destroy(ctypes.c_void_p(self._gbm_device))
            os.close(self._drm_fd)
            raise EGLContextError("eglInitialize failed")

        log.debug(
            "EGL initialized: version %d.%d on %s (GBM platform)",
            major.value, minor.value, self.render_device,
        )

        # Bind OpenGL API
        if not self._egl.eglBindAPI(EGL_OPENGL_API):
            raise EGLContextError("eglBindAPI(EGL_OPENGL_API) failed")

        # Choose config
        config_attribs = (ctypes.c_int * 7)(
            EGL_SURFACE_TYPE, EGL_PBUFFER_BIT,
            EGL_RENDERABLE_TYPE, EGL_OPENGL_BIT,
            EGL_NONE, 0, 0,
        )
        config = ctypes.c_void_p()
        num_configs = ctypes.c_int()
        self._egl.eglChooseConfig(
            self._display,
            config_attribs,
            ctypes.byref(config),
            1,
            ctypes.byref(num_configs),
        )
        if num_configs.value == 0:
            raise EGLContextError("eglChooseConfig found no valid configs")

        # Create OpenGL 3.3 Core context
        context_attribs = (ctypes.c_int * 7)(
            EGL_CONTEXT_MAJOR_VERSION, 3,
            EGL_CONTEXT_MINOR_VERSION, 3,
            EGL_CONTEXT_OPENGL_PROFILE_MASK, EGL_CONTEXT_OPENGL_CORE_PROFILE_BIT,
            EGL_NONE,
        )
        create_context = self._egl.eglCreateContext
        create_context.restype = ctypes.c_void_p
        self._context = create_context(
            self._display, config, None, context_attribs
        )
        if not self._context:
            raise EGLContextError("eglCreateContext failed (OpenGL 3.3 Core)")

        # Make current and create FBO
        self.make_current()
        self._create_fbo()
        self._created = True

        log.info(
            "EGL headless context created: %dx%d, OpenGL 3.3 Core",
            self.width,
            self.height,
        )

    def make_current(self) -> None:
        """Bind this context as the current GL context (surfaceless, no surface).

        Raises:
            EGLContextError: If eglMakeCurrent fails.
        """
        if not self._egl or not self._display or not self._context:
            raise EGLContextError("Cannot make_current: context not created")
        if not self._egl.eglMakeCurrent(
            self._display, None, None, self._context
        ):
            raise EGLContextError("eglMakeCurrent failed")

    def read_pixels(self) -> bytes:
        """Read FBO contents as RGBA bytes.

        Returns:
            Exactly width * height * 4 bytes of RGBA pixel data.
            For default 1280x720: 3,686,400 bytes.

        Raises:
            EGLContextError: If the context is not valid.
        """
        if not self._created:
            raise EGLContextError("Cannot read_pixels: context not created")
        buf = (ctypes.c_ubyte * (self.width * self.height * 4))()
        self._gl.glReadPixels(
            0, 0, self.width, self.height, GL_RGBA, GL_UNSIGNED_BYTE, buf
        )
        return bytes(buf)

    def destroy(self) -> None:
        """Release all EGL/GL resources (FBO, renderbuffer, context, display).

        Idempotent — safe to call multiple times. Completes within 500ms.
        """
        if not self._created:
            return

        # Delete FBO
        if self._fbo is not None:
            fbo_id = ctypes.c_uint(self._fbo)
            self._gl.glDeleteFramebuffers(1, ctypes.byref(fbo_id))
            self._fbo = None

        # Delete renderbuffer
        if self._rbo is not None:
            rbo_id = ctypes.c_uint(self._rbo)
            self._gl.glDeleteRenderbuffers(1, ctypes.byref(rbo_id))
            self._rbo = None

        # Destroy EGL context
        if self._context:
            self._egl.eglMakeCurrent(self._display, None, None, None)
            self._egl.eglDestroyContext(self._display, self._context)
            self._context = None

        # Terminate EGL display
        if self._display:
            self._egl.eglTerminate(self._display)
            self._display = None

        # Release GBM device and close DRM fd
        if hasattr(self, '_gbm_device') and self._gbm_device:
            self._gbm.gbm_device_destroy(ctypes.c_void_p(self._gbm_device))
            self._gbm_device = None
        if hasattr(self, '_drm_fd') and self._drm_fd is not None:
            import os
            os.close(self._drm_fd)
            self._drm_fd = None

        self._created = False
        log.info("EGL headless context destroyed")

    def _create_fbo(self) -> None:
        """Create offscreen framebuffer with RGBA8 renderbuffer.

        Raises:
            EGLContextError: If framebuffer is incomplete.
        """
        # Create and configure renderbuffer
        rbo = ctypes.c_uint()
        self._gl.glGenRenderbuffers(1, ctypes.byref(rbo))
        self._gl.glBindRenderbuffer(GL_RENDERBUFFER, rbo)
        self._gl.glRenderbufferStorage(
            GL_RENDERBUFFER, GL_RGBA8, self.width, self.height
        )
        self._rbo = rbo.value

        # Create FBO and attach renderbuffer
        fbo = ctypes.c_uint()
        self._gl.glGenFramebuffers(1, ctypes.byref(fbo))
        self._gl.glBindFramebuffer(GL_FRAMEBUFFER, fbo)
        self._gl.glFramebufferRenderbuffer(
            GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_RENDERBUFFER, rbo
        )
        self._fbo = fbo.value

        # Validate
        status = self._gl.glCheckFramebufferStatus(GL_FRAMEBUFFER)
        if status != GL_FRAMEBUFFER_COMPLETE:
            raise EGLContextError(f"FBO incomplete: status=0x{status:04X}")

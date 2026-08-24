"""GPU Engine Base Class — shared foundation for all server-rendered GPU engines.

Provides EGL context lifecycle, FBO pixel readback, AudioFeatures buffering,
and a standard render loop yielding RGBA frames at 30fps.

Requirements: Req 2 (AC 1-5), Req 11 (AC 4), Req 12 (AC 4)
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import AsyncIterator

from video.visualizer_engines.base import AudioFeatures, TrackMetadata, VisualizerRenderer
from video.visualizer_engines.egl_context import EGLHeadlessContext

log = logging.getLogger(__name__)


class GPURenderError(Exception):
    """Raised when a GPU rendering operation fails (device loss, GL error)."""
    pass


class GPUEngineBase(VisualizerRenderer):
    """Shared base for server-rendered GPU engines.

    Provides EGL context lifecycle, FBO pixel readback, AudioFeatures
    buffering, and a standard render loop yielding RGBA at 30fps.

    Subclasses must implement:
        _on_gl_ready(metadata) — called after EGL context creation
        _render_gl_frame(features) — render one frame into the FBO
    """

    TARGET_FPS: int = 30
    FRAME_INTERVAL: float = 1.0 / 30

    def __init__(self) -> None:
        self._egl_ctx: EGLHeadlessContext | None = None
        self._latest_features: AudioFeatures | None = None
        self._running: bool = False

    # -----------------------------------------------------------------------
    # Properties (VisualizerRenderer interface)
    # -----------------------------------------------------------------------

    @property
    def is_client_side(self) -> bool:
        return False

    @property
    def consumes_gpu_while_suspended(self) -> bool:
        return False  # Context destroyed on suspend (Req 12 AC 4)

    @property
    def client_config(self) -> dict | None:
        return None

    # -----------------------------------------------------------------------
    # Audio callback (non-blocking, ~47fps safe)
    # -----------------------------------------------------------------------

    def on_audio_features(self, features: AudioFeatures) -> None:
        """Atomic reference swap — non-blocking, safe at ~47fps."""
        self._latest_features = features

    # -----------------------------------------------------------------------
    # Lifecycle methods
    # -----------------------------------------------------------------------

    async def initialize(self, metadata: TrackMetadata | None = None) -> None:
        """One-time setup (no-op for GPU engines; activate handles context)."""
        pass

    async def activate(self, metadata: TrackMetadata | None = None) -> None:
        """Create EGL headless context and call subclass _on_gl_ready hook."""
        import glob as _glob
        render_devices = sorted(_glob.glob("/dev/dri/renderD*"))
        # Prefer VF render nodes (renderD129+) over PF (renderD128) because
        # the PF doesn't support EGL context creation when SR-IOV VFs are active.
        vf_devices = [d for d in render_devices if d != "/dev/dri/renderD128"]
        render_device = vf_devices[0] if vf_devices else (render_devices[0] if render_devices else "/dev/dri/renderD128")
        self._egl_ctx = EGLHeadlessContext(render_device=render_device)
        self._egl_ctx.create()
        self._running = True
        await self._on_gl_ready(metadata)

    async def suspend(self) -> None:
        """Destroy EGL context — zero GPU while suspended (Req 12 AC 4)."""
        self._running = False
        if self._egl_ctx:
            self._egl_ctx.destroy()
            self._egl_ctx = None

    async def resume(self, metadata: TrackMetadata | None = None) -> None:
        """Re-create context via activate()."""
        await self.activate(metadata)

    async def stop(self) -> None:
        """Destroy context and all resources."""
        self._running = False
        if self._egl_ctx:
            self._egl_ctx.destroy()
            self._egl_ctx = None

    async def on_track_change(self, metadata: TrackMetadata) -> None:
        """Handle track change. Override in subclass for specific behavior."""
        pass

    # -----------------------------------------------------------------------
    # Render loop
    # -----------------------------------------------------------------------

    async def render_frames(self) -> AsyncIterator[bytes]:
        """Yield RGBA frames at TARGET_FPS (30fps).

        Exception isolation (Req 11 AC 4): GL errors during rendering are
        caught and re-raised as GPURenderError so the caller (_render_loop
        in VisualizerManager) can handle them without propagating to the
        bot's main event loop.
        """
        while self._running:
            t0 = time.monotonic()
            try:
                self._egl_ctx.make_current()
                self._render_gl_frame(self._latest_features)
                frame = self._egl_ctx.read_pixels()
            except Exception as exc:
                log.error(
                    "GPU render error in %s: %s",
                    type(self).__name__,
                    exc,
                )
                self._running = False
                raise GPURenderError(
                    f"GPU rendering failed in {type(self).__name__}: {exc}"
                ) from exc
            yield frame
            elapsed = time.monotonic() - t0
            sleep_time = self.FRAME_INTERVAL - elapsed
            if sleep_time > 0:
                await asyncio.sleep(sleep_time)

    # -----------------------------------------------------------------------
    # Subclass hooks (must be implemented)
    # -----------------------------------------------------------------------

    async def _on_gl_ready(self, metadata: TrackMetadata | None) -> None:
        """Called after EGL context is ready. Load shaders here."""
        raise NotImplementedError

    def _render_gl_frame(self, features: AudioFeatures | None) -> None:
        """Render one frame into the FBO. Called at 30fps."""
        raise NotImplementedError

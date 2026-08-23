# Technical Design — GPU Visualizer Engines

## Overview

This design defines the GPU-accelerated visualizer engine system for HelloDJ's Discord Activity. The system renders audio-reactive visualizations on Intel Meteor Lake iGPUs (SR-IOV, QSV/VA-API) in the Kubernetes cluster and delivers them as HLS streams to Activity viewers. Four engine implementations (projectM, AudioVis, Fosfora, Varda) share a common EGL headless rendering module, GPU resource scheduler, and frame pipeline.

Key constraints:
- Intel Meteor Lake iGPU (device `0300-7d55`) with SR-IOV (8 VFs, 7 allocatable for visualizers)
- FFmpeg 9 with QSV/VA-API/libvpl already in the bot container
- All rendering at 720p, 30fps via EGL surfaceless platform (no display server)
- Mesa iris driver for OpenGL 3.3 Core on Intel
- Python 3.11 — GPU rendering via ctypes bindings to EGL/GL/libprojectM
- Existing modules: AudioFeatureBus, VisualizerManager, VisualizerRegistry, HLSTranscodePipeline

## Architecture

### System Data Flow

```
┌──────────────────────────────────────────────────────────────────────────┐
│  Discord Voice (Opus)                                                    │
│      │                                                                   │
│      ▼                                                                   │
│  voice_recv → PCM buffer                                                 │
│      │                                                                   │
│      ▼                                                                   │
│  ┌─────────────────────┐                                                 │
│  │   AudioFeatureBus   │  (existing — Req 1)                             │
│  │   FFT 512 bins      │                                                 │
│  │   Beat detection    │                                                 │
│  │   BPM estimation    │                                                 │
│  │   7-band energy     │                                                 │
│  └────────┬────────────┘                                                 │
│           │ AudioFeatures callback @ ~47fps                              │
│           ▼                                                              │
│  ┌─────────────────────────────────────────────┐                         │
│  │       Engine (server-rendered)               │  (Req 2, 5-8)          │
│  │  ┌───────────┐  ┌────────────┐  ┌────────┐ │                         │
│  │  │ EGL Ctx   │  │ GLSL/      │  │ FBO    │ │                         │
│  │  │ (headless)│  │ libprojectM│  │ 720p   │ │                         │
│  │  └───────────┘  └────────────┘  └───┬────┘ │                         │
│  └──────────────────────────────────────┼──────┘                         │
│                                         │ glReadPixels() → RGBA bytes    │
│                                         ▼                                │
│  ┌─────────────────────────────────────────────┐                         │
│  │   Frame Pipeline (render_frames() → stdin)  │  (Req 3, 13)           │
│  │                                             │                         │
│  │   RGBA 1280×720×4 bytes/frame @ 30fps       │                         │
│  └─────────────────────┬───────────────────────┘                         │
│                         │ pipe to ffmpeg stdin                            │
│                         ▼                                                │
│  ┌─────────────────────────────────────────────┐                         │
│  │   HLSTranscodePipeline (start_visualizer)   │  (existing — Req 13)   │
│  │   ffmpeg rawvideo → hwupload_qsv → h264_qsv│                         │
│  │   2s HLS segments → /tmp/hellodj_hls/{gid}/ │                         │
│  └─────────────────────┬───────────────────────┘                         │
│                         │                                                │
│                         ▼                                                │
│  ┌─────────────────────────────────────────────┐                         │
│  │   Activity Frontend (HLS.js playback)       │  (existing)             │
│  │   /activity/stream/{gid}/viz/playlist.m3u8  │                         │
│  └─────────────────────────────────────────────┘                         │
│                                                                          │
│  ┌─────────────────────┐       ┌──────────────────┐                     │
│  │  GPU Resource        │       │ VisualizerManager │ (existing — Req 12)│
│  │  Scheduler (Req 4)   │◄─────│ state machine     │                     │
│  │  SR-IOV VF tracking  │       │ viewer-driven     │                     │
│  └──────────────────────┘       └──────────────────┘                     │
└──────────────────────────────────────────────────────────────────────────┘
```

### Component Responsibilities

| Component | Satisfies | Description |
|-----------|-----------|-------------|
| `AudioFeatureBus` | Req 1 | Existing. Subscriber-gated FFT/beat/BPM pipeline at ~47fps |
| `EGLHeadlessContext` (new) | Req 2 | Headless OpenGL context creation on render nodes |
| `GPUResourceScheduler` (new) | Req 4 | SR-IOV VF allocation/release tracking |
| `HLSTranscodePipeline.start_visualizer()` | Req 3, 13 | rawvideo stdin → h264_qsv → HLS segments |
| `VisualizerManager` | Req 1, 3, 9, 11, 12 | Existing. State machine, render loop, demand rendering |
| `ProjectMEngine` | Req 5, 17 | libprojectM binding for Milkdrop presets |
| `AudioVisEngine` | Req 6 | GLSL spectrum/waveform/waterfall shaders |
| `FosforaEngine` | Req 7 | GPU particle system with transform feedback |
| `VardaEngine` | Req 8 | Shadertoy-compatible fragment shader runner |
| `GPUEngineBase` (new) | Req 2, 12 | Shared base class for all GPU engines |
| `VisualizerConfigCog` (new) | Req 14, 15, 16, 17 | Discord slash commands for config/presets |
| `GPUProbe` | Req 10, 11 | Existing. GPU detection at startup |

## Components and Interfaces

### 3.1 EGL Headless Rendering Module (Req 2)

The bot container has no X11 or Wayland. All GPU rendering uses EGL with the surfaceless platform to create OpenGL contexts directly on DRM render nodes.

**Platform requirements:**
- Render node: `/dev/dri/renderD128` (discovered by GPUProbe)
- Mesa iris driver for OpenGL 3.3 Core on Meteor Lake
- EGL extensions: `EGL_MESA_platform_surfaceless`, `EGL_KHR_create_context`

**Module:** `bot/video/visualizer_engines/egl_context.py`

```python
"""EGL headless rendering context for GPU-accelerated visualizer engines.

Creates an OpenGL 3.3 Core context on a DRM render node using the
EGL surfaceless platform. No X11/Wayland required.
"""

import ctypes
import logging
from pathlib import Path

log = logging.getLogger(__name__)

FRAME_WIDTH = 1280
FRAME_HEIGHT = 720
FRAME_SIZE = FRAME_WIDTH * FRAME_HEIGHT * 4  # RGBA

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

    Lifecycle:
        ctx = EGLHeadlessContext(render_device="/dev/dri/renderD128")
        ctx.create(width=1280, height=720)
        ctx.make_current()
        # ... OpenGL rendering via ctypes ...
        frame = ctx.read_pixels()  # RGBA bytes, 1280x720
        ctx.destroy()
    """

    def __init__(self, render_device: str = "/dev/dri/renderD128") -> None:
        self.render_device = render_device
        self.width = FRAME_WIDTH
        self.height = FRAME_HEIGHT
        self._egl = None
        self._gl = None
        self._display = None
        self._context = None
        self._fbo = None
        self._rbo = None
        self._created = False

    def create(self, width: int = FRAME_WIDTH, height: int = FRAME_HEIGHT) -> None:
        """Initialize EGL display, context, and offscreen FBO.

        Raises:
            EGLContextError: If any EGL/GL operation fails.
        """
        self.width = width
        self.height = height

        self._egl = ctypes.CDLL("libEGL.so.1")
        self._gl = ctypes.CDLL("libGL.so.1")

        # eglGetPlatformDisplay(EGL_PLATFORM_SURFACELESS_MESA, NULL, NULL)
        get_platform_display = self._egl.eglGetPlatformDisplay
        get_platform_display.restype = ctypes.c_void_p
        self._display = get_platform_display(
            EGL_PLATFORM_SURFACELESS_MESA, None, None,
        )
        if not self._display:
            raise EGLContextError("eglGetPlatformDisplay failed (surfaceless)")

        # Initialize EGL
        major, minor = ctypes.c_int(), ctypes.c_int()
        if not self._egl.eglInitialize(self._display, ctypes.byref(major), ctypes.byref(minor)):
            raise EGLContextError("eglInitialize failed")

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
            self._display, config_attribs,
            ctypes.byref(config), 1, ctypes.byref(num_configs),
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
        self._context = create_context(self._display, config, None, context_attribs)
        if not self._context:
            raise EGLContextError("eglCreateContext failed (OpenGL 3.3 Core)")

        self.make_current()
        self._create_fbo()
        self._created = True

    def make_current(self) -> None:
        """Bind this context as the current GL context (surfaceless)."""
        if not self._egl.eglMakeCurrent(self._display, None, None, self._context):
            raise EGLContextError("eglMakeCurrent failed")

    def read_pixels(self) -> bytes:
        """Read FBO contents as RGBA bytes (width × height × 4)."""
        buf = (ctypes.c_ubyte * (self.width * self.height * 4))()
        self._gl.glReadPixels(0, 0, self.width, self.height, GL_RGBA, GL_UNSIGNED_BYTE, buf)
        return bytes(buf)

    def destroy(self) -> None:
        """Release all EGL/GL resources."""
        if not self._created:
            return
        if self._fbo is not None:
            fbo_id = ctypes.c_uint(self._fbo)
            self._gl.glDeleteFramebuffers(1, ctypes.byref(fbo_id))
        if self._rbo is not None:
            rbo_id = ctypes.c_uint(self._rbo)
            self._gl.glDeleteRenderbuffers(1, ctypes.byref(rbo_id))
        if self._context:
            self._egl.eglDestroyContext(self._display, self._context)
        if self._display:
            self._egl.eglTerminate(self._display)
        self._created = False

    def _create_fbo(self) -> None:
        """Create offscreen framebuffer with RGBA8 renderbuffer."""
        rbo = ctypes.c_uint()
        self._gl.glGenRenderbuffers(1, ctypes.byref(rbo))
        self._gl.glBindRenderbuffer(GL_RENDERBUFFER, rbo)
        self._gl.glRenderbufferStorage(GL_RENDERBUFFER, GL_RGBA8, self.width, self.height)
        self._rbo = rbo.value

        fbo = ctypes.c_uint()
        self._gl.glGenFramebuffers(1, ctypes.byref(fbo))
        self._gl.glBindFramebuffer(GL_FRAMEBUFFER, fbo)
        self._gl.glFramebufferRenderbuffer(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_RENDERBUFFER, rbo)
        self._fbo = fbo.value

        status = self._gl.glCheckFramebufferStatus(GL_FRAMEBUFFER)
        if status != GL_FRAMEBUFFER_COMPLETE:
            raise EGLContextError(f"FBO incomplete: status=0x{status:04X}")

    @property
    def is_valid(self) -> bool:
        return self._created
```

**Design decisions:**
1. ctypes over cffi — stdlib, no extra dependency; EGL/GL API surface is small (~20 functions)
2. Surfaceless platform — `EGL_MESA_platform_surfaceless` works without /dev/dri/card* (only renderD*)
3. FBO readback — at 720p30, glReadPixels yields ~105 MB/s, well within PCIe bandwidth
4. One context per engine — GPU scheduler limits concurrent contexts to available VF slots

### 3.2 Engine Base Class & Registry (Req 9, 10, 11)

#### Existing VisualizerRenderer ABC (unchanged)

```python
class VisualizerRenderer(ABC):
    async def initialize(metadata: TrackMetadata | None) -> None: ...
    async def activate(metadata: TrackMetadata | None) -> None: ...
    async def suspend() -> None: ...
    async def resume(metadata: TrackMetadata | None) -> None: ...
    async def stop() -> None: ...
    async def on_track_change(metadata: TrackMetadata) -> None: ...
    @property
    def is_client_side(self) -> bool: ...
    @property
    def consumes_gpu_while_suspended(self) -> bool: ...
    @property
    def client_config(self) -> dict | None: ...
    async def render_frames(self) -> AsyncIterator[bytes]: ...
```

#### New Audio Callback (added to ABC)

```python
def on_audio_features(self, features: AudioFeatures) -> None:
    """Receive audio analysis data from AudioFeatureBus.

    Called synchronously at ~47fps. Store features for next render pass.
    Must be non-blocking.
    """
    pass  # Default no-op for client-side engines
```

#### GPU Engine Base Class (new)

**File:** `bot/video/visualizer_engines/gpu_engine_base.py`

```python
class GPUEngineBase(VisualizerRenderer):
    """Shared base for server-rendered GPU engines.

    Provides EGL context lifecycle, FBO pixel readback, AudioFeatures
    buffering, and a standard render loop yielding RGBA at 30fps.
    """

    TARGET_FPS: int = 30
    FRAME_INTERVAL: float = 1.0 / 30

    def __init__(self) -> None:
        self._egl_ctx: EGLHeadlessContext | None = None
        self._latest_features: AudioFeatures | None = None
        self._running: bool = False

    @property
    def is_client_side(self) -> bool:
        return False

    @property
    def consumes_gpu_while_suspended(self) -> bool:
        return False  # Context destroyed on suspend (Req 12 AC 4)

    @property
    def client_config(self) -> dict | None:
        return None

    def on_audio_features(self, features: AudioFeatures) -> None:
        self._latest_features = features  # Atomic reference swap

    async def activate(self, metadata: TrackMetadata | None = None) -> None:
        self._egl_ctx = EGLHeadlessContext()
        self._egl_ctx.create()
        self._running = True
        await self._on_gl_ready(metadata)

    async def suspend(self) -> None:
        self._running = False
        if self._egl_ctx:
            self._egl_ctx.destroy()
            self._egl_ctx = None

    async def resume(self, metadata: TrackMetadata | None = None) -> None:
        await self.activate(metadata)

    async def stop(self) -> None:
        self._running = False
        if self._egl_ctx:
            self._egl_ctx.destroy()
            self._egl_ctx = None

    async def render_frames(self) -> AsyncIterator[bytes]:
        """Yield RGBA frames at TARGET_FPS."""
        while self._running:
            t0 = time.monotonic()
            self._egl_ctx.make_current()
            self._render_gl_frame(self._latest_features)
            frame = self._egl_ctx.read_pixels()
            yield frame
            elapsed = time.monotonic() - t0
            sleep_time = self.FRAME_INTERVAL - elapsed
            if sleep_time > 0:
                await asyncio.sleep(sleep_time)

    # Subclass hooks
    async def _on_gl_ready(self, metadata: TrackMetadata | None) -> None:
        """Called after EGL context is ready. Load shaders here."""
        raise NotImplementedError

    def _render_gl_frame(self, features: AudioFeatures | None) -> None:
        """Render one frame into the FBO. Called at 30fps."""
        raise NotImplementedError
```

#### Engine Registry (existing pattern, no changes)

```python
ENGINE_REGISTRY: dict[str, type[VisualizerRenderer]] = {
    "audiovis": AudioVisEngine,
    "dvd": DVDEngine,
    "fosfora": FosforaEngine,
    "native": NativeEngine,
    "projectm": ProjectMEngine,
    "varda": VardaEngine,
}
```

The `create_engine()` factory instantiates by name. The `vgalizer` entry is removed per Req 10 (no GPU-acceleratable implementation feasible).

#### Random Pool (Req 9)

```python
# VisualizerManager
_RANDOM_POOL_ENGINES: list[str] = ["projectm", "audiovis", "fosfora", "varda"]
```

### 3.3 GPU Resource Scheduler (Req 4, 12)

**File:** `bot/video/gpu_scheduler.py`

Tracks SR-IOV VF slot allocation across concurrent guild visualizer sessions.

```python
"""GPU Resource Scheduler — SR-IOV VF allocation for visualizer engines."""

import logging
import time
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

MAX_VFS_PER_NODE = 8
RESERVED_FOR_VIDEO_TRANSCODE = 1
MAX_VISUALIZER_VFS = MAX_VFS_PER_NODE - RESERVED_FOR_VIDEO_TRANSCODE  # 7


@dataclass
class VFAllocation:
    guild_id: int
    engine_type: str
    allocated_at: float = field(default_factory=time.monotonic)


class GPUCapacityExceededError(Exception):
    """Raised when no SR-IOV VFs are available."""


class GPUResourceScheduler:
    """Manages SR-IOV VF allocation across concurrent visualizer sessions.

    All methods are synchronous (asyncio single-threaded event loop).
    """

    def __init__(self, max_visualizer_vfs: int = MAX_VISUALIZER_VFS) -> None:
        self._max_vfs = max_visualizer_vfs
        self._allocations: dict[int, VFAllocation] = {}

    @property
    def active_sessions(self) -> int:
        return len(self._allocations)

    @property
    def available_vfs(self) -> int:
        return self._max_vfs - len(self._allocations)

    def allocate(self, guild_id: int, engine_type: str) -> VFAllocation:
        """Allocate a VF slot. Raises GPUCapacityExceededError if full."""
        if guild_id in self._allocations:
            self.release(guild_id)

        if len(self._allocations) >= self._max_vfs:
            raise GPUCapacityExceededError(
                f"All {self._max_vfs} visualizer VF slots occupied"
            )

        alloc = VFAllocation(guild_id=guild_id, engine_type=engine_type)
        self._allocations[guild_id] = alloc
        log.info(
            "GPU VF allocated: guild=%d engine=%s (%d/%d in use)",
            guild_id, engine_type, len(self._allocations), self._max_vfs,
        )
        return alloc

    def release(self, guild_id: int) -> None:
        """Release VF for a guild. No-op if not allocated."""
        alloc = self._allocations.pop(guild_id, None)
        if alloc:
            log.info("GPU VF released: guild=%d engine=%s", guild_id, alloc.engine_type)

    def is_allocated(self, guild_id: int) -> bool:
        return guild_id in self._allocations
```

**Integration:** `VisualizerManager._start_engine()` calls `allocate()` before creating the EGL context. On `_stop_engine()` and `_execute_suspension()`, calls `release()`. If `GPUCapacityExceededError` is raised, the manager stays in `IDLE_NO_VIEWERS` with a warning log.

### 3.4 Frame Pipeline — HLS (Req 3, 13)

**Extension to:** `bot/video/hls_transcode.py`

New method `start_visualizer()` on `HLSTranscodePipeline`:

```python
async def start_visualizer(self) -> None:
    """Start ffmpeg pipeline accepting raw RGBA frames on stdin.

    Encodes 1280x720 RGBA frames to H.264 via QSV and outputs 2s HLS segments.
    """
    self.output_dir.mkdir(parents=True, exist_ok=True)
    self.ready.clear()
    self._running = True

    args = self._build_visualizer_ffmpeg_args()
    self.process = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    self.stdin_pipe = self.process.stdin
    self._segment_watcher_task = asyncio.ensure_future(self._watch_segments())
    self._stderr_task = asyncio.ensure_future(self._monitor_stderr())
```

**The ffmpeg command:**

```bash
ffmpeg -hide_banner -loglevel error -y \
  -f rawvideo \
  -pixel_format rgba \
  -video_size 1280x720 \
  -framerate 30 \
  -i pipe:0 \
  -init_hw_device qsv=qsv:hw \
  -filter_hw_device qsv \
  -vf "format=nv12,hwupload=extra_hw_frames=64" \
  -c:v h264_qsv \
  -profile:v main \
  -preset fast \
  -b:v 2500k \
  -maxrate 3750k \
  -bufsize 5000k \
  -g 60 \
  -force_key_frames "expr:gte(t,n_forced*2)" \
  -r 30 \
  -f hls \
  -hls_time 2 \
  -hls_list_size 10 \
  -hls_flags delete_segments+independent_segments \
  -hls_segment_filename "/tmp/hellodj_hls/{guild_id}/viz/seg%05d.ts" \
  "/tmp/hellodj_hls/{guild_id}/viz/playlist.m3u8"
```

**Key differences from video pipeline:**
- `-f rawvideo` input on stdin (not a file/URL)
- `format=nv12,hwupload` filter chain (RGBA → NV12 → QSV surface)
- `-hls_flags delete_segments` (live window, not VOD — visualizer is infinite)
- `-hls_list_size 10` (keep last 20s of segments)
- No audio stream (audio comes from Discord voice, not the visualizer)
- 2.5 Mbps bitrate (lower than video — generated content compresses well)
- `-r 30` ensures constant 30fps output even if engine renders slower (frame duplication)

### 3.5 Engine Implementations

#### 3.5.1 projectM Engine (Req 5, 17)

**File:** `bot/video/visualizer_engines/projectm.py`

Binds to libprojectM 4.x C API via ctypes. The library handles all internal OpenGL rendering — we provide an active GL context and audio data.

```python
class ProjectMEngine(GPUEngineBase):
    """Milkdrop-compatible audio visualization via libprojectM.

    Binds to libprojectM's C API for preset rendering into our EGL FBO.
    """

    PRESET_DIR = "/app/data/presets/projectm"
    BLEND_DURATION = 3.0  # seconds (Req 5 AC 4)

    def __init__(self, **kwargs) -> None:
        super().__init__()
        self._pm_handle = None
        self._lib = None
        self._preset_category: str = "all"
        self._preset_duration: float = 30.0
        self._brightness: float = 1.0
        self._sensitivity: float = 1.0

    async def _on_gl_ready(self, metadata: TrackMetadata | None) -> None:
        self._lib = ctypes.CDLL("libprojectM-4.so")
        self._lib.projectm_create.restype = ctypes.c_void_p
        self._pm_handle = self._lib.projectm_create()
        self._lib.projectm_set_window_size(self._pm_handle, self.width, self.height)
        self._lib.projectm_set_preset_duration(self._pm_handle, ctypes.c_double(self._preset_duration))
        self._lib.projectm_set_soft_cut_duration(self._pm_handle, ctypes.c_double(self.BLEND_DURATION))
        self._lib.projectm_set_beat_sensitivity(self._pm_handle, ctypes.c_float(self._sensitivity))
        preset_path = self._resolve_preset_path()
        self._lib.projectm_set_preset_path(self._pm_handle, preset_path.encode())
        self._lib.projectm_set_shuffle_enabled(self._pm_handle, True)

    def _render_gl_frame(self, features: AudioFeatures | None) -> None:
        if features and self._pm_handle:
            pcm_data = (ctypes.c_float * 512)(*features.fft)
            self._lib.projectm_pcm_add_float(self._pm_handle, pcm_data, 512, 1)
        if self._pm_handle:
            self._lib.projectm_opengl_render_frame(self._pm_handle)

    async def on_track_change(self, metadata: TrackMetadata) -> None:
        if self._pm_handle and self._lib:
            self._lib.projectm_select_random_preset(self._pm_handle, True)

    def _resolve_preset_path(self) -> str:
        base = Path(self.PRESET_DIR)
        if self._preset_category == "all":
            return str(base)
        category_path = base / self._preset_category
        return str(category_path) if category_path.exists() else str(base)
```

**Preset directory structure** (bundled in Docker image from [presets-cream-of-the-crop](https://github.com/projectM-visualizer/presets-cream-of-the-crop)):

```
/app/data/presets/projectm/
├── Abstract/         → factory preset "psychedelic"
├── Fluid Motion/     → factory preset "chill"
├── Geometric/        → factory preset "geometric"
├── Simple/           → factory preset "minimal"
├── Space/            → factory preset "space"
├── Trippy/           → factory preset "trippy"
├── Classic/          → factory preset "milkdrop-classic"
└── Energy/           → factory preset "energy"
```

#### 3.5.2 AudioVis Engine (Req 6)

**File:** `bot/video/visualizer_engines/audiovis.py`

Custom GLSL shaders render spectrum bars, waveform, and waterfall. Audio data is uploaded as a 1D texture each frame.

```python
class AudioVisEngine(GPUEngineBase):
    """GPU-accelerated spectrum/waveform/waterfall visualizer."""

    STYLES = ("bars", "waveform", "waterfall", "circular")

    def __init__(self, **kwargs) -> None:
        super().__init__()
        self._style: str = "bars"
        self._color_scheme: str = "neon"
        self._fft_bins: int = 7
        self._glow_intensity: float = 0.5
        self._bg_opacity: float = 0.9
        self._shader_program: int = 0
        self._audio_texture: int = 0
        self._beat_pulse: float = 0.0

    async def _on_gl_ready(self, metadata: TrackMetadata | None) -> None:
        vert_src = self._load_shader("audiovis_vert.glsl")
        frag_src = self._load_shader(f"audiovis_{self._style}.glsl")
        self._shader_program = self._compile_program(vert_src, frag_src)
        self._audio_texture = self._create_audio_texture()

    def _render_gl_frame(self, features: AudioFeatures | None) -> None:
        # Beat pulse decay: 200ms (Req 6 AC 4)
        if features and features.beat:
            self._beat_pulse = 1.0
        else:
            self._beat_pulse = max(0.0, self._beat_pulse - (1.0 / 30) / 0.2)

        if features:
            self._upload_audio_texture(features.fft)

        # Set uniforms: iTime, iBeat, iBPM, iBandEnergy[7], iResolution
        # Draw fullscreen quad
        ...
```

**Shader uniforms (shared across AudioVis styles):**

```glsl
uniform float     iTime;
uniform vec2      iResolution;     // 1280.0, 720.0
uniform float     iBeat;           // 0-1 decaying beat pulse
uniform float     iBPM;
uniform float     iBandEnergy[7];
uniform sampler1D iFFT;            // 512-bin FFT magnitude
uniform int       iFFTBins;        // display bins (7/32/64/128/512)
```

#### 3.5.3 Fosfora Engine (Req 7)

**File:** `bot/video/visualizer_engines/fosfora.py`

GPU particle system using OpenGL transform feedback for physics simulation entirely on the GPU.

```python
class FosforaEngine(GPUEngineBase):
    """GPU particle system driven by audio features.

    Transform feedback for physics, additive blending for rendering.
    """

    MAX_PARTICLES = 10_000

    def __init__(self, **kwargs) -> None:
        super().__init__()
        self._particle_count: int = 5000
        self._gravity: float = 0.5
        self._emission_style: str = "burst"
        self._color_mode: str = "spectrum"
        self._trail_length: float = 0.3
        self._vao: list[int] = [0, 0]   # ping-pong
        self._vbo: list[int] = [0, 0]
        self._transform_program: int = 0
        self._render_program: int = 0
        self._current_buffer: int = 0

    async def _on_gl_ready(self, metadata: TrackMetadata | None) -> None:
        self._transform_program = self._compile_transform_feedback_program(
            "fosfora_physics.vert",
            varyings=["out_position", "out_velocity", "out_lifetime", "out_color"],
        )
        self._render_program = self._compile_program("fosfora_render.vert", "fosfora_render.frag")
        self._allocate_particle_buffers()

    def _render_gl_frame(self, features: AudioFeatures | None) -> None:
        # Physics pass (transform feedback, rasterizer discard)
        # Swap ping-pong buffers
        # Render pass (additive blending point sprites)
        ...
```

**Particle data layout (per-particle VBO):**

```
struct Particle {
    vec3  position;   // 12 bytes
    vec3  velocity;   // 12 bytes
    float lifetime;   // 4 bytes
    vec4  color;      // 16 bytes
};  // Total: 44 bytes/particle × 10,000 = 440 KB
```

#### 3.5.4 Varda Engine (Req 8)

**File:** `bot/video/visualizer_engines/varda.py`

Shadertoy-compatible fragment shader runner with audio-reactive uniforms.

```python
class VardaEngine(GPUEngineBase):
    """Shadertoy-compatible GLSL fragment shader runner."""

    SHADER_DIR = "/app/data/presets/varda"

    def __init__(self, **kwargs) -> None:
        super().__init__()
        self._shader_name: str = "plasma"
        self._color_intensity: float = 1.0
        self._speed: float = 1.0
        self._shader_program: int = 0
        self._audio_texture: int = 0
        self._start_time: float = 0.0
        self._beat_pulse: float = 0.0

    async def _on_gl_ready(self, metadata: TrackMetadata | None) -> None:
        self._start_time = time.monotonic()
        frag_src = self._load_shader_file(self._shader_name)
        program = self._compile_shader_safe(frag_src)
        if program is None:
            # Fallback to default (Req 8 AC 6)
            frag_src = self._load_shader_file("plasma")
            program = self._compile_program(VARDA_VERTEX_SHADER, frag_src)
        self._shader_program = program
        self._audio_texture = self._create_audio_texture_2d()

    def _render_gl_frame(self, features: AudioFeatures | None) -> None:
        elapsed = (time.monotonic() - self._start_time) * self._speed
        if features and features.beat:
            self._beat_pulse = 1.0
        else:
            self._beat_pulse = max(0.0, self._beat_pulse - (1.0 / 30) / 0.3)

        # Set uniforms: iTime, iResolution, iBeat, iBPM, iBandEnergy[7]
        # Upload audio texture (512x2: row 0 waveform, row 1 FFT)
        # Draw fullscreen triangle
        ...

    async def on_track_change(self, metadata: TrackMetadata) -> None:
        # Select new shader, crossfade over 2s (Req 8 AC 4)
        ...
```

**Varda audio texture format (iChannel0):**

```
512×2 RGBA float texture:
  Row 0 (y=0): Waveform (512 samples, R channel, normalized -1..1)
  Row 1 (y=1): FFT spectrum (512 bins, R channel, magnitude 0..1)
```

**Uniform convention (Shadertoy-compatible + audio extensions):**

```glsl
uniform float     iTime;           // elapsed seconds × speed
uniform vec2      iResolution;     // 1280.0, 720.0
uniform sampler2D iChannel0;       // 512×2 audio texture
uniform float     iBeat;           // 0-1 decaying beat pulse
uniform float     iBPM;            // current BPM
uniform float     iBandEnergy[7];  // 7-band energy array
```

**Bundled shaders (Req 16 AC 6):**

```
/app/data/presets/varda/
├── fractal_zoom.glsl      — Mandelbrot zoom driven by bass
├── tunnel.glsl            — Beat-reactive neon tunnel
├── plasma.glsl            — Classic plasma + audio color cycling
├── voronoi_pulse.glsl     — Voronoi with bass-reactive cells
├── raymarched_orbs.glsl   — Floating orbs with beat deformation
├── kaleidoscope.glsl      — Mirror pattern, FFT-driven complexity
├── neon_grid.glsl         — 80s grid with bass ripples
├── star_field.glsl        — 3D stars with bass-accelerated speed
├── liquid_metal.glsl      — Metallic surface, spectrum displacement
└── cosmic_web.glsl        — Neural connections reacting to bands
```

## Data Models

### Engine Configuration Schema

```python
# bot/video/visualizer_engines/config_schema.py

ENGINE_CONFIG_SCHEMAS: dict[str, dict[str, dict]] = {
    "projectm": {
        "preset_category": {"type": "string", "default": "all"},
        "blend_duration": {"type": "float", "min": 1.0, "max": 10.0, "default": 3.0},
        "preset_duration": {"type": "float", "min": 10.0, "max": 300.0, "default": 30.0},
        "brightness": {"type": "float", "min": 0.5, "max": 2.0, "default": 1.0},
        "sensitivity": {"type": "float", "min": 0.5, "max": 2.0, "default": 1.0},
    },
    "audiovis": {
        "style": {"type": "choice", "choices": ["bars", "waveform", "waterfall", "circular"], "default": "bars"},
        "color_scheme": {"type": "string", "default": "neon"},
        "fft_bins": {"type": "choice", "choices": [7, 32, 64, 128, 512], "default": 7},
        "glow_intensity": {"type": "float", "min": 0.0, "max": 1.0, "default": 0.5},
        "background_opacity": {"type": "float", "min": 0.0, "max": 1.0, "default": 0.9},
    },
    "fosfora": {
        "particle_count": {"type": "int", "min": 1000, "max": 10000, "default": 5000},
        "gravity": {"type": "float", "min": 0.0, "max": 2.0, "default": 0.5},
        "emission_style": {"type": "choice", "choices": ["burst", "stream", "rain", "fountain"], "default": "burst"},
        "color_mode": {"type": "choice", "choices": ["spectrum", "mono", "gradient"], "default": "spectrum"},
        "trail_length": {"type": "float", "min": 0.0, "max": 1.0, "default": 0.3},
    },
    "varda": {
        "shader_name": {"type": "string", "default": "plasma"},
        "color_intensity": {"type": "float", "min": 0.5, "max": 2.0, "default": 1.0},
        "speed": {"type": "float", "min": 0.25, "max": 4.0, "default": 1.0},
        "complexity": {"type": "choice", "choices": ["low", "medium", "high"], "default": "medium"},
    },
    "dvd": {
        "speed": {"type": "float", "min": 0.5, "max": 3.0, "default": 1.0},
        "hue_shift": {"type": "bool", "default": True},
        "icon_size": {"type": "int", "min": 10, "max": 30, "default": 15},
    },
}
```

### Guild Settings Storage (JSON)

```json
{
  "123456789": {
    "mode": "restrictive",
    "visualizer_engine": "projectm",
    "visualizer_config": {
      "projectm": { "preset_category": "all", "blend_duration": 3.0, "brightness": 1.0, "sensitivity": 1.0 },
      "audiovis": { "style": "bars", "color_scheme": "neon", "fft_bins": 7, "glow_intensity": 0.5 },
      "fosfora": { "particle_count": 5000, "gravity": 0.5, "emission_style": "burst" },
      "varda": { "shader_name": "plasma", "color_intensity": 1.0, "speed": 1.0 },
      "dvd": { "speed": 1.0, "hue_shift": true, "icon_size": 15 }
    },
    "visualizer_presets": {
      "my-chill-preset": {
        "engine": "projectm",
        "config": { "preset_category": "chill", "blend_duration": 5.0 },
        "description": "Saved 2026-09-01"
      }
    }
  }
}
```

### Factory Presets Data Model (Req 16)

```python
# bot/video/visualizer_engines/factory_presets.py

FACTORY_PRESETS: dict[str, dict] = {
    # projectM (Req 16 AC 3)
    "milkdrop-classic": {"engine": "projectm", "config": {"preset_category": "Classic"}, "factory": True},
    "psychedelic": {"engine": "projectm", "config": {"preset_category": "Abstract", "sensitivity": 1.5}, "factory": True},
    "chill": {"engine": "projectm", "config": {"preset_category": "Fluid Motion", "brightness": 0.8}, "factory": True},
    "trippy": {"engine": "projectm", "config": {"preset_category": "Trippy", "sensitivity": 1.3}, "factory": True},
    "geometric": {"engine": "projectm", "config": {"preset_category": "Geometric"}, "factory": True},
    "space": {"engine": "projectm", "config": {"preset_category": "Space"}, "factory": True},
    "energy": {"engine": "projectm", "config": {"preset_category": "Energy", "sensitivity": 1.5}, "factory": True},
    "minimal": {"engine": "projectm", "config": {"preset_category": "Simple", "brightness": 0.7}, "factory": True},
    # audiovis (Req 16 AC 4)
    "spectrum-bars": {"engine": "audiovis", "config": {"style": "bars", "fft_bins": 7}, "factory": True},
    "full-spectrum": {"engine": "audiovis", "config": {"style": "bars", "fft_bins": 128, "glow_intensity": 0.8}, "factory": True},
    "waveform": {"engine": "audiovis", "config": {"style": "waveform"}, "factory": True},
    "waterfall": {"engine": "audiovis", "config": {"style": "waterfall"}, "factory": True},
    "circular": {"engine": "audiovis", "config": {"style": "circular"}, "factory": True},
    "vinyl": {"engine": "audiovis", "config": {"style": "circular", "color_scheme": "warm"}, "factory": True},
    "neon-city": {"engine": "audiovis", "config": {"style": "bars", "color_scheme": "synthwave"}, "factory": True},
    # fosfora (Req 16 AC 5)
    "stardust": {"engine": "fosfora", "config": {"emission_style": "rain", "gravity": 0.3}, "factory": True},
    "fireworks": {"engine": "fosfora", "config": {"emission_style": "burst", "gravity": 1.0, "particle_count": 8000}, "factory": True},
    "aurora": {"engine": "fosfora", "config": {"emission_style": "stream", "gravity": 0.1}, "factory": True},
    "vortex": {"engine": "fosfora", "config": {"emission_style": "fountain", "gravity": 0.0}, "factory": True},
    "rain": {"engine": "fosfora", "config": {"emission_style": "rain", "gravity": 1.5}, "factory": True},
    "nebula": {"engine": "fosfora", "config": {"emission_style": "stream", "particle_count": 10000}, "factory": True},
    "pulse": {"engine": "fosfora", "config": {"emission_style": "burst", "gravity": 0.0}, "factory": True},
    # varda (Req 16 AC 6)
    "fractal-zoom": {"engine": "varda", "config": {"shader_name": "fractal_zoom"}, "factory": True},
    "tunnel": {"engine": "varda", "config": {"shader_name": "tunnel"}, "factory": True},
    "plasma": {"engine": "varda", "config": {"shader_name": "plasma"}, "factory": True},
    "voronoi-pulse": {"engine": "varda", "config": {"shader_name": "voronoi_pulse"}, "factory": True},
    "raymarched-orbs": {"engine": "varda", "config": {"shader_name": "raymarched_orbs"}, "factory": True},
    "kaleidoscope": {"engine": "varda", "config": {"shader_name": "kaleidoscope"}, "factory": True},
    "neon-grid": {"engine": "varda", "config": {"shader_name": "neon_grid"}, "factory": True},
    "star-field": {"engine": "varda", "config": {"shader_name": "star_field"}, "factory": True},
    "liquid-metal": {"engine": "varda", "config": {"shader_name": "liquid_metal"}, "factory": True},
    "cosmic-web": {"engine": "varda", "config": {"shader_name": "cosmic_web"}, "factory": True},
    # dvd (Req 16 AC 7)
    "classic": {"engine": "dvd", "config": {"speed": 1.0, "hue_shift": True, "icon_size": 15}, "factory": True},
    "fast": {"engine": "dvd", "config": {"speed": 2.5, "hue_shift": True}, "factory": True},
    "slow": {"engine": "dvd", "config": {"speed": 0.5, "hue_shift": True}, "factory": True},
    "no-hue": {"engine": "dvd", "config": {"speed": 1.0, "hue_shift": False}, "factory": True},
}
```

### Discord Command Structure (Req 14, 15, 17)

```
/visualizer engine <engine>                    — Set engine
/visualizer config <engine> <setting> <value>  — Configure parameter
/visualizer settings [engine]                  — Show current config
/visualizer preset save <name>                 — Save current config
/visualizer preset load <name>                 — Load named preset
/visualizer preset list                        — List all presets
/visualizer preset delete <name>               — Delete saved preset
/visualizer projectm list-categories           — List Milkdrop categories
```

## Error Handling

**Satisfies:** Req 11

### GPU Error Recovery Chain

1. **Engine render error** → `VisualizerManager._render_loop()` catches exception → calls `_handle_render_error()`
2. **`_handle_render_error()`** → stops server-rendered resources → releases GPU VF → transitions to ERROR state → falls back to DVD engine (client-side, zero GPU)
3. **EGL context failure** → `EGLContextError` raised during `activate()` → `_start_engine()` catches it → transitions to ERROR state → fallback to DVD
4. **GPU device loss mid-session** → broken pipe / SIGPIPE on ffmpeg stdin → detected within frame timeout (2s) → same recovery as render error
5. **GPU capacity exceeded** → `GPUCapacityExceededError` → manager stays in `IDLE_NO_VIEWERS` → logs "GPU capacity exceeded"

### Exception Isolation (Req 11 AC 4)

The `_render_loop()` in `VisualizerManager` runs as an `asyncio.Task`. Unhandled exceptions are caught by the task wrapper and routed to `_handle_render_error()`. They never propagate to the bot's main event loop.

### GPU Device Loss Detection (Req 11 AC 5)

When the GPU device becomes unavailable mid-session:
- `glReadPixels()` returns an error code → `EGLContextError`
- ffmpeg stdin write gets `BrokenPipeError` (ffmpeg crashed due to QSV device loss)
- The segment watchdog detects no new segments within 5 seconds

All three paths converge to `_handle_render_error()` → DVD fallback.

## Testing Strategy

### Unit Tests

1. **GPUResourceScheduler** — allocation/release/capacity tests (pure logic, no GPU)
2. **Config schema validation** — type checking, range clamping, choice validation
3. **Factory presets** — all presets reference valid engines and valid config keys
4. **Engine registry** — all engines in pool are registered and instantiable

### Integration Tests (require GPU)

1. **EGLHeadlessContext** — create/destroy lifecycle on `/dev/dri/renderD128`
2. **Frame pipeline** — pipe 10 frames through ffmpeg, verify HLS segments appear
3. **Engine render** — each engine produces non-zero RGBA frames for 1 second
4. **AudioFeatures flow** — subscribe engine, feed PCM, verify features arrive

### Mock-Based Tests (CI without GPU)

1. Mock `EGLHeadlessContext` to return blank frames
2. Test VisualizerManager state machine transitions with mocked engine
3. Test preset save/load/delete via guild_settings
4. Test command validation (invalid engine, invalid setting, out-of-range value)

## Correctness Properties

### Property 1: VF Allocation Invariant

`gpu_scheduler.active_sessions <= MAX_VISUALIZER_VFS` always holds. The scheduler never allocates more VF slots than available (7 per node). When capacity is reached, new requests are rejected with `GPUCapacityExceededError`.

**When:** Any call to `GPUResourceScheduler.allocate()`
**Then:** `len(self._allocations) <= self._max_vfs`
**Validates: Requirements 4.1, 4.2, 4.3, 4.5**

### Property 2: Zero GPU When No Viewers

If `viewer_count == 0` for a guild and the debounce period has elapsed, that guild holds zero VF allocations and no EGL context exists. GPU resources are only consumed while viewers are actively connected.

**When:** VisualizerManager state is `IDLE_NO_VIEWERS` or `DISABLED`
**Then:** `gpu_scheduler.is_allocated(guild_id) == False` AND engine `_egl_ctx is None`
**Validates: Requirements 12.1, 12.3, 12.4**

### Property 3: Frame Size Consistency

Every frame yielded by `render_frames()` is exactly `1280 × 720 × 4 = 3,686,400 bytes` of RGBA pixel data. ffmpeg expects exactly this size per frame on stdin.

**When:** `render_frames()` yields a frame
**Then:** `len(frame) == 3_686_400`
**Validates: Requirements 3.1, 13.1**

### Property 4: Graceful Degradation

Any engine failure (GPU error, shader compile failure, device loss) results in fallback to the DVD client-side engine. The bot's main event loop is never disrupted by engine exceptions.

**When:** Engine raises any exception during `render_frames()` or `activate()`
**Then:** VisualizerManager transitions to ERROR state then activates DVD fallback
**Validates: Requirements 11.1, 11.3, 11.4, 11.5**

### Property 5: Config Validation

All `/visualizer config` values pass schema validation before being stored. Invalid types, out-of-range values, and unknown settings are rejected with an error response.

**When:** User issues `/visualizer config <engine> <setting> <value>`
**Then:** Value is validated against `ENGINE_CONFIG_SCHEMAS[engine][setting]` before storage
**Validates: Requirements 14.1, 14.2**

### Property 6: Factory Preset Immutability

Factory presets (marked `"factory": True`) cannot be deleted via `/visualizer preset delete`. They can be overridden by guild-saved presets of the same name, but the factory version persists.

**When:** User issues `/visualizer preset delete <name>` where name matches a factory preset
**Then:** Command responds with error; factory preset remains available
**Validates: Requirements 16.1, 16.2**

### Property 7: Engine Registry Consistency

The set of valid engine choices for users is a superset of registered engine implementations plus the meta-engines "random" and "off".

**Invariant:** `VALID_VISUALIZER_ENGINES ⊇ ENGINE_REGISTRY.keys() ∪ {"random", "off"}`
**Validates: Requirements 10.1, 10.3**

## Requirement Traceability

| Requirement | Components | Key Files |
|-------------|-----------|-----------|
| Req 1 | AudioFeatureBus ↔ Engine wiring | `visualizer_manager.py`, `audio_feature_bus.py` |
| Req 2 | EGL headless context | `egl_context.py` (new) |
| Req 3 | Frame pipeline + HLS | `hls_transcode.py` (extend), `visualizer_manager.py` |
| Req 4 | GPU resource scheduler | `gpu_scheduler.py` (new) |
| Req 5 | projectM engine | `projectm.py` (rewrite) |
| Req 6 | AudioVis engine | `audiovis.py` (rewrite) |
| Req 7 | Fosfora engine | `fosfora.py` (rewrite) |
| Req 8 | Varda engine | `varda.py` (rewrite) |
| Req 9 | Random engine pool | `visualizer_manager.py` (extend) |
| Req 10 | Engine feasibility gate | `guild_settings.py`, `__init__.py` |
| Req 11 | Graceful degradation | `visualizer_manager.py`, `gpu_engine_base.py` |
| Req 12 | Viewer-driven demand | `visualizer_manager.py`, `gpu_scheduler.py` |
| Req 13 | HLS visualizer pipeline | `hls_transcode.py` (extend) |
| Req 14 | Config commands | `cogs/visualizer.py`, `config_schema.py` |
| Req 15 | Preset system | `cogs/visualizer.py`, `factory_presets.py` |
| Req 16 | Factory presets | `factory_presets.py` (new) |
| Req 17 | projectM preset management | `projectm.py`, `cogs/visualizer.py` |

## Dependencies & Container Changes

### New Packages in Bot Dockerfile

```dockerfile
# EGL/GL for headless GPU rendering (Mesa iris already present)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libegl1-mesa-dev \
    libgl1-mesa-dev \
    libgles2-mesa-dev \
    && rm -rf /var/lib/apt/lists/*

# libprojectM 4.x
RUN apt-get update && apt-get install -y --no-install-recommends \
    libprojectm-dev \
    && rm -rf /var/lib/apt/lists/*
```

### New Files

```
bot/video/visualizer_engines/egl_context.py      — EGL headless context
bot/video/visualizer_engines/gpu_engine_base.py   — Shared GPU engine base
bot/video/visualizer_engines/config_schema.py     — Config validation schemas
bot/video/visualizer_engines/factory_presets.py   — Factory preset definitions
bot/video/gpu_scheduler.py                        — SR-IOV VF scheduler
bot/video/visualizer_engines/shaders/             — GLSL shader files
```

### Security Considerations

- **SR-IOV isolation**: Hardware-level memory isolation between guilds via separate VFs
- **No user shaders**: Only bundled presets — eliminates GPU shader bombing attacks
- **Render node only**: EGL surfaceless needs only `/dev/dri/renderD*` (already granted via `supplementalGroups: [26]`)
- **Resource caps**: GPU scheduler hard-limits concurrent sessions to 7; frame timeouts prevent runaway rendering

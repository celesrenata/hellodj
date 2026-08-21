# Design Document: Activity Visualizer

## Overview

This feature implements two interconnected systems for the HelloDJ Discord Activity:

1. **Video Start Rework** — Replaces the current approach where videos start ~10 seconds in (because HLS segments must be pre-generated before playback begins) with a WebSocket-driven countdown protocol. The first connected viewer triggers a 3-2-1 countdown, and all clients begin playback from position 0 simultaneously.

2. **Visualizer System** — When audio is playing but no video session is active, the Activity frontend displays a visualizer. The default is a client-side DVD-style bouncing logo (zero server resources). Server-rendered engines (projectM, vgalizer, etc.) use demand-driven rendering: zero GPU/CPU when no viewers are connected, instant start when viewers appear.

Both systems extend the existing WebSocket Hub, Activity Frontend, and HLS pipeline infrastructure without modifying the Lavalink audio playback path.

### Key Design Decisions

1. **Visualizer Manager is per-guild, in-process** — Follows the same pattern as `ActivityStreamer`. One instance per guild, managed within the bot's asyncio event loop. No separate container needed.

2. **DVD screensaver is entirely client-side** — The default visualizer requires zero server resources. The backend sends a single WebSocket message with the engine type and avatar URL; all rendering happens in the browser via CSS/JS.

3. **Server-rendered engines reuse HLSTranscodePipeline** — Rather than inventing a new delivery mechanism, server-rendered visualizers produce raw frames piped to ffmpeg stdin, which encodes via QSV and outputs HLS segments. The frontend plays them with the same hls.js player used for video.

4. **AudioFeatureBus is subscriber-gated** — Zero processing when no engines need audio data. Reference counting ensures the analysis pipeline starts/stops exactly when needed.

5. **Countdown protocol uses existing WebSocket Hub** — New message types (`countdown`, `ready`, `start`) are added to the existing protocol. No new connection endpoints needed.

6. **Audio independence is absolute** — The visualizer system shares NO mutable state with the Lavalink playback pipeline. A visualizer crash cannot affect audio.

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│                         Discord Client (Activity iframe)                  │
│                                                                          │
│  ┌─────────────────┐  ┌──────────────────┐  ┌────────────────────────┐  │
│  │  hls.js Player  │  │  DVD Screensaver │  │  Countdown Overlay     │  │
│  │  (video + viz)  │  │  (CSS/JS only)   │  │  (3-2-1 animation)    │  │
│  └────────┬────────┘  └────────┬─────────┘  └───────────┬───────────┘  │
│           │                     │                         │              │
│           └─────────────────────┼─────────────────────────┘              │
│                                 │                                        │
│                    ┌────────────┴────────────┐                           │
│                    │  Frontend State Machine  │                           │
│                    │  (mode dispatcher)       │                           │
│                    └────────────┬────────────┘                           │
│                                 │                                        │
│                    ┌────────────┴────────────┐                           │
│                    │  WebSocket Client        │                           │
│                    └────────────┬────────────┘                           │
└─────────────────────────────────┼────────────────────────────────────────┘
                                  │ ws://.../activity/ws/{guild_id}
                                  │
┌─────────────────────────────────┼────────────────────────────────────────┐
│  Bot Pod (hellodj)              │                                        │
│                                 │                                        │
│  ┌──────────────────────────────┴──────────────────────────────────┐    │
│  │                    WebSocket Hub (ws_hub.py)                      │    │
│  │  [Extended: viewer tracking + countdown protocol + viz events]    │    │
│  └───────┬──────────────────┬──────────────────────────┬───────────┘    │
│          │                  │                           │                │
│          ▼                  ▼                           ▼                │
│  ┌───────────────┐  ┌──────────────────┐  ┌───────────────────────┐    │
│  │ Activity      │  │ Visualizer       │  │ player.py             │    │
│  │ Streamer      │  │ Manager          │  │ (audio queue)         │    │
│  │ (video life-  │  │ (per-guild)      │  │                       │    │
│  │  cycle)       │  │                  │  │ [track start/end      │    │
│  │               │  │ State Machine:   │  │  events → viz mgr]    │    │
│  └───────┬───────┘  │ DISABLED         │  └───────────────────────┘    │
│          │          │ IDLE_NO_VIEWERS   │                               │
│          │          │ STARTING          │           ┌──────────────┐    │
│          │          │ ACTIVE            │           │ guild_       │    │
│          │          │ SUSPENDING        │           │ settings.py  │    │
│          │          │ ERROR             │           │              │    │
│          │          └────────┬──────────┘           │ [visualizer_ │    │
│          │                   │                      │  engine key] │    │
│          │                   ▼                      └──────────────┘    │
│          │          ┌────────────────────┐                              │
│          │          │ AudioFeatureBus    │                              │
│          │          │ (subscriber-gated) │                              │
│          │          │                    │                              │
│          │          │ Sources:           │                              │
│          │          │ - voice_recv PCM   │                              │
│          │          │                    │                              │
│          │          │ Outputs:           │                              │
│          │          │ - FFT 1024-sample  │                              │
│          │          │ - Beat detection   │                              │
│          │          │ - BPM estimate     │                              │
│          │          │ - Band energy      │                              │
│          │          └────────────────────┘                              │
│          │                                                              │
│          ▼                                                              │
│  ┌───────────────────────────────────┐                                  │
│  │  HLS Transcode Pipeline           │                                  │
│  │  (ffmpeg + h264_qsv)             │                                  │
│  │                                   │                                  │
│  │  Used by:                         │                                  │
│  │  - ActivityStreamer (video)        │                                  │
│  │  - VisualizerManager (server-     │                                  │
│  │    rendered engines)               │                                  │
│  └───────────────────────────────────┘                                  │
│                                                                          │
└──────────────────────────────────────────────────────────────────────────┘
```

## Components and Interfaces

### WebSocket Countdown Protocol

### Server States (per guild)

| State | Description |
|-------|-------------|
| `WAITING_FOR_VIEWER` | HLS pipeline buffering, no viewer connected yet |
| `COUNTDOWN` | First viewer connected, 3-2-1 countdown active |
| `PLAYING` | All clients playing from position 0 |

### Message Types

| Direction | Type | Payload | Purpose |
|-----------|------|---------|---------|
| Server → Client | `countdown` | `{ type: "countdown", seconds: 3, video_title: "..." }` | Start 3-2-1 animation |
| Client → Server | `ready` | `{ type: "ready" }` | Client finished countdown, ready to play |
| Server → Client | `start` | `{ type: "start", position: 0.0, timestamp: <mono> }` | Begin playback at position 0 |
| Server → Client | `state` | `{ type: "state", playing: true, position: 42.5, ... }` | Late-joiner sync (existing, unchanged) |

### Sequence Diagram — First Viewer

```
    Client A                WebSocket Hub              ActivityStreamer
       │                         │                          │
       │── ws connect ──────────▶│                          │
       │                         │── viewer_count: 0→1 ────▶│
       │                         │                          │
       │                         │◀── state: BUFFERING ─────│
       │                         │    (elapsed < 5s)        │
       │                         │                          │
       │◀── countdown(3) ───────│                          │
       │                         │                          │
       │   [3-2-1 animation]     │                          │
       │                         │                          │
       │── ready ───────────────▶│                          │
       │                         │── mark position 0 ──────▶│
       │                         │                          │
       │◀── start(pos=0) ───────│                          │
       │                         │                          │
       │   [hls.js starts at 0]  │                          │
```

### Sequence Diagram — Late Joiner (elapsed > 5s)

```
    Client B                WebSocket Hub              ActivityStreamer
       │                         │                          │
       │── ws connect ──────────▶│                          │
       │                         │── get elapsed ──────────▶│
       │                         │◀── elapsed: 47.2s ───────│
       │                         │                          │
       │◀── state(pos=47.2) ────│                          │
       │                         │                          │
       │   [hls.js seeks to 47s] │                          │
```

### Sequence Diagram — Multiple Clients During Countdown

```
    Client A         Client B         WebSocket Hub
       │                │                   │
       │── connect ────▶│                   │
       │                │                   │
       │◀── countdown ──┼───────────────────│ (sent to A)
       │                │                   │
       │                │── connect ────────▶│
       │                │                   │
       │                │◀── countdown ─────│ (sent to B, remaining time)
       │                │                   │
       │── ready ──────▶│                   │
       │                │── ready ─────────▶│
       │                │                   │
       │◀── start ──────┼───────────────────│ (broadcast to all)
       │                │◀── start ─────────│
```

The countdown triggers on the FIRST `ready` received. The server does not wait for all clients — late-joiners during countdown get a shorter countdown or immediate `start` if it already fired.

### VisualizerManager Design

### State Machine

```
                          ┌──────────────┐
           video starts   │              │  /visualizer type:off
        ┌────────────────▶│   DISABLED   │◀─────────────────────┐
        │                 │              │                       │
        │                 └──────┬───────┘                       │
        │                        │                               │
        │        video ends OR   │  /visualizer type:<engine>    │
        │        engine set      │                               │
        │                        ▼                               │
        │                 ┌──────────────────┐                   │
        │                 │                  │                   │
        ├────────────────▶│ IDLE_NO_VIEWERS  │◀──────┐           │
        │                 │                  │       │           │
        │                 └────────┬─────────┘       │           │
        │                          │                 │           │
        │           viewer joins + │                 │ debounce  │
        │           audio playing  │                 │ expires   │
        │                          ▼                 │           │
        │                 ┌──────────────────┐       │           │
        │                 │                  │       │           │
        ├────────────────▶│    STARTING      │       │           │
        │                 │                  │       │           │
        │                 └────────┬─────────┘       │           │
        │                          │                 │           │
        │              engine init │                 │           │
        │              completes   │                 │           │
        │                          ▼                 │           │
        │                 ┌──────────────────┐  ┌────┴────────┐ │
        │                 │                  │  │             │ │
        ├────────────────▶│     ACTIVE       │─▶│ SUSPENDING  │ │
        │                 │                  │  │ (2s timer)  │ │
        │                 └──────────────────┘  │             │ │
        │                          ▲            └──────┬──────┘ │
        │                          │                   │        │
        │                          │  viewer rejoins   │        │
        │                          └───────────────────┘        │
        │                                                       │
        │                 ┌──────────────────┐                  │
        │                 │                  │                  │
        └────────────────▶│     ERROR        │──────────────────┘
                          │                  │  auto-recover to DISABLED
                          └──────────────────┘
```

### Per-Guild Instance Lifecycle

```python
class VisualizerManager:
    """Per-guild visualizer state machine and rendering lifecycle."""

    def __init__(self, guild_id: int, ws_hub: WebSocketHub) -> None:
        self.guild_id = guild_id
        self.state = VisualizerState.DISABLED
        self._ws_hub = ws_hub
        self._engine: VisualizerRenderer | None = None
        self._engine_type: str = "dvd"  # loaded from guild_settings
        self._suspend_task: asyncio.Task | None = None
        self._pipeline: HLSTranscodePipeline | None = None
        self._audio_bus: AudioFeatureBus | None = None
        self._track_metadata: dict | None = None

    async def on_viewer_join(self) -> None: ...
    async def on_viewer_leave(self, viewer_count: int) -> None: ...
    async def on_video_start(self) -> None: ...
    async def on_video_end(self) -> None: ...
    async def on_track_change(self, metadata: dict) -> None: ...
    async def set_engine(self, engine_type: str) -> None: ...
    async def shutdown(self) -> None: ...
```

### Integration with WebSocket Viewer Count Events

The WebSocket Hub already tracks `_connections: dict[int, set[WebSocketResponse]]`. We extend it with viewer count change notifications:

```python
# In WebSocketHub, after adding/removing a connection:
viewer_count = len(self._connections.get(guild_id, set()))

if previous_count == 0 and viewer_count == 1:
    # Notify VisualizerManager: first viewer
    await self._on_viewer_count_change(guild_id, 0, 1)
elif previous_count == 1 and viewer_count == 0:
    # Notify VisualizerManager: last viewer left
    await self._on_viewer_count_change(guild_id, 1, 0)
```

### Suspension Debounce Implementation

```python
async def _begin_suspension(self) -> None:
    """Start 2-second debounce before actual suspension."""
    self.state = VisualizerState.SUSPENDING
    self._suspend_task = asyncio.create_task(self._suspension_timer())

async def _suspension_timer(self) -> None:
    """Wait 2s, then re-check viewer count before suspending."""
    await asyncio.sleep(2.0)
    # Re-check: a viewer may have reconnected during the window
    viewer_count = len(self._ws_hub._connections.get(self.guild_id, set()))
    if viewer_count == 0:
        await self._execute_suspension()
    else:
        # Someone reconnected — cancel suspension
        self.state = VisualizerState.ACTIVE

async def _cancel_suspension(self) -> None:
    """Cancel pending suspension and resume ACTIVE state."""
    if self._suspend_task and not self._suspend_task.done():
        self._suspend_task.cancel()
        self._suspend_task = None
    self.state = VisualizerState.ACTIVE
```

### Track Change Metadata Updates While Suspended

When a track changes and the manager is in `IDLE_NO_VIEWERS`:
- Update `_track_metadata` (title, artist, artwork URL, duration)
- Do NOT start any rendering
- When a viewer later connects, the engine receives current metadata immediately on `activate()`

### Visualizer Engine Adapter Interface

```python
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import AsyncIterator


@dataclass
class AudioFeatures:
    """Audio analysis frame from AudioFeatureBus."""
    fft: list[float]          # 512 bins (1024-sample FFT, magnitude only)
    beat: bool                # True if beat detected this frame
    bpm: float                # Current estimated BPM
    band_energy: list[float]  # [sub_bass, bass, low_mid, mid, upper_mid, presence, brilliance]
    timestamp: float          # Monotonic time of this frame


@dataclass
class TrackMetadata:
    """Current track information for visualizer display."""
    title: str
    artist: str
    artwork_url: str | None
    duration_ms: int
    position_ms: int


class VisualizerRenderer(ABC):
    """Abstract base for visualizer engine implementations.

    Engines fall into two categories:
    - Client-side (is_client_side=True): Send config to frontend, all rendering in browser
    - Server-rendered (is_client_side=False): Produce raw frames piped to HLS pipeline
    """

    @abstractmethod
    async def initialize(self, metadata: TrackMetadata | None = None) -> None:
        """One-time setup. Load resources, configure shaders, etc."""
        ...

    @abstractmethod
    async def activate(self, metadata: TrackMetadata | None = None) -> None:
        """Start producing output (frames or client config)."""
        ...

    @abstractmethod
    async def suspend(self) -> None:
        """Pause rendering, release GPU resources. Must be resumable."""
        ...

    @abstractmethod
    async def resume(self, metadata: TrackMetadata | None = None) -> None:
        """Resume from suspended state with current metadata."""
        ...

    @abstractmethod
    async def stop(self) -> None:
        """Full shutdown. Release all resources."""
        ...

    @abstractmethod
    async def on_track_change(self, metadata: TrackMetadata) -> None:
        """Update displayed metadata. Called in any active/suspended state."""
        ...

    @property
    @abstractmethod
    def is_client_side(self) -> bool:
        """True if rendering happens entirely in the browser."""
        ...

    @property
    @abstractmethod
    def consumes_gpu_while_suspended(self) -> bool:
        """True if suspending still holds GPU allocations (e.g., GPU context)."""
        ...

    @property
    @abstractmethod
    def client_config(self) -> dict | None:
        """Config to send to frontend for client-side engines. None for server-rendered."""
        ...

    async def render_frames(self) -> AsyncIterator[bytes]:
        """Yield raw RGBA frames for server-rendered engines.

        Each frame is width*height*4 bytes (RGBA). Frame rate is engine-controlled.
        Only called for server-rendered engines (is_client_side=False).
        """
        raise NotImplementedError("Server-rendered engines must implement render_frames()")
        yield b""  # pragma: no cover
```

### DVD Screensaver (Client-Side Engine)

### Architecture

The DVD screensaver is entirely client-side. The server's role is minimal:
1. VisualizerManager decides DVD should be active
2. Server sends a single WebSocket message with configuration
3. Frontend handles all rendering via CSS/JS

### WebSocket Activation Message

```json
{
  "type": "visualizer",
  "state": "active",
  "engine": "dvd",
  "config": {
    "avatar_url": "https://cdn.discordapp.com/avatars/{bot_id}/{hash}.png?size=128",
    "track": {
      "title": "Song Title",
      "artist": "Artist Name"
    }
  }
}
```

### Frontend Implementation

```javascript
// dvd-screensaver.js — Inline in app.js or separate module

class DVDScreensaver {
  constructor(container, avatarUrl) {
    this.container = container;
    this.logo = document.createElement('img');
    this.logo.src = avatarUrl;
    this.logo.className = 'dvd-logo';
    this.x = Math.random() * (container.clientWidth - 128);
    this.y = Math.random() * (container.clientHeight - 128);
    this.dx = 2;  // pixels per frame (constant velocity)
    this.dy = 2;
    this.hue = 0;
    this.animFrame = null;
  }

  start() {
    this.container.appendChild(this.logo);
    this._animate();
  }

  stop() {
    cancelAnimationFrame(this.animFrame);
    this.logo.remove();
  }

  _animate() {
    const w = this.container.clientWidth - 128;
    const h = this.container.clientHeight - 128;

    this.x += this.dx;
    this.y += this.dy;

    let hitEdge = false;
    if (this.x <= 0 || this.x >= w) { this.dx = -this.dx; hitEdge = true; }
    if (this.y <= 0 || this.y >= h) { this.dy = -this.dy; hitEdge = true; }

    if (hitEdge) {
      this.hue = (this.hue + 60) % 360;
      this.logo.style.filter = `hue-rotate(${this.hue}deg)`;
    }

    this.logo.style.transform = `translate(${this.x}px, ${this.y}px)`;
    this.animFrame = requestAnimationFrame(() => this._animate());
  }
}
```

### CSS

```css
.dvd-logo {
  position: absolute;
  width: 128px;
  height: 128px;
  border-radius: 50%;
  transition: filter 0.2s ease;
  will-change: transform, filter;
  pointer-events: none;
}
```

### Server-Side Engine (Metadata Only)

The `dvd.py` engine on the server is a thin shim that implements `VisualizerRenderer` with `is_client_side = True`. It never renders frames — it only provides the `client_config` dict for the WebSocket message.

```python
class DVDEngine(VisualizerRenderer):
    """DVD screensaver — client-side only, zero server rendering."""

    def __init__(self, bot_avatar_url: str) -> None:
        self._avatar_url = bot_avatar_url
        self._metadata: TrackMetadata | None = None

    async def initialize(self, metadata=None): self._metadata = metadata
    async def activate(self, metadata=None): self._metadata = metadata or self._metadata
    async def suspend(self): pass
    async def resume(self, metadata=None): self._metadata = metadata or self._metadata
    async def stop(self): pass
    async def on_track_change(self, metadata): self._metadata = metadata

    @property
    def is_client_side(self) -> bool: return True

    @property
    def consumes_gpu_while_suspended(self) -> bool: return False

    @property
    def client_config(self) -> dict:
        return {
            "avatar_url": self._avatar_url,
            "track": {
                "title": self._metadata.title if self._metadata else "",
                "artist": self._metadata.artist if self._metadata else "",
            }
        }
```

### Server-Rendered Engine HLS Integration

### Pipeline Architecture

Server-rendered visualizers produce raw video frames that are piped into ffmpeg for QSV-accelerated HLS encoding. This reuses the existing `HLSTranscodePipeline` pattern with a modified input source.

```
┌──────────────┐     raw RGBA frames      ┌──────────────┐     HLS segments
│  Visualizer  │ ─────────────────────────▶│  ffmpeg      │ ──────────────────▶ /tmp/hellodj_hls/
│  Engine      │     (pipe to stdin)       │  -f rawvideo │     viz/{guild_id}/
│  (render     │                           │  -c:v h264_  │     playlist.m3u8
│   loop)      │                           │     qsv      │     seg00000.ts
└──────────────┘                           └──────────────┘
       │
       │ subscribes
       ▼
┌──────────────┐
│ AudioFeature │
│ Bus          │
└──────────────┘
```

### Modified ffmpeg Arguments for Visualizer Input

```python
def build_visualizer_ffmpeg_args(
    self,
    width: int = 1280,
    height: int = 720,
    fps: int = 30,
) -> list[str]:
    """Build ffmpeg args for raw frame input → QSV HLS output."""
    return [
        "ffmpeg", "-hide_banner", "-y",
        # Input: raw RGBA from stdin
        "-f", "rawvideo",
        "-pixel_format", "rgba",
        "-video_size", f"{width}x{height}",
        "-framerate", str(fps),
        "-i", "pipe:0",
        # Hardware upload + QSV encode
        "-vf", "format=nv12,hwupload=extra_hw_frames=64",
        "-c:v", "h264_qsv",
        "-preset", "veryfast",
        "-b:v", "2500k",
        "-maxrate", "3000k",
        "-bufsize", "6000k",
        # HLS output
        "-f", "hls",
        "-hls_time", "2",  # shorter segments for low latency
        "-hls_list_size", "5",  # rolling window (live-like)
        "-hls_flags", "delete_segments+append_list",
        "-hls_segment_filename", str(self._output_dir / "seg%05d.ts"),
        str(self._output_dir / "playlist.m3u8"),
    ]
```

### Pipeline Lifecycle (Demand-Driven)

1. **First viewer connects** → VisualizerManager transitions to STARTING
2. **Engine initializes** → Subscribes to AudioFeatureBus, begins rendering frames
3. **Pipeline starts** → ffmpeg launched, engine frames piped to stdin
4. **First HLS segment ready** → Frontend notified, switches to HLS player
5. **Last viewer disconnects** → 2s debounce starts
6. **Debounce expires** → Engine suspended, pipeline killed, segments deleted
7. **Viewer reconnects during debounce** → Suspension cancelled, continues normally

### Frontend HLS Handoff

When a server-rendered visualizer becomes available:

```json
{
  "type": "visualizer",
  "state": "active",
  "engine": "projectm",
  "hls_ready": true,
  "playlist_url": "/activity/stream/{guild_id}/viz/playlist.m3u8"
}
```

The frontend transitions from its loading state ("Starting visualizer...") to the hls.js player pointed at the visualizer playlist.

### AudioFeatureBus Design

### Architecture

```python
class AudioFeatureBus:
    """Subscriber-gated audio analysis pipeline.

    Performs FFT, beat detection, and BPM estimation on audio data.
    Zero processing when no subscribers are connected.
    """

    def __init__(self, guild_id: int) -> None:
        self.guild_id = guild_id
        self._subscribers: set[Callable[[AudioFeatures], None]] = set()
        self._processing_task: asyncio.Task | None = None
        self._audio_source: AudioSource | None = None
        self._lock = asyncio.Lock()

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    async def subscribe(self, callback: Callable[[AudioFeatures], None]) -> None:
        """Add a subscriber. Starts processing if this is the first."""
        async with self._lock:
            self._subscribers.add(callback)
            if len(self._subscribers) == 1:
                await self._start_processing()

    async def unsubscribe(self, callback: Callable[[AudioFeatures], None]) -> None:
        """Remove a subscriber. Stops processing if this was the last."""
        async with self._lock:
            self._subscribers.discard(callback)
            if len(self._subscribers) == 0:
                await self._stop_processing()

    async def _start_processing(self) -> None:
        """Begin audio analysis pipeline. Must complete within 100ms."""
        self._audio_source = await self._acquire_audio_source()
        self._processing_task = asyncio.create_task(self._analysis_loop())

    async def _stop_processing(self) -> None:
        """Halt audio analysis pipeline. Must complete within 100ms."""
        if self._processing_task:
            self._processing_task.cancel()
            self._processing_task = None
        if self._audio_source:
            await self._audio_source.close()
            self._audio_source = None

    async def _analysis_loop(self) -> None:
        """Main loop: read PCM → compute features → dispatch to subscribers."""
        ...
```

### Data Sources

The AudioFeatureBus needs PCM audio data. Two potential sources, in priority order:

1. **Discord `voice_recv` PCM** (preferred) — The bot already uses `discord.ext.voice_recv` with `PipelineSink` for wake word detection. The same PCM stream can feed the AudioFeatureBus. This captures what's actually being heard in the voice channel.

2. **Lavalink audio passthrough** (if accessible) — Wavelink doesn't expose raw audio. This is a fallback only if voice_recv is insufficient.

### Audio Features Computed

| Feature | Method | Update Rate |
|---------|--------|-------------|
| FFT Spectrum | 1024-sample FFT (numpy/scipy) | Every 1024 samples (~23ms at 48kHz) |
| Beat Detection | Onset detection via spectral flux | Per FFT frame |
| BPM Estimate | Autocorrelation of onset function | Updated every 2s |
| Band Energy | Sum FFT bins in 7 frequency bands | Per FFT frame |

### Frequency Bands

| Band | Range | Use Case |
|------|-------|----------|
| Sub-bass | 20–60 Hz | Kick drum pulse |
| Bass | 60–250 Hz | Bass line energy |
| Low-mid | 250–500 Hz | Warmth/body |
| Mid | 500–2000 Hz | Vocals/leads |
| Upper-mid | 2000–4000 Hz | Presence/attack |
| Presence | 4000–6000 Hz | Clarity/definition |
| Brilliance | 6000–20000 Hz | Air/shimmer |

### Independence Guarantee

The AudioFeatureBus:
- Reads audio data passively (voice_recv already captures it)
- Never writes to the audio output path
- Never calls wavelink/Lavalink APIs
- Runs in a separate asyncio task from audio playback
- A crash in the bus is caught by VisualizerManager and transitions to ERROR state without affecting audio

### Frontend State Machine

### Modes

```
┌─────────────────────────────────────────────────────────────┐
│                   Frontend Mode Dispatcher                    │
├─────────────────────────────────────────────────────────────┤
│                                                              │
│  IDLE            → "No active session" message               │
│  COUNTDOWN       → 3-2-1 countdown animation overlay         │
│  VIDEO_PLAYING   → hls.js player (video HLS stream)          │
│  VISUALIZER_DVD  → CSS/JS bouncing logo                      │
│  VISUALIZER_HLS  → hls.js player (visualizer HLS stream)     │
│                                                              │
└─────────────────────────────────────────────────────────────┘
```

### Transitions (Driven by WebSocket Messages)

```
                              ┌──────────────────┐
                     ws close │                  │ ws connect + no session
                ┌────────────▶│      IDLE        │◀────────────────────┐
                │             │                  │                     │
                │             └────────┬─────────┘                     │
                │                      │                               │
                │       ws: countdown  │         ws: visualizer        │
                │       (video start)  │         (state: active)       │
                │                      ▼                               │
                │             ┌──────────────────┐                     │
                │             │    COUNTDOWN     │                     │
                │             │  (3-2-1 overlay) │                     │
                │             └────────┬─────────┘                     │
                │                      │                               │
                │         countdown    │ done + ws: start              │
                │                      ▼                               │
                │             ┌──────────────────┐                     │
                │             │  VIDEO_PLAYING   │─────────────────────┘
                │             │  (hls.js video)  │   ws: session_end
                │             └────────┬─────────┘
                │                      │
                │         ws: visualizer (state: active, engine: dvd)
                │                      ▼
                │             ┌──────────────────┐
                │             │ VISUALIZER_DVD   │
                │             │ (CSS/JS bounce)  │
                │             └────────┬─────────┘
                │                      │
                │         ws: visualizer (state: active, engine: projectm, hls_ready: true)
                │                      ▼
                │             ┌──────────────────┐
                │             │ VISUALIZER_HLS   │
                │             │ (hls.js viz)     │
                │             └──────────────────┘
                │                      │
                │         ws: visualizer (state: suspended) OR ws: countdown
                └──────────────────────┘
```

### WebSocket Messages That Drive Transitions

```json
// Video session starts (with countdown)
{ "type": "countdown", "seconds": 3, "video_title": "Never Gonna Give You Up" }

// Video playback begins
{ "type": "start", "position": 0.0, "timestamp": 1724180400.0 }

// Visualizer becomes active (DVD)
{ "type": "visualizer", "state": "active", "engine": "dvd", "config": { "avatar_url": "...", "track": {...} } }

// Visualizer becomes active (server-rendered, HLS ready)
{ "type": "visualizer", "state": "active", "engine": "projectm", "hls_ready": true, "playlist_url": "/activity/stream/123/viz/playlist.m3u8" }

// Visualizer becomes active (server-rendered, loading)
{ "type": "visualizer", "state": "starting", "engine": "projectm" }

// Visualizer suspended (all viewers left momentarily, or video starting)
{ "type": "visualizer", "state": "suspended" }

// Session ended
{ "type": "session_end" }

// Track changed (updates DVD metadata or server-rendered overlay)
{ "type": "track_change", "track": { "title": "...", "artist": "...", "artwork_url": "..." } }
```

## Data Models

### GuildVisualizerConfig (Persisted in guild_settings.json)

Stored via `guild_settings.set_setting(guild_id, "visualizer_engine", value)`:

```python
# In guild_settings.json:
{
  "123456789": {
    "mode": "restrictive",
    "visualizer_engine": "dvd"       # "dvd"|"projectm"|"vgalizer"|"varda"|"fosfora"|"audiovis"|"native"|"random"|"off"
  }
}
```

Accessor functions:

```python
# In guild_settings.py (new helpers):

VALID_VISUALIZER_ENGINES = {"dvd", "projectm", "vgalizer", "varda", "fosfora", "audiovis", "native", "random", "off"}
DEFAULT_VISUALIZER_ENGINE = "dvd"

def get_visualizer_engine(guild_id: int) -> str:
    """Return the configured visualizer engine for a guild. Default: 'dvd'."""
    engine = get_setting(guild_id, "visualizer_engine", DEFAULT_VISUALIZER_ENGINE)
    if engine not in VALID_VISUALIZER_ENGINES:
        return DEFAULT_VISUALIZER_ENGINE
    return engine

def set_visualizer_engine(guild_id: int, engine: str) -> None:
    """Set the visualizer engine for a guild and persist."""
    if engine not in VALID_VISUALIZER_ENGINES:
        raise ValueError(f"Invalid engine '{engine}'; must be one of {VALID_VISUALIZER_ENGINES}")
    set_setting(guild_id, "visualizer_engine", engine)
```

### VisualizerState (Runtime, In-Memory)

```python
from enum import Enum

class VisualizerState(Enum):
    """Runtime states for the per-guild VisualizerManager."""
    DISABLED = "disabled"
    IDLE_NO_VIEWERS = "idle_no_viewers"
    STARTING = "starting"
    ACTIVE = "active"
    SUSPENDING = "suspending"
    ERROR = "error"
```

### WebSocket Message Schemas (Visualizer Control)

```python
from dataclasses import dataclass, asdict
from typing import Literal


@dataclass
class VisualizerMessage:
    """Server → Client: visualizer state update."""
    type: Literal["visualizer"] = "visualizer"
    state: str = ""              # "active", "starting", "suspended", "disabled"
    engine: str = ""             # "dvd", "projectm", etc.
    config: dict | None = None   # Client-side engine config (avatar_url, track, etc.)
    hls_ready: bool = False      # True when server-rendered HLS playlist is available
    playlist_url: str = ""       # Relative URL to viz HLS playlist


@dataclass
class CountdownMessage:
    """Server → Client: initiate countdown before video start."""
    type: Literal["countdown"] = "countdown"
    seconds: int = 3
    video_title: str = ""


@dataclass
class StartMessage:
    """Server → Client: begin playback at position."""
    type: Literal["start"] = "start"
    position: float = 0.0
    timestamp: float = 0.0       # Server monotonic timestamp


@dataclass
class ReadyMessage:
    """Client → Server: countdown complete, ready to play."""
    type: Literal["ready"] = "ready"


@dataclass
class TrackChangeMessage:
    """Server → Client: track metadata updated."""
    type: Literal["track_change"] = "track_change"
    track: dict | None = None    # { title, artist, artwork_url, duration_ms }
```

## File Structure

### New Files

```
bot/
├── video/
│   ├── visualizer_manager.py          # VisualizerManager (per-guild state machine)
│   ├── audio_feature_bus.py           # AudioFeatureBus (subscriber-gated analysis)
│   ├── visualizer_engines/
│   │   ├── __init__.py                # Engine registry + factory function
│   │   ├── base.py                    # VisualizerRenderer ABC + AudioFeatures dataclass
│   │   └── dvd.py                     # DVDEngine (client-side, metadata-only server shim)
│   └── activity_frontend/
│       └── dvd-screensaver.js         # (or inline in app.js) client-side DVD animation
├── cogs/
│   └── visualizer.py                  # /visualizer slash command group
└── (existing files modified)
    ├── video/ws_hub.py                # Extended: viewer tracking, countdown protocol, viz events
    ├── video/activity_backend.py      # Extended: viz HLS route (/activity/stream/{gid}/viz/)
    ├── video/activity_streamer.py     # Extended: countdown state, notify viz manager on start/end
    ├── video/activity_frontend/app.js # Extended: frontend state machine, DVD mode, countdown
    ├── video/activity_frontend/style.css  # Extended: DVD screensaver styles, countdown styles
    ├── guild_settings.py              # Extended: visualizer_engine helpers
    └── player.py                      # Extended: track change events → viz manager
```

### Modified Files (Summary of Changes)

| File | Changes |
|------|---------|
| `ws_hub.py` | Add viewer count tracking, countdown protocol handlers, `_on_viewer_count_change` callback, visualizer state broadcast |
| `activity_backend.py` | Add `/activity/stream/{gid}/viz/` route for visualizer HLS, register VisualizerManager reference |
| `activity_streamer.py` | Add `WAITING_FOR_VIEWER`/`COUNTDOWN` sub-states, emit `on_video_start`/`on_video_end` to VisualizerManager |
| `app.js` | Add frontend state machine dispatcher, DVD screensaver class, countdown overlay, visualizer HLS mode |
| `style.css` | Add `.dvd-logo`, `.countdown-overlay`, `.visualizer-loading` styles |
| `guild_settings.py` | Add `VALID_VISUALIZER_ENGINES`, `get_visualizer_engine()`, `set_visualizer_engine()` |
| `player.py` | Emit track change events to VisualizerManager via callback/event |

## Phased Implementation Plan

### Phase 1: DVD Screensaver + WebSocket Countdown Protocol

**Scope:** Client-side only. No GPU usage. Establishes the foundational WebSocket extensions.

**Deliverables:**
- `ws_hub.py` — Viewer count tracking + countdown message flow
- `activity_streamer.py` — Countdown sub-states (WAITING_FOR_VIEWER → COUNTDOWN → PLAYING)
- `app.js` — Frontend state machine, countdown overlay (3-2-1 animation), DVD screensaver class
- `style.css` — Countdown and DVD styles
- `visualizer_engines/base.py` — `VisualizerRenderer` ABC
- `visualizer_engines/dvd.py` — `DVDEngine` (client-side shim)
- `guild_settings.py` — Visualizer engine persistence helpers
- `cogs/visualizer.py` — `/visualizer type:<engine>` slash command

**Dependencies:** None (client-side rendering, existing WebSocket infrastructure)

### Phase 2: VisualizerManager State Machine + Viewer Tracking

**Scope:** Server-side orchestration without actual rendering. Prepares lifecycle management.

**Deliverables:**
- `visualizer_manager.py` — Full state machine (DISABLED → IDLE_NO_VIEWERS → STARTING → ACTIVE → SUSPENDING → ERROR)
- `ws_hub.py` — `_on_viewer_count_change` → VisualizerManager notifications
- `player.py` — Track change event emissions to VisualizerManager
- `activity_streamer.py` — `on_video_start`/`on_video_end` signals to VisualizerManager
- Suspension debounce (2s timer with re-check)
- Integration tests for state machine transitions

**Dependencies:** Phase 1 (WebSocket viewer tracking, engine interface)

### Phase 3: AudioFeatureBus + First Server-Rendered Engine

**Scope:** Full server-rendered visualizer pipeline. GPU resources consumed during active viewing.

**Deliverables:**
- `audio_feature_bus.py` — Subscriber-gated FFT/beat/BPM analysis
- Integration with `voice_recv` PCM source
- Modified `HLSTranscodePipeline` for raw frame stdin input
- First server-rendered engine (projectM wrapper or native Python shader engine)
- `activity_backend.py` — `/activity/stream/{gid}/viz/` route
- `app.js` — VISUALIZER_HLS mode (hls.js pointed at viz playlist)
- End-to-end test: audio playing → viewer connects → visualizer HLS appears

**Dependencies:** Phase 2 (VisualizerManager lifecycle), QSV GPU availability on gremlin nodes

### Phase 4: Additional Engines

**Scope:** Expand engine catalog. Each engine is independent once the base infrastructure exists.

**Deliverables (per engine):**
- `visualizer_engines/vgalizer.py` — vgalizer integration
- `visualizer_engines/varda.py` — Varda shader engine
- `visualizer_engines/fosfora.py` — Fosfora audio visualizer
- `visualizer_engines/audiovis.py` — AudioVis spectrum analyzer
- `visualizer_engines/native.py` — Custom Python shader (numpy/PIL frame gen)
- `random` mode logic in VisualizerManager (pick from available server-rendered engines on track change)

**Dependencies:** Phase 3 (AudioFeatureBus, HLS pipeline for visualizers)

## Error Handling

### Visualizer Engine Failures

| Error | Detection | Response |
|-------|-----------|----------|
| Engine initialization timeout (>5s) | asyncio.timeout in STARTING state | Transition to ERROR, log reason, notify viewers with error message |
| Engine crash during rendering | Exception in render_frames() or process exit | Transition to ERROR, kill ffmpeg pipeline, clean segments, fallback to DVD |
| AudioFeatureBus source unavailable | voice_recv not connected or PCM stream interrupted | Bus emits silence frames (zero-energy features), engines degrade gracefully |
| HLS pipeline ffmpeg crash | Non-zero exit code from ffmpeg subprocess | Kill pipeline, notify VisualizerManager → transition to ERROR → auto-fallback to DVD |
| GPU unavailable for server-rendered engine | QSV init failure or device busy | Log warning, refuse server-rendered engine, suggest DVD as fallback |

### WebSocket / Countdown Failures

| Error | Detection | Response |
|-------|-----------|----------|
| Client sends `ready` but no countdown was active | State check: guild not in COUNTDOWN state | Ignore message, log debug |
| All clients disconnect during countdown | Viewer count drops to 0 in COUNTDOWN state | Cancel countdown, revert to WAITING_FOR_VIEWER |
| WebSocket heartbeat timeout | No pong received within 30s | Close connection, decrement viewer count, trigger suspension check |
| Malformed WebSocket message | JSON parse failure or missing `type` field | Send error response to client, do not disconnect |

### Resource Cleanup Failures

| Error | Detection | Response |
|-------|-----------|----------|
| Visualizer HLS segments not deleted on suspend | Orphaned `/tmp/hellodj_hls/{guild_id}/viz/` after suspension | Startup cleanup task scans for orphaned viz directories |
| Suspension debounce task leaked | Task reference exists but never fires | Watchdog in VisualizerManager checks for stale SUSPENDING state (>10s) and forces transition |
| AudioFeatureBus subscriber leak | subscriber_count > 0 but engine is stopped | Unsubscribe all on engine stop(), defensive cleanup in VisualizerManager |

### Graceful Degradation

- If a server-rendered engine fails, the system falls back to DVD screensaver (always available, zero server resources)
- If the AudioFeatureBus cannot acquire a PCM source, server-rendered engines that require audio features refuse to start; DVD remains functional
- If guild_settings.json is corrupted, `get_visualizer_engine()` returns the default (`dvd`)
- A crash in the VisualizerManager never propagates to the Lavalink audio path (separate asyncio task, no shared mutable state)

## Testing Strategy

### Unit Tests (pytest)

- **VisualizerManager state machine** — Verify all valid transitions, reject invalid ones
- **Suspension debounce** — Mock asyncio.sleep, verify cancel-on-rejoin and execute-on-timeout
- **Guild settings helpers** — `get_visualizer_engine()` / `set_visualizer_engine()` with valid/invalid values
- **AudioFeatureBus subscriber counting** — Subscribe/unsubscribe reference counting, start/stop lifecycle
- **DVD engine client_config** — Verify config dict shape with various metadata states
- **Countdown protocol message construction** — Verify all message dataclasses serialize correctly
- **Viewer count tracking** — Connect/disconnect sequences produce correct counts

### Property-Based Tests (Hypothesis)

Each correctness property implemented as a Hypothesis test with minimum 100 iterations:

- **Property 1**: Generate random elapsed times + connection events → verify countdown vs state message
- **Property 2**: Generate random sequences of events → verify only valid transitions occur
- **Property 3**: Generate random state sequences ending in IDLE_NO_VIEWERS → verify zero resources
- **Property 4**: Generate random subscribe/unsubscribe sequences → verify processing task lifecycle
- **Property 5**: Generate random VisualizerManager failures → verify player.py state unchanged
- **Property 6**: Generate random reconnect timings around 2s boundary → verify debounce behavior
- **Property 7**: Generate random DVD activation sequences → verify zero ffmpeg processes
- **Property 8**: Generate random connect/disconnect/timeout sequences → verify count accuracy
- **Property 9**: Generate random engine values → verify persistence roundtrip
- **Property 10**: Generate random viz states + video start → verify transition to DISABLED

Configuration:
- Library: `hypothesis` (already in project)
- Min iterations: 100 per property (`@settings(max_examples=100)`)

### Integration Tests

- Full countdown flow: mock WebSocket client → countdown → ready → start
- VisualizerManager lifecycle with mock engine: IDLE → viewer joins → STARTING → ACTIVE → viewer leaves → SUSPENDING → IDLE
- AudioFeatureBus with synthetic PCM: subscribe → receive features → unsubscribe → verify stopped
- DVD screensaver activation via WebSocket: verify message shape matches frontend expectations

### Manual / E2E Tests

- DVD screensaver displays correctly in Discord Activity iframe
- Countdown 3-2-1 animation plays before video starts
- Server-rendered visualizer HLS stream appears after engine startup
- Viewer disconnect/reconnect within 2s does not interrupt visualizer
- `/visualizer type:dvd` persists across bot restart

## Correctness Properties

### Property 1: Countdown triggers only on first viewer within 5s

*For any* video session in BUFFERING or STREAMING state with elapsed time < 5 seconds, *when* the first WebSocket client connects, the server SHALL send a `countdown` message. *For any* connection where elapsed time ≥ 5 seconds, the server SHALL send a `state` message with computed position instead.

**Validates: Requirements 1.1, 1.5**

### Property 2: Visualizer state machine valid transitions

*For any* VisualizerManager instance, the state SHALL only transition along the defined edges: DISABLED↔IDLE_NO_VIEWERS, IDLE_NO_VIEWERS→STARTING, STARTING→ACTIVE, ACTIVE→SUSPENDING, SUSPENDING→IDLE_NO_VIEWERS, SUSPENDING→ACTIVE, any→DISABLED (video start), any→ERROR. No other transitions SHALL occur.

**Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 2.8**

### Property 3: Zero resource consumption when idle

*For any* VisualizerManager in IDLE_NO_VIEWERS state, the associated engine SHALL have zero running processes, zero GPU memory allocations, and zero asyncio tasks performing rendering work. The `subscriber_count` of the AudioFeatureBus for that guild SHALL be 0.

**Validates: Requirements 3.1, 3.2, 3.4**

### Property 4: AudioFeatureBus subscriber gating

*For any* AudioFeatureBus instance, *while* `subscriber_count == 0`, the `_processing_task` SHALL be None and no FFT/beat/BPM computation SHALL occur. *When* `subscriber_count` transitions from 0 to 1, processing SHALL start within 100ms. *When* it transitions from 1 to 0, processing SHALL stop within 100ms.

**Validates: Requirements 4.2, 4.3, 4.4**

### Property 5: Audio independence invariant

*For any* state transition of the VisualizerManager (including ERROR), the wavelink Player for that guild SHALL maintain its `is_playing` state unchanged, its current track unchanged, and its position advancing normally. The VisualizerManager SHALL share no mutable state with player.py's `guild_state` dict.

**Validates: Requirements 8.1, 8.2, 8.3, 8.4**

### Property 6: Suspension debounce correctness

*For any* SUSPENDING state entry, *if* a viewer reconnects within 2 seconds, the state SHALL return to ACTIVE without releasing resources. *If* no viewer reconnects within 2 seconds AND the viewer count is still 0 at re-check time, the state SHALL transition to IDLE_NO_VIEWERS and all GPU resources SHALL be released.

**Validates: Requirements 2.5, 2.6, 3.6**

### Property 7: DVD screensaver server resource usage

*While* the configured engine is `dvd` and the VisualizerManager is in ACTIVE state, the server SHALL consume zero GPU resources and zero ffmpeg processes for that guild. All rendering SHALL occur client-side.

**Validates: Requirements 6.1, 6.5**

### Property 8: Viewer count accuracy

*For any* guild, the WebSocket Hub's tracked viewer count SHALL equal the number of live, non-timed-out WebSocket connections for that guild. *When* a connection drops, the count SHALL decrement within 1 second (heartbeat timeout).

**Validates: Requirements 9.1, 9.4**

### Property 9: Configuration persistence

*For any* `/visualizer type:<engine>` invocation with a valid engine value, the engine SHALL be persisted in `guild_settings.json` and SHALL survive bot restarts. *On* bot restart, the VisualizerManager for that guild SHALL initialize with the persisted engine type.

**Validates: Requirements 5.1, 5.6**

### Property 10: Visualizer yields to video

*For any* guild where a video session starts (ActivityStreamer transitions to BUFFERING), the VisualizerManager SHALL transition to DISABLED regardless of its current state, and the frontend SHALL switch to COUNTDOWN or VIDEO_PLAYING mode.

**Validates: Requirements 2.8, 6.6**

# Design Document: Lavalink Audio Pipe

## Overview

This feature creates a Unix FIFO (named pipe) bridge between Lavalink's DSP filter chain and FFmpeg's HLS video transcoding pipeline. Lavalink writes filtered PCM frames to a FIFO; FFmpeg reads them as its audio input. Both video and audio flow through the same FFmpeg muxing pipeline, guaranteeing perfect A/V sync with full filter support on music videos in the Discord Activity.

## Architecture

This feature creates a Unix FIFO (named pipe) bridge between Lavalink's DSP filter chain and FFmpeg's HLS video transcoding pipeline. Lavalink writes filtered PCM frames to a FIFO; FFmpeg reads them as its audio input. Both video and audio flow through the same FFmpeg muxing pipeline, guaranteeing perfect A/V sync with full filter support.

The design spans three codebases:
- **lavaplayer** (Java) — New `PipePcmSink` that tees filtered PCM to a FIFO
- **Lavalink** (Kotlin) — New REST endpoint + player state for audio pipe control
- **hellodj bot** (Python) — Pipe lifecycle, FFmpeg integration, filter coordination

### System Diagram

```
┌─────────────────────────────────────────────────────────────────────┐
│                         Kubernetes Pod                               │
│                                                                     │
│  ┌──────────────────────────┐    ┌───────────────────────────────┐  │
│  │    Lavalink (Java)       │    │      Bot (Python)             │  │
│  │                          │    │                               │  │
│  │  AudioPlayer             │    │  ActivityStreamer              │  │
│  │    ├─ Track decode       │    │    ├─ Creates FIFO pipe       │  │
│  │    ├─ FilterChain        │    │    ├─ Calls Lavalink API      │  │
│  │    │   ├─ EQ             │    │    ├─ Starts FFmpeg           │  │
│  │    │   ├─ Rotation       │    │    │                          │  │
│  │    │   ├─ Tremolo        │    │    │  FFmpeg                  │  │
│  │    │   ├─ Vibrato        │    │    │    ├─ Input 0: video URL │  │
│  │    │   ├─ Distortion     │    │    │    ├─ Input 1: FIFO      │  │
│  │    │   └─ (no timescale) │    │    │    │   (s16le/48k/2ch)   │  │
│  │    │                     │    │    │    ├─ -vf setpts (speed)  │  │
│  │    ├─ Opus → Discord VC  │    │    │    ├─ -af atempo (speed)  │  │
│  │    └─ PCM → FIFO ────────┼────┼────┼────┘                     │  │
│  │         (dual output)    │    │    │    └─ HLS segments out    │  │
│  │                          │    │    │                          │  │
│  └──────────────────────────┘    └────┼──────────────────────────┘  │
│                                       │                             │
│                                       ▼                             │
│                              /tmp/hellodj_hls/                      │
│                              {guild_id}/{session_id}/               │
│                                 ├─ audio.pipe (named FIFO)          │
│                                 ├─ playlist.m3u8                    │
│                                 └─ seg00001.ts ...                  │
└─────────────────────────────────────────────────────────────────────┘
```

### Key Design Decisions

1. **Named FIFO over Unix socket**: FFmpeg reads FIFOs natively as file paths (`-i /path/to/fifo`). Lavalink writes via standard `FileOutputStream`. One-directional (write→read) matches the use case. The blocking behavior naturally rate-limits to real-time.

2. **Timescale exclusion from pipe**: Lavalink builds two filter chains — a full chain (with timescale) for Discord voice, and a pipe chain (without timescale) for FFmpeg. Timescale is applied by FFmpeg using `setpts` (video) + `atempo` (audio), keeping A/V sync perfect.

3. **Dual output**: Lavalink continues sending audio to Discord voice simultaneously. Users in voice chat hear filtered audio; Activity viewers see/hear synced filtered video+audio.

## Components and Interfaces

### Component 1: Lavaplayer PipePcmSink (Java)

**Location:** `lavaplayer/main/src/main/java/com/sedmelluq/discord/lavaplayer/track/playback/PipePcmSink.java`

Receives PCM frames after the filter chain and writes them to a named FIFO.

```java
/**
 * Writes filtered PCM audio frames to a named FIFO for external consumers.
 * Operates alongside the normal Opus encoder — does not replace it.
 *
 * Frame format: signed 16-bit LE, stereo (2ch), 48000 Hz.
 * Each frame: 20ms = 960 samples × 2 channels × 2 bytes = 3840 bytes.
 */
public interface PipePcmSink {
    /** Connect/open the FIFO at the given path. */
    boolean connect(Path fifoPath);

    /** Write a PCM frame. Blocks if reader is behind (natural rate-limiting). */
    void write(byte[] data, int offset, int length);

    /** Flush and close. */
    void close();

    /** @return true if the FIFO is open and writable */
    boolean isConnected();

    /** @return Frames written since connect */
    long getFrameCount();

    /** @return Write errors since connect */
    long getErrorCount();
}
```

**Integration point:** A `TeeAudioFilter` is inserted at the end of the filter pipeline (before the frame buffer) that copies PCM samples to the `PipePcmSink` while passing them through to the normal output.

### Component 2: Lavalink Audio Pipe API (Kotlin)

**Location:** `Lavalink/LavalinkServer/src/main/java/lavalink/server/player/PlayerRestHandler.kt`

New REST endpoints on the existing player resource:

```
POST   /v4/sessions/{sessionId}/players/{guildId}/audiopipe
  Body: { "socketPath": "/tmp/hellodj_hls/123/abc/audio.pipe" }
  Response: AudioPipeStatus

DELETE /v4/sessions/{sessionId}/players/{guildId}/audiopipe
  Response: AudioPipeStatus
```

**LavalinkPlayer modifications:**
- `enableAudioPipe(path)` — Opens the PipePcmSink, installs TeeAudioFilter
- `disableAudioPipe()` — Closes sink, removes TeeAudioFilter
- `getAudioPipeStatus()` — Returns enabled/connected/frameCount/errorCount
- `rebuildPipeFilterChain()` — Called on filter update; builds chain without timescale

**FilterChain extension:**
```kotlin
fun withoutTimescale(): FilterChain  // Returns copy with timescale=null
```

### Component 3: Bot AudioPipeSession (Python)

**Location:** `bot/video/audio_pipe.py` (new module)

```python
class AudioPipeSession:
    """Manages a single FIFO pipe between Lavalink and FFmpeg."""
    
    guild_id: int
    session_id: str
    pipe_path: Path  # /tmp/hellodj_hls/{guild_id}/{session_id}/audio.pipe
    
    async def start() -> bool    # mkfifo, returns True on success
    async def stop() -> None     # unlink FIFO
    
    @property
    def ffmpeg_input_path() -> str  # Path for FFmpeg -i argument
    @property
    def active() -> bool
```

### Component 4: LavalinkPipeClient (Python)

**Location:** `bot/video/lavalink_pipe_client.py` (new module)

```python
class LavalinkPipeClient:
    """REST client for Lavalink audio pipe endpoints."""
    
    async def enable_pipe(guild_id: int, pipe_path: str) -> bool
    async def disable_pipe(guild_id: int) -> bool
    async def get_pipe_status(guild_id: int) -> dict | None
```

### Component 5: HLS Pipeline Audio Pipe Mode (Python)

**Modified:** `bot/video/hls_transcode.py`

`_build_streaming_ffmpeg_args` gains new parameters:

```python
def _build_streaming_ffmpeg_args(
    self, source_url: str, resolution: Resolution, *,
    audio_url: str | None = None,
    audio_pipe_path: str | None = None,   # NEW: path to Lavalink PCM FIFO
    timescale_speed: float = 1.0,          # NEW: FFmpeg-side speed adjustment
) -> list[str]:
```

When `audio_pipe_path` is set:
- Input 1 becomes `-f s16le -ar 48000 -ac 2 -i {pipe_path}`
- Mapping becomes `-map 0:v:0 -map 1:a:0`
- If `timescale_speed != 1.0`: adds `-vf setpts=PTS/{speed}` and `-af atempo={speed}`

### Component 6: ActivityStreamer Pipe Orchestration (Python)

**Modified:** `bot/video/activity_streamer.py`

`_play_source` is extended to:
1. Check if guild has active filters
2. Create `AudioPipeSession` if filters present
3. Call `LavalinkPipeClient.enable_pipe()`
4. Pass pipe path to `HLSTranscodePipeline.start_streaming()`
5. On stop/skip: call `disable_pipe()` and `session.stop()`

## Data Models

### AudioPipeStatus (Lavalink → Bot)

```json
{
  "enabled": true,
  "socketPath": "/tmp/hellodj_hls/123456/session-uuid/audio.pipe",
  "connected": true,
  "frameCount": 15000,
  "errorCount": 0
}
```

Included in player state responses from Lavalink's existing `GET /v4/sessions/{sid}/players/{gid}` endpoint under a new `audioPipe` key.

### AudioPipeRequest (Bot → Lavalink)

```json
{
  "socketPath": "/tmp/hellodj_hls/123456/session-uuid/audio.pipe"
}
```

### PCM Frame Format

| Field | Value |
|-------|-------|
| Encoding | Signed 16-bit little-endian (s16le) |
| Sample rate | 48000 Hz |
| Channels | 2 (stereo) |
| Frame duration | 20ms |
| Frame size | 3840 bytes (960 samples × 2ch × 2 bytes) |
| Byte rate | 192000 bytes/sec |

### Filter Classification

| Category | Filters | Pipe Output | FFmpeg Handling |
|----------|---------|-------------|-----------------|
| Non-timing | EQ, rotation, tremolo, vibrato, distortion, karaoke, low-pass, channel-mix | Applied by Lavalink | None (passthrough) |
| Timing | timescale (speed, pitch, rate) | NOT applied | `setpts=PTS/{speed}` (video), `atempo={speed}` (audio) |

Note: Lavalink's `pitch` parameter (without `speed` change) is NOT a timing filter — it uses a resampler that doesn't alter playback rate. Only the `speed` component of timescale requires FFmpeg handling.

## Correctness Properties

### Property 1: A/V Sync Guarantee
Audio and video are muxed by the same FFmpeg instance from synchronized inputs. The FIFO's blocking behavior ensures Lavalink cannot write faster than FFmpeg consumes, maintaining timeline alignment.
**Validates: Requirement 4.3**

### Property 2: Filter Consistency
Discord voice listeners and Activity viewers hear identical non-timing filter effects because Lavalink applies the same filter chain to both outputs.
**Validates: Requirement 7.2**

### Property 3: Timescale Correctness
When speed=1.25x, FFmpeg scales both video timestamps (`setpts=PTS/1.25`) and audio tempo (`atempo=1.25`) by the same factor. Total playback duration is reduced by the speed factor for both streams equally.
**Validates: Requirement 5.1**

### Property 4: Graceful Degradation
If the pipe fails at any point, the system falls back to normal HLS (source audio, no filters applied to video). Video playback continues without interruption.
**Validates: Requirement 4.4**

### Property 5: Real-time Rate Enforcement
The FIFO naturally enforces real-time delivery. If FFmpeg is faster than real-time (pulling from pipe), it blocks on the read. If Lavalink is faster (writing ahead), it blocks on the write. Both converge to real-time consumption.
**Validates: Requirement 1.2**

### Property 6: Frame Boundary Alignment
Filter changes in Lavalink are applied between 20ms frame boundaries. This prevents mid-sample audio artifacts when filters are added/removed during playback.
**Validates: Requirement 6.2**

## Error Handling

| Scenario | Detection | Recovery |
|----------|-----------|----------|
| FIFO creation fails (permissions, disk) | `mkfifo` raises `OSError` | Skip pipe mode, use normal HLS with source audio |
| Lavalink API enable fails | HTTP non-200 response | Clean up FIFO, fall back to normal HLS |
| Lavalink write blocks > 5s | Lavalink internal watchdog timer | Log warning, close pipe; FFmpeg detects EOF on read |
| FFmpeg pipe read timeout (no data 5s) | FFmpeg `-timeout` / internal stall detection | FFmpeg exits; bot detects process exit, restarts without pipe |
| Filter change during playback (non-timing) | Normal filter update path | Lavalink applies to pipe chain; no restart needed |
| Timescale change during playback | Bot detects `speed` parameter in filter state | Restart HLS pipeline with new `timescale_speed`; broadcast `filter_sync` WS for immediate frontend feedback |
| Track ends during pipe session | Lavalink signals EOF on pipe (close) | FFmpeg reads EOF on audio input; auto-advance creates new pipe for next track |
| Bot crash mid-session | Orphaned FIFO in tmpfs | On startup, glob `/tmp/hellodj_hls/*/*/audio.pipe` and unlink; tmpfs also cleaned on pod restart |
| Lavalink crash mid-session | Pipe read returns EOF to FFmpeg | Bot detects pipeline failure, restarts without pipe (fallback to source audio) |

## Migration Notes

- The `_start_lavalink_audio` / `_stop_lavalink_audio` methods from the previous commit should be replaced by this pipe-based approach
- The `filter_sync` WS message and frontend `playbackRate` adjustment remain useful for immediate visual feedback while the pipeline restarts
- The `lavalink_audio` WS message type and frontend mute logic can be removed (HLS audio is always in sync)
- The mute button changes from the previous commit can be reverted

## Testing Strategy

1. **Unit (Java)**: PipePcmSink write/read through a test FIFO — verify frame format (3840 bytes, s16le), frame count, error handling on closed reader
2. **Unit (Java)**: FilterChain.withoutTimescale() — verify timescale is excluded, all other filters preserved
3. **Unit (Python)**: AudioPipeSession — create/cleanup FIFO, verify path generation
4. **Integration**: Bot creates FIFO → Lavalink API enable → Lavalink writes PCM → FFmpeg reads → verify HLS output contains audio
5. **Sync validation**: Play a video with filters, record HLS output, compare audio onset with video onset (acceptable drift: < 40ms, one PCM frame)
6. **Filter hot-swap**: Apply EQ during video playback via pipe, verify audio changes within 100ms without glitches or pipeline restart
7. **Timescale**: Apply nightcore (1.25x) during video, verify both video duration and audio duration are shortened by 1.25x in output segments
8. **Fallback**: Kill the FIFO mid-stream (rm the file), verify FFmpeg exits gracefully and bot restarts pipeline with source audio
9. **Dual output**: While pipe is active, verify Discord voice channel receives audio with full filters (including timescale) simultaneously
10. **Cleanup**: Crash the bot process, restart, verify orphaned FIFOs are cleaned up on startup

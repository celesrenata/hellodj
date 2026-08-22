# Video Streaming

## Overview

HelloDJ streams video content into Discord voice channels via Discord's Activity (Embedded App) system. Videos are transcoded to HLS using FFmpeg with Intel QSV hardware acceleration and served through an in-process HTTP server.

## Architecture

```
Source (YouTube/Tidal/Upload/URL)
  │
  ▼
Source Resolution (yt-dlp / TidalResolver / URLDownloader)
  │
  ▼
HLS Transcode (FFmpeg 9 + QSV on /dev/dri/renderD128)
  │    Output: /tmp/hellodj_hls/{guild_id}_{channel_id}/
  │            ├── playlist.m3u8
  │            ├── video_0.m3u8 (variant)
  │            └── segment_000.ts, segment_001.ts, ...
  ▼
Activity Backend (aiohttp, port 8090)
  │    Serves: /activity/stream/{gid}/playlist.m3u8
  │            /activity/stream/{gid}/{segment}.ts
  ▼
Discord Activity iframe (hls.js player)
  │
  ▼
WebSocket Hub (real-time sync)
  │    Events: play, pause, seek, skip, now_playing, lyrics, strokes
  ▼
All connected viewers receive synchronized playback
```

## Components

### Source Router (`video/source_router.py`)

Classifies input into source types:
- `SourceType.YOUTUBE` — YouTube URLs
- `SourceType.TIDAL` — Tidal URLs (resolved via `TidalResolver`)
- `SourceType.DIRECT_URL` — Direct video file URLs (.mp4, .webm, etc.)
- `SourceType.UPLOAD` — Discord attachment uploads
- `SourceType.SEARCH` — Text queries (YouTube search)

### Source Resolvers

| Resolver | Purpose |
|----------|---------|
| `YouTubeResolver` | yt-dlp download → local file |
| `TidalResolver` | Tidal API → HLS manifest or CDN URL |
| `URLDownloader` | Direct HTTP download of video files |
| `UploadHandler` | Discord attachment download + validation |
| `MusicVideoResolver` | Multi-source music video search (YouTube + Tidal) |

### HLS Transcoding (`video/hls_transcode.py`)

FFmpeg 9 pipeline with Intel QSV (VA-API via libvpl):

```
Input → decode (h264_qsv/hevc_qsv/auto) → scale_vaapi → encode (h264_qsv)
                                                              │
                                                              ▼
                                                    HLS muxer (segment_time=4s)
```

Key parameters:
- Codec: h264_qsv (hardware) with vaapi fallback
- Resolution: 720p (scale to fit)
- Bitrate: 2.5 Mbps video, 128k audio
- Segment duration: 4 seconds
- GOP size: 48 frames (clean segment boundaries)

GPU probe (`video/gpu_probe.py`) validates:
- `/dev/dri/renderD128` exists
- VA-API driver responds (Intel iHD driver)
- Profiles and entrypoints available

### Activity Backend (`video/activity_backend.py`)

HTTP routes:
| Route | Purpose |
|-------|---------|
| `GET /activity/` | Activity frontend (index.html) |
| `GET /activity/static/{fn}` | JS/CSS assets |
| `GET /activity/status/{gid}` | Session status JSON |
| `GET /activity/stream/{gid}/playlist.m3u8` | HLS master playlist |
| `GET /activity/stream/{gid}/{variant}.m3u8` | HLS variant playlist |
| `GET /activity/stream/{gid}/{seg}.ts` | HLS segments |
| `GET /activity/stream/{gid}/subtitles/{lang}.vtt` | Subtitles |
| `WS /activity/ws/{gid}` | Real-time sync WebSocket |
| `GET /activity/modules/{fn}` | Whiteboard JS modules |
| `GET /activity/stickers/catalog.json` | Sticker catalog |
| `GET /activity/stickers/images/{name}` | Sticker images |
| `POST /activity/clientlog` | Client-side error reporting |

Authentication: Discord Embedded App SDK `instance_id` as bearer token. Format: `i-{launch_id}-gc-{guild_id}-{channel_id}`.

### WebSocket Hub (`video/ws_hub.py`)

Real-time communication for synchronized playback:

**Bot → Clients:**
- `now_playing` — Track metadata + position
- `seek` — Seek to position
- `pause` / `resume` — Playback state
- `lyrics_update` — Synced lyrics lines
- `queue_update` — Queue state change

**Client → Bot:**
- `seek` — User seeks in player
- `pause` / `resume` — User toggles playback
- `stroke` — Whiteboard drawing data
- `stroke_undo` / `stroke_clear` — Whiteboard undo/clear

### Lyrics Service (`video/lyrics_service.py`)

Per-guild lyrics orchestrator:
1. **Resolution chain:** LRU cache (50 entries) → LRCLIB synced → LRCLIB plain + beat timing → Genius plain + beat timing → unavailable
2. **Beat timing** (`video/beat_timing.py`): Converts plain text lyrics to timed format using track BPM
3. **Broadcast:** Sends `lyrics_update` WebSocket events at precise timestamps
4. **Audio independence:** All exceptions caught — lyrics never crash audio playback

Providers:
- `LRCLIBProvider` — Free synced lyrics database (lrclib.net)
- `GeniusProvider` — Genius API for plain text lyrics (requires `genius.access_token`)

### Activity Launcher (`video/activity_launcher.py`)

Creates Discord Activities (embedded iframes) in voice channels:
1. Calls Discord API to create Activity invite
2. Sends Activity link to text channel
3. Registers authentication token for the session

### Session Registry (`video/session_registry.py`)

Tracks active video streaming sessions:
- Keyed by `(guild_id, channel_id)` composite
- Manages `ActivityStreamer` instances
- Grace period (30s) when all viewers leave before stopping

### Sticker Catalog (`video/sticker_catalog.py`)

Provides whiteboard stickers from zip archives in `bot/stickers/`:
- Catalogs images from zip files at startup
- Serves via HTTP for the whiteboard overlay
- Supports PNG/JPG/SVG sticker formats

### Whiteboard Overlay

Collaborative drawing during video playback:
- Tools: pen, line, shapes, eraser, text, stickers
- Real-time sync via WebSocket (stroke broadcast)
- Stroke size and opacity controls
- Undo/redo per-user
- Reset with double-tap confirmation

## Slash Commands

| Command | Description |
|---------|-------------|
| `/play mode:video <url>` | Play a video via Activity |
| `/play mode:music_video <query>` | Find + play a music video |
| Video controls (in Activity) | Play/pause, seek, skip, previous |

## Configuration

| Setting | Purpose |
|---------|---------|
| Intel QSV GPU (`/dev/dri/renderD128`) | Required for hardware transcoding |
| `hls-tmp` volume (2Gi tmpfs) | Segment scratch space |
| Port 8090 | Activity backend HTTP server |
| `privileged: true` securityContext | Device access for GPU |

## HLS Cleanup

`video/hls_cleanup.py` manages temporary files:
- Startup: removes all orphaned HLS directories
- Runtime: cleans up sessions after grace period expires
- Stale files (>24h) cleaned on bot startup

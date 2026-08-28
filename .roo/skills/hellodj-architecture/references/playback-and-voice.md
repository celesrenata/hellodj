# Playback, Voice, and Subsystem Details

## Player architecture (player.py)

- Per-guild state dict (`guild_state`) tracks: queue, current track, player, text_channel, voice_channel, source_provider, repeat_mode, filters, autoplay settings.
- `_resolve_and_play()` handles source resolution:
  - For spotify/tidal: tries direct stream sidecars first (port 8801/8802)
  - Falls back to Lavalink search (LavasRC → YouTube)
  - Source map: youtube→YouTube, youtube_music→YouTubeMusic, soundcloud→SoundCloud, spotify→spsearch, tidal→tidal
- Track retry: `MAX_TRACK_RETRIES=3`, `RETRY_BACKOFF_SECONDS=1.5` (env-overridable)
- Uses wavelink 3.5+ (`wavelink.Playable.search`, `wavelink.Pool`)

## Unified playback system (playback/)

- `session_registry.py` — active sessions per guild:channel
- `orchestrator.py` — multi-instance bot orchestrator (health checks, credential loading)
- `router.py` — PlaybackRouter routes play requests through content classification
- `content_filter.py` — per-guild content filtering
- `user_bans.py` — per-guild user ban management
- `classifier.py` — classifies content type (audio/video/radio)
- `persistence.py` — unified queue persistence (replaces legacy session.json)
- `unified_controls.py` — unified control interface
- `queue_display.py` — queue rendering utilities

## views/unified_remote.py

`UnifiedControlView` — persistent Discord view (`timeout=None`, registered in setup_hook), handles both audio (wavelink) and video (activity streamer) playback. Buttons: Previous, Pause/Resume, Next, Add to Playlist, Block Track. Detects media type and delegates to the correct backend.

## session.py

JSON file persistence (`data/sessions.json`); saves voice/text channel ids, current track, queue, auto_resume, source_provider, repeat_mode, filters, crossfade, tune. Auto-resumes on bot restart when `auto_resume=True`.

## Discord Activity system (video/)

The bot runs a Discord Activity (Embedded App) for video streaming, whiteboard, visualizer, lyrics overlay. Backend runs in the bot container on port 8090.

| Module | Purpose |
|--------|---------|
| `activity_backend.py` | aiohttp server, serves frontend, Activity API endpoints |
| `activity_launcher.py` | Launches Discord Activity sessions via the API |
| `activity_streamer.py` | HLS video transcoding + streaming orchestration |
| `ws_hub.py` | WebSocket hub for real-time state sync (play/pause/seek/whiteboard) |
| `hls_transcode.py` | FFmpeg HLS transcode with QSV hardware acceleration |
| `hls_cleanup.py` | Cleanup of stale HLS segments |
| `source_router.py` | Routes video requests to source-specific resolvers |
| `music_video_resolver.py` | Resolves music video URLs from multiple sources |
| `tidal_resolver.py` | Tidal-specific video resolution |
| `sources.py` | YouTube video downloading (yt-dlp) |
| `session_registry.py` | Tracks active video sessions per guild |
| `stroke_registry.py` | Whiteboard stroke persistence + sync |
| `sticker_catalog.py` | Whiteboard sticker asset management |
| `lyrics_service.py` | Synced lyrics overlay (LRC + Genius providers) |
| `visualizer_manager.py` | Audio visualizer engine management |
| `visualizer_registry.py` | Available visualizer types |
| `audio_feature_bus.py` | Real-time audio features for visualizer |
| `beat_timing.py` | Beat detection for visualizer sync |
| `gpu_probe.py` | Intel GPU capability detection (QSV/VA-API) |

Frontend (`activity_frontend/`): single-page HTML/JS app in Discord's Activity iframe; HLS.js for video, canvas whiteboard (pen/shapes/text/stickers/eraser), WebSocket client, Discord Embedded App SDK.

HLS transcoding: FFmpeg 9 (built from source) with QSV/VA-API; Intel iGPU (device 0300-7d55) via `/dev/dri` hostPath; `supplementalGroups: [26]` + `privileged: true`; segments to tmpfs emptyDir `/tmp/hellodj_hls` (2Gi RAM); `-re` for live streaming.

## Voice pipeline (voice/)

- `wakeword.py` — ONNX wake word model inference (80ms tick loop)
- `audio_pipeline.py` — Opus frame receive, buffering, VAD
- `hybrid_player.py` — wavelink + discord.ext.voice_recv (PipelineSink)
- `stt.py` — speech-to-text (local Whisper or cloud APIs)
- `tts.py` — text-to-speech (Speaches service or cloud)
- `intent.py` — intent classification from transcribed text
- `llm_intent.py` — LLM-based intent (OpenAI-compatible API)
- `query_handler.py` — general query routing (music, news, stocks, time, etc.)
- `voice_commands.py` — voice command execution

External: **Speaches** (TTS) `http://speaches.speaches-service.svc.cluster.local:8000`; **LLM API** configurable (default `https://api.openai.com/v1`, model `gpt-4o-mini`).

## Bot cogs

Music, Playlists, Filters, Autoplay, Admin, AdminPanel (`/hellodj` group), Lyrics, Info, Help, Radio, Voice, Video (Discord Activity/video/visualizer), Playback, Visualizer.

## Bot background tasks

| Task | Interval | What it does |
|------|----------|--------------|
| `_token_refresh_watchdog` | 5 min | Refreshes Tidal token + re-pushes YouTube OAuth+PoToken to Lavalink |
| `_potoken_refresh_task` | 1 hour (configurable) | Fetches fresh poToken from bgutil server, stores in cred DB, pushes to Lavalink |
| `_gateway_health_watchdog` | 30s checks | Detects gateway READY stalls, force-reconnects, escalates to pod restart |
| `_guild_policy_watchdog` | periodic | Re-checks guild authorization as admins join/leave |
| `_orchestrator_health_loop` | 30s | Health checks for multi-instance bot orchestrator |

## LavasRC provider configuration

```yaml
providers:
  - "scsearch:%QUERY%"        # SoundCloud first (most reliable, no auth)
  - "ytsearch:\"%ISRC%\""     # YouTube ISRC lookup (fallback)
  - "ytsearch:%QUERY%"        # YouTube text search (last resort)
```

When YouTube is broken, SoundCloud becomes the ONLY working provider for Spotify/Tidal resolution (unless direct stream sidecars work). Do NOT reorder.

## PoToken server (bgutil-ytdlp-pot-provider)

- Image `brainicism/bgutil-ytdlp-pot-provider:latest`, port 4416
- API `POST /get_pot` with optional `{ "content_binding": "<visitor_data>" }` body
- Response `{ "poToken": "...", "contentBinding": "...", "expiresAt": "..." }`
- Health `GET /ping`
- Bot integration: `fetch_and_push_potoken()` in `bot.py` fetches hourly (`POTOKEN_REFRESH_INTERVAL`) and pushes to Lavalink
- Note: official youtube-source plugin (1.18.2) has NO `remotePot` config key — PoTokens must be static in config or pushed via `POST /youtube` at runtime.

## YouTube plugin config keys (official 1.18.2 + SABR)

```yaml
plugins:
  youtube:
    enabled: true
    clients: [TV, TVHTML5_SIMPLY, ANDROID_VR, MUSIC, WEB]
    clientOptions:
      MUSIC: { playback: false, videoLoading: false }
    oauth:
      enabled: true/false
      skipInitialization: true  # prevents broken empty-token poller at boot
      refreshToken: "..."
    pot:
      token: "..."        # static poToken (also pushable at runtime)
      visitorData: "..."  # static visitorData (also pushable at runtime)
    remoteCipher:
      url: "http://..."
      password: "..."
      userAgent: "..."
```

There is NO `remotePot` key in the official plugin.

# Unified Playback System

## Overview

The unified playback system provides centralized command routing, multi-instance orchestration, and session management across both audio and video playback modes.

## Components

### PlaybackRouter (`playback/router.py`)

Central dispatch for all `/play` commands. Determines the appropriate backend based on content type and session state.

**Flow:**
```
/play command
  │
  ▼
Content Classification (playback/classifier.py)
  │
  ├─ Audio content → _handle_audio_play()
  │     ├─ Check user ban (UserBans)
  │     ├─ Check content filter (ContentFilter)
  │     ├─ Check audio exclusivity (one audio session per channel)
  │     ├─ Assign bot instance (primary preferred for first session)
  │     └─ Delegate to Music Cog internal helpers
  │
  ├─ Video content → _handle_video_play()
  │     ├─ Video never blocked by existing audio (dual-session OK)
  │     └─ Delegate to Video Cog
  │
  └─ Music video → _handle_music_video_play()
        ├─ Enqueue as "music_video" type in audio queue
        └─ When reached: disconnect audio, launch Activity
```

**Features:**
- Playlist URL detection (heuristic patterns)
- Dual-session tie-breaking by `started_at` timestamp
- 5-minute inactivity timeout (no humans in channel)
- Content filter enforcement before playback

### InstanceOrchestrator (`playback/orchestrator.py`)

Manages multiple Discord bot application connections for simultaneous multi-channel music. Works around Discord's one-voice-connection-per-bot limit.

**Design:**
```
Primary Bot (HelloDJ#8609)          ← Owns slash commands, first voice session
  │
  ├─ Secondary Instance #1          ← Voice-only, minimal intents
  ├─ Secondary Instance #2          ← Voice-only, minimal intents
  └─ ... (up to 10 total)
```

**Lifecycle:**
1. `initialize()` — Load credentials (`playback.instance_count`, `instance.<N>.token/app_id/name`)
2. `assign_instance(guild_id, channel_id)` — Pick first available, connect to channel
3. `health_check()` — Periodic latency/readiness verification (10s timeout)
4. `release_instance(guild_id, channel_id)` — Disconnect, mark available (5s deadline)

**Limits:**
- Minimum 2, maximum 10 instances
- Health check timeout: 10s
- Release deadline: 5s
- Unhealthy instances auto-recover on next passing health check

**Credential Store Keys:**
```
playback.instance_count = 3
instance.0.token = "discord-bot-token-..."
instance.0.app_id = "1234567890"
instance.0.name = "HelloDJ Instance #2"
instance.1.token = ...
```

### SessionRegistry (`playback/session_registry.py`)

Unified registry for all active playback sessions (audio + video).

**Key Design:**
- Composite key: `(guild_id, channel_id)` tuple
- Enables multiple simultaneous sessions in different channels of the same guild
- Grace period management: brief disconnects don't kill sessions

**ChannelSession dataclass:**
```python
@dataclass
class ChannelSession:
    guild_id: int
    channel_id: int
    session_type: Literal["audio", "video"]
    started_at: float
    bot_instance_id: str | None  # Which instance owns this
    player: wavelink.Player | None
    streamer: ActivityStreamer | None
    text_channel_id: int | None
    queue: list[dict]
    current: dict | None
    auto_resume: bool = True
```

**Grace Period:**
- When all viewers leave a channel, a grace period starts (configurable timeout)
- If a viewer returns before timeout, grace period cancels — session continues
- If timeout expires, callback fires → cleanup and unregister

### Unified Persistence (`playback/persistence.py`)

Composite-keyed session persistence in `data/sessions.json`.

**Key Format:** `"guild_id:channel_id"` (e.g., `"1501686893765595296:1501688238165721128"`)

**Migration:** On first load, legacy guild_id-only keys are migrated to composite format. Fields added:
- `session_type`: "audio" (legacy default)
- `bot_instance_index`: 0 (primary instance)

**API:**
```python
await save_session(guild_id, channel_id, session_type="audio", ...)
await load_all()  # Returns dict[(guild_id, channel_id), session_data]
await clear_session(guild_id, channel_id)
await mark_suspended(guild_id, channel_id, reason)  # Restore failed
await set_auto_resume(guild_id, channel_id, value)
```

### Content Classifier (`playback/classifier.py`)

Classifies user input into content types:
- `ContentType.AUDIO` — Music, podcasts, audio files
- `ContentType.VIDEO` — Video files, YouTube videos
- `ContentType.MUSIC_VIDEO` — Music videos (queued in audio queue, plays as Activity)

### Content Filter (`playback/content_filter.py`)

Guild-scoped content blocking:
- Rule types: artist, track (URL), domain (glob pattern), keyword (title substring)
- Rules stored per-guild with creator ID and timestamp
- Checked before every playback request

### User Bans (`playback/user_bans.py`)

Per-guild user playback bans:
- Banned users cannot use any playback commands
- Managed via `/hellodj ban/unban` commands
- Stored with banner ID and timestamp

### Queue Display (`playback/queue_display.py`)

Shared queue formatting utilities:
- Paginated queue embeds with prev/next buttons
- Dual-queue embed (audio + video sessions in same guild)
- Track duration and position formatting

## Admin Panel (`cogs/admin_panel.py`)

The `/hellodj` command group exposes:

| Command | Permission | Purpose |
|---------|-----------|---------|
| `/hellodj ping` | None | Latency + Lavalink + instance health |
| `/hellodj status` | None | Active sessions in guild |
| `/hellodj settings` | Manage Guild | Guild config display |
| `/hellodj block artist\|track\|domain\|keyword` | Manage Guild | Content filter |
| `/hellodj block list` | Manage Guild | List filter rules |
| `/hellodj unblock <id>` | Manage Guild | Remove filter rule |
| `/hellodj ban <user>` | Manage Guild | Ban from playback |
| `/hellodj unban <user>` | Manage Guild | Restore access |
| `/hellodj banlist` | Manage Guild | List banned users |
| `/hellodj instances` | Manage Guild | View instance assignments |

## Playback Cog (`cogs/playback.py`)

Registers the unified `/play` slash command that replaces per-cog commands:
- `/play song:<query>` — Search and play audio
- `/play link:<url>` — Play a direct URL
- `/play album:<query>` — Play an album
- `/play playlist:<url>` — Play a playlist
- `/play mode:video <url>` — Play video via Activity
- `/play mode:music_video <query>` — Find and play a music video

Routes all commands through the PlaybackRouter for unified handling.

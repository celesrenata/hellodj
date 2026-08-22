# Playback Engine

## Overview

The playback engine (`player.py`) is the central orchestrator for all audio playback. It manages per-guild state, voice channel connectivity, track resolution, queue advancement, and session persistence.

## Per-Guild State

Each guild has an isolated state dictionary containing:

```python
{
    "queue": [],              # List of {webpage_url, title, type?, query?} entries
    "current": None,          # Currently playing entry
    "player": None,           # wavelink.Player (HybridPlayer)
    "voice_channel": None,    # discord.VoiceChannel
    "text_channel": None,     # discord.TextChannel (bound for embeds)
    "source_provider": "youtube",  # Active source (youtube/spotify/tidal/soundcloud)
    "repeat_mode": "off",     # off/single/queue
    "crossfade_seconds": 0,   # Crossfade duration (0 = disabled)
    "persist_enabled": True,  # Whether to auto-save session
    "filters": {},            # Active audio filters
    "tune_enabled": False,    # Enhanced audio mode
    "autoplay_enabled": False, # Auto-play when queue empties
    "autoplay_genres": [],    # Genre seeds for auto-play
    "video_session": None,    # Active video Activity session (if any)
}
```

## Voice Connection

`connect_player(channel)` handles robust voice connection:

1. Acquire per-guild lock (prevents race with wake word pipeline)
2. Detect stale voice presence → send gateway LEAVE to clear
3. Multi-attempt connection (3 attempts, 30s total budget):
   - Send gateway op-4 IDENTIFY
   - Wait for VOICE_STATE_UPDATE + VOICE_SERVER_UPDATE
   - Establish WebSocket + UDP
4. Store player into state immediately for receive-sink wiring
5. Create HybridPlayer (wavelink + voice_recv for audio input)

### Failure Handling

- Stale `_voice_clients` entries are force-removed
- Gateway LEAVE sent before reconnect to ensure clean state
- ChannelTimeoutException raised if handshake never completes

## Track Resolution

`_resolve_and_play(player, guild_id, entry)` resolves tracks through a priority chain:

```
1. Detect source from URL (spotify.com → spotify, tidal.com → tidal, etc.)
2. Try direct stream resolution (Spotify/Tidal sidecars):
   └─ stream_resolver.resolve_direct_stream(source, url)
      └─ Returns CDN URL → feed to Lavalink as HTTP source
      └─ Inject metadata (title, author, URI, source) via object.__setattr__
3. Fall back to Lavalink search:
   ├─ Tidal: tdsearch:{title} → YouTube fallback
   ├─ YouTube: direct URL or TrackSource.YouTube search
   ├─ Spotify: spsearch:{title} → YouTube fallback
   └─ SoundCloud: TrackSource.SoundCloud search
4. Post-filter results:
   ├─ Prefer tracks whose title+author match search words
   ├─ Prefer explicit/original versions over covers/remixes/live
   └─ Prefer highest quality (bitrate)
5. Play the top result
```

### Retry Logic

On `on_track_exception`, the engine retries the SAME track up to `MAX_TRACK_RETRIES=3` times with `RETRY_BACKOFF_SECONDS=1.5` backoff. This handles YouTube's transient failures without advancing the queue.

## Queue Operations

| Function | Behavior |
|----------|----------|
| `add_track(state, gid, entry)` | Append to queue, auto-start if idle |
| `enqueue_and_start(guild, channel, entries, replace)` | Bulk-load queue, start playback |
| `clear_queue(state)` | Empty the queue |
| `shuffle_queue(state)` | Randomize queue order |
| `remove_from_queue(state, index)` | Remove by position |
| `move_in_queue(state, from, to)` | Reorder queue |
| `get_queue_page(state, page, size)` | Paginated view |
| `set_repeat(state, mode)` | off/single/queue |

## Queue Advancement

`_play_next_from_queue(guild_id)` handles queue progression:

```
1. Check repeat_mode:
   ├─ "single" → re-resolve current track
   └─ "queue" → append current to end, then pop next
2. Pop next entry from queue
3. Check entry type:
   ├─ "video" or "music_video" → _start_video_from_queue()
   └─ audio (default) → _resolve_and_play()
4. If queue empty:
   ├─ autoplay_enabled? → fetch recommendations, enqueue
   └─ Otherwise → persist session, go idle
```

## Crossfade

When `crossfade_seconds > 0`:
1. Before track ends, start fading volume to 0 over `crossfade_seconds`
2. Pre-resolve next track during fade-out
3. Start next track at volume 0, fade to 100 over `crossfade_seconds`
4. Uses Lavalink volume filter (not native crossfade)

## Video Integration

When a video entry is in the queue:
1. `_start_video_from_queue()` disconnects the audio player
2. Launches a Discord Activity via the Video Cog's backend
3. Video plays through HLS in the Activity iframe
4. When video ends, audio resumes from the next queue entry

## Session Persistence

Two persistence systems operate on the same `data/sessions.json`:

1. **Legacy** (`session.py`): Keys are `"guild_id"` strings. Used by `bot.py` resume.
2. **Unified** (`playback/persistence.py`): Keys are `"guild_id:channel_id"` composites. Supports audio + video session types with bot_instance_index.

The unified system migrates legacy keys on first load. `bot.py._resume_sessions()` skips composite keys (handled by the unified system).

## Track Start Callback

A fire-and-forget callback chain fires on every track start:

```
player.py on_track_start
  └─ _on_track_start_callback (set by lyrics_service.register_track_start_callback)
      ├─ Original callback (VisualizerManager) — wrapped in try/except
      └─ LyricsService.on_track_change — wrapped in try/except
```

Audio independence is enforced: no callback exception ever propagates to the audio pipeline.

## Environment Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `HELLODJ_MAX_TRACK_RETRIES` | 3 | Max retry attempts on track exception |
| `HELLODJ_RETRY_BACKOFF_SECONDS` | 1.5 | Sleep between retries |
| `VOICE_CONNECT_TIMEOUT` | 30 | Voice handshake timeout (seconds) |

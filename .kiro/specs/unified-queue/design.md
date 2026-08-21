# Technical Design: Unified Queue

## Overview

This design replaces the dual-queue architecture (audio in `player.py` state, video in `ActivityStreamer.queue`) with a single `UnifiedQueue` class owned by each `ChannelSession`. A new `QueueAdvancer` orchestrates playback transitions, ensuring only one backend is active at a time and items play sequentially regardless of type.

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    PlaybackRouter                             │
│  /play → classify → QueueItem → session.queue.append()       │
│  /skip → advancer.skip()                                     │
│  /stop → advancer.stop() + session.queue.clear()             │
└─────────────────────┬───────────────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────────────────┐
│                   ChannelSession                              │
│  guild_id, channel_id                                        │
│  queue: UnifiedQueue                                         │
│  advancer: QueueAdvancer                                     │
│  current: QueueItem | None                                   │
│  active_backend: "audio" | "video" | None                    │
└─────────────────────┬───────────────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────────────────┐
│                   QueueAdvancer                               │
│  Listens for:                                                │
│    - wavelink track_end event (audio complete)               │
│    - ActivityStreamer playback_complete signal (video done)   │
│  On complete:                                                │
│    1. Tear down current backend if type changes              │
│    2. Pop next from queue                                    │
│    3. Dispatch to matching backend                           │
└──────────┬──────────────────────────────┬───────────────────┘
           │                              │
           ▼                              ▼
┌────────────────────┐      ┌────────────────────────┐
│   Audio Backend    │      │    Video Backend        │
│   (player.py +    │      │    (ActivityStreamer +   │
│    wavelink)       │      │     HLS transcode)      │
│                    │      │                          │
│ connect_player()   │      │ launch Activity          │
│ _resolve_and_play()│      │ resolve video source     │
│ wavelink events    │      │ HLS transcode + serve    │
└────────────────────┘      └────────────────────────┘
```

## Data Models

### QueueItem

```python
@dataclass
class QueueItem:
    """A single entry in the unified queue."""
    id: str                          # UUID for stable identity
    dispatch_type: Literal["audio", "video"]
    title: str
    url: str | None                  # Source URL (Spotify, Tidal, YouTube, etc.)
    requester_id: int                # Discord user ID who queued this
    duration_seconds: float          # Estimated duration (0 if unknown)
    source_metadata: dict            # Provider-specific data (author, album, source_provider, etc.)
    added_at: float                  # time.time() when queued
```

### UnifiedQueue

```python
class UnifiedQueue:
    """Thread-safe (asyncio-safe) ordered queue of QueueItems."""
    
    MAX_CAPACITY = 200
    
    def __init__(self) -> None:
        self._items: list[QueueItem] = []
        self._history: list[QueueItem] = []  # Previously played (for /previous)
    
    def append(self, item: QueueItem) -> int:
        """Add item, return queue position. Raises QueueFullError if at capacity."""
    
    def pop_next(self) -> QueueItem | None:
        """Remove and return the next item, or None if empty."""
    
    def peek_next(self) -> QueueItem | None:
        """Return the next item without removing it."""
    
    def clear(self) -> int:
        """Remove all items, return count removed."""
    
    def shuffle(self) -> None:
        """Randomize order of remaining items."""
    
    def remove(self, index: int) -> QueueItem | None:
        """Remove item at 0-based index."""
    
    def move(self, from_idx: int, to_idx: int) -> bool:
        """Move item from one position to another."""
    
    @property
    def items(self) -> list[QueueItem]:
        """Read-only view of queue contents."""
    
    def __len__(self) -> int: ...
```

### Updated ChannelSession

```python
@dataclass
class ChannelSession:
    guild_id: int
    channel_id: int
    started_at: float
    queue: UnifiedQueue
    current: QueueItem | None = None
    active_backend: Literal["audio", "video"] | None = None
    # Audio state (when active_backend == "audio")
    player: wavelink.Player | None = None
    # Video state (when active_backend == "video")  
    streamer: ActivityStreamer | None = None
    activity_url: str | None = None
    # Lifecycle
    idle_since: float | None = None
    auto_resume: bool = True
```

## QueueAdvancer

The `QueueAdvancer` is the central state machine that manages transitions between items. It's instantiated per-session and holds references to both backends.

```python
class QueueAdvancer:
    """Orchestrates sequential playback from the unified queue."""
    
    def __init__(
        self,
        session: ChannelSession,
        audio_backend: AudioBackendAdapter,
        video_backend: VideoBackendAdapter,
    ) -> None: ...
    
    async def play_immediate(self, item: QueueItem) -> None:
        """Start playing an item immediately (first item or skip-to)."""
    
    async def on_item_complete(self) -> None:
        """Called when current item finishes — advances to next."""
    
    async def skip(self) -> None:
        """Stop current, advance to next."""
    
    async def stop(self) -> None:
        """Stop current, do NOT advance."""
    
    async def _dispatch(self, item: QueueItem) -> None:
        """Route item to the correct backend."""
    
    async def _teardown_backend(self, backend_type: str, timeout: float = 10.0) -> None:
        """Stop the specified backend with timeout."""
```

### State Machine

```
                    ┌──────────┐
         play() ───►│  IDLE    │◄─── stop() / queue empty
                    └────┬─────┘
                         │ dispatch(item)
                         ▼
              ┌────────────────────┐
              │     PLAYING        │
              │ (audio OR video)   │
              └──────┬─────────────┘
                     │
        ┌────────────┼────────────┐
        │ track_end  │ skip()     │ error
        ▼            ▼            ▼
   ┌─────────┐  ┌─────────┐  ┌─────────┐
   │ADVANCING│  │ADVANCING│  │ADVANCING│
   └────┬────┘  └────┬────┘  └────┬────┘
        │             │            │
        └──────┬──────┘            │
               │                   │
               ▼                   ▼
    ┌──────────────────┐   ┌───────────┐
    │ next item exists │   │ skip fail │
    │ → dispatch()     │   │ → IDLE    │
    └──────────────────┘   └───────────┘
```

## Backend Adapters

Thin wrappers that provide a uniform interface for the advancer:

### AudioBackendAdapter

```python
class AudioBackendAdapter:
    """Wraps player.py for the advancer."""
    
    async def play(self, session: ChannelSession, item: QueueItem) -> None:
        """Connect voice + resolve + play via player._resolve_and_play()."""
    
    async def stop(self) -> None:
        """Stop wavelink player."""
    
    def is_playing(self) -> bool:
        """True if wavelink player is actively playing."""
    
    # Event: on_track_end → calls advancer.on_item_complete()
```

### VideoBackendAdapter

```python
class VideoBackendAdapter:
    """Wraps VideoCog/ActivityStreamer for the advancer."""
    
    async def play(self, session: ChannelSession, item: QueueItem) -> None:
        """Resolve video → launch/reuse Activity → start HLS streaming."""
    
    async def stop(self) -> None:
        """Stop ActivityStreamer + close Activity."""
    
    def is_playing(self) -> bool:
        """True if ActivityStreamer is actively streaming."""
    
    # Event: on_video_complete → calls advancer.on_item_complete()
```

## Integration Points

### Event Wiring

1. **Audio completion**: `on_wavelink_track_end` in `bot.py` → check if the guild has a unified session → call `advancer.on_item_complete()`
2. **Video completion**: `ActivityStreamer.playback_complete` callback → call `advancer.on_item_complete()`
3. **Error handling**: Both backends emit errors → advancer treats as completion (skip to next)

### PlaybackRouter Changes

The router's `_start_audio_session` and `_start_video_session` stubs are replaced with:

```python
async def _handle_play(self, interaction, query, guild_id, channel_id, audio_type):
    # 1. Resolve query → QueueItem (classify, search metadata, build item)
    item = await self._resolve_to_queue_item(interaction, query, audio_type)
    
    # 2. Get or create ChannelSession with UnifiedQueue
    session = self._get_or_create_session(guild_id, channel_id)
    
    # 3. If idle (nothing playing), play immediately
    if session.current is None:
        await session.advancer.play_immediate(item)
        # Send "Now Playing" embed
    else:
        # 4. Append to queue, confirm position
        pos = session.queue.append(item)
        # Send "Added to queue (position N)" embed
```

### Persistence

The unified queue serializes to the existing `data/sessions.json` structure:

```json
{
  "1501686893765595296:1501688238165721128": {
    "voice_channel_id": 1501688238165721128,
    "text_channel_id": 111111111,
    "current": {
      "id": "uuid",
      "dispatch_type": "audio",
      "title": "Killshot",
      "url": "https://open.spotify.com/track/...",
      "source_metadata": {"author": "Eminem", "source": "spotify"}
    },
    "queue": [
      {"id": "uuid", "dispatch_type": "video", "title": "Exit Plan", ...},
      {"id": "uuid", "dispatch_type": "audio", "title": "Lose Yourself", ...}
    ],
    "auto_resume": true,
    "source_provider": "spotify"
  }
}
```

## Migration Strategy

1. **Phase 1**: Create `UnifiedQueue`, `QueueItem`, `QueueAdvancer` classes in `bot/playback/`
2. **Phase 2**: Wire advancer into the existing `on_wavelink_track_end` event and `ActivityStreamer` completion callback
3. **Phase 3**: Update `PlaybackRouter._handle_audio_play` and `_handle_video_play` to use `session.queue.append()` + `advancer.play_immediate()` instead of delegating to Music/Video cogs directly
4. **Phase 4**: Update `/queue`, `/clear`, `/shuffle`, `/skip`, `/stop` to operate on the unified queue
5. **Phase 5**: Remove the old `player.py` `state["queue"]` usage and `ActivityStreamer.queue` in favor of the unified queue
6. **Phase 6**: Update persistence to save/restore the unified queue format

## Key Design Decisions

1. **QueueAdvancer per session, not global** — each (guild_id, channel_id) pair gets its own advancer. Multi-channel guilds get independent queues.

2. **Backend teardown before switch** — when transitioning audio→video or vice versa, the current backend is fully stopped (with 10s timeout + force-kill) before the new one starts. No overlap.

3. **Existing player.py resolution preserved** — the AudioBackendAdapter delegates to `player._resolve_and_play()` which already handles direct-stream sidecars, source fallbacks, retry logic, crossfade, etc. We don't rewrite the resolve pipeline.

4. **Existing ActivityStreamer preserved** — the VideoBackendAdapter delegates to the same HLS pipeline (resolve → download → transcode → Activity serve). We don't rewrite the video pipeline.

5. **Graceful degradation** — if a backend fails, the advancer skips to the next item rather than stalling. Failed items are logged but don't block the queue.

6. **Queue display shows type** — 🎵 for audio, 🎬 for video, so users know what's coming.

## Files to Create/Modify

| File | Action | Purpose |
|------|--------|---------|
| `bot/playback/unified_queue.py` | CREATE | `UnifiedQueue` + `QueueItem` classes |
| `bot/playback/queue_advancer.py` | CREATE | `QueueAdvancer` state machine |
| `bot/playback/audio_backend.py` | CREATE | `AudioBackendAdapter` wrapping player.py |
| `bot/playback/video_backend.py` | CREATE | `VideoBackendAdapter` wrapping VideoCog |
| `bot/playback/router.py` | MODIFY | Replace stubs with unified queue logic |
| `bot/playback/session_registry.py` | MODIFY | Update `ChannelSession` to hold `UnifiedQueue` + `QueueAdvancer` |
| `bot/cogs/playback.py` | MODIFY | Wire commands to unified queue operations |
| `bot/bot.py` | MODIFY | Wire `on_wavelink_track_end` to advancer |
| `bot/playback/persistence.py` | MODIFY | Save/restore unified queue format |
| `bot/cogs/video.py` | MODIFY | Wire `ActivityStreamer` completion to advancer |

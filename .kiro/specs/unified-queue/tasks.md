# Implementation Plan: Unified Queue

## Overview

Replace the dual-queue architecture (audio in `player.py` state, video in `ActivityStreamer.queue`) with a single `UnifiedQueue` per `ChannelSession`. Implement the `QueueAdvancer` state machine to orchestrate sequential playback across audio and video backends. Update all user-facing commands to operate on the unified queue.

## Tasks

- [ ] 1. Create core data models and queue implementation
  - [ ] 1.1 Create `bot/playback/unified_queue.py` with `QueueItem` dataclass and `UnifiedQueue` class
    - Define `QueueItem` dataclass with fields: id, dispatch_type, title, url, requester_id, duration_seconds, source_metadata, added_at
    - Define `QueueFullError` exception
    - Implement `UnifiedQueue` with: append (with 200-item capacity check), pop_next, peek_next, clear, shuffle, remove, move, items property, __len__
    - Include `_history` list for previously played items
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6_

  - [ ]* 1.2 Write unit tests for `UnifiedQueue`
    - Test append/pop ordering, capacity enforcement (200 items), shuffle, remove, move
    - Test interleaving audio and video dispatch_types
    - Test clear returns count and empties queue
    - _Requirements: 1.1, 1.2, 1.4, 1.5, 1.6_

- [ ] 2. Create backend adapters
  - [ ] 2.1 Create `bot/playback/audio_backend.py` with `AudioBackendAdapter`
    - Implement `play(session, item)` delegating to existing `player._resolve_and_play()` logic
    - Implement `stop()` to stop wavelink player
    - Implement `is_playing()` checking wavelink player state
    - Store reference to advancer for `on_track_end` callback
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 11.1_

  - [ ] 2.2 Create `bot/playback/video_backend.py` with `VideoBackendAdapter`
    - Implement `play(session, item)` delegating to ActivityStreamer launch/reuse + HLS pipeline
    - Implement `stop()` to stop ActivityStreamer and close Activity
    - Implement `is_playing()` checking ActivityStreamer state
    - Store reference to advancer for `on_video_complete` callback
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 11.1_

- [ ] 3. Create QueueAdvancer state machine
  - [ ] 3.1 Create `bot/playback/queue_advancer.py` with `QueueAdvancer` class
    - Accept session, audio_backend, video_backend in constructor
    - Implement `play_immediate(item)` — dispatch item directly (for first item or skip-to)
    - Implement `on_item_complete()` — pop next from queue, dispatch or enter idle
    - Implement `skip()` — stop current backend, advance to next
    - Implement `stop()` — stop current backend, do NOT advance
    - Implement `_dispatch(item)` — route to correct backend based on dispatch_type
    - Implement `_teardown_backend(backend_type, timeout=10.0)` — stop backend with timeout, force-terminate on timeout
    - Ensure backend exclusivity: teardown current before starting different type
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 11.1, 11.2, 11.3_

  - [ ]* 3.2 Write unit tests for `QueueAdvancer`
    - Test sequential dispatch (audio → audio, video → video, audio → video, video → audio)
    - Test backend teardown on type transition
    - Test skip advances to next item
    - Test stop does not advance
    - Test empty queue leads to idle state
    - Test error/failure skips to next item
    - Test 10s timeout force-termination
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 11.2, 11.3_

- [ ] 4. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 5. Update ChannelSession and SessionRegistry
  - [ ] 5.1 Update `bot/playback/session_registry.py` to integrate UnifiedQueue and QueueAdvancer into ChannelSession
    - Add `queue: UnifiedQueue` field to ChannelSession
    - Add `advancer: QueueAdvancer` field to ChannelSession
    - Add `current: QueueItem | None` field
    - Add `active_backend: Literal["audio", "video"] | None` field
    - Add `idle_since: float | None` for lifecycle tracking
    - Update session creation to instantiate UnifiedQueue and QueueAdvancer with backend adapters
    - _Requirements: 12.1, 12.2, 12.3_

- [ ] 6. Wire advancer into event sources
  - [ ] 6.1 Update `bot/bot.py` to route `on_wavelink_track_end` to the advancer
    - In the existing track_end handler, look up the ChannelSession for the guild
    - Call `session.advancer.on_item_complete()` when track ends
    - Handle case where session doesn't exist (legacy/cleanup)
    - _Requirements: 2.1, 3.3_

  - [ ] 6.2 Update `bot/cogs/video.py` to wire ActivityStreamer completion to the advancer
    - On `playback_complete` callback from ActivityStreamer, call `session.advancer.on_item_complete()`
    - On error/failure, call `session.advancer.on_item_complete()` to skip to next
    - _Requirements: 2.2, 4.3, 4.4_

- [ ] 7. Update PlaybackRouter for unified queue
  - [ ] 7.1 Update `bot/playback/router.py` to use unified queue for play commands
    - Replace `_start_audio_session` / `_start_video_session` stubs with `_handle_play` method
    - Resolve query to QueueItem (classify dispatch_type, build metadata)
    - If session idle (no current item): call `advancer.play_immediate(item)`
    - If session playing: call `session.queue.append(item)` and confirm queue position
    - Handle `QueueFullError` with user-facing message
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5_

  - [ ] 7.2 Update `bot/playback/router.py` to implement skip and stop via advancer
    - `/skip` → call `session.advancer.skip()`, report next item or empty queue
    - `/stop` → call `session.advancer.stop()` + `session.queue.clear()`, set session to idle
    - _Requirements: 6.1, 6.2, 6.3, 7.1, 7.2, 7.3_

- [ ] 8. Update queue management commands
  - [ ] 8.1 Update `bot/cogs/playback.py` for `/queue` display command
    - Show currently playing item with type indicator (🎵 audio, 🎬 video)
    - List remaining QueueItems with position, title, requester, type indicator
    - Paginate when queue has more than 10 items
    - _Requirements: 8.1, 8.2, 8.3_

  - [ ] 8.2 Update `bot/cogs/playback.py` for `/clear` and `/shuffle` commands
    - `/clear` → call `session.queue.clear()`, confirm count removed, current item continues
    - `/shuffle` → call `session.queue.shuffle()`, confirm shuffled, current item continues
    - _Requirements: 9.1, 9.2, 9.3, 10.1, 10.2, 10.3_

- [ ] 9. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 10. Remove legacy queue state
  - [ ] 10.1 Remove old `player.py` `state["queue"]` usage
    - Remove queue-related keys from per-guild state dict
    - Remove any direct queue manipulation in player.py (track advancement, etc.)
    - Ensure `_resolve_and_play()` is still callable by AudioBackendAdapter without guild_state queue
    - _Requirements: 2.3, 3.1_

  - [ ] 10.2 Remove `ActivityStreamer.queue` in favor of unified queue
    - Remove internal queue from ActivityStreamer
    - ActivityStreamer should only handle single-item playback (called by VideoBackendAdapter)
    - Remove any auto-advance logic from ActivityStreamer itself
    - _Requirements: 2.3, 4.1_

- [ ] 11. Update persistence layer
  - [ ] 11.1 Update `bot/playback/persistence.py` to save/restore unified queue format
    - Serialize UnifiedQueue items to JSON (id, dispatch_type, title, url, source_metadata, etc.)
    - Serialize current playing item
    - Save auto_resume flag and source_provider
    - On restore: rebuild UnifiedQueue from saved items, resume from last known position
    - Handle graceful degradation if saved format is old/incompatible
    - _Requirements: 12.1, 12.4_

  - [ ]* 11.2 Write integration tests for persistence round-trip
    - Test save and restore of mixed audio/video queue
    - Test resume from last position after reconnect
    - Test handling of corrupted/missing persistence data
    - _Requirements: 12.4_

- [ ] 12. Session lifecycle and cleanup
  - [ ] 12.1 Implement idle timeout and auto-cleanup in session registry
    - When queue exhausts and no repeat mode, set `idle_since = time.time()`
    - After 5 minutes of idle, auto-deregister session
    - On reconnect within grace period, restore queue state
    - _Requirements: 12.2, 12.3, 12.4_

- [ ] 13. Final checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- Checkpoints ensure incremental validation
- The design preserves existing `player._resolve_and_play()` and `ActivityStreamer` HLS pipelines — backend adapters are thin wrappers
- Backend exclusivity (Req 11) is enforced by QueueAdvancer teardown logic, not by the backends themselves
- The 10-second timeout with force-termination (Req 11.3) prevents stuck backends from blocking the queue

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1"] },
    { "id": 1, "tasks": ["1.2", "2.1", "2.2"] },
    { "id": 2, "tasks": ["3.1"] },
    { "id": 3, "tasks": ["3.2", "5.1"] },
    { "id": 4, "tasks": ["6.1", "6.2"] },
    { "id": 5, "tasks": ["7.1", "7.2"] },
    { "id": 6, "tasks": ["8.1", "8.2"] },
    { "id": 7, "tasks": ["10.1", "10.2"] },
    { "id": 8, "tasks": ["11.1", "12.1"] },
    { "id": 9, "tasks": ["11.2"] }
  ]
}
```

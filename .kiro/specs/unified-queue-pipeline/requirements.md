# Requirements Document

## Introduction

The HelloDJ playback queue is currently manipulated by 12+ unserialized code paths (Discord buttons, Activity WebSocket buttons, auto-advance callbacks, `on_track_end` events, video session-end callbacks, search-play, jump-to navigation, and legacy skip commands). All of these mutate the same per-guild queue state (`queue`, `current`, `history`) without any locking or coordination. Because the bot runs on `asyncio` and these operations `await` across suspension points (network calls to Lavalink, HLS teardown, WebSocket broadcasts), interleaving occurs and produces race conditions:

- Tracks get double-advanced (a track is skipped over entirely)
- "Previous" moves forward instead of backward
- Video entries get silently skipped when audio and video transitions collide
- The queue grows unexpectedly because history pushes fire during concurrent advances

The root cause is the absence of a single serialization point. There is exactly ONE logical playback queue per channel session, and both audio (Lavalink/wavelink) and video (ActivityStreamer) playback are dispatched from that same queue. This feature introduces a per-`(guild_id, channel_id)` mutual-exclusion lock and routes ALL queue advancement and control operations through a single serialized pipeline, so that no two operations can mutate the same queue state concurrently.

The scope is a structural refactor of coordination and state keying. It does NOT change the audio resolution logic, the video transcoding pipeline, or the user-facing control surfaces themselves — those surfaces are re-pointed at the serialized pipeline.

## Glossary

- **Pipeline**: The single serialized code path through which all queue advancement and playback control operations execute. Implemented as an async function or set of functions that acquire the Channel_Lock before mutating Queue_State.
- **Channel_Lock**: An `asyncio.Lock` instance associated with exactly one Channel_Key. Held for the duration of a single queue operation to guarantee mutual exclusion.
- **Channel_Key**: The composite tuple `(guild_id, channel_id)` that identifies a single channel playback session. Matches the existing `CompositeKey` pattern used by the Session_Registry.
- **Queue_State**: The mutable per-channel playback state consisting of the ordered `queue` list, the `current` entry, and the `history` list.
- **Queue_Operation**: A single logical mutation of Queue_State: advance to next, go to previous, jump to an index, enqueue-and-start, auto-advance on track end, or auto-advance on video session end.
- **Control_Surface**: An external caller that requests a Queue_Operation. The nine Control_Surfaces are: Discord embed buttons, Activity WebSocket messages, `on_track_end`, video session-end callback, legacy video skip, unified skip/previous, jump-to navigation, add-track auto-start, and Activity search-play.
- **Audio_Backend**: The wavelink/Lavalink playback path invoked via `_resolve_and_play`.
- **Video_Backend**: The ActivityStreamer playback path invoked via `_start_video_from_queue`.
- **Session_Registry**: The existing `SessionRegistry` keyed by `(guild_id, channel_id)` that tracks active audio and video sessions.
- **Advancement**: The act of moving playback from the `current` entry to a different entry (next, previous, jump, or auto-advance).

## Requirements

### Requirement 1: Per-Channel Serialization Lock

**User Story:** As a bot operator, I want all queue operations for a given channel to be serialized, so that concurrent control actions cannot corrupt the queue state.

#### Acceptance Criteria

1. THE Pipeline SHALL maintain one Channel_Lock per Channel_Key.
2. WHEN a Queue_Operation for a Channel_Key begins, THE Pipeline SHALL acquire the Channel_Lock associated with that Channel_Key before reading or mutating Queue_State.
3. WHEN a Queue_Operation for a Channel_Key completes, THE Pipeline SHALL release the Channel_Lock associated with that Channel_Key.
4. WHILE a Channel_Lock is held for a Channel_Key, THE Pipeline SHALL cause any other Queue_Operation requesting the same Channel_Key to wait until the lock is released.
5. WHERE two Queue_Operations target different Channel_Keys, THE Pipeline SHALL allow them to execute concurrently.
6. IF a Queue_Operation raises an exception while holding a Channel_Lock, THEN THE Pipeline SHALL release the Channel_Lock before propagating or logging the exception.

### Requirement 2: Channel-Scoped Queue State

**User Story:** As a developer, I want queue state keyed by channel rather than by guild, so that the lock and the state it protects share the same identity and multiple channels in one guild do not collide.

#### Acceptance Criteria

1. THE Pipeline SHALL identify Queue_State by Channel_Key.
2. THE Pipeline SHALL identify the Channel_Lock by the same Channel_Key used to identify the Queue_State it protects.
3. WHEN a Queue_Operation resolves a `guild_id` to a Channel_Key, THE Pipeline SHALL use the `channel_id` of the active voice channel session recorded in the Session_Registry.
4. IF no Channel_Key can be resolved for a requested Queue_Operation, THEN THE Pipeline SHALL log a diagnostic message identifying the `guild_id` and SHALL NOT mutate any Queue_State.

### Requirement 3: Single Advancement Path

**User Story:** As a user, I want the "next" action to advance exactly one entry regardless of which control I use, so that tracks are never skipped over or double-advanced.

#### Acceptance Criteria

1. WHEN an advance-to-next Queue_Operation completes for a Channel_Key, THE Pipeline SHALL set `current` to the entry that immediately followed the previous `current` in the Advancement order.
2. WHEN an advance-to-next Queue_Operation removes an entry from the `queue`, THE Pipeline SHALL remove exactly one non-blocked entry per completed operation.
3. WHILE a Channel_Lock is held for an advance operation, THE Pipeline SHALL prevent a concurrent `on_track_end` event from advancing the same Queue_State.
4. WHEN the `queue` is empty at the start of an advance-to-next Queue_Operation, THE Pipeline SHALL invoke the queue-empty handling path and SHALL NOT set `current` to a new entry.

### Requirement 4: Correct Previous Navigation

**User Story:** As a user, I want "previous" to move backward through history, so that I return to the track I just heard rather than skipping ahead.

#### Acceptance Criteria

1. WHEN a previous Queue_Operation completes AND the `history` list is non-empty, THE Pipeline SHALL set `current` to the most recent entry from `history`.
2. WHEN a previous Queue_Operation moves the outgoing `current` entry back into the `queue`, THE Pipeline SHALL place that outgoing entry so that a subsequent advance-to-next Queue_Operation returns to it.
3. IF a previous Queue_Operation is requested AND the `history` list is empty AND playback is active, THEN THE Pipeline SHALL restart the `current` entry from position zero.
4. IF a previous Queue_Operation is requested AND the `history` list is empty AND no playback is active, THEN THE Pipeline SHALL leave Queue_State unchanged.

### Requirement 5: Unified Audio and Video Dispatch

**User Story:** As a user, I want the queue to play both audio and video entries in order, so that mixed queues advance correctly without one backend clobbering the other.

#### Acceptance Criteria

1. WHEN an Advancement selects an entry whose type is `music_video`, THE Pipeline SHALL dispatch that entry to the Video_Backend.
2. WHEN an Advancement selects an entry whose type is not `music_video`, THE Pipeline SHALL dispatch that entry to the Audio_Backend.
3. WHEN an Advancement transitions from an active video session to any successor entry, THE Pipeline SHALL terminate the active video session before dispatching the successor entry.
4. WHILE a video session is active for a Channel_Key, THE Pipeline SHALL prevent the Audio_Backend from starting playback for that Channel_Key until the video session is terminated.
5. WHEN a video session is terminated during an Advancement, THE Pipeline SHALL unregister that session from the Session_Registry before dispatching the successor entry.

### Requirement 6: Control Surface Routing

**User Story:** As a developer, I want every control surface to route through the serialized pipeline, so that no code path can bypass the lock.

#### Acceptance Criteria

1. WHEN a Discord embed button requests a Queue_Operation, THE Pipeline SHALL execute that operation under the Channel_Lock.
2. WHEN an Activity WebSocket message requests skip, previous, play, pause, or seek, THE Pipeline SHALL execute the corresponding Queue_Operation under the Channel_Lock.
3. WHEN the `on_track_end` event requests auto-advance, THE Pipeline SHALL execute the advance-to-next Queue_Operation under the Channel_Lock.
4. WHEN the video session-end callback requests auto-advance, THE Pipeline SHALL execute the advance-to-next Queue_Operation under the Channel_Lock.
5. WHEN the legacy video skip command requests a skip, THE Pipeline SHALL execute the skip Queue_Operation under the Channel_Lock.
6. WHEN the unified skip or unified previous function requests a Queue_Operation, THE Pipeline SHALL execute that operation under the Channel_Lock.
7. WHEN the jump-to navigation function requests a jump to a history or queue index, THE Pipeline SHALL execute that operation under the Channel_Lock.
8. WHEN the add-track auto-start path requests playback while the player is idle, THE Pipeline SHALL execute the start Queue_Operation under the Channel_Lock.
9. WHEN the Activity search-play path requests immediate playback, THE Pipeline SHALL execute that operation under the Channel_Lock.

### Requirement 7: History Integrity Under Concurrency

**User Story:** As a user, I want the queue and history to reflect exactly the tracks I played, so that the queue does not grow unexpectedly from duplicated history pushes.

#### Acceptance Criteria

1. WHEN an Advancement pushes the outgoing `current` entry to `history`, THE Pipeline SHALL push that entry exactly once per completed Advancement.
2. THE Pipeline SHALL cap the `history` list at 50 entries after each push.
3. WHEN a previous Queue_Operation returns to a prior entry, THE Pipeline SHALL NOT push the outgoing entry to `history`.
4. WHILE a Channel_Lock is held for an Advancement, THE Pipeline SHALL prevent a concurrent Queue_Operation from pushing to the same `history` list.

### Requirement 8: Behavior Preservation

**User Story:** As a user, I want existing playback behavior to remain intact after the refactor, so that serialization fixes races without changing what the controls do.

#### Acceptance Criteria

1. WHEN a single Control_Surface requests a Queue_Operation with no concurrent operation in flight, THE Pipeline SHALL produce the same resulting Queue_State as the pre-refactor behavior for that operation.
2. THE Pipeline SHALL preserve the existing repeat modes `single` and `queue` when advancing.
3. WHEN an Advancement selects an entry that is blocked by URL or title keyword, THE Pipeline SHALL skip that entry and advance to the next non-blocked entry within the same Queue_Operation.
4. WHEN the `queue` and any active video session are both exhausted, THE Pipeline SHALL invoke the existing queue-empty handling path.

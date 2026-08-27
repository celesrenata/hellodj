# Video Queue Transitions Bugfix Design

## Overview

Three bugs in the unified playback queue's video transition logic prevent correct behavior during video-to-video skips, video-to-audio transitions, and session-end idle states. The core issue is that `_play_next_from_queue` and the skip handlers in `player.py` / `video.py` do not terminate the current ActivityStreamer session before starting the next entry. Additionally, the frontend `session_end` handler transitions to IDLE (blank screen) instead of the DVD screensaver mode.

The fix is surgical: add session teardown logic at the correct transition points and change the frontend's `session_end` handler to activate VISUALIZER_DVD mode.

## Glossary

- **Bug_Condition (C)**: The condition where a video session is active AND a transition is triggered (skip to next video, skip to audio, or session ends with empty queue)
- **Property (P)**: The current video session is fully terminated before the successor starts; the frontend shows DVD screensaver on session end
- **Preservation**: All non-transition behaviors remain unchanged — internal video queue skips, normal audio skips, idle streamer reuse, video-active guards, `_video_transition` flag blocking
- **ActivityStreamer**: The per-guild video session manager in `bot/video/activity_streamer.py` that handles HLS transcoding lifecycle
- **Unified Queue**: `player.get_state(guild_id)["queue"]` — ordered list of dicts with `type` field (`music_video` or absent for audio)
- **Session Registry**: `video_cog._registry` — tracks active video sessions per guild:channel pair
- **`_is_video_active(guild_id)`**: Guard function that checks session registry AND `state["current"]["type"] == "music_video"`
- **DVD Screensaver (VISUALIZER_DVD)**: Frontend mode showing a bouncing logo animation as the Activity idle state

## Bug Details

### Bug Condition

The bugs manifest when the unified queue advances away from an active video session. The current code either fails to terminate the session (Bugs 1.1, 1.2) or fails to transition the frontend to the correct idle state (Bug 1.3).

**Formal Specification:**
```
FUNCTION isBugCondition(input)
  INPUT: input of type QueueTransitionEvent
  OUTPUT: boolean

  LET session = getActiveVideoSession(input.guild_id)
  LET nextEntry = peekQueue(input.guild_id)

  CASE 1 (video-to-video):
    RETURN session IS NOT NULL
           AND session.is_active
           AND nextEntry.type == "music_video"
           AND input.trigger IN ["skip", "auto_advance"]
           AND NOT session.queue  -- streamer's internal queue is empty

  CASE 2 (video-to-audio):
    RETURN session IS NOT NULL
           AND session.is_active
           AND nextEntry.type != "music_video"
           AND input.trigger IN ["skip", "auto_advance"]

  CASE 3 (session end → idle):
    RETURN session IS NOT NULL
           AND session.state transitions to IDLE
           AND NOT session.queue
           AND peekUnifiedQueue(input.guild_id) is EMPTY
           AND frontend receives "session_end" message
END FUNCTION
```

### Examples

- **Bug 1.1**: User skips while "Bohemian Rhapsody" video plays. Next in unified queue is "Take On Me" (music_video). Expected: old session terminates, new session starts with "Take On Me" video. Actual: Lavalink audio starts for "Take On Me" while the old video keeps playing visually.
- **Bug 1.2**: User skips while a music video plays. Next in unified queue is "Blinding Lights" (audio). Expected: video session fully terminates, then audio starts. Actual: audio starts playing alongside the still-visible video.
- **Bug 1.3**: Video session ends naturally (streamer's queue empty, unified queue empty). Expected: Activity frontend shows DVD bouncing logo. Actual: frontend shows blank/stale IDLE screen.
- **Regression guard**: User skips while a video is playing AND the streamer's internal queue has items → should skip within the streamer (no unified queue involvement).

## Expected Behavior

### Preservation Requirements

**Unchanged Behaviors:**
- Streamer-internal queue skips (`streamer.skip()` when `streamer.queue` has items) must continue to work without involving the unified player queue
- Normal audio-to-audio skips (no active video session) must continue through the Lavalink path unchanged
- Idle streamer reuse (existing streamer with connected clients, `is_active == False`) must continue to reuse the session instead of creating a new one
- `_on_queue_empty` behavior must continue to handle disconnect/idle when both queues are exhausted
- `_video_transition` flag must continue blocking audio playback during video setup transitions

**Scope:**
All inputs that do NOT involve transitioning away from an active video session should be completely unaffected by this fix. This includes:
- Audio-only queue operations
- Video playback within the streamer's internal queue
- Streamer creation and Activity launch for new video entries
- WebSocket state sync messages unrelated to session_end

## Hypothesized Root Cause

Based on the code analysis, the root causes are:

1. **Missing session termination in `_play_next_from_queue`** (Bug 1.1 + 1.2): When `_play_next_from_queue` pops a `music_video` entry, it calls `_start_video_from_queue` which checks for an existing streamer. If one exists and `is_active`, it enqueues to the streamer's internal queue. But when the unified queue skip happens (via the unified remote's Next button or `_on_video_session_end`), the _calling code_ doesn't terminate the existing session before calling `_play_next_from_queue`. The next entry hits the "peek guard" which blocks audio (good) but doesn't terminate the video for video entries either.

2. **`video_skip` in `video.py` only handles the streamer-empty case** (Bug 1.1 + 1.2): When `streamer.skip()` is called and the streamer's queue IS empty, `_stop_internal` fires and calls `_on_video_session_end`. That callback calls `_play_next_from_queue`. But there's a race: the session is already stopping, and `_play_next_from_queue` sees the next entry. If it's a `music_video`, `_start_video_from_queue` tries to reuse the now-stopping streamer, leading to inconsistent state. If it's audio, the `_is_video_active` guard may still return True because the registry hasn't been unregistered yet.

3. **Frontend `session_end` handler goes to IDLE instead of VISUALIZER_DVD** (Bug 1.3): In `app.js`, the `session_end` WebSocket message handler calls `setMode('IDLE')`, which hides all elements including the DVD container. It should transition to `VISUALIZER_DVD` mode to show the bouncing screensaver.

## Correctness Properties

Property 1: Bug Condition - Video Session Terminated Before Successor Starts

_For any_ queue transition where an active video session exists AND the next entry in the unified queue is either a music_video or an audio track, the system SHALL fully terminate the current video session (state → IDLE, pipeline stopped, registry unregistered, `session_end` broadcast) BEFORE starting playback of the next entry.

**Validates: Requirements 2.1, 2.2**

Property 2: Bug Condition - DVD Screensaver on Session End

_For any_ session end event where no successor video is queued (both streamer queue and unified queue empty of music_videos), the Activity frontend SHALL transition to VISUALIZER_DVD mode displaying the DVD bouncing logo screensaver, not IDLE/blank mode.

**Validates: Requirements 2.3**

Property 3: Preservation - Internal Queue and Audio Skips Unchanged

_For any_ input where the bug condition does NOT hold (streamer-internal queue has items, or no video session is active), the system SHALL produce the same behavior as the original code, preserving internal video queue progression and audio-only queue operations.

**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5**

## Fix Implementation

### Changes Required

Assuming our root cause analysis is correct:

**File**: `bot/player.py`

**Function**: `_play_next_from_queue`

**Specific Changes**:
1. **Add video session termination before popping next entry**: After the peek guard that blocks audio during active video, add a NEW check: if a video session is currently active AND the peeked entry is `music_video` (video-to-video), terminate the current session before proceeding. This ensures a clean handoff.
2. **Terminate session before audio starts**: If the peeked entry is audio AND a video session is active, terminate the session (stop + unregister) and THEN allow the audio entry through (remove the early return that currently blocks it).

**Function**: `_on_video_session_end`

**Specific Changes**:
3. **Unregister from session registry before advancing queue**: The callback must unregister the streamer from `video_cog._registry` and `ws_hub` so that `_is_video_active()` returns False before `_play_next_from_queue` runs. Currently the callback only clears `state["current"]` and advances — the registry cleanup happens separately in the `video_skip` handler but NOT in the auto-advance path.

**Function**: `_start_video_from_queue`

**Specific Changes**:
4. **Handle active streamer by terminating, not enqueuing**: When an existing streamer is found and `is_active`, the current code enqueues to the streamer's internal queue. This is correct for the `/video play` command adding to a session, but WRONG for unified queue progression. Add a parameter or check to distinguish "adding to existing session" from "replacing existing session with next unified queue entry". For unified queue transitions, stop the old session and create a new one.

---

**File**: `bot/video/activity_streamer.py`

**Function**: `_stop_internal` / `stop`

**Specific Changes**:
5. **Broadcast `session_end` with DVD transition hint**: The `session_end` message should include a `mode` field (e.g., `"visualizer_dvd"`) so the frontend knows to show the screensaver rather than going blank. Alternatively, send a separate `visualizer` message with `engine: "dvd"` before or after `session_end`.

---

**File**: `bot/video/activity_frontend/app.js`

**Function**: `handleWsMessage` — `case 'session_end'`

**Specific Changes**:
6. **Transition to VISUALIZER_DVD instead of IDLE**: Change `setMode('IDLE')` to `setMode('VISUALIZER_DVD')` and instantiate the DVDScreensaver. Use the bot's avatar URL (available from the status API or included in the `session_end` payload). If a track was playing, pass its info to the screensaver's `trackInfo`.

---

**File**: `bot/cogs/video.py`

**Function**: `video_skip` — the post-skip cleanup block

**Specific Changes**:
7. **Ensure registry unregister happens before `_play_next_from_queue`**: In the "queue was empty, session stopped" branch, the code already unregisters and calls `_play_next_from_queue`. Verify this ordering is correct and doesn't race with the `_on_session_end` callback that also fires from `_stop_internal`.

## Testing Strategy

### Validation Approach

The testing strategy follows a two-phase approach: first, surface counterexamples that demonstrate the bugs on unfixed code, then verify the fix works correctly and preserves existing behavior.

### Exploratory Bug Condition Checking

**Goal**: Surface counterexamples that demonstrate the bugs BEFORE implementing the fix. Confirm or refute the root cause analysis. If we refute, we will need to re-hypothesize.

**Test Plan**: Write integration tests that simulate queue transitions with mocked ActivityStreamer and WebSocket hub. Verify that session termination occurs at the correct points.

**Test Cases**:
1. **Video-to-Video Skip Test**: Set up state with active streamer + next entry as `music_video`. Call `_play_next_from_queue`. Assert old session was terminated (state → IDLE) before new session starts. (Will fail on unfixed code — no termination occurs)
2. **Video-to-Audio Skip Test**: Set up state with active streamer + next entry as audio. Call `_play_next_from_queue`. Assert session was terminated and audio playback started. (Will fail on unfixed code — audio is blocked by peek guard)
3. **Session End DVD Test**: Simulate `_stop_internal` completing with empty queues. Assert `session_end` message triggers DVD mode on frontend. (Will fail on unfixed code — goes to IDLE)
4. **Auto-Advance End Test**: Let streamer's `_auto_advance` complete with empty internal queue and empty unified queue. Assert `_on_video_session_end` fires and frontend shows DVD. (Will fail on unfixed code)

**Expected Counterexamples**:
- Video-to-video: `_start_video_from_queue` enqueues to the existing active streamer instead of replacing it
- Video-to-audio: The peek guard returns early without terminating the session
- Session end: Frontend receives `session_end` and calls `setMode('IDLE')` — blank screen

### Fix Checking

**Goal**: Verify that for all inputs where the bug condition holds, the fixed function produces the expected behavior.

**Pseudocode:**
```
FOR ALL input WHERE isBugCondition(input) DO
  result := executeTransition_fixed(input)
  ASSERT oldSessionTerminated(result)
  ASSERT newPlaybackStartedCorrectly(result)
  ASSERT frontendInCorrectMode(result)
END FOR
```

### Preservation Checking

**Goal**: Verify that for all inputs where the bug condition does NOT hold, the fixed function produces the same result as the original function.

**Pseudocode:**
```
FOR ALL input WHERE NOT isBugCondition(input) DO
  ASSERT executeTransition_original(input) = executeTransition_fixed(input)
END FOR
```

**Testing Approach**: Property-based testing is recommended for preservation checking because:
- It generates many test cases automatically across the input domain (various queue compositions, session states)
- It catches edge cases that manual unit tests might miss (e.g., empty queue + idle streamer + video_transition flag)
- It provides strong guarantees that behavior is unchanged for all non-buggy inputs

**Test Plan**: Observe behavior on UNFIXED code first for normal audio skips, internal video queue skips, and idle streamer reuse, then write property-based tests capturing that behavior.

**Test Cases**:
1. **Internal Queue Skip Preservation**: Generate random streamer states with non-empty internal queues, verify `streamer.skip()` still advances within the streamer without involving `_play_next_from_queue`
2. **Audio-Only Skip Preservation**: Generate random audio-only unified queues, verify `_play_next_from_queue` advances normally via Lavalink without video-related logic
3. **Idle Streamer Reuse Preservation**: Generate states with idle streamer + new music_video entry, verify session reuse path still works
4. **Video Transition Flag Preservation**: Generate states where `_video_transition` is True, verify audio entries are still blocked

### Unit Tests

- Test `_play_next_from_queue` with active video session + next entry is `music_video` → session terminated then new video started
- Test `_play_next_from_queue` with active video session + next entry is audio → session terminated then audio starts
- Test `_on_video_session_end` properly unregisters from session registry before advancing
- Test frontend `session_end` handler transitions to VISUALIZER_DVD
- Test `session_end` message includes avatar URL and track info for DVD screensaver

### Property-Based Tests

- Generate random unified queue states (mix of `music_video` and audio entries) with random session states, verify transition correctness
- Generate random non-video states and verify `_play_next_from_queue` behavior is unchanged from baseline
- Generate random session lifecycle events and verify the DVD screensaver always shows on session end without successor

### Integration Tests

- Full video-to-video flow: enqueue two music_videos, skip first, verify second plays in Activity
- Full video-to-audio flow: enqueue music_video then audio track, skip video, verify audio plays via Lavalink after session teardown
- Full session-end flow: play single music_video, let it complete, verify Activity shows DVD screensaver
- Unified remote Next button: verify it properly delegates to the correct skip path

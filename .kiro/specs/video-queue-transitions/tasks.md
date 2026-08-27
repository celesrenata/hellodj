# Implementation Plan

## Overview

This plan fixes three related bugs in the unified playback queue's video transition logic: (1) video session not terminated on video-to-video skip, (2) video session not terminated on video-to-audio skip, and (3) frontend showing blank IDLE screen instead of DVD screensaver on session end. The fix follows the exploratory bugfix workflow: write tests to confirm bugs exist, write preservation tests, implement the fix, then verify.

## Tasks

- [ ] 1. Write bug condition exploration test
  - **Property 1: Bug Condition** - Video Session Not Terminated on Queue Transition
  - **IMPORTANT**: Write this property-based test BEFORE implementing the fix
  - **CRITICAL**: This test MUST FAIL on unfixed code - failure confirms the bug exists
  - **DO NOT attempt to fix the test or the code when it fails**
  - **NOTE**: This test encodes the expected behavior - it will validate the fix when it passes after implementation
  - **GOAL**: Surface counterexamples that demonstrate the bug exists
  - **Scoped PBT Approach**: Scope the property to these concrete failing cases:
    - Case 1 (video-to-video): Active streamer with `is_active=True`, streamer internal queue empty, next unified queue entry has `type="music_video"`, trigger is skip/auto_advance → assert old session terminated before new session starts
    - Case 2 (video-to-audio): Active streamer with `is_active=True`, next unified queue entry is audio (no `type` field or `type != "music_video"`), trigger is skip → assert session terminated and `_is_video_active()` returns False before audio starts
    - Case 3 (session end → DVD): Session transitions to idle, both streamer queue and unified queue empty, frontend receives `session_end` → assert frontend transitions to `VISUALIZER_DVD` mode (not `IDLE`)
  - Test file: `tests/test_video_queue_transitions_bug.py`
  - Mock `ActivityStreamer`, `video_cog._registry`, `ws_hub`, and the frontend WebSocket message handler
  - For Cases 1 & 2: call `_play_next_from_queue(guild_id)` with mocked state where `_is_video_active()` returns True and verify session termination occurs
  - For Case 3: simulate `_stop_internal` completing and verify the `session_end` WS message triggers `setMode('VISUALIZER_DVD')` not `setMode('IDLE')`
  - Run test on UNFIXED code
  - **EXPECTED OUTCOME**: Test FAILS (this is correct - it proves the bugs exist)
  - Document counterexamples:
    - Video-to-video: `_start_video_from_queue` calls `streamer.enqueue()` on existing active streamer instead of terminating and replacing
    - Video-to-audio: peek guard returns early without terminating session, audio entry stays stuck in queue
    - Session end: `app.js` case `session_end` calls `setMode('IDLE')` — blank screen instead of DVD screensaver
  - Mark task complete when test is written, run, and failure is documented
  - _Requirements: 1.1, 1.2, 1.3, 2.1, 2.2, 2.3_

- [ ] 2. Write preservation property tests (BEFORE implementing fix)
  - **Property 2: Preservation** - Non-Transition Behaviors Unchanged
  - **IMPORTANT**: Follow observation-first methodology
  - **Observe on UNFIXED code**:
    - Internal queue skip: When `streamer.queue` has items, `streamer.skip()` advances within the streamer without calling `_play_next_from_queue`
    - Audio-only skip: When no video session is active (`_is_video_active()` returns False), `_play_next_from_queue` pops next audio entry and calls `_resolve_and_play` via Lavalink
    - Idle streamer reuse: When an existing streamer has `is_active=False` (idle, clients still connected), `_start_video_from_queue` calls `streamer.play(source)` on the idle streamer without creating a new Activity
    - Video transition flag: When `state["_video_transition"]` is True and peek entry is audio, `_play_next_from_queue` returns early (audio blocked)
    - Queue empty handler: When unified queue is empty after exhausting all entries, `_on_queue_empty` fires for disconnect/idle behavior
  - Write property-based tests capturing observed behavior:
    - Property: For all guild states where `streamer.queue` is non-empty, calling video_skip delegates to `streamer.skip()` (no unified queue advancement)
    - Property: For all guild states where `_is_video_active()` returns False and queue has audio entries, `_play_next_from_queue` pops and plays via Lavalink
    - Property: For all guild states where streamer exists with `is_active=False`, `_start_video_from_queue` reuses the streamer (no new ActivityStreamer creation)
    - Property: For all guild states where `_video_transition=True` and peek entry is audio, `_play_next_from_queue` leaves the entry in queue and returns
  - Test file: `tests/test_video_queue_transitions_preservation.py`
  - Use Hypothesis library with custom strategies for guild state generation (queue compositions, session states, video_transition flag values)
  - Verify tests PASS on UNFIXED code
  - **EXPECTED OUTCOME**: Tests PASS (this confirms baseline behavior to preserve)
  - Mark task complete when tests are written, run, and passing on unfixed code
  - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5_

- [ ] 3. Fix video queue transition bugs

  - [ ] 3.1 Add session termination in `_play_next_from_queue` (player.py)
    - After the existing peek guard that blocks audio during active video, add a NEW check:
    - If `_is_video_active(guild_id)` returns True AND `peek_entry.get("type") == "music_video"` (video-to-video): terminate the current session via `streamer.stop()`, unregister from `video_cog._registry`, unregister from `ws_hub`, then proceed to pop and start new video
    - If `_is_video_active(guild_id)` returns True AND `peek_entry.get("type") != "music_video"` (video-to-audio): terminate the current session fully (stop + unregister from registry + unregister from ws_hub), THEN allow the audio entry to be popped and played (remove the early return that currently blocks it)
    - Add a `from_unified_queue=True` parameter to `_start_video_from_queue` to distinguish unified queue progression from direct `/video play` enqueue behavior
    - _Bug_Condition: isBugCondition(input) where session IS NOT NULL AND session.is_active AND input.trigger IN ["skip", "auto_advance"] AND NOT session.queue_
    - _Expected_Behavior: Old session fully terminated (state → IDLE, pipeline stopped, registry unregistered, session_end broadcast) BEFORE new playback starts_
    - _Preservation: Audio-only skips and video_transition flag blocking must remain unchanged_
    - _Requirements: 2.1, 2.2, 3.2, 3.5_

  - [ ] 3.2 Fix registry cleanup ordering in `_on_video_session_end` (player.py)
    - Before calling `_play_next_from_queue`, unregister the streamer from `video_cog._registry` and `ws_hub` so that `_is_video_active()` returns False
    - Current code only clears `state["current"] = None` then calls `_play_next_from_queue` — the registry still has the streamer registered, so `_is_video_active()` returns True and blocks the next audio entry
    - Ordering must be: (1) unregister from registry, (2) unregister from ws_hub, (3) clear current, (4) advance queue
    - Handle the case where `video_cog` or `_bot_ref` is None (bot shutting down)
    - _Bug_Condition: session.state transitions to IDLE AND _on_video_session_end fires_
    - _Expected_Behavior: _is_video_active() returns False before _play_next_from_queue runs_
    - _Preservation: Guard against None bot_ref / video_cog (shutdown safety)_
    - _Requirements: 2.2, 3.4_

  - [ ] 3.3 Handle `from_unified_queue` flag in `_start_video_from_queue` (player.py)
    - When `from_unified_queue=True` and an existing streamer is found with `is_active=True`: do NOT enqueue to the streamer's internal queue — instead, stop the old session (await `streamer.stop()`), unregister from registry + ws_hub, then create a new session
    - When `from_unified_queue=False` (default, for `/video play` command): preserve existing behavior — enqueue to active streamer's internal queue
    - This distinguishes "unified queue advancing to next entry" from "user explicitly adding to video session"
    - _Bug_Condition: session IS NOT NULL AND session.is_active AND nextEntry.type == "music_video" AND trigger is unified queue progression_
    - _Expected_Behavior: Old streamer stops, new streamer created for the new video entry_
    - _Preservation: Idle streamer reuse path unchanged; direct enqueue from /video play unchanged_
    - _Requirements: 2.1, 3.1, 3.3_

  - [ ] 3.4 Frontend `session_end` handler → VISUALIZER_DVD mode (app.js)
    - In `bot/video/activity_frontend/app.js`, change `case 'session_end'` handler:
    - Replace `setMode('IDLE')` with `setMode('VISUALIZER_DVD')`
    - Instantiate `DVDScreensaver` with the bot's avatar URL (from the `session_end` payload or cached `status.bot_avatar_url`)
    - Optionally pass last track info to `DVDScreensaver` for display
    - Also update `bot/video/activity/app.js` (legacy copy) with the same change
    - _Bug_Condition: frontend receives "session_end" message AND no successor video_
    - _Expected_Behavior: Activity shows DVD bouncing logo screensaver, not blank/IDLE_
    - _Preservation: session_change messages (video-to-video) still reinit HLS correctly_
    - _Requirements: 2.3_

  - [ ] 3.5 Verify `video_skip` cleanup ordering in video.py (cogs/video.py)
    - In the `video_skip` method's "streamer queue was empty, session stopped" branch: verify that registry unregister happens BEFORE `_play_next_from_queue` is called
    - Ensure there is no race between `_stop_internal`'s `_on_session_end` callback and the `video_skip` handler's own cleanup
    - Add guard: if `_on_video_session_end` has already been called (e.g., via `_stop_internal` → `_auto_advance`), don't double-advance the queue
    - Add a `_session_end_handled` flag or check `state["current"]` to detect double-fire
    - _Bug_Condition: video_skip called AND streamer.queue is empty AND _stop_internal fires_
    - _Expected_Behavior: Queue advances exactly once, no double-skip, registry cleaned up before audio starts_
    - _Preservation: video_skip with non-empty streamer queue still delegates to streamer.skip()_
    - _Requirements: 2.1, 2.2, 3.1_

  - [ ] 3.6 Verify bug condition exploration test now passes
    - **Property 1: Expected Behavior** - Video Session Terminated Before Successor Starts
    - **IMPORTANT**: Re-run the SAME test from task 1 - do NOT write a new test
    - The test from task 1 encodes the expected behavior
    - When this test passes, it confirms the expected behavior is satisfied
    - Run bug condition exploration test from step 1
    - **EXPECTED OUTCOME**: Test PASSES (confirms bugs are fixed)
    - _Requirements: 2.1, 2.2, 2.3_

  - [ ] 3.7 Verify preservation tests still pass
    - **Property 2: Preservation** - Non-Transition Behaviors Unchanged
    - **IMPORTANT**: Re-run the SAME tests from task 2 - do NOT write new tests
    - Run preservation property tests from step 2
    - **EXPECTED OUTCOME**: Tests PASS (confirms no regressions)
    - Confirm all preservation tests still pass after fix (no regressions to internal queue skips, audio-only skips, idle streamer reuse, video_transition blocking)

- [ ] 4. Checkpoint - Ensure all tests pass
  - Run full test suite: `pytest tests/test_video_queue_transitions_bug.py tests/test_video_queue_transitions_preservation.py -v`
  - Ensure all property-based tests pass (both bug condition and preservation)
  - Manually verify (if possible) the three scenarios:
    - Video-to-video skip: old session terminates, new video starts cleanly in Activity
    - Video-to-audio skip: session terminates, audio plays via Lavalink without visual residue
    - Session end with empty queue: Activity shows DVD bouncing logo screensaver
  - Ensure no regressions in existing test suite (`pytest` full run)
  - Ask the user if questions arise


## Task Dependency Graph

```json
{
  "waves": [
    [1, 2],
    [3],
    [4]
  ]
}
```

## Notes

- Tasks 1 and 2 have no dependencies and can be written in parallel (both run on UNFIXED code)
- Task 3 (all implementation sub-tasks 3.1–3.7) requires tasks 1 and 2 to be complete
- Task 4 (checkpoint) requires task 3 to be complete
- Key files: `bot/player.py` (3.1, 3.2, 3.3), `bot/video/activity_frontend/app.js` (3.4), `bot/cogs/video.py` (3.5)
- Test files: `tests/test_video_queue_transitions_bug.py`, `tests/test_video_queue_transitions_preservation.py`
- Property-based tests use the `hypothesis` library

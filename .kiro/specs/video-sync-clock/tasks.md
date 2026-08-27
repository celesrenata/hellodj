# Implementation Plan: Video Sync Clock

## Overview

Replace the wall-clock-based video synchronization protocol with a monotonic-clock protocol. Implementation proceeds bottom-up: server data models first, then server handlers, then broadcast integration, then client-side sync, then wiring and final integration. Python (server) and JavaScript (client) are the implementation languages.

## Tasks

- [x] 1. Implement server-side data models and state machine
  - [x] 1.1 Modify `PlaybackState` dataclass to use monotonic time
    - In `bot/video/ws_hub.py`, change `anchor_time` default from `time.time()` to `time.monotonic()`
    - Add `_epoch_offset` field: `dataclasses.field(default_factory=lambda: time.time() - time.monotonic())`
    - Add `anchor_time_wall` property that returns `self.anchor_time + self._epoch_offset`
    - Ensure `set_playing(False)` freezes position using monotonic delta: `anchor_position += time.monotonic() - anchor_time`
    - Ensure `set_playing(True)` updates `anchor_time` to `time.monotonic()` without changing `anchor_position`
    - _Requirements: 2.1, 2.5, 2.6, 2.7_

  - [x]* 1.2 Write property tests for PlaybackState anchor invariants
    - **Property 5: PlaybackState anchor invariants under seek/pause/resume**
    - **Validates: Requirements 2.5, 2.6, 2.7**
    - Test file: `tests/test_video_sync_clock_properties.py`
    - Use Hypothesis with `@given` for arbitrary anchor_position and anchor_time values

  - [x] 1.3 Implement `CountdownPhase` enum and `CountdownStateMachine` class
    - Create `CountdownPhase` enum in `bot/video/activity_streamer.py` with WAITING, COUNTDOWN, PLAYING values
    - Create `CountdownStateMachine` class with `phase`, `countdown_seconds`, `countdown_start_mono`, `_disconnect_timer` fields
    - Implement `can_start_countdown()`, `start_countdown()`, `complete_countdown()`, `reset()` methods with guarded transitions
    - Implement `remaining_seconds` property using `time.monotonic()` delta
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.8_

  - [x]* 1.4 Write property tests for countdown state machine transitions
    - **Property 6: Countdown state machine valid forward transitions**
    - **Property 7: PLAYING state is a terminal guard against retrigger**
    - **Property 8: Countdown remaining time computation**
    - **Property 9: State machine reset from any phase**
    - **Validates: Requirements 3.2, 3.3, 3.4, 3.6, 3.8**
    - Test file: `tests/test_video_sync_clock_properties.py`
    - Use Hypothesis stateful testing for transition sequences

- [x] 2. Implement clock sync handler and segment-zero polling
  - [x] 2.1 Add `_handle_clock_sync` method to `WebSocketHub`
    - In `bot/video/ws_hub.py`, add handler for `clock_sync` message type
    - Respond immediately with `clock_sync_reply` containing echoed `client_t1` and current `time.monotonic()` as `server_mono`
    - Validate that `client_t1` is present; log debug and ignore if missing
    - Register handler in the WebSocket message dispatch logic
    - _Requirements: 1.2, 1.6_

  - [x]* 2.2 Write property test for clock sync reply
    - **Property 1: Clock sync reply echoes client timestamp in any state**
    - **Validates: Requirements 1.2, 1.6**
    - Test file: `tests/test_video_sync_clock_properties.py`
    - Verify reply contains identical `client_t1` and positive `server_mono`

  - [x] 2.3 Implement segment-zero readiness polling in `ActivityStreamer`
    - Add `_await_segment_zero` coroutine that polls HLS output directory every 200ms
    - Check for `stream0.ts` (or first segment) with size > 0
    - Timeout after 10 seconds; on timeout compute fallback offset from lowest available segment
    - Add `_find_lowest_segment_offset` helper to parse `.m3u8` and compute `min_index * segment_duration`
    - _Requirements: 4.1, 4.4_

  - [x]* 2.4 Write property test for segment-zero fallback offset
    - **Property 13: Segment-zero fallback offset computation**
    - **Validates: Requirements 4.4**
    - Test file: `tests/test_video_sync_clock_properties.py`
    - Test with arbitrary segment index sets and durations

- [x] 3. Integrate countdown state machine into ActivityStreamer
  - [x] 3.1 Replace boolean flags with `CountdownStateMachine` in `ActivityStreamer`
    - Remove `waiting_for_viewer`, `countdown_active`, `playback_started` booleans
    - Add `self._csm = CountdownStateMachine(countdown_seconds=3)` instance
    - Refactor `_on_first_viewer` to call `_await_segment_zero` then `_csm.start_countdown()`
    - Refactor countdown timer to call `_csm.complete_countdown()` on expiry
    - Add `_csm.reset()` call in `_on_new_track` / session teardown
    - Send `waiting` message when client connects and phase is WAITING
    - Send `countdown` message with `remaining_seconds` for late joiners in COUNTDOWN phase
    - _Requirements: 3.2, 3.3, 3.5, 3.6, 3.7, 3.8, 3.9, 4.1, 4.2_

  - [x] 3.2 Add 5-second disconnect timeout in COUNTDOWN phase
    - When all viewers disconnect during COUNTDOWN, start a 5s asyncio timer
    - If no viewer reconnects within 5s, call `_csm.reset()` and cancel countdown
    - If a viewer reconnects within 5s, cancel the timeout timer
    - Use `_csm._disconnect_timer` field to track the task
    - _Requirements: 3.7_

- [x] 4. Checkpoint - Verify server-side logic
  - Ensure all tests pass, ask the user if questions arise.

- [x] 5. Update server broadcast messages to include monotonic anchor time
  - [x] 5.1 Update `state` broadcast in `ws_hub.py`
    - Include `anchor_time_mono` (from `PlaybackState.anchor_time`) in state messages
    - Include `anchor_time` (from `PlaybackState.anchor_time_wall`) for backward compat
    - _Requirements: 6.1_

  - [x] 5.2 Update `play`/`pause` broadcasts in `ws_hub.py` and `views/unified_remote.py`
    - Add `anchor_time_mono` field to play/pause broadcast dicts
    - Keep existing `anchor_time` field using `anchor_time_wall` property
    - Update `UnifiedControlView` pause/resume handlers to use monotonic anchor
    - _Requirements: 6.3_

  - [x] 5.3 Update `start` broadcast and state messages in `bot/cogs/video.py`
    - Add `anchor_time_mono` to the start message after countdown completes
    - Set `anchor_position: 0.0` (or fallback offset) in the start message
    - Update any state broadcast calls to include both timestamp fields
    - _Requirements: 4.2, 6.2_

  - [x] 5.4 Update state broadcasts in `bot/player.py`
    - In `_start_video_from_queue`, use `time.monotonic()` for `anchor_time_mono` in state broadcasts
    - Include backward-compat `anchor_time` (wall-clock) alongside
    - _Requirements: 6.1, 6.2_

  - [x]* 5.5 Write property test for broadcast message format
    - **Property 10: All broadcast messages include monotonic anchor time**
    - **Validates: Requirements 6.1, 6.2, 6.3**
    - Test file: `tests/test_video_sync_clock_properties.py`
    - Verify all message types contain both `anchor_time_mono` > 0 and `anchor_time`

- [x] 6. Implement client-side ClockSync class
  - [x] 6.1 Create `ClockSync` class in `bot/video/activity_frontend/app.js`
    - Implement constructor with `serverOffset`, `rtt`, `synced`, `_pendingT1`, `_retryCount`, `_maxRetries`, `_timeout` fields
    - Implement `initiate()` method: send `clock_sync` message with `performance.now()` as `client_t1`, start 2000ms timeout
    - Implement `handleReply(data)`: validate `client_t1` match, compute RTT and serverOffset, set `synced = true`
    - Implement `serverNow()`: returns `performance.now() + this.serverOffset`
    - Implement `driftTolerance` getter: `Math.min(10.0, Math.max(3.0, this.rtt / 1000 * 2))`
    - Implement timeout handler: retry up to 3 times with 500ms delay, close WS if exhausted
    - _Requirements: 1.1, 1.3, 1.4, 1.7, 1.8, 1.9, 5.1_

  - [x]* 6.2 Write property tests for client-side offset and tolerance computation
    - **Property 2: Server offset computation**
    - **Property 3: Stale clock sync reply rejection**
    - **Property 12: RTT-adaptive drift tolerance formula**
    - **Validates: Requirements 1.3, 1.8, 5.1**
    - Test file: `tests/test_video_sync_clock_properties.py`
    - Use Hypothesis to generate arbitrary timestamps and RTT values

- [x] 7. Implement client-side drift checker and position computation
  - [x] 7.1 Implement `computeExpectedPosition` and drift checking in `bot/video/activity_frontend/app.js`
    - Add `computeExpectedPosition(state, clockSync)` function: if playing, return `anchor_position + (clockSync.serverNow() - state.anchor_time_mono)`; if paused, return `anchor_position`
    - Add `anchor_time_mono` field selection logic: prefer `anchor_time_mono` if present and > 0, fall back to `anchor_time`
    - Add drift check interval (every 2 seconds): compare `videoEl.currentTime` to expected, seek if drift > `clockSync.driftTolerance`
    - Clamp negative expected positions to 0.0
    - Skip drift check during buffering (wait for `canplay` event)
    - _Requirements: 2.3, 2.4, 5.1, 5.2, 5.4, 5.5, 6.4_

  - [x]* 7.2 Write property tests for expected position computation and field selection
    - **Property 4: Expected position computation**
    - **Property 11: Client anchor field selection**
    - **Validates: Requirements 2.3, 2.4, 6.4**
    - Test file: `tests/test_video_sync_clock_properties.py`
    - Test with arbitrary anchor positions, offsets, and playing/paused states

- [x] 8. Implement client-side reconnection manager
  - [x] 8.1 Enhance `connectWebSocket` reconnection flow in `bot/video/activity_frontend/app.js`
    - On reconnect: initiate clock sync immediately, queue incoming state messages until sync completes or 5s timeout
    - After sync completes: process queued messages with new offset
    - If sync times out (5s): use previously stored serverOffset and RTT, process queued messages
    - Preserve video element and HLS.js session across reconnections (no pause/detach/reinitialize)
    - Only seek on reconnect if drift exceeds RTT-adaptive tolerance
    - Set `startPosition: 0` in HLS.js config (or fallback anchor_position if > 0)
    - _Requirements: 1.5, 7.1, 7.2, 7.3, 7.4, 7.5, 4.3, 4.5_

- [x] 9. Checkpoint - Verify client-side logic
  - Ensure all tests pass, ask the user if questions arise.

- [x] 10. Duplicate frontend and final wiring
  - [x] 10.1 Apply identical ClockSync, DriftChecker, and reconnection changes to `bot/video/activity/app.js`
    - Mirror all changes from `activity_frontend/app.js` to the duplicate `activity/app.js`
    - Ensure both frontends have identical sync behavior
    - _Requirements: 1.1, 1.3, 2.3, 5.1, 7.1_

  - [x] 10.2 Bump cache buster in `bot/video/activity_frontend/index.html`
    - Increment the `?v=` query parameter on `app.js` script tag
    - _Requirements: N/A (deployment hygiene)_

  - [x]* 10.3 Write integration tests for WebSocket clock sync round-trip
    - Test full handshake lifecycle with aiohttp test client
    - Test reconnection flow: sync → queue → process
    - Test countdown non-retrigger on reconnect
    - _Requirements: 1.2, 1.5, 3.4, 3.5, 7.3_

- [x] 11. Final checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- The design uses Python (Hypothesis) for property-based tests covering server logic and the client-side formulas (tested as pure functions)
- Both `activity_frontend/app.js` and `activity/app.js` must stay in sync — they are duplicate frontends
- Backward compatibility is maintained: all messages include both `anchor_time` (wall-clock) and `anchor_time_mono` (monotonic)
- The `CountdownStateMachine` replaces existing boolean flags — ensure old behavior is preserved during refactor
- Property tests target: `tests/test_video_sync_clock_properties.py` (Hypothesis already in use in this project)

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "1.3"] },
    { "id": 1, "tasks": ["1.2", "1.4", "2.1", "2.3"] },
    { "id": 2, "tasks": ["2.2", "2.4", "3.1"] },
    { "id": 3, "tasks": ["3.2", "5.1", "5.4"] },
    { "id": 4, "tasks": ["5.2", "5.3", "5.5"] },
    { "id": 5, "tasks": ["6.1"] },
    { "id": 6, "tasks": ["6.2", "7.1"] },
    { "id": 7, "tasks": ["7.2", "8.1"] },
    { "id": 8, "tasks": ["10.1", "10.2"] },
    { "id": 9, "tasks": ["10.3"] }
  ]
}
```

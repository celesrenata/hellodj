# Requirements Document

## Introduction

Replace the wall-clock-based video synchronization in the Discord Activity with a monotonic-clock protocol that eliminates cross-client jitter, prevents countdown retriggering on WebSocket reconnections, guarantees playback starts at segment zero, and adapts drift tolerance to measured network latency. The server remains the single source of truth for playback position.

## Glossary

- **Sync_Server**: The Python aiohttp WebSocket hub (`ws_hub.py`) that manages per-guild playback state and broadcasts sync messages to all connected Activity clients.
- **Activity_Client**: The JavaScript frontend running inside the Discord Activity iframe, responsible for rendering video via HLS.js and maintaining local clock offset.
- **Monotonic_Clock**: A clock source that only moves forward and is immune to wall-clock adjustments — `performance.now()` on clients, `time.monotonic()` on the server.
- **Server_Offset**: The difference between a client's local monotonic clock and the server's monotonic clock, computed via the clock sync handshake. Used to translate server timestamps into local time.
- **RTT**: Round-Trip Time — the measured latency between a client sending a ping and receiving the server's pong response.
- **Drift**: The absolute difference between a client's current video playback position and the expected position derived from the server's authoritative state.
- **Countdown_State_Machine**: A three-state finite automaton (WAITING → COUNTDOWN → PLAYING) governing the lifecycle of video session startup. Transitions are one-directional once in PLAYING.
- **Segment_Zero**: The first HLS segment (index 0) produced by the FFmpeg transcode pipeline. Playback must begin from this segment, not from whichever segment happens to be available when the client connects.
- **PlaybackState**: The server-side dataclass tracking authoritative playback position using an anchor-based model (anchor_position + elapsed monotonic time).
- **HLS_Pipeline**: The FFmpeg HLS transcode process that produces `.m3u8` playlist and `.ts` segment files on the server's tmpfs.

## Requirements

### Requirement 1: Clock Sync Handshake

**User Story:** As an Activity client, I want to establish my time offset relative to the server on each WebSocket connection, so that all position calculations use a shared monotonic time reference instead of unreliable wall clocks.

#### Acceptance Criteria

1. WHEN an Activity_Client establishes a WebSocket connection, THE Activity_Client SHALL send a `clock_sync` message containing its local `performance.now()` timestamp within 100ms of connection open.
2. WHEN the Sync_Server receives a `clock_sync` message, THE Sync_Server SHALL respond with a `clock_sync_reply` containing the client's original timestamp and the server's current `time.monotonic()` value within 50ms of receipt.
3. WHEN the Activity_Client receives a `clock_sync_reply`, THE Activity_Client SHALL compute Server_Offset as `server_monotonic - (client_t1 + rtt/2)` where `rtt = client_now - client_t1`.
4. WHEN the Activity_Client receives a `clock_sync_reply`, THE Activity_Client SHALL store the measured RTT for use in drift tolerance calculations.
5. THE Activity_Client SHALL perform the clock sync handshake on every WebSocket connection establishment, including reconnections.
6. THE Sync_Server SHALL respond to `clock_sync` messages regardless of the current playback state or countdown phase.
7. IF the Activity_Client does not receive a `clock_sync_reply` within 2000ms of sending a `clock_sync` message, THEN THE Activity_Client SHALL retry the handshake up to 3 times with a 500ms delay between attempts before treating the connection as failed.
8. IF the Activity_Client receives a `clock_sync_reply` whose original timestamp does not match the `client_t1` value from the pending handshake request, THEN THE Activity_Client SHALL discard the reply and retry the handshake.
9. IF all clock sync handshake retry attempts are exhausted without a valid reply, THEN THE Activity_Client SHALL close the WebSocket connection and initiate a reconnection.

### Requirement 2: Monotonic Anchor-Based Position

**User Story:** As a viewer, I want all playback position math to use monotonic time deltas rather than wall-clock timestamps, so that system clock adjustments or NTP corrections on my device do not cause video jumps.

#### Acceptance Criteria

1. THE PlaybackState SHALL store `anchor_time` as a `time.monotonic()` value instead of a `time.time()` value.
2. WHEN the Sync_Server broadcasts a `state` message, THE Sync_Server SHALL include `anchor_time_mono` containing the monotonic anchor timestamp.
3. WHILE the PlaybackState `playing` field is true, THE Activity_Client SHALL compute expected playback position as `anchor_position + (local_monotonic_now + Server_Offset - anchor_time_mono)`.
4. WHILE the PlaybackState `playing` field is false, THE Activity_Client SHALL treat the expected playback position as `anchor_position` without adding any time delta.
5. WHEN the Sync_Server handles a seek operation, THE Sync_Server SHALL set `anchor_position` to the seek target and update `anchor_time` to the current `time.monotonic()` value.
6. WHEN the Sync_Server handles a pause, THE Sync_Server SHALL set `anchor_position` to the current computed position (`anchor_position + (time.monotonic() - anchor_time)`) and then update `anchor_time` to the current `time.monotonic()` value.
7. WHEN the Sync_Server handles a resume (play), THE Sync_Server SHALL update `anchor_time` to the current `time.monotonic()` value without modifying `anchor_position`.

### Requirement 3: Countdown State Machine

**User Story:** As a viewer, I want the 3-2-1 countdown to execute exactly once per video session and never retrigger during playback, so that WebSocket reconnections do not disrupt viewing.

#### Acceptance Criteria

1. THE Countdown_State_Machine SHALL have exactly three states: WAITING, COUNTDOWN, and PLAYING.
2. WHILE the HLS_Pipeline is in STREAMING state, WHEN the first Activity_Client WebSocket connection is established, THE Countdown_State_Machine SHALL transition from WAITING to COUNTDOWN.
3. WHEN the countdown timer reaches zero (after 3 seconds), THE Countdown_State_Machine SHALL transition from COUNTDOWN to PLAYING.
4. WHILE the Countdown_State_Machine is in PLAYING state, THE Sync_Server SHALL ignore any message or event that would transition the state back to WAITING or COUNTDOWN, without altering state or sending countdown messages.
5. WHEN an Activity_Client reconnects via WebSocket while the Countdown_State_Machine is in PLAYING state, THE Sync_Server SHALL send the current playback state (anchor position and monotonic anchor time) without triggering a countdown.
6. WHEN an Activity_Client reconnects via WebSocket while the Countdown_State_Machine is in COUNTDOWN state, THE Sync_Server SHALL send the remaining countdown duration so the client joins the in-progress countdown.
7. IF all viewers disconnect while the Countdown_State_Machine is in COUNTDOWN state and no viewer reconnects within 5 seconds, THEN THE Sync_Server SHALL transition back to WAITING and cancel the countdown timer.
8. WHEN a new video source begins playback (next track in queue), THE Countdown_State_Machine SHALL reset to WAITING for the new session.
9. WHEN an Activity_Client connects while the HLS_Pipeline is not yet in STREAMING state, THE Countdown_State_Machine SHALL remain in WAITING and THE Sync_Server SHALL send a waiting-state message to the client.

### Requirement 4: Segment-Zero Start

**User Story:** As a viewer, I want video playback to begin at 0:00 rather than 5 seconds in, so that I do not miss the opening of the video.

#### Acceptance Criteria

1. WHEN the HLS_Pipeline begins transcoding, THE Sync_Server SHALL poll the HLS output directory every 200ms and wait for the first segment file (index 0 in the `.m3u8` playlist) to exist on disk with a file size greater than 0 bytes before allowing the Countdown_State_Machine to transition from WAITING to COUNTDOWN.
2. WHEN the Countdown_State_Machine transitions to PLAYING, THE Sync_Server SHALL set `anchor_position` to 0.0 in the PlaybackState.
3. WHEN the Activity_Client attaches HLS.js to the video element after countdown completes, THE Activity_Client SHALL set `startPosition: 0` in the HLS.js configuration.
4. IF segment 0 does not appear within 10 seconds of transcode start, THEN THE Sync_Server SHALL log an error, identify the lowest-indexed segment present in the `.m3u8` playlist, set `anchor_position` in PlaybackState to that segment's start time offset (segment index multiplied by segment duration), and allow the countdown to proceed.
5. WHEN the Activity_Client receives a PlaybackState with `anchor_position` greater than 0.0 due to a segment-zero timeout fallback, THE Activity_Client SHALL set `startPosition` in the HLS.js configuration to the received `anchor_position` value.

### Requirement 5: RTT-Adaptive Drift Tolerance

**User Story:** As a viewer on a high-latency connection, I want the sync system to tolerate proportionally larger drift before seeking, so that my video does not constantly stutter from corrective seeks.

#### Acceptance Criteria

1. WHEN the Activity_Client detects drift between its current playback position and the expected server position, THE Activity_Client SHALL only perform a corrective seek if drift exceeds `max(3.0, measured_rtt * 2)` seconds, capped at a maximum tolerance of 10.0 seconds regardless of RTT.
2. WHEN the Activity_Client performs a corrective seek, THE Activity_Client SHALL seek to the server's expected position computed via monotonic clock math.
3. THE Activity_Client SHALL update its stored RTT value on each successful clock sync handshake.
4. WHILE the Activity_Client has not yet completed a clock sync handshake (RTT unknown), THE Activity_Client SHALL use a default drift tolerance of 3.0 seconds.
5. THE Activity_Client SHALL evaluate drift against the expected server position at least once every 2 seconds while in PLAYING state.

### Requirement 6: Server State Message Protocol

**User Story:** As a developer, I want the server state messages to carry monotonic timestamps alongside the existing fields, so that the new sync protocol is backward-compatible during rollout.

#### Acceptance Criteria

1. WHEN the Sync_Server broadcasts a `state` message, THE Sync_Server SHALL include both `anchor_time` (wall-clock, for backward compatibility) and `anchor_time_mono` (monotonic) fields.
2. WHEN the Sync_Server broadcasts a `start` message after countdown completes, THE Sync_Server SHALL include `anchor_time_mono` set to the current `time.monotonic()` value.
3. WHEN the Sync_Server broadcasts a `play` or `pause` message, THE Sync_Server SHALL include the updated `anchor_time_mono` field.
4. IF the `anchor_time_mono` field is present and has a value greater than zero in a received message, THEN THE Activity_Client SHALL use `anchor_time_mono` for position calculations; otherwise THE Activity_Client SHALL fall back to the `anchor_time` (wall-clock) field.

### Requirement 7: Reconnection Resilience

**User Story:** As a viewer whose WebSocket reconnects every ~30 seconds, I want reconnections to seamlessly resume playback at the correct position without visual disruption, so that the known reconnection issue does not degrade viewing experience.

#### Acceptance Criteria

1. WHEN an Activity_Client reconnects, THE Activity_Client SHALL perform the clock sync handshake and queue any incoming state messages until the handshake completes or times out after 5 seconds.
2. IF the clock sync handshake does not complete within 5 seconds of reconnection, THEN THE Activity_Client SHALL proceed using its previously stored Server_Offset and RTT values.
3. WHEN an Activity_Client reconnects during PLAYING state, THE Sync_Server SHALL send the current state with monotonic anchor time so the client can compute expected position immediately.
4. WHEN an Activity_Client reconnects and computes expected position, THE Activity_Client SHALL only seek if drift exceeds the RTT-adaptive tolerance threshold.
5. THE Activity_Client SHALL preserve its video element and HLS.js session (not pause, detach, or reinitialize the media pipeline) across WebSocket reconnections.

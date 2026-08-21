# Implementation Plan: Activity Visualizer

## Overview

Implement the Activity Visualizer in four phases: (1) DVD screensaver + WebSocket countdown protocol (client-side, no GPU), (2) VisualizerManager state machine + viewer tracking, (3) AudioFeatureBus + first server-rendered engine, (4) additional engine implementations. The system uses Python for all server-side components and JavaScript/CSS for the Activity frontend.

## Prerequisites

- Existing `video/ws_hub.py`, `video/activity_streamer.py`, `video/activity_backend.py`, and `video/activity_frontend/` infrastructure
- `hypothesis` available for property-based tests
- `guild_settings.py` with existing `get_setting`/`set_setting` helpers
- `discord.ext.voice_recv` already integrated for wake word PCM capture

## Tasks

- [x] 1. Phase 1: Foundation — Engine interface, guild settings, and slash command
  - [x] 1.1 Create `bot/video/visualizer_engines/base.py` with VisualizerRenderer ABC and data classes
    - Define `AudioFeatures` dataclass (fft, beat, bpm, band_energy, timestamp)
    - Define `TrackMetadata` dataclass (title, artist, artwork_url, duration_ms, position_ms)
    - Define `VisualizerRenderer` ABC with methods: initialize, activate, suspend, resume, stop, on_track_change
    - Define properties: is_client_side, consumes_gpu_while_suspended, client_config
    - Define async generator: render_frames() for server-rendered engines
    - Create `bot/video/visualizer_engines/__init__.py` with engine registry and factory function
    - _Requirements: 2.1, 7.1_

  - [x] 1.2 Create `bot/video/visualizer_engines/dvd.py` with DVDEngine implementation
    - Implement `DVDEngine(VisualizerRenderer)` with `is_client_side = True`
    - Store bot avatar URL and track metadata
    - Return `client_config` dict with avatar_url and track info
    - All lifecycle methods are no-ops (no server rendering)
    - _Requirements: 6.1, 6.5_

  - [x] 1.3 Extend `bot/guild_settings.py` with visualizer engine helpers
    - Add `VALID_VISUALIZER_ENGINES` set: dvd, projectm, vgalizer, varda, fosfora, audiovis, native, random, off
    - Add `DEFAULT_VISUALIZER_ENGINE = "dvd"`
    - Implement `get_visualizer_engine(guild_id)` returning configured or default engine
    - Implement `set_visualizer_engine(guild_id, engine)` with validation
    - _Requirements: 5.1, 5.2, 5.3, 5.6_

  - [x]* 1.4 Write property test for configuration persistence (Property 9)
    - **Property 9: Configuration persistence**
    - Generate random valid engine values → set → get → verify roundtrip consistency
    - Generate random invalid engine values → verify `get_visualizer_engine` returns default
    - Verify persistence survives simulated restart (re-read from file)
    - **Validates: Requirements 5.1, 5.6**

  - [x] 1.5 Create `bot/cogs/visualizer.py` slash command group
    - Implement `/visualizer type:<engine>` command with autocomplete for valid engine values
    - Call `set_visualizer_engine(guild_id, engine)` on invocation
    - Send confirmation embed showing the new engine type
    - Handle `off` by signaling VisualizerManager (once implemented) to disable
    - Register cog in bot startup
    - _Requirements: 5.1, 5.2, 5.4_

- [x] 2. Phase 1: WebSocket countdown protocol
  - [x] 2.1 Extend `bot/video/ws_hub.py` with viewer count tracking
    - Track per-guild viewer counts using existing `_connections` dict
    - Add `_on_viewer_count_change(guild_id, old_count, new_count)` callback hook
    - Emit viewer count change when connection added (0→1) or removed (1→0)
    - Ensure heartbeat timeout disconnects decrement the count within 1 second
    - _Requirements: 9.1, 9.2, 9.3, 9.4_

  - [x]* 2.2 Write property test for viewer count accuracy (Property 8)
    - **Property 8: Viewer count accuracy**
    - Generate random connect/disconnect/timeout sequences → verify tracked count equals live connections
    - Verify heartbeat timeout decrements count within 1 second
    - **Validates: Requirements 9.1, 9.4**

  - [x] 2.3 Implement countdown protocol in `bot/video/ws_hub.py` and `bot/video/activity_streamer.py`
    - Add `WAITING_FOR_VIEWER` and `COUNTDOWN` sub-states to ActivityStreamer
    - When first viewer connects and elapsed < 5s: send `countdown` message to all clients
    - When first `ready` received: mark position 0, broadcast `start` to all clients
    - When viewer connects and elapsed ≥ 5s: send `state` message with computed position (late-joiner sync)
    - Handle edge cases: all clients disconnect during countdown, `ready` without active countdown
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5_

  - [x]* 2.4 Write property test for countdown trigger logic (Property 1)
    - **Property 1: Countdown triggers only on first viewer within 5s**
    - Generate random elapsed times + connection events → verify countdown vs state message selection
    - Verify countdown is never sent when elapsed ≥ 5 seconds
    - Verify state message always includes computed position for late joiners
    - **Validates: Requirements 1.1, 1.5**

- [x] 3. Phase 1: Frontend countdown overlay and DVD screensaver
  - [x] 3.1 Implement frontend state machine in `bot/video/activity_frontend/app.js`
    - Add mode dispatcher with states: IDLE, COUNTDOWN, VIDEO_PLAYING, VISUALIZER_DVD, VISUALIZER_HLS
    - Handle WebSocket messages: countdown, start, state, visualizer, session_end, track_change
    - Transition between modes based on incoming messages
    - Send `ready` message when countdown animation completes
    - _Requirements: 1.2, 1.3, 6.1, 6.6_

  - [x] 3.2 Implement countdown overlay in `bot/video/activity_frontend/app.js`
    - Create `CountdownOverlay` class with 3-2-1 animated number display
    - On completion, send `{ type: "ready" }` to WebSocket
    - Display video title during countdown
    - Handle receiving countdown with remaining time (late-joining during countdown)
    - _Requirements: 1.2, 1.3_

  - [x] 3.3 Implement DVD screensaver in `bot/video/activity_frontend/app.js`
    - Create `DVDScreensaver` class with bouncing avatar animation
    - Constant velocity movement with angle reflection on edge contact
    - Hue-rotate color change on each edge/corner hit
    - Accept config from WebSocket `visualizer` message (avatar_url, track metadata)
    - _Requirements: 6.1, 6.2, 6.3, 6.4_

  - [x] 3.4 Add CSS styles in `bot/video/activity_frontend/style.css`
    - Add `.dvd-logo` styles (absolute positioning, border-radius, will-change, transition)
    - Add `.countdown-overlay` styles (centered, large font, animation keyframes)
    - Add `.visualizer-loading` styles for "Starting visualizer..." state
    - _Requirements: 6.1, 6.2_

  - [x]* 3.5 Write property test for DVD screensaver zero server resources (Property 7)
    - **Property 7: DVD screensaver server resource usage**
    - Generate random DVD activation sequences → verify zero ffmpeg processes spawned
    - Verify DVDEngine never calls render_frames()
    - Verify VisualizerManager with DVD engine has no pipeline allocated
    - **Validates: Requirements 6.1, 6.5**

- [x] 4. Checkpoint — Phase 1 complete
  - Ensure all tests pass, ask the user if questions arise.
  - Verify: countdown protocol works end-to-end, DVD screensaver displays, guild settings persist, slash command functional

- [x] 5. Phase 2: VisualizerManager state machine
  - [x] 5.1 Create `bot/video/visualizer_manager.py` with VisualizerManager class
    - Implement `VisualizerState` enum: DISABLED, IDLE_NO_VIEWERS, STARTING, ACTIVE, SUSPENDING, ERROR
    - Implement per-guild `VisualizerManager` with state machine logic
    - Implement event handlers: on_viewer_join, on_viewer_leave, on_video_start, on_video_end, on_track_change, set_engine, shutdown
    - Initialize with guild's configured engine from guild_settings
    - Transition to DISABLED when video starts, regardless of current state
    - Transition to IDLE_NO_VIEWERS when video ends and engine is not `off`
    - _Requirements: 2.1, 2.2, 2.3, 2.7, 2.8_

  - [x]* 5.2 Write property test for state machine valid transitions (Property 2)
    - **Property 2: Visualizer state machine valid transitions**
    - Generate random sequences of events → verify only valid transitions occur
    - Verify no transition path violates the defined state machine edges
    - Verify DISABLED is always reachable from any state via video_start
    - **Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 2.8**

  - [x] 5.3 Implement suspension debounce in `bot/video/visualizer_manager.py`
    - When last viewer disconnects: transition to SUSPENDING, start 2s asyncio timer
    - On timer expiry: re-check viewer count, execute suspension if still 0
    - If viewer reconnects during SUSPENDING: cancel timer, transition back to ACTIVE
    - Implement watchdog for stale SUSPENDING state (>10s → force transition)
    - _Requirements: 2.4, 2.5, 2.6, 3.6_

  - [x]* 5.4 Write property test for suspension debounce correctness (Property 6)
    - **Property 6: Suspension debounce correctness**
    - Generate random reconnect timings around the 2s boundary → verify state transitions
    - Verify reconnect within 2s cancels suspension and returns to ACTIVE
    - Verify no viewer after 2s transitions to IDLE_NO_VIEWERS
    - **Validates: Requirements 2.5, 2.6, 3.6**

  - [x] 5.5 Wire VisualizerManager into WebSocket Hub and ActivityStreamer
    - Connect `ws_hub.py` viewer count changes to VisualizerManager's on_viewer_join/on_viewer_leave
    - Connect `activity_streamer.py` video start/end events to on_video_start/on_video_end
    - Broadcast visualizer state changes to all connected viewers via WebSocket
    - _Requirements: 9.2, 9.3, 9.5_

  - [x] 5.6 Wire track change events from `bot/player.py` to VisualizerManager
    - Emit track change event when a new track starts playing
    - VisualizerManager updates `_track_metadata` in any state
    - If in ACTIVE state with client-side engine: broadcast updated config to viewers
    - If in IDLE_NO_VIEWERS: store metadata only, no rendering
    - _Requirements: 3.5, 5.5_

  - [x]* 5.7 Write property test for zero resource consumption when idle (Property 3)
    - **Property 3: Zero resource consumption when idle**
    - Generate random state sequences ending in IDLE_NO_VIEWERS → verify zero running processes
    - Verify AudioFeatureBus subscriber_count is 0 when manager is idle
    - Verify no asyncio tasks performing rendering work
    - **Validates: Requirements 3.1, 3.2, 3.4**

  - [x]* 5.8 Write property test for audio independence (Property 5)
    - **Property 5: Audio independence invariant**
    - Generate random VisualizerManager failures → verify player.py guild_state unchanged
    - Verify no shared mutable state between VisualizerManager and player
    - Verify ERROR transition has zero effect on audio playback state
    - **Validates: Requirements 8.1, 8.2, 8.3, 8.4**

  - [x]* 5.9 Write property test for visualizer yields to video (Property 10)
    - **Property 10: Visualizer yields to video**
    - Generate random viz states + video start events → verify transition to DISABLED
    - Verify frontend receives appropriate mode switch message
    - **Validates: Requirements 2.8, 6.6**

- [x] 6. Checkpoint — Phase 2 complete
  - Ensure all tests pass, ask the user if questions arise.
  - Verify: state machine transitions correctly, debounce works, viewer tracking integrated, track changes propagate, audio independence maintained

- [x] 7. Phase 3: AudioFeatureBus and server-rendered engine pipeline
  - [x] 7.1 Create `bot/video/audio_feature_bus.py` with AudioFeatureBus
    - Implement subscriber-gated audio analysis pipeline
    - Reference counting: start processing on first subscriber, stop on last
    - Integrate with `voice_recv` PCM source (existing PipelineSink)
    - Compute FFT spectrum (1024-sample, numpy), beat detection, BPM estimation, 7-band energy
    - Dispatch `AudioFeatures` frames to all subscribers
    - Start/stop within 100ms of subscriber changes
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5_

  - [x]* 7.2 Write property test for AudioFeatureBus subscriber gating (Property 4)
    - **Property 4: AudioFeatureBus subscriber gating**
    - Generate random subscribe/unsubscribe sequences → verify processing task lifecycle
    - Verify _processing_task is None when subscriber_count == 0
    - Verify processing starts within 100ms of first subscribe
    - **Validates: Requirements 4.2, 4.3, 4.4**

  - [x] 7.3 Extend HLS pipeline for visualizer raw frame input
    - Add `build_visualizer_ffmpeg_args()` method to produce ffmpeg args for rawvideo stdin → QSV HLS output
    - Parameters: width=1280, height=720, fps=30, pixel_format=rgba
    - Output to `/tmp/hellodj_hls/{guild_id}/viz/playlist.m3u8`
    - Use shorter HLS segments (2s) with rolling window (hls_list_size=5) for low latency
    - Include `delete_segments+append_list` flags for live-like streaming
    - _Requirements: 7.1, 7.3_

  - [x] 7.4 Implement first server-rendered engine (native Python shader)
    - Create `bot/video/visualizer_engines/native.py` implementing VisualizerRenderer
    - Subscribe to AudioFeatureBus for audio features
    - Generate raw RGBA frames (numpy/PIL-based spectrum visualizer)
    - Implement render_frames() async generator yielding frame bytes
    - React to beat detection and band energy for visual effects
    - _Requirements: 7.1_

  - [x] 7.5 Extend `bot/video/activity_backend.py` with visualizer HLS route
    - Add route: `/activity/stream/{guild_id}/viz/playlist.m3u8` and segment serving
    - Serve from `/tmp/hellodj_hls/{guild_id}/viz/` directory
    - Return 404 when no visualizer stream active
    - _Requirements: 7.1, 7.2_

  - [x] 7.6 Implement VISUALIZER_HLS mode in frontend `app.js`
    - Handle `visualizer` message with `hls_ready: true` and `playlist_url`
    - Create hls.js player instance pointed at viz playlist
    - Display "Starting visualizer..." message while `state: "starting"`
    - Switch from loading to HLS playback when `hls_ready` received
    - _Requirements: 7.1, 7.2_

  - [x] 7.7 Wire server-rendered engine lifecycle in VisualizerManager
    - On STARTING: initialize engine, subscribe to AudioFeatureBus, spawn ffmpeg pipeline
    - On ACTIVE: pipe render_frames() output to ffmpeg stdin, notify frontend when first segment ready
    - On SUSPENDING→IDLE: unsubscribe from bus, kill pipeline, clean temp segment files
    - On ERROR: kill pipeline, log failure, fallback to DVD engine
    - Segment cleanup within 5 seconds of suspension
    - _Requirements: 3.3, 3.4, 7.1, 7.3, 7.4_

- [x] 8. Checkpoint — Phase 3 complete
  - Ensure all tests pass, ask the user if questions arise.
  - Verify: AudioFeatureBus produces features from voice_recv PCM, native engine renders frames, HLS pipeline encodes and serves visualizer stream, frontend switches to HLS mode, demand-driven lifecycle works end-to-end

- [x] 9. Phase 4: Additional visualizer engines
  - [x] 9.1 Implement `random` mode logic in VisualizerManager
    - On track change: select a different engine from available server-rendered engines
    - Maintain list of available engines (exclude dvd, random, off)
    - Cycle through engines rather than purely random (avoid repeats)
    - _Requirements: 5.5_

  - [x] 9.2 Create engine stubs for future server-rendered engines
    - Create `bot/video/visualizer_engines/projectm.py` — projectM wrapper (stub with NotImplementedError for render_frames)
    - Create `bot/video/visualizer_engines/vgalizer.py` — vgalizer integration stub
    - Create `bot/video/visualizer_engines/varda.py` — Varda shader engine stub
    - Create `bot/video/visualizer_engines/fosfora.py` — Fosfora audio visualizer stub
    - Create `bot/video/visualizer_engines/audiovis.py` — AudioVis spectrum analyzer stub
    - Each stub implements VisualizerRenderer with proper metadata but raises on render_frames()
    - Update engine registry in `__init__.py` with all engine mappings
    - _Requirements: 5.2, 7.1_

  - [x]* 9.3 Write integration tests for full visualizer lifecycle
    - Test: IDLE → viewer joins → STARTING → ACTIVE → viewer leaves → SUSPENDING → IDLE (with mock engine)
    - Test: DVD activation via WebSocket → verify message shape matches frontend expectations
    - Test: AudioFeatureBus with synthetic PCM → subscribe → receive features → unsubscribe → verify stopped
    - Test: Track change during IDLE_NO_VIEWERS → metadata stored → viewer joins → engine receives current metadata
    - _Requirements: 2.1, 3.3, 4.1, 6.1_

- [x] 10. Final checkpoint
  - Ensure all tests pass, ask the user if questions arise.
  - Verify: all engines registered, random mode cycles correctly, engine stubs follow interface, full lifecycle integration tests pass

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- Checkpoints ensure incremental validation between phases
- Property tests validate universal correctness properties from the design document
- Unit tests validate specific examples and edge cases
- Phase 1 is entirely client-side (zero GPU) — can be developed and deployed independently
- Phase 2 adds server-side state management without rendering — testable in isolation
- Phase 3 requires QSV GPU availability on gremlin nodes for HLS encoding
- Phase 4 engine stubs can be fleshed out incrementally as each visualizer is integrated
- Python (server) and JavaScript (frontend) used throughout as specified in the design

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "1.3"] },
    { "id": 1, "tasks": ["1.2", "1.4", "1.5", "2.1"] },
    { "id": 2, "tasks": ["2.2", "2.3", "3.4"] },
    { "id": 3, "tasks": ["2.4", "3.1"] },
    { "id": 4, "tasks": ["3.2", "3.3", "3.5"] },
    { "id": 5, "tasks": ["5.1"] },
    { "id": 6, "tasks": ["5.2", "5.3"] },
    { "id": 7, "tasks": ["5.4", "5.5", "5.6"] },
    { "id": 8, "tasks": ["5.7", "5.8", "5.9"] },
    { "id": 9, "tasks": ["7.1", "7.3"] },
    { "id": 10, "tasks": ["7.2", "7.4", "7.5", "7.6"] },
    { "id": 11, "tasks": ["7.7"] },
    { "id": 12, "tasks": ["9.1", "9.2"] },
    { "id": 13, "tasks": ["9.3"] }
  ]
}
```

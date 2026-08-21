# Requirements Document

## Introduction

This feature implements two related systems for the HelloDJ Discord Activity:

1. **Video Playback Flow Rework** — Replaces the current approach where videos start ~10 seconds in (because HLS segments are pre-generated) with a WebSocket-driven countdown-then-start protocol. The first connected client triggers a countdown, signals readiness, and then all clients begin playback from position 0 simultaneously.

2. **Visualizer System** — When no video is playing but audio IS playing (or the bot is idle in an Activity session), the Activity frontend displays a visualizer. The default is a client-side DVD-style bouncing logo screensaver. Server-rendered visualizer engines (projectM, etc.) use demand-driven rendering that consumes zero GPU/CPU when no viewers are connected.

## Glossary

- **Activity_Frontend**: The HTML/JS/CSS application rendered inside a Discord Activity iframe, served by the Activity_Backend at `/activity/`
- **Activity_Backend**: The aiohttp HTTP server (port 8090) that serves frontend assets, HLS streams, status API, and WebSocket connections
- **WebSocket_Hub**: The per-guild WebSocket connection manager (`ws_hub.py`) that synchronizes playback state across connected clients
- **Activity_Streamer**: The per-guild session manager that orchestrates video playback lifecycle and HLS pipeline control
- **Visualizer_Manager**: A new per-guild server-side component that manages visualizer state, rendering lifecycle, and viewer tracking
- **Audio_Feature_Bus**: A subscriber-gated audio analysis pipeline providing FFT, beat detection, and BPM data to visualizer engines
- **HLS_Pipeline**: The existing QSV hardware-accelerated transcode pipeline that converts video sources to HLS segments for streaming
- **Viewer**: A connected WebSocket client in an Activity session for a guild
- **Countdown_Protocol**: The WebSocket message exchange (countdown → ready → start) that synchronizes video start across clients
- **Visualizer_Engine**: A specific rendering implementation (dvd, projectm, vgalizer, varda, fosfora, audiovis, native)
- **DVD_Screensaver**: A client-side CSS/JS animation displaying the bot's Discord avatar bouncing around the screen, changing color on corner hits
- **Demand_Driven_Rendering**: A pattern where GPU/CPU rendering only occurs while at least one viewer is connected
- **Suspension_Debounce**: A 2-second delay before suspending rendering after the last viewer disconnects, absorbing brief reconnections
- **Guild_Config**: Per-guild persistent settings including the selected visualizer engine

## Requirements

### Requirement 1: WebSocket-Driven Video Start

**User Story:** As a viewer, I want videos to start from position 0 with a countdown, so that I see the full video from the beginning regardless of when the HLS pipeline became ready.

#### Acceptance Criteria

1. WHEN the first WebSocket client connects to a video session in BUFFERING or STREAMING state AND elapsed time is less than 5 seconds, THE WebSocket_Hub SHALL send a `countdown` message to all connected clients
2. WHEN the Activity_Frontend receives a `countdown` message, THE Activity_Frontend SHALL play a 3-2-1 countdown animation
3. WHEN the countdown animation completes, THE Activity_Frontend SHALL send a `ready` message to the WebSocket_Hub
4. WHEN the WebSocket_Hub receives a `ready` message from the first client, THE Activity_Backend SHALL mark position 0 as the playback start time
5. WHEN a client connects after playback has already started (elapsed time greater than 5 seconds), THE WebSocket_Hub SHALL send the current playback state with computed position for late-joiner sync without triggering a countdown

### Requirement 2: Visualizer State Machine

**User Story:** As a server operator, I want the visualizer to follow a well-defined state machine, so that resource usage and transitions are predictable and debuggable.

#### Acceptance Criteria

1. THE Visualizer_Manager SHALL maintain one of six runtime states per guild: DISABLED, IDLE_NO_VIEWERS, STARTING, ACTIVE, SUSPENDING, ERROR
2. WHEN a guild has no active video session AND audio is playing AND at least one viewer is connected, THE Visualizer_Manager SHALL transition from IDLE_NO_VIEWERS to STARTING
3. WHEN the visualizer engine initialization completes successfully, THE Visualizer_Manager SHALL transition from STARTING to ACTIVE
4. WHEN the last viewer disconnects, THE Visualizer_Manager SHALL transition from ACTIVE to SUSPENDING
5. WHEN the suspension debounce period (2 seconds) elapses with zero viewers still connected, THE Visualizer_Manager SHALL transition from SUSPENDING to IDLE_NO_VIEWERS
6. WHEN a viewer connects during the SUSPENDING state, THE Visualizer_Manager SHALL cancel the suspension and transition back to ACTIVE
7. IF an unrecoverable error occurs during rendering, THEN THE Visualizer_Manager SHALL transition to ERROR and log the failure reason
8. WHEN a video session starts for the guild, THE Visualizer_Manager SHALL transition to DISABLED regardless of current state

### Requirement 3: Demand-Driven Rendering

**User Story:** As a server operator, I want the visualizer to consume zero GPU/CPU when no one is watching, so that cluster resources are not wasted on invisible rendering.

#### Acceptance Criteria

1. WHILE the Visualizer_Manager is in IDLE_NO_VIEWERS state, THE Visualizer_Manager SHALL consume zero GPU and zero CPU for rendering operations
2. WHILE the Visualizer_Manager is in IDLE_NO_VIEWERS state, THE Visualizer_Manager SHALL preserve all guild configuration (engine type, parameters) without data loss
3. WHEN the first viewer connects to a guild with an active audio session and no video session, THE Visualizer_Manager SHALL start the configured visualizer engine within 5 seconds
4. WHEN the Visualizer_Manager transitions from SUSPENDING to IDLE_NO_VIEWERS, THE Visualizer_Manager SHALL release all GPU resources allocated for rendering
5. WHEN a track change occurs while the Visualizer_Manager is in IDLE_NO_VIEWERS state, THE Visualizer_Manager SHALL update track metadata without starting any rendering process
6. THE Visualizer_Manager SHALL re-check the viewer count after the 2-second debounce period before executing suspension, preventing race conditions from rapid disconnect/reconnect cycles

### Requirement 4: Audio Feature Bus

**User Story:** As a visualizer engine developer, I want audio analysis data (FFT, beat, BPM) to be available via a subscriber-based bus, so that engines receive consistent audio features without duplicating analysis work.

#### Acceptance Criteria

1. THE Audio_Feature_Bus SHALL provide FFT spectrum data, beat detection events, and BPM tracking to subscribed visualizer engines
2. WHILE zero visualizer engines are subscribed to the Audio_Feature_Bus, THE Audio_Feature_Bus SHALL perform zero audio analysis processing
3. WHEN the first visualizer engine subscribes, THE Audio_Feature_Bus SHALL start audio analysis within 100 milliseconds
4. WHEN the last visualizer engine unsubscribes, THE Audio_Feature_Bus SHALL stop all audio analysis processing within 100 milliseconds
5. THE Audio_Feature_Bus SHALL operate independently from the Lavalink audio playback pipeline, ensuring subscription and unsubscription never affect audio output to Discord voice

### Requirement 5: Per-Guild Visualizer Configuration

**User Story:** As a server admin, I want to set the visualizer type for my guild with a slash command, so that my community sees the visualizer style I prefer.

#### Acceptance Criteria

1. WHEN a user invokes `/visualizer type:<engine>`, THE Bot SHALL update the guild's configured visualizer engine to the specified type
2. THE Bot SHALL accept the following engine values for the `type` parameter: dvd, projectm, vgalizer, varda, fosfora, audiovis, native, random, off
3. WHEN no visualizer type has been configured for a guild, THE Visualizer_Manager SHALL use `dvd` as the default engine
4. WHEN the engine type is set to `off`, THE Visualizer_Manager SHALL transition to DISABLED and release all rendering resources
5. WHEN the engine type is set to `random`, THE Visualizer_Manager SHALL select a different engine from the available server-rendered engines on each track change
6. THE Guild_Config SHALL persist the configured engine type across bot restarts, session suspensions, and pod restarts

### Requirement 6: DVD Screensaver (Client-Side Default)

**User Story:** As a viewer, I want to see a fun DVD-style bouncing logo when music is playing but no video is streaming, so that the Activity iframe shows something entertaining.

#### Acceptance Criteria

1. WHEN the Activity_Frontend receives a visualizer state message indicating `dvd` engine is active, THE Activity_Frontend SHALL render a bouncing logo animation using CSS and JavaScript without any server-side rendering
2. THE DVD_Screensaver SHALL use the bot's Discord avatar image as the bouncing logo
3. THE DVD_Screensaver SHALL change the logo tint color each time the logo hits a screen edge or corner
4. THE DVD_Screensaver SHALL animate the logo bouncing at a constant velocity with angle reflection on edge contact
5. WHILE the DVD_Screensaver is active AND audio is playing, THE Activity_Frontend SHALL continue displaying the animation without consuming server GPU resources
6. WHEN a video session starts for the guild, THE Activity_Frontend SHALL stop the DVD_Screensaver and switch to video playback display

### Requirement 7: Server-Rendered Visualizer HLS Pipeline

**User Story:** As a viewer, I want server-rendered visualizers (projectM, shaders, etc.) to stream via HLS, so that they display in the Activity iframe using the same player infrastructure as video.

#### Acceptance Criteria

1. WHILE the Visualizer_Manager is in ACTIVE state with a server-rendered engine (projectm, vgalizer, varda, fosfora, audiovis, native), THE Visualizer_Manager SHALL produce a live HLS stream using the existing QSV-accelerated HLS_Pipeline
2. WHEN the Visualizer_Manager starts a server-rendered engine, THE Activity_Frontend SHALL display a "Starting visualizer..." message until the first HLS segment is available
3. THE server-rendered visualizer HLS pipeline SHALL exist only while at least one viewer is connected, matching the demand-driven rendering lifecycle
4. WHEN the Visualizer_Manager suspends a server-rendered engine, THE Visualizer_Manager SHALL terminate the HLS pipeline process and remove temporary segment files within 5 seconds

### Requirement 8: Audio Independence

**User Story:** As a listener, I want the visualizer lifecycle to never interrupt my music, so that suspending or crashing the visualizer has zero effect on audio playback.

#### Acceptance Criteria

1. THE Visualizer_Manager SHALL operate on a separate execution path from the Lavalink audio playback pipeline, with no shared mutable state that could cause audio interruption
2. IF the Visualizer_Manager transitions to ERROR state, THEN THE Lavalink audio playback for the guild SHALL continue unaffected
3. WHEN the Visualizer_Manager suspends rendering due to zero viewers, THE Lavalink audio playback for the guild SHALL continue unaffected
4. WHEN the Visualizer_Manager starts or restarts a visualizer engine, THE Lavalink audio playback for the guild SHALL continue unaffected

### Requirement 9: Viewer Tracking and WebSocket Integration

**User Story:** As the system, I want accurate real-time viewer counts per guild, so that demand-driven rendering activates and suspends correctly.

#### Acceptance Criteria

1. THE WebSocket_Hub SHALL track the number of connected viewers per guild in real time
2. WHEN the viewer count for a guild transitions from 0 to 1 AND a visualizer is configured (not `off` or DISABLED), THE WebSocket_Hub SHALL notify the Visualizer_Manager to start rendering
3. WHEN the viewer count for a guild transitions from 1 to 0, THE WebSocket_Hub SHALL notify the Visualizer_Manager to begin the suspension debounce
4. WHEN a WebSocket connection drops unexpectedly (network error, timeout), THE WebSocket_Hub SHALL decrement the viewer count within 1 second via the existing heartbeat timeout mechanism
5. THE WebSocket_Hub SHALL broadcast visualizer state changes (engine type, active/suspended status) to all connected viewers for the guild

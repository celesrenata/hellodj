# Requirements Document

## Introduction

The Lavalink Audio Pipe feature enables Lavalink's DSP filter chain (EQ, rotation, tremolo, vibrato, distortion, karaoke) to be applied to audio during HLS video streaming in the Discord Activity. Currently, video audio goes through FFmpeg's HLS pipeline directly (no Lavalink filters), or is routed separately through Lavalink (causing A/V drift). This feature creates a pipe-based bridge: Lavalink decodes and filters the audio, outputs filtered PCM frames to a Unix socket, and FFmpeg consumes that socket as its audio input during video transcoding. The result is perfect A/V sync with full Lavalink filter support on music videos.

The feature spans three repositories:
- **lavaplayer** — New PCM output sink that writes filtered audio frames to a pipe
- **Lavalink** — Server-side API/config to enable audio pipe mode per guild player
- **hellodj bot** — Pipe lifecycle management, FFmpeg integration, filter coordination

Timing-altering filters (timescale) are handled by FFmpeg directly (atempo/asetrate for audio, setpts for video) to avoid the complexity of synchronizing two independent decode clocks. Lavalink's pipe output carries only non-timing DSP effects.

## Glossary

- **Audio_Pipe**: A Unix domain socket or named FIFO on the local filesystem through which Lavalink writes filtered PCM frames and FFmpeg reads them as audio input
- **Lavaplayer**: The Java audio processing engine used by Lavalink for track decoding, filter application, and frame output (celesrenata/lavaplayer fork)
- **Lavalink**: The Java audio server that orchestrates lavaplayer, exposes a REST/WebSocket API, and manages per-guild players (celesrenata/Lavalink fork)
- **HLS_Pipeline**: The FFmpeg-based transcode pipeline in the bot that produces HLS segments (video + audio) for the Discord Activity
- **Filter_Chain**: The sequence of DSP filters Lavalink applies to audio (equalizer, rotation, tremolo, vibrato, distortion, karaoke, low-pass, channel-mix)
- **PCM_Frames**: Raw uncompressed audio samples (signed 16-bit little-endian, stereo, 48kHz) output by lavaplayer after filter processing
- **Pipe_Session**: A single active audio pipe connection between Lavalink and FFmpeg, scoped to one guild's video playback session
- **Bot**: The hellodj Python Discord bot that orchestrates video streaming, Lavalink control, and FFmpeg pipelines
- **Timing_Filters**: Filters that alter audio playback rate or pitch (timescale speed/pitch/rate) — excluded from pipe output and handled by FFmpeg instead
- **Non_Timing_Filters**: DSP filters that modify audio characteristics without altering playback speed (EQ, rotation, tremolo, vibrato, distortion, karaoke, low-pass, channel-mix)

## Requirements

### Requirement 1: Lavaplayer PCM Output Sink

**User Story:** As the system architect, I want lavaplayer to write filtered PCM frames to an external sink, so that downstream consumers (FFmpeg) can receive Lavalink-processed audio without going through the Discord voice pathway.

#### Acceptance Criteria

1. WHEN audio pipe mode is enabled for a player, THE Lavaplayer SHALL write filtered PCM frames (signed 16-bit LE, stereo, 48000 Hz) to the configured Unix domain socket path
2. THE Lavaplayer SHALL output PCM frames at real-time rate (20ms per frame, matching the standard Opus frame duration) regardless of decode speed
3. WHILE the audio pipe sink is active, THE Lavaplayer SHALL apply all configured Non_Timing_Filters to the audio before writing to the pipe
4. WHILE the audio pipe sink is active, THE Lavaplayer SHALL exclude Timing_Filters (timescale speed/pitch/rate changes) from the filter chain applied to pipe output
5. IF the Unix domain socket is not connectable or write fails, THEN THE Lavaplayer SHALL log the error and cease writing without crashing the player
6. WHEN audio pipe mode is enabled, THE Lavaplayer SHALL continue sending audio to the normal Discord voice output simultaneously (dual output)
7. WHEN a track ends or the player is stopped, THE Lavaplayer SHALL flush remaining buffered frames to the pipe and close the connection gracefully

### Requirement 2: Lavalink Audio Pipe API

**User Story:** As the bot developer, I want Lavalink to expose an API for enabling and configuring the audio pipe per guild player, so that the bot can activate pipe output when video streaming begins.

#### Acceptance Criteria

1. THE Lavalink SHALL expose a REST endpoint to enable audio pipe mode for a specific guild's player, accepting the Unix socket path as a parameter
2. THE Lavalink SHALL expose a REST endpoint to disable audio pipe mode for a specific guild's player
3. WHEN audio pipe mode is enabled via the API, THE Lavalink SHALL instruct the underlying lavaplayer instance to begin writing PCM frames to the specified socket path
4. WHEN audio pipe mode is disabled via the API, THE Lavalink SHALL instruct lavaplayer to stop writing to the pipe and close the connection
5. THE Lavalink SHALL include the audio pipe status (enabled, socket path, active/error) in the player state returned by existing player info endpoints
6. IF the specified socket path does not exist or is not a valid Unix socket, THEN THE Lavalink SHALL return an error response with a descriptive message
7. WHEN filters are updated on a player with an active audio pipe, THE Lavalink SHALL apply Non_Timing_Filters to the pipe output and communicate Timing_Filter parameters back to the client via the player state (for FFmpeg to handle)

### Requirement 3: Bot Pipe Lifecycle Management

**User Story:** As the bot operator, I want the bot to create, manage, and clean up Unix sockets for the audio pipe, so that the Lavalink-to-FFmpeg bridge operates reliably throughout a video session.

#### Acceptance Criteria

1. WHEN a video playback session starts with filters enabled, THE Bot SHALL create a Unix domain socket at a predictable path (containing the guild ID and session ID)
2. WHEN the Unix socket is created, THE Bot SHALL instruct Lavalink to enable audio pipe mode targeting that socket path
3. WHEN a video playback session ends (track complete, skip, stop, or error), THE Bot SHALL disable audio pipe mode on Lavalink and remove the Unix socket file
4. IF the bot process crashes or restarts, THEN THE Bot SHALL detect and clean up orphaned socket files from previous sessions on startup
5. THE Bot SHALL create Unix sockets within the tmpfs-backed HLS output directory (/tmp/hellodj_hls/{guild_id}/) to ensure automatic cleanup on pod restart
6. WHILE a Pipe_Session is active, THE Bot SHALL monitor the socket for liveness (Lavalink writing, FFmpeg reading) and restart the pipe if either end disconnects unexpectedly

### Requirement 4: FFmpeg Audio Pipe Input Integration

**User Story:** As the video streaming system, I want FFmpeg to read Lavalink-filtered audio from the Unix socket instead of decoding audio from the source file, so that the HLS output contains filtered audio perfectly synchronized with video.

#### Acceptance Criteria

1. WHEN audio pipe mode is active for a video session, THE HLS_Pipeline SHALL use the Unix socket as its audio input (via ffmpeg -i unix://{socket_path} or equivalent) instead of the source file's audio stream
2. THE HLS_Pipeline SHALL configure the audio pipe input with format specifiers matching Lavaplayer output (f s16le, ar 48000, ac 2)
3. WHEN audio pipe mode is active, THE HLS_Pipeline SHALL map video from the source input and audio from the pipe input into the same HLS segment output
4. IF the audio pipe input stalls or produces no data within 5 seconds of pipeline start, THEN THE HLS_Pipeline SHALL fall back to using the source file's native audio stream and log a warning
5. THE HLS_Pipeline SHALL use the -thread_queue_size parameter on the pipe input to buffer against transient write latency from Lavalink
6. WHEN audio pipe mode is not active (no filters enabled, or pipe unavailable), THE HLS_Pipeline SHALL operate identically to its current behavior (decode audio from source)

### Requirement 5: Timescale Handling via FFmpeg

**User Story:** As a listener using speed/pitch filters on music videos, I want both video and audio to change speed together, so that playback remains synchronized when timescale is applied.

#### Acceptance Criteria

1. WHEN a timescale filter is applied to a guild's player during video mode, THE Bot SHALL apply equivalent speed adjustment to FFmpeg's video stream (using setpts filter) and audio stream (using atempo/asetrate filters)
2. THE Bot SHALL restart the HLS_Pipeline with updated FFmpeg filter parameters when timescale changes during active video playback
3. WHILE timescale is active in video mode, THE Lavalink SHALL report the timescale parameters (speed, pitch, rate) in the player state without applying them to the pipe audio output
4. WHEN timescale is removed (reset to 1.0x), THE Bot SHALL restart the HLS_Pipeline without speed adjustment filters
5. THE Bot SHALL broadcast timescale changes to Activity clients via WebSocket so the frontend can adjust its video playbackRate for immediate visual feedback while the pipeline restarts

### Requirement 6: Filter Change Synchronization

**User Story:** As a listener, I want filter changes (adding/removing EQ, 8D, tremolo, etc.) to take effect on the video audio smoothly, so that the experience feels responsive without causing audio glitches.

#### Acceptance Criteria

1. WHEN a Non_Timing_Filter is added or removed on a player with an active audio pipe, THE Lavalink SHALL apply the filter change to the pipe output within 100ms without interrupting the PCM stream
2. THE Lavaplayer SHALL apply filter changes between frame boundaries (on 20ms frame edges) to avoid mid-frame audio artifacts
3. WHEN filters are reset (all cleared) during video playback, THE Bot SHALL disable audio pipe mode and restart the HLS_Pipeline with native source audio (no pipe needed when no filters are active)
4. WHEN filters are applied during video playback that previously had no filters, THE Bot SHALL enable audio pipe mode and restart the HLS_Pipeline to use the pipe input
5. IF a filter change causes the audio pipe to produce silence or errors, THEN THE Bot SHALL fall back to source audio within 3 seconds and notify the user that filters could not be applied to the video audio

### Requirement 7: Concurrent Voice and Video Audio

**User Story:** As the system, I want Lavalink to continue sending audio to Discord voice while simultaneously outputting to the pipe, so that users not in the Activity can still hear filtered audio in the voice channel.

#### Acceptance Criteria

1. WHILE audio pipe mode is active, THE Lavaplayer SHALL output filtered PCM frames to both the Discord voice pathway (Opus-encoded) and the Unix socket (raw PCM) simultaneously
2. THE Lavaplayer SHALL apply identical Non_Timing_Filters to both outputs so Discord voice listeners and Activity viewers hear the same filtered audio
3. IF the pipe output fails or disconnects, THEN THE Lavaplayer SHALL continue sending audio to Discord voice without interruption
4. WHEN timescale is active in video mode, THE Lavaplayer SHALL apply timescale to the Discord voice output (normal Lavalink behavior) but exclude timescale from the pipe output (FFmpeg handles video-mode timescale)

### Requirement 8: Audio Pipe Health Monitoring

**User Story:** As the bot operator, I want visibility into the audio pipe's health and performance, so that I can diagnose sync issues or pipe failures.

#### Acceptance Criteria

1. WHILE a Pipe_Session is active, THE Bot SHALL track the number of bytes written by Lavalink and bytes read by FFmpeg (via socket stats or frame counting)
2. IF the pipe buffer exceeds 500ms of audio data (48000 samples/sec × 2 channels × 2 bytes × 0.5s = 96000 bytes), THEN THE Bot SHALL log a warning indicating FFmpeg is consuming audio slower than Lavalink produces it
3. WHEN a Pipe_Session ends, THE Bot SHALL log session duration, total frames transferred, and whether it ended cleanly or due to error
4. THE Lavalink SHALL include frame count and write error count in the audio pipe status reported via the player info endpoint

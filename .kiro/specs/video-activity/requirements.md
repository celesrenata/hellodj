# Requirements Document

## Introduction

This feature implements a Discord Activity (embedded iframe application) that enables synchronized video playback within Discord voice channels. It replaces the previously attempted Go Live/screenshare approach which was blocked by Discord's restriction on bot accounts using opcode 18 (STREAM_CREATE). The Activity leverages the existing yt-dlp download pipeline and Intel QSV hardware-accelerated transcoding infrastructure, outputting HLS segments served over HTTPS to a lightweight hls.js-based player embedded in Discord's voice channel UI.

All connected viewers share synchronized playback state via WebSocket — when any viewer plays, pauses, or seeks, all other viewers follow. The player supports subtitle track selection (by language), audio language switching, and per-user volume controls. Subtitles and audio language can optionally be set "for everyone" via a checkbox, while volume is always per-user. The same playback controls (play/pause, seek, skip, stop) are available both in the Activity player and from the "Now Playing" embed in the Discord text channel, which includes a visual seek bar.

## Glossary

- **Activity**: A Discord Embedded App that runs as an iframe inside Discord's voice channel UI, launched via the Discord API and configured in the Developer Portal
- **Bot**: The HelloDJ Python Discord bot (discord.py 2.7.1) running in the hellodj-service Kubernetes namespace
- **HLS**: HTTP Live Streaming — an adaptive bitrate protocol using `.m3u8` playlists and `.ts` media segments
- **QSV**: Intel Quick Sync Video — hardware-accelerated video encode/decode via Intel iGPUs with SR-IOV on the gremlin cluster nodes
- **Embedded_App_SDK**: Discord's JavaScript SDK providing context (guild_id, channel_id, user info) to Activities running inside the Discord client iframe
- **Transcode_Pipeline**: The ffmpeg subprocess that decodes source video and re-encodes to the target format using QSV hardware acceleration
- **Activity_Backend**: The HTTP server component that serves the Activity frontend, HLS segments, and stream metadata API
- **Video_Queue**: An ordered list of pending videos for sequential playback within a guild's Activity session
- **Session**: A per-guild Activity lifecycle from launch through playback to teardown
- **Viewer**: A Discord user participating in the voice channel who can see the Activity iframe

## Requirements

### Requirement 1: Discord Activity Registration and Launch

**User Story:** As a user in a voice channel, I want to launch a video watching Activity so that everyone in the channel can watch videos together inside Discord.

#### Acceptance Criteria

1. WHEN a user runs `/video play <url_or_query>`, THE Bot SHALL launch a Discord Activity in the user's current voice channel
2. WHEN an Activity is already running in the voice channel, THE Bot SHALL enqueue the video rather than launching a new Activity
3. IF the user is not connected to a voice channel, THEN THE Bot SHALL respond with an error message indicating a voice channel connection is required
4. THE Bot SHALL register the Activity with Discord using the Application's configured Activity URL pointing to `hellodj.celestium.life`
5. IF the Discord API rejects the Activity launch request, THEN THE Bot SHALL respond with a user-facing error message describing the failure

### Requirement 2: Video Acquisition

**User Story:** As a user, I want to play YouTube videos by URL or search query so that I can watch any video available on YouTube.

#### Acceptance Criteria

1. WHEN a YouTube URL is provided, THE Bot SHALL resolve the video metadata and download the source file using yt-dlp
2. WHEN a search query (non-URL text) is provided, THE Bot SHALL search YouTube via yt-dlp and resolve the first result
3. WHEN a direct video URL (non-YouTube) with a supported extension is provided, THE Bot SHALL download the file via HTTP
4. IF video resolution exceeds the source quality, THEN THE Bot SHALL download at the highest available quality up to 720p
5. IF yt-dlp fails to resolve or download the video, THEN THE Bot SHALL respond with a descriptive error message and not launch the Activity

### Requirement 3: HLS Transcode Pipeline

**User Story:** As a user, I want videos transcoded efficiently so that playback starts quickly and streams smoothly in the Activity.

#### Acceptance Criteria

1. WHEN a video source file is ready, THE Transcode_Pipeline SHALL encode it to HLS format producing an `.m3u8` playlist and `.ts` segments using QSV hardware acceleration
2. THE Transcode_Pipeline SHALL target a maximum output resolution of 720p (1280×720) for all transcodes
3. THE Transcode_Pipeline SHALL produce HLS segments of 4 seconds duration
4. THE Transcode_Pipeline SHALL include AAC audio at 128 kbps in the HLS output
5. IF QSV hardware decode is unavailable for the source codec, THEN THE Transcode_Pipeline SHALL fall back to software decode while retaining QSV encode
6. IF the GPU device is not available, THEN THE Transcode_Pipeline SHALL report an error and not attempt transcoding
7. THE Transcode_Pipeline SHALL write HLS output to a temporary directory scoped to the guild and session
8. WHEN the first HLS segment is written, THE Transcode_Pipeline SHALL signal readiness so playback can begin before the full transcode completes

### Requirement 4: Activity Frontend (Video Player)

**User Story:** As a viewer in the voice channel, I want a video player embedded in Discord so that I can watch the video without leaving the app.

#### Acceptance Criteria

1. THE Activity_Backend SHALL serve an HTML page containing an hls.js video player at the Activity URL path
2. WHEN the Activity iframe loads, THE Activity frontend SHALL initialize the Embedded_App_SDK and obtain the guild_id and channel_id from Discord context
3. WHEN the player receives the HLS playlist URL from the Activity_Backend, THE Activity frontend SHALL begin playback automatically
4. THE Activity frontend SHALL display the current video title and duration to Viewers
5. THE Activity frontend SHALL render correctly within Discord's Activity iframe dimensions without horizontal scrolling
6. IF HLS playback encounters a fatal error, THEN THE Activity frontend SHALL display an error message to the Viewer

### Requirement 5: Activity Backend API

**User Story:** As the Activity frontend, I need a backend API so that I can discover the current stream state and HLS endpoint for my guild's session.

#### Acceptance Criteria

1. THE Activity_Backend SHALL expose a `GET /activity/status/<guild_id>` endpoint returning the current session state (idle, transcoding, playing, stopped) and video metadata
2. THE Activity_Backend SHALL expose a `GET /activity/stream/<guild_id>/playlist.m3u8` endpoint serving the HLS master playlist for the active session
3. THE Activity_Backend SHALL expose a `GET /activity/stream/<guild_id>/<segment>.ts` endpoint serving individual HLS segments
4. THE Activity_Backend SHALL validate that requests originate from an authenticated Activity session before serving stream data
5. IF no active session exists for the guild_id, THEN THE Activity_Backend SHALL return HTTP 404 with a JSON error body

### Requirement 6: Playback Synchronization

**User Story:** As a viewer joining mid-stream, I want to be synchronized to the current playback position so that everyone watches the same content at the same time.

#### Acceptance Criteria

1. THE Activity_Backend SHALL track a server-side playback start timestamp for each active session
2. WHEN a Viewer's Activity iframe connects, THE Activity_Backend SHALL provide the current playback offset (elapsed time since session start) so the player can seek to the live edge
3. WHEN all Viewers have closed the Activity, THE Session SHALL remain active for 30 seconds before stopping (grace period for rejoins)
4. THE Activity frontend SHALL use the HLS live edge (latest segment) for synchronization rather than implementing custom sync logic

### Requirement 7: Bot Command Integration

**User Story:** As a user, I want to control video playback through familiar slash commands so that I can stop, skip, and manage the queue.

#### Acceptance Criteria

1. WHEN a user runs `/video stop`, THE Bot SHALL stop the current video, terminate the transcode process, and close the Activity session
2. WHEN a user runs `/video skip` or `/video next`, THE Bot SHALL advance to the next video in the Video_Queue or stop if the queue is empty
3. WHEN a user runs `/video queue`, THE Bot SHALL display the current Video_Queue contents as an embed
4. WHEN a user runs `/video play <url_or_query>` while a session is active, THE Bot SHALL append the video to the Video_Queue
5. THE Bot SHALL send a "Now Playing" embed to the text channel when a new video begins playback
6. WHEN a user runs `/video previous` or `/video last`, THE Bot SHALL go back to the previously played video from the history stack

### Requirement 8: Video Queue Management

**User Story:** As a user, I want a video queue with history so that multiple videos play in sequence and I can go back to previously played videos.

#### Acceptance Criteria

1. THE Video_Queue SHALL maintain an ordered list of pending videos per guild
2. WHEN the current video finishes playback, THE Bot SHALL automatically begin transcoding and playing the next video in the Video_Queue
3. WHEN the Video_Queue becomes empty and the current video finishes, THE Bot SHALL close the Activity session
4. THE Video_Queue SHALL support a maximum of 50 entries per guild
5. THE Bot SHALL maintain a Video_History stack of previously played videos (max 20 entries, LIFO)
6. WHEN a video finishes or is skipped, THE Bot SHALL push it onto the Video_History stack
7. WHEN a user invokes "previous", THE Bot SHALL pop the most recent entry from Video_History, push the current video back to the front of the queue, and begin playing the history entry
8. IF Video_History is empty when "previous" is invoked, THE Bot SHALL respond with an error message indicating no previous video is available
5. IF a user attempts to add a video beyond the queue limit, THEN THE Bot SHALL respond with a message indicating the queue is full

### Requirement 9: Session Cleanup

**User Story:** As a system operator, I want temporary files and sessions cleaned up automatically so that disk space is not exhausted.

#### Acceptance Criteria

1. WHEN a Session ends (stop command, queue empty, or grace period expiry), THE Bot SHALL delete all HLS segment files and playlists from the temporary directory for that session
2. WHEN the Bot starts up, THE Bot SHALL scan for and remove any orphaned HLS temporary directories from previous sessions
3. THE Bot SHALL enforce a maximum session duration of 8 hours, after which the Session is automatically stopped and cleaned up
4. IF the transcode process crashes, THEN THE Bot SHALL clean up partial HLS output and report the error to the text channel

### Requirement 10: Synchronized Playback via WebSocket

**User Story:** As a viewer, I want all participants' video players to stay in sync so that when someone pauses, plays, or seeks, it affects everyone watching together.

#### Acceptance Criteria

1. THE Activity_Backend SHALL maintain a WebSocket endpoint at `/activity/ws/{guild_id}` for real-time bidirectional communication between all connected Activity clients for a given guild
2. WHEN the first viewer connects via WebSocket, THE Activity_Backend SHALL begin HLS playback on all connected clients simultaneously
3. WHEN any viewer pauses playback, THE Activity_Backend SHALL broadcast a `pause` event to ALL connected clients so they all pause at the same position
4. WHEN any viewer resumes playback, THE Activity_Backend SHALL broadcast a `play` event to ALL connected clients so they all resume together
5. WHEN any viewer seeks to a new position, THE Activity_Backend SHALL broadcast a `seek` event with the target timestamp to ALL connected clients so they all jump to the same position
6. WHEN a new viewer connects mid-session, THE Activity_Backend SHALL send the current playback state (playing/paused, current position) so the late joiner syncs immediately
7. THE WebSocket protocol SHALL use JSON messages with fields: `type` (pause, play, seek, state), `position` (seconds), `timestamp` (server time for drift correction)

### Requirement 11: Subtitle Support

**User Story:** As a viewer, I want to enable subtitles in my preferred language so that I can follow along with the video content.

#### Acceptance Criteria

1. WHEN the source video contains embedded subtitle tracks, THE Transcode_Pipeline SHALL extract all subtitle tracks and include them as WebVTT sidecar files alongside the HLS segments
2. THE Activity_Backend SHALL expose a `GET /activity/stream/{guild_id}/subtitles/{lang}.vtt` endpoint serving subtitle tracks by language code
3. THE Activity_Backend SHALL expose available subtitle languages in the status API response (`subtitles` field: list of `{lang, label}` objects)
4. THE Activity frontend SHALL display a subtitle track selector in the player controls when subtitle tracks are available
5. WHEN a viewer selects a subtitle language, THE Activity frontend SHALL render subtitles on ONLY that viewer's player (per-user setting) unless the "for everyone" option is checked
6. WHEN a viewer enables a subtitle track with "for everyone" checked, THE Activity_Backend SHALL broadcast a `subtitle_change` WebSocket event to ALL connected clients
7. IF no subtitle tracks are available for the source video, THEN the subtitle selector SHALL be hidden

### Requirement 12: Audio Language Selection

**User Story:** As a viewer, I want to switch the audio language when multiple audio tracks are available so that I can listen in my preferred language.

#### Acceptance Criteria

1. WHEN the source video contains multiple audio tracks, THE Transcode_Pipeline SHALL produce separate HLS variant playlists for each audio language
2. THE Activity_Backend SHALL expose available audio languages in the status API response (`audio_tracks` field: list of `{lang, label}` objects)
3. THE Activity frontend SHALL display an audio language selector in the player controls when multiple audio tracks are available
4. WHEN a viewer selects an audio language, THE Activity frontend SHALL switch to that audio variant on ONLY that viewer's player (per-user setting) unless the "for everyone" option is checked
5. WHEN a viewer switches audio language with "for everyone" checked, THE Activity_Backend SHALL broadcast an `audio_change` WebSocket event to ALL connected clients
6. IF only one audio track is available, THEN the audio language selector SHALL be hidden

### Requirement 13: Volume Controls

**User Story:** As a viewer, I want to adjust the volume of the video player independently from other viewers so that I can set my own comfortable listening level.

#### Acceptance Criteria

1. THE Activity frontend SHALL display a volume slider in the player controls
2. WHEN a viewer adjusts the volume slider, THE change SHALL apply ONLY to that viewer's player (always per-user, never broadcast)
3. THE Activity frontend SHALL persist the viewer's volume preference in localStorage so it survives page reloads
4. THE volume slider SHALL support a range from 0% (muted) to 100% with a mute toggle button
5. THE volume control SHALL NOT have a "for everyone" option — it is always per-user

### Requirement 14: Now Playing Seek Bar in Discord Chat

**User Story:** As a user in the text channel, I want to see a visual progress indicator in the Now Playing embed so that I can tell where in the video we are and control playback from Discord chat.

#### Acceptance Criteria

1. THE Bot SHALL include a text-based seek bar in the Now Playing embed with format `▬🔘▬▬▬▬▬▬▬ 0:30 / 4:24` showing current position relative to total duration
2. THE Now Playing embed SHALL include buttons for playback control: play/pause, skip, stop, and a seek-forward/seek-back control
3. WHEN a user clicks the play/pause button on the Now Playing embed, THE Bot SHALL broadcast the corresponding WebSocket event to all Activity clients
4. WHEN a user clicks the skip button on the Now Playing embed, THE Bot SHALL advance to the next video in the queue (same as `/video skip`)
5. THE Bot SHALL periodically update the Now Playing embed seek bar position (every 30 seconds or on state changes) to reflect current playback progress
6. WHEN a user clicks seek-forward/seek-back buttons, THE Bot SHALL broadcast a seek event to all Activity clients, advancing or rewinding by 10 seconds

### Requirement 15: Activity Authentication and Security

**User Story:** As a system operator, I want the Activity backend secured so that only authorized Discord Activity sessions can access video streams.

#### Acceptance Criteria

1. WHEN the Activity iframe initializes, THE Activity frontend SHALL authenticate with the Activity_Backend using the instance_id provided by the Embedded_App_SDK
2. THE Activity_Backend SHALL validate Activity session tokens against the Discord API before serving stream content
3. THE Activity_Backend SHALL reject requests without a valid session token with HTTP 401
4. THE Activity_Backend SHALL scope stream access so that a session token for one guild cannot access another guild's stream
5. THE WebSocket endpoint SHALL require the same authentication token as the HTTP endpoints

### Requirement 16: Deployment and Infrastructure

**User Story:** As a system operator, I want the Activity backend deployed alongside the existing bot infrastructure so that it shares the GPU resources and TLS ingress.

#### Acceptance Criteria

1. THE Activity_Backend SHALL run as a container within the existing hellodj pod in the hellodj-service namespace
2. THE Activity_Backend SHALL be exposed via the existing `hellodj.celestium.life` ingress with a `/activity/` path prefix
3. THE Activity_Backend SHALL have access to Intel QSV GPU devices via the `intel.com/sriov-gpudevice` resource allocation
4. THE Activity_Backend SHALL share the temporary HLS output volume with the Bot container (emptyDir or shared tmpfs)
5. THE Activity_Backend SHALL log to stdout in JSON format consistent with the existing bot logging

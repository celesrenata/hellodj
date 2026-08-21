# Requirements Document

## Introduction

HelloDJ currently maintains two completely separate playback systems — music (Lavalink/wavelink audio through Discord voice connection) and video (Discord Activities with HLS streaming). Each has its own command tree (`/play`, `/skip`, `/stop` vs `/video play`, `/video stop`, `/video skip`), queue, and state management. This causes user confusion and inconsistent behavior.

The Unified Playback feature consolidates both systems under a single command namespace. The bot auto-detects content type and routes to the appropriate backend (Lavalink for audio, Activity for video). Commands like `/skip`, `/stop`, `/pause`, `/queue` operate on whichever system is active in the user's current voice channel. Additionally, the video system is re-keyed to support multiple simultaneous Activities across different channels in the same guild, and a multi-instance orchestration layer enables music playback in multiple channels via separate Discord bot applications (ToS-compliant).

## Glossary

- **Playback_Router**: The routing layer that inspects content type and directs playback requests to the appropriate backend (Lavalink or Activity).
- **Lavalink_Backend**: The audio playback engine using wavelink/Lavalink, connecting the bot's voice connection to a Discord voice channel. Limited to one voice connection per bot instance per guild.
- **Activity_Backend**: The video playback engine using Discord Activities (embedded iframe) with HLS streaming. Does not consume the bot's voice connection slot.
- **Session_Registry**: The central registry of active ActivityStreamer instances. Currently keyed by guild_id; to be re-keyed by (guild_id, channel_id) composite key.
- **Channel_Session**: A playback session scoped to a specific voice channel, encompassing either a Lavalink audio session or an Activity video session.
- **Content_Classifier**: The component that determines whether a given input (URL, search query, attachment) should be handled as audio or video content.
- **Instance_Orchestrator**: The coordination layer that manages multiple bot applications (separate tokens/application IDs) to enable music playback in multiple voice channels within the same guild.
- **Bot_Instance**: A single Discord bot application with its own token and application ID, capable of joining one voice channel per guild.
- **Guild_State**: The per-guild state dictionary tracking queue, current track, player reference, and playback metadata.
- **Composite_Key**: A tuple of (guild_id, channel_id) used to uniquely identify a playback session within a specific channel.

## Requirements

### Requirement 1: Unified Play Command

**User Story:** As a Discord user, I want to use a single `/play` command for both audio and video content, so that I do not need to remember separate command trees.

#### Acceptance Criteria

1. WHEN a user invokes `/play` with a query, THE Playback_Router SHALL classify the input using the Content_Classifier and route to the appropriate backend.
2. WHEN the Content_Classifier determines the input is audio content (music track, playlist, album, audio stream), THE Playback_Router SHALL route the request to the Lavalink_Backend.
3. WHEN the Content_Classifier determines the input is video content (video URL, video file attachment, video search result), THE Playback_Router SHALL route the request to the Activity_Backend.
4. WHEN the content type is ambiguous (a YouTube URL that has both audio and video), THE Playback_Router SHALL default to audio playback via the Lavalink_Backend.
5. WHEN a user invokes `/play` with an explicit `mode` parameter set to "video", THE Playback_Router SHALL route the request to the Activity_Backend regardless of content classification.
6. WHEN a user invokes `/play` with an explicit `mode` parameter set to "audio", THE Playback_Router SHALL route the request to the Lavalink_Backend regardless of content classification.

### Requirement 2: Unified Playback Control Commands

**User Story:** As a Discord user, I want `/skip`, `/stop`, `/pause`, `/queue`, and `/clear` to work on whichever playback system is active in my voice channel, so that I do not need separate commands for audio and video.

#### Acceptance Criteria

1. WHEN a user invokes `/skip` in a channel with an active Lavalink_Backend session, THE Playback_Router SHALL execute the skip operation on the Lavalink_Backend.
2. WHEN a user invokes `/skip` in a channel with an active Activity_Backend session, THE Playback_Router SHALL execute the skip operation on the Activity_Backend.
3. WHEN a user invokes `/stop` in a channel with an active Lavalink_Backend session, THE Playback_Router SHALL stop playback and disconnect from the Lavalink_Backend.
4. WHEN a user invokes `/stop` in a channel with an active Activity_Backend session, THE Playback_Router SHALL stop the video stream and close the Activity.
5. WHEN a user invokes `/pause` in a channel with an active Lavalink_Backend session, THE Playback_Router SHALL pause audio playback on the Lavalink_Backend.
6. WHEN a user invokes `/pause` in a channel with an active Activity_Backend session, THE Playback_Router SHALL pause video playback on the Activity_Backend.
7. WHEN a user invokes `/queue` in a channel with an active session, THE Playback_Router SHALL display the queue for whichever backend is active in that channel.
8. WHEN a user invokes `/clear` in a channel with an active session, THE Playback_Router SHALL clear the queue for whichever backend is active in that channel.
9. IF no playback session is active in the user's voice channel, THEN THE Playback_Router SHALL respond with a message indicating no active session exists in that channel.

### Requirement 3: Content Classification

**User Story:** As a Discord user, I want the bot to automatically determine whether my input is audio or video, so that I get the correct playback experience without manual intervention.

#### Acceptance Criteria

1. THE Content_Classifier SHALL classify YouTube Music URLs (music.youtube.com) as audio content.
2. THE Content_Classifier SHALL classify Spotify URLs and `spsearch:` prefixed queries as audio content.
3. THE Content_Classifier SHALL classify Tidal URLs whose path does not match `/video/<id>` or `/browse/video/<id>` and `tdsearch:` prefixed queries as audio content.
4. THE Content_Classifier SHALL classify SoundCloud URLs as audio content.
5. THE Content_Classifier SHALL classify file attachments whose MIME type starts with `video/` (e.g., video/mp4, video/webm, video/mkv) as video content.
6. THE Content_Classifier SHALL classify direct URLs ending in video extensions (.mp4, .webm, .mkv, .avi, .mov, .m4v) as video content.
7. THE Content_Classifier SHALL classify YouTube video URLs (youtube.com/watch, youtu.be) as audio content by default.
8. THE Content_Classifier SHALL classify plain text search queries without URL or prefix as audio content by default.
9. THE Content_Classifier SHALL classify Tidal URLs whose path matches `/video/<id>` or `/browse/video/<id>` as video content.
10. IF the input is a URL that does not match any recognized domain (YouTube, Tidal, Spotify, SoundCloud) and does not end in a video extension, THEN THE Content_Classifier SHALL classify it as video content and route it to the URL download pipeline for content-type verification.

### Requirement 4: Multi-Channel Video Sessions

**User Story:** As a server admin, I want video Activities to run simultaneously in multiple voice channels within the same guild, so that different groups can watch different videos at the same time.

#### Acceptance Criteria

1. THE Session_Registry SHALL use a Composite_Key of (guild_id, channel_id) to identify and store active Activity sessions.
2. WHEN a user starts a video Activity in Channel A and another user starts a video Activity in Channel B of the same guild, THE Session_Registry SHALL maintain both sessions independently.
3. WHEN a user invokes a control command, THE Playback_Router SHALL resolve the session using the user's current voice channel_id combined with the guild_id.
4. WHEN a video session is stopped in one channel, THE Session_Registry SHALL leave sessions in other channels of the same guild unaffected.
5. THE Session_Registry SHALL enforce no upper limit on concurrent video sessions per guild beyond Discord's own Activity limits.

### Requirement 5: Single-Channel Music Constraint

**User Story:** As a Discord user, I want clear feedback when music is already playing in another channel, so that I understand why my request cannot be fulfilled by this bot instance.

#### Acceptance Criteria

1. WHILE a Lavalink_Backend session is active in a voice channel for a given Bot_Instance, IF a user in a different voice channel of the same guild invokes `/play` with audio content, THEN THE Playback_Router SHALL reject the request with an error message indicating which channel currently has the active music session by name.
2. WHILE a Lavalink_Backend session is active in Channel A, THE Playback_Router SHALL allow new video Activity requests (via `/video play`) from users in Channel B without blocking or terminating either session.
3. IF a user in the same channel as the active music session invokes `/play` with audio content, THEN THE Lavalink_Backend SHALL enqueue the track into the existing session queue and confirm the addition to the user.
4. IF a user who is not connected to any voice channel invokes `/play`, THEN THE Playback_Router SHALL reject the request with an error message indicating a voice channel connection is required.
5. WHILE a Lavalink_Backend session is active in a voice channel, IF the session ends (via `/stop`, queue completion, or disconnect), THEN THE Playback_Router SHALL immediately allow new Lavalink_Backend play requests from users in any voice channel of the same guild.

### Requirement 6: Multi-Instance Orchestration

**User Story:** As a server admin, I want to enable music playback in multiple voice channels simultaneously by running multiple verified bot instances, so that my community is not limited by Discord's one-voice-connection-per-bot constraint.

#### Acceptance Criteria

1. THE Instance_Orchestrator SHALL maintain a registry of between 2 and 10 Bot_Instance applications, storing each instance's token, application ID, current voice channel assignment, and connection status (available, connected, or unhealthy).
2. WHEN a user invokes `/play` with audio content and the requesting user's voice channel does not already have a Bot_Instance connected, and the primary Bot_Instance is connected to a different voice channel, THE Instance_Orchestrator SHALL assign the first available Bot_Instance (status: available, not connected to any voice channel) to serve the requesting user's channel.
3. WHEN a user invokes `/play` with audio content and a Bot_Instance is already connected to the requesting user's voice channel, THE Instance_Orchestrator SHALL route the request to that existing Bot_Instance without reassignment.
4. IF no Bot_Instance is available (all instances have status "connected" to other voice channels), THEN THE Instance_Orchestrator SHALL respond with a message indicating all music slots are in use and listing the channel name and Bot_Instance display name for each occupied slot.
5. WHEN a Bot_Instance disconnects from a voice channel (via `/stop` command or after 5 minutes of inactivity with no listeners in the channel), THE Instance_Orchestrator SHALL set that instance's status to "available" and clear its voice channel assignment within 5 seconds.
6. THE Instance_Orchestrator SHALL store Bot_Instance credentials (tokens and application IDs) in the encrypted SQLite credential store using the existing Fernet encryption scheme, keyed under the prefix `instance.<index>.`.
7. THE Instance_Orchestrator SHALL share the same Lavalink sidecar across all Bot_Instance connections within the same pod.
8. IF a Bot_Instance fails to respond to a health check within 10 seconds, THEN THE Instance_Orchestrator SHALL mark that instance as "unhealthy", skip it during assignment, and attempt reassignment to the next available instance.

### Requirement 7: Session Resolution by Channel

**User Story:** As a Discord user, I want commands to automatically target the playback session in my current voice channel, so that I do not need to specify which session I mean.

#### Acceptance Criteria

1. WHEN a user invokes any playback control command (play, skip, stop, pause, queue, clear), THE Playback_Router SHALL determine the user's current voice channel from the interaction context.
2. WHEN the user's voice channel has an active Lavalink_Backend session, THE Playback_Router SHALL route the command to the Lavalink_Backend.
3. WHEN the user's voice channel has an active Activity_Backend session, THE Playback_Router SHALL route the command to the Activity_Backend.
4. WHEN the user's voice channel has both an active Lavalink_Backend session and an active Activity_Backend session, THE Playback_Router SHALL route the command to the session with the more recent start timestamp as recorded in the Session_Registry.
5. IF the user is not in a voice channel, THEN THE Playback_Router SHALL respond with an ephemeral message instructing the user to join a voice channel first.
6. IF the user is in a voice channel but no active session (Lavalink_Backend or Activity_Backend) exists for that channel, THEN THE Playback_Router SHALL respond with an ephemeral message indicating no active session exists in that channel.

### Requirement 8: Unified Queue Display

**User Story:** As a Discord user, I want the `/queue` command to show me a clear view of what is playing and queued in my channel, including whether it is audio or video content.

#### Acceptance Criteria

1. WHEN a user invokes `/queue`, THE Playback_Router SHALL display the queue for the active session in the user's voice channel as a Discord embed.
2. THE queue display SHALL indicate the playback type for each item using a prefix emoji: 🎵 for audio items and 🎬 for video items.
3. THE queue display SHALL show the currently playing item with its title (truncated to 100 characters if longer), duration in M:SS or H:MM:SS format when available (or "Live" if the stream has no known duration), and playback type emoji.
4. THE queue display SHALL show 10 items per page and provide previous/next navigation buttons for queues exceeding 10 items, with navigation buttons disabled at the first and last pages respectively.
5. IF both an audio and video session are active in the same channel, THEN THE queue display SHALL show both queues in separate embed sections, each headed with its type label ("Audio" and "Video").
6. IF no playback session is active in the user's voice channel, THEN THE Playback_Router SHALL respond with a message indicating no active session exists in that channel.

### Requirement 9: Legacy Command Deprecation

**User Story:** As a Discord user who learned the old `/video` command tree, I want a transition period where old commands still work but inform me of the new unified commands.

#### Acceptance Criteria

1. WHILE the legacy transition period is enabled via the bot's configuration, THE VideoCog SHALL accept `/video play`, `/video stop`, `/video skip`, `/video previous`, and `/video queue` commands and route them to the equivalent unified command logic.
2. WHILE the legacy transition period is active, WHEN a user invokes a legacy `/video` command, THE VideoCog SHALL execute the requested action and append a deprecation notice to the response indicating the equivalent unified command (e.g., "Use `/play mode:video` instead").
3. THE deprecation notice SHALL be included inline in the command's normal response message so the user sees both the action result and the migration guidance in a single reply.
4. WHERE a guild member with Manage Guild permission has configured immediate migration for the guild, THE VideoCog SHALL reject legacy commands with an ephemeral message listing the equivalent unified command instead of executing the action.
5. IF the legacy transition period is disabled in the bot's configuration, THEN THE VideoCog SHALL reject all `/video` subcommands with an ephemeral message indicating the commands have been removed and listing their unified replacements.

### Requirement 10: Session Persistence

**User Story:** As a Discord user, I want my playback session to survive bot restarts, so that my queue is not lost during maintenance or crashes.

#### Acceptance Criteria

1. THE session persistence layer SHALL store Channel_Session state keyed by (guild_id, channel_id) Composite_Key.
2. WHEN the bot restarts, THE session persistence layer SHALL restore each Lavalink_Backend session that had auto_resume enabled by reconnecting to the saved voice channel, rebuilding the queue from persisted track data, and resuming playback of the saved current track at the stored position.
3. THE session persistence layer SHALL store the session type (audio or video) alongside existing queue and track state.
4. WHEN the bot restarts, THE session persistence layer SHALL NOT auto-resume Activity_Backend sessions (Activities require user-initiated relaunch); the persisted session data SHALL remain on disk so users can view the saved queue.
5. WHEN the session persistence layer loads session data and encounters entries keyed by guild_id only (legacy format without channel_id), THE session persistence layer SHALL migrate those entries to Composite_Key format using the voice_channel_id stored within the session record as the channel_id component.
6. IF restoration of a Lavalink_Backend session fails (voice channel unavailable, bot lacks permission to join, or track resolution fails), THEN THE session persistence layer SHALL mark the session as suspended rather than discard it, and SHALL log the failure reason.
7. IF a legacy session record lacks a stored voice_channel_id, THEN THE session persistence layer SHALL skip migration for that entry and log a warning rather than crash or discard other valid sessions.

# Requirements Document

## Introduction

This feature extends the HelloDJ video streaming subsystem with three new video source types that integrate into the existing Activity engine (HLS transcode pipeline → Discord Activity iframe playback). The new sources are: Tidal music videos resolved via the Tidal API using existing OAuth credentials, general video links (arbitrary direct URLs), and direct video file uploads from Discord users. All three sources produce a `VideoSource` object compatible with `ActivityStreamer.play()`, feeding into the same QSV-accelerated HLS transcode pipeline and synchronized Activity playback experience.

## Glossary

- **Bot**: The HelloDJ Python Discord bot (discord.py 2.7.1) running in the hellodj-service Kubernetes namespace
- **Activity_Engine**: The existing video Activity subsystem comprising ActivityStreamer, HLS TranscodePipeline, ActivityBackend, and the Discord Activity iframe player
- **VideoSource**: A dataclass representing a resolved video file ready for transcoding, with fields: source_type, file_path, title, duration_seconds, metadata, audio_url, cleanup_on_finish
- **ActivityStreamer**: The per-guild session manager that orchestrates video playback lifecycle, queue, and history
- **Tidal_API**: The Tidal streaming service HTTP API for resolving and fetching music video content
- **Tidal_Stream_Sidecar**: The existing `tidal-stream` container (port 8801) that handles Tidal OAuth token management and direct audio streaming
- **OAuth_Store**: The credential storage system (encrypted SQLite database + legacy `data/oauth.json`) holding Tidal OAuth refresh and access tokens
- **TidalResolver**: The new component responsible for resolving Tidal music video URLs/IDs to downloadable video stream URLs
- **URLDownloader**: The existing component that downloads video files from arbitrary HTTP URLs with Content-Type validation and size limits
- **Now_Playing_Embed**: The Discord embed message showing current video title, seek bar, and control buttons
- **Video_Queue**: The ordered list of pending VideoSource entries for sequential playback within a guild's Activity session
- **Uploader**: The Discord user who attaches a video file directly to a message for playback in the Activity
- **HLS_TranscodePipeline**: The ffmpeg QSV-accelerated pipeline that converts source video files into HLS segments for Activity playback

## Requirements

### Requirement 1: Tidal Music Video URL Resolution

**User Story:** As a user, I want to play Tidal music videos by pasting a Tidal URL so that I can watch music videos from my Tidal library in the Activity.

#### Acceptance Criteria

1. WHEN a user provides a Tidal music video URL (matching `tidal.com/*/video/*` or `tidal.com/video/*` patterns), THE TidalResolver SHALL extract the numeric video ID from the URL path and resolve the video metadata (title, duration, artist) from the Tidal API with a request timeout of 15 seconds
2. WHEN a valid Tidal music video ID is resolved, THE TidalResolver SHALL fetch the highest-quality video stream URL (preferring resolution ≥ 720p) from the Tidal API using the stored OAuth access token
3. WHEN the Tidal video stream URL is obtained, THE TidalResolver SHALL download the video content to a temporary file in the video download directory with a download timeout of 10 minutes and produce a VideoSource with source_type="tidal"
4. THE TidalResolver SHALL include the artist name and track title in the VideoSource metadata for display in the Now_Playing_Embed
5. IF the Tidal OAuth access token is expired, THEN THE TidalResolver SHALL refresh the token using the stored refresh token before retrying the API request (one retry only)
6. IF the Tidal API returns an authentication error after token refresh, THEN THE TidalResolver SHALL respond with an error message indicating Tidal authentication has failed and re-login is required
7. IF the video ID extracted from the URL does not correspond to a valid Tidal music video (HTTP 404 or empty response), THEN THE TidalResolver SHALL respond with an error message indicating the Tidal video was not found
8. IF the resolved Tidal track does not have a video stream available (audio-only track), THEN THE TidalResolver SHALL respond with an error message indicating this track has no music video

### Requirement 2: Tidal Music Video Search

**User Story:** As a user, I want to search for Tidal music videos by artist or title so that I can find and play music videos without needing the exact URL.

#### Acceptance Criteria

1. WHEN a user provides a query prefixed with `tidal:` (e.g., `tidal:artist - title`) where the text after the prefix is between 1 and 200 characters, THE TidalResolver SHALL strip the `tidal:` prefix and search the Tidal API for music videos matching the remaining query text
2. WHEN search results are returned, THE TidalResolver SHALL select the first music video result and resolve it to a VideoSource following the same download and metadata extraction flow as Tidal URL resolution (Requirement 1)
3. IF no music video results are found for the query, THEN THE TidalResolver SHALL respond with an error message indicating no Tidal music videos matched the query
4. IF Tidal credentials are not present in the OAuth_Store (no refresh token or access token stored), THEN THE TidalResolver SHALL respond with an error message indicating Tidal is not connected
5. IF the query after stripping the `tidal:` prefix is empty or contains only whitespace, THEN THE TidalResolver SHALL respond with an error message indicating a search query is required
6. IF the Tidal API returns a non-success response (network error, timeout, or HTTP 4xx/5xx other than authentication errors), THEN THE TidalResolver SHALL respond with an error message indicating the Tidal search failed without crashing the bot or affecting other video sources
7. IF the selected search result cannot be resolved to a playable video stream (geo-restricted, unavailable, or stream fetch failure), THEN THE TidalResolver SHALL respond with an error message indicating the video is unavailable

### Requirement 3: Tidal Integration with Activity Engine

**User Story:** As a user, I want Tidal music videos to play in the same Activity as YouTube videos so that the experience is unified regardless of source.

#### Acceptance Criteria

1. WHEN a Tidal VideoSource is produced, THE Bot SHALL pass the VideoSource to ActivityStreamer.play() using the same invocation path as YouTube and URL sources, without source-type-specific branching in the play pipeline
2. WHEN a Tidal video begins playback, THE Now_Playing_Embed SHALL display the artist name and track title from the VideoSource metadata dict formatted as "Artist — Title" in the embed title field
3. WHILE a Tidal video is playing, THE Activity_Engine SHALL accept queue, skip, previous, and stop commands and execute them identically to YouTube and URL sources (enqueueing, advancing, reversing, and terminating the session respectively)
4. THE VideoSource produced by TidalResolver SHALL set cleanup_on_finish to True so temporary files are removed after playback

### Requirement 4: General Link Video Support

**User Story:** As a user, I want to paste any direct video URL and have it play in the Activity so that I can watch videos from any hosting platform.

#### Acceptance Criteria

1. WHEN a user provides a URL that is not recognized as YouTube or Tidal, THE Bot SHALL attempt to download the video file using the URLDownloader with a connection timeout of 10 seconds
2. WHEN the URLDownloader successfully downloads a video file, THE Bot SHALL produce a VideoSource with source_type="url" and pass it to ActivityStreamer.play()
3. THE Now_Playing_Embed SHALL display the filename extracted from the URL path as the title for URL-sourced videos, falling back to the URL hostname when no filename is present in the path
4. THE URLDownloader SHALL validate that the response Content-Type header starts with "video/" before downloading the response body, treating a missing Content-Type header as a validation failure
5. THE URLDownloader SHALL enforce a maximum download size of 100MB for URL-sourced videos, aborting the download and deleting partial files when the limit is exceeded
6. IF the URL is not publicly accessible (HTTP 401 or 403), THEN THE Bot SHALL respond with an error message indicating the URL is not accessible
7. IF the URL does not contain video content (Content-Type validation failure), THEN THE Bot SHALL respond with an error message indicating the URL does not point to a video
8. IF the URL is unreachable or the connection times out, THEN THE Bot SHALL respond with an error message indicating the URL could not be reached

### Requirement 5: Direct Video Upload via Discord Attachment

**User Story:** As a user, I want to upload a video file directly to the bot so that I can play personal or local videos in the Activity without needing a hosting URL.

#### Acceptance Criteria

1. WHEN a user sends a message with a video file attachment in a channel where the bot has read-message permissions, THE Bot SHALL download the first video attachment and produce a VideoSource with source_type="upload", title set to the attachment filename (without extension), and duration_seconds populated from ffprobe output
2. THE Bot SHALL validate that the attachment content type or file extension matches a supported video format (mp4, mkv, webm, avi, mov, m4v) and that the attachment file size does not exceed 500 MB
3. IF the attachment format is unsupported or the file size exceeds 500 MB, THEN THE Bot SHALL respond with an error message indicating the rejection reason and not download the file
4. THE Bot SHALL validate the uploaded file using ffprobe (with a timeout of 10 seconds) to confirm the file contains at least one video stream before enqueuing
5. IF the uploaded file fails ffprobe validation, THEN THE Bot SHALL respond with an error message indicating the file is not a playable video and delete the downloaded file
6. IF the attachment download fails due to a network error or timeout, THEN THE Bot SHALL respond with an error message indicating the download failed
7. THE VideoSource metadata SHALL include the Uploader's Discord display name as the "uploader" field
8. THE VideoSource SHALL set cleanup_on_finish to True so the downloaded file is removed after playback

### Requirement 6: Upload Source Attribution

**User Story:** As a viewer in the Activity, I want to see who uploaded a video so that I know the source of user-uploaded content.

#### Acceptance Criteria

1. WHEN a video with source_type="upload" is playing, THE Now_Playing_Embed SHALL display the Uploader's Discord display name as the source attribution in the format "Uploaded by {display_name}" below the video title
2. WHEN a video with source_type="upload" appears in the Video_Queue listing, THE queue display SHALL include the Uploader's Discord display name in parentheses after the video title (e.g., "video title (uploaded by display_name)")
3. IF the current video has source_type="upload", THEN THE Activity_Engine status API SHALL include an `uploader` string field in the session metadata response containing the Uploader's Discord display name
4. IF the current video does not have source_type="upload", THEN THE Activity_Engine status API SHALL set the `uploader` field to null in the session metadata response
5. WHEN the Activity frontend receives a non-null `uploader` field from the status API, THE Activity frontend SHALL display the uploader attribution alongside the video title in the player UI

### Requirement 7: Upload Size Validation

**User Story:** As a system operator, I want upload sizes validated so that excessive uploads do not exhaust disk space or slow down transcoding.

#### Acceptance Criteria

1. THE Bot SHALL enforce a maximum video upload size of 500MB for directly uploaded Discord message attachments
2. IF the attachment size exceeds 500MB, THEN THE Bot SHALL respond with an error message indicating the file is too large and specifying the maximum allowed size of 500MB
3. WHEN a user provides a video as a Discord message attachment, THE Bot SHALL check the attachment size from the Discord attachment metadata before downloading the file
4. IF the Discord attachment metadata does not include a file size, THEN THE Bot SHALL reject the attachment with an error message indicating the file cannot be validated

### Requirement 8: Source Type Routing

**User Story:** As a developer, I want a unified source resolution flow so that the video cog routes queries to the correct resolver based on input type.

#### Acceptance Criteria

1. WHEN a user runs `/video play <query>`, THE Bot SHALL classify the input by evaluating the following categories in priority order: (1) YouTube URL (matching `youtube.com` or `youtu.be` domains), (2) Tidal URL (matching `tidal.com` domain with a `/video/` path segment), (3) Tidal search (query prefixed with `tidal:`), (4) general URL (any other URL with a video file extension from the set mp4, mkv, webm, avi, mov, m4v, or whose HTTP response Content-Type starts with `video/`), (5) YouTube search query (any remaining non-URL text)
2. WHEN the input is classified as a YouTube URL or a YouTube search query, THE Bot SHALL route the query to the YouTubeResolver
3. WHEN the input is classified as a Tidal URL, THE Bot SHALL route the query to the TidalResolver
4. WHEN the input is classified as a Tidal search (after stripping the `tidal:` prefix), THE Bot SHALL route the query to the TidalResolver search function
5. WHEN the input is classified as a general URL, THE Bot SHALL route the query to the URLDownloader
6. IF the input is a URL that does not match YouTube or Tidal domains and has no recognized video file extension, THEN THE Bot SHALL attempt the URLDownloader first, and if the URLDownloader returns an error within 10 seconds, THE Bot SHALL fall back to the YouTubeResolver treating the URL as a search query
7. IF the routed resolver returns an error, THEN THE Bot SHALL respond with an ephemeral error message indicating the source type and failure reason, without attempting a different resolver (except as specified in criterion 6)

### Requirement 9: Tidal OAuth Token Management

**User Story:** As a system operator, I want Tidal OAuth tokens managed automatically so that music video playback continues without manual intervention.

#### Acceptance Criteria

1. THE TidalResolver SHALL read Tidal OAuth credentials (access token, refresh token, expiration timestamp, and issuing client ID) from the existing OAuth_Store (credential database) using the same key namespace (`tidal.*`) as the Tidal_Stream_Sidecar
2. WHEN the Tidal access token is expired (determined by the stored expiration timestamp with a 5-minute safety buffer) or the Tidal API returns HTTP 401 during a video resolution attempt, THE TidalResolver SHALL use the refresh token to obtain a new access token from the Tidal OAuth endpoint (`https://auth.tidal.com/v1/oauth2/token`) with a request timeout of 15 seconds
3. WHEN a token refresh succeeds, THE TidalResolver SHALL update the stored access token and expiration timestamp in the OAuth_Store, and IF the refresh response includes a new refresh token, THE TidalResolver SHALL also update the stored refresh token
4. IF the refresh token itself is invalid or expired (Tidal OAuth endpoint returns HTTP 400 or 401 to the refresh request), THEN THE TidalResolver SHALL respond with a user-facing error message indicating Tidal re-authentication is required, log the failure at warning level, and continue processing other video sources without interrupting the bot
5. THE TidalResolver SHALL use the stored issuing client ID (`tidal.issuing_client_id`) for refresh requests, falling back to the tidalapi internal client ID if no issuing client ID is stored, to match the client that originally issued the refresh token

### Requirement 10: Video Upload via `/video play` Command

**User Story:** As a user, I want to upload a video directly through the `/video play` command attachment option so that I have a consistent interface for all video sources.

#### Acceptance Criteria

1. WHEN a user runs `/video play` with a file attachment and no query text, THE Bot SHALL process the attachment using the same validation pipeline as message-based uploads: type detection (MIME type with filename extension fallback), ffprobe integrity check, and a maximum file size of 500 MB
2. IF the attachment fails validation (unsupported type, ffprobe detects a corrupt/unplayable file, or file exceeds 500 MB), THEN THE Bot SHALL respond with an ephemeral error message indicating the specific failure reason and SHALL NOT launch or enqueue a video
3. THE Bot SHALL use the user's Discord display name as the uploader attribution in the video metadata
4. IF both a query and a file attachment are provided, THEN THE Bot SHALL use the attachment and ignore the text query
5. IF the attachment type detection returns "audio", "image", or "unknown" rather than "video", THEN THE Bot SHALL respond with an ephemeral error message indicating only video files are accepted

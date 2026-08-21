# Requirements Document

## Introduction

The Music Video Command feature extends the `/play music_video` subcommand to resolve queries from multiple source providers (YouTube, YouTube Music, Tidal, Spotify metadata) and route all resolved videos through the Video Activity pipeline (HLS transcode → Activity iframe). The command detects the input type (URL, search query) and provider, resolves to a playable video file, and feeds it into the existing ActivityStreamer infrastructure. Source priority favors native Tidal music videos over YouTube when available.

## Glossary

- **Music_Video_Command**: The `/play music_video <query>` slash command that accepts URLs, track links, or plain text searches and resolves them to music video content for playback through the Video Activity
- **Video_Activity**: The Discord Activity (embedded iframe app) that plays HLS-transcoded video content in a voice channel, consisting of ActivityStreamer → HLSTranscodePipeline → Activity iframe with hls.js
- **Source_Resolver**: A component that takes a user query and produces a VideoSource object suitable for the Video Activity pipeline
- **Query_Classifier**: A component that inspects the user input and determines which Source_Resolver to invoke based on URL domain, path structure, or prefix patterns
- **VideoSource**: The data object produced by resolvers containing a local file path, title, source_type, metadata, and cleanup flag — consumed by ActivityStreamer
- **YouTubeResolver**: The existing resolver that downloads YouTube videos via yt-dlp and produces VideoSource objects
- **TidalResolver**: The existing resolver that downloads Tidal native video content via the Tidal API and produces VideoSource objects
- **Spotify_Metadata_Extractor**: A component that calls the Spotify API to retrieve artist and title metadata from a Spotify track URL for use as a YouTube search query
- **Fallback_Search**: The process of constructing a YouTube search query (e.g., "{artist} - {title} official music video") when the primary source does not have native video content
- **ActivityStreamer**: The per-guild Activity session manager that orchestrates video playback, queuing, and lifecycle
- **HLS_Pipeline**: The HLSTranscodePipeline that converts video files to HLS segments for browser playback via hls.js

## Requirements

### Requirement 1: Query Classification

**User Story:** As a Discord user, I want the music video command to automatically detect what type of input I provide, so that I don't need to specify the source manually.

#### Acceptance Criteria

1. WHEN a YouTube URL (youtube.com or youtu.be domain) is provided as query, THE Query_Classifier SHALL classify the input as "youtube_direct"
2. WHEN a YouTube Music URL (music.youtube.com domain) is provided as query, THE Query_Classifier SHALL classify the input as "youtube_music"
3. WHEN a Tidal video URL (tidal.com/video or tidal.com/browse/video path) is provided as query, THE Query_Classifier SHALL classify the input as "tidal_video"
4. WHEN a Tidal track URL (tidal.com/track or tidal.com/browse/track path) is provided as query, THE Query_Classifier SHALL classify the input as "tidal_track"
5. WHEN a Spotify track URL (open.spotify.com/track path) is provided as query, THE Query_Classifier SHALL classify the input as "spotify_track"
6. WHEN a plain text query without URL scheme is provided, THE Query_Classifier SHALL classify the input as "text_search"
7. THE Query_Classifier SHALL classify exactly one source type per input

### Requirement 2: YouTube Direct Resolution

**User Story:** As a Discord user, I want to paste a YouTube video URL and have it play immediately as a music video, so that I can share specific videos with my friends.

#### Acceptance Criteria

1. WHEN the Query_Classifier returns "youtube_direct", THE Source_Resolver SHALL pass the URL to the existing YouTubeResolver
2. WHEN the YouTubeResolver produces a VideoSource, THE Music_Video_Command SHALL route the VideoSource to the Video_Activity pipeline
3. IF the YouTubeResolver fails to resolve the URL, THEN THE Music_Video_Command SHALL display an error message to the user

### Requirement 3: YouTube Music Resolution

**User Story:** As a Discord user, I want to paste a YouTube Music URL and have the corresponding music video play, so that I can use links from my YouTube Music library.

#### Acceptance Criteria

1. WHEN the Query_Classifier returns "youtube_music", THE Source_Resolver SHALL extract the video ID from the YouTube Music URL
2. WHEN a video ID is extracted from a YouTube Music URL, THE Source_Resolver SHALL construct a standard YouTube URL using the extracted video ID and pass it to the YouTubeResolver
3. IF the YouTube Music URL does not contain a valid video ID, THEN THE Music_Video_Command SHALL display an error message to the user

### Requirement 4: Tidal Native Video Resolution

**User Story:** As a Discord user, I want to paste a Tidal video URL and have the native high-quality music video play, so that I can enjoy Tidal's video content.

#### Acceptance Criteria

1. WHEN the Query_Classifier returns "tidal_video", THE Source_Resolver SHALL pass the URL to the existing TidalResolver via resolve_url
2. WHEN the TidalResolver produces a VideoSource, THE Music_Video_Command SHALL route the VideoSource to the Video_Activity pipeline
3. IF the TidalResolver fails with a non-recoverable error, THEN THE Music_Video_Command SHALL display an error message to the user
4. IF the TidalResolver fails with a recoverable error, THEN THE Music_Video_Command SHALL fall back to searching YouTube for the video title

### Requirement 5: Tidal Track to Video Resolution

**User Story:** As a Discord user, I want to paste a Tidal track link and have the bot find the music video version of that track, so that I don't need to manually find the video URL.

#### Acceptance Criteria

1. WHEN the Query_Classifier returns "tidal_track", THE Source_Resolver SHALL extract the track ID from the Tidal track URL
2. WHEN a track ID is extracted, THE Source_Resolver SHALL query the Tidal API to determine whether a music video version of the track exists
3. WHEN the Tidal API confirms a video version exists, THE Source_Resolver SHALL resolve the video using the TidalResolver
4. WHEN the Tidal API indicates no video version exists, THE Source_Resolver SHALL extract the track artist and title from Tidal metadata and perform a Fallback_Search on YouTube using "{artist} - {title} official music video"
5. IF the Tidal API call fails, THEN THE Source_Resolver SHALL extract available metadata from the URL context and perform a Fallback_Search on YouTube

### Requirement 6: Spotify Track to Video Resolution

**User Story:** As a Discord user, I want to paste a Spotify track link and have the bot find the corresponding music video on YouTube, so that I can use my Spotify links to watch music videos.

#### Acceptance Criteria

1. WHEN the Query_Classifier returns "spotify_track", THE Spotify_Metadata_Extractor SHALL call the Spotify API to retrieve the track artist and title
2. WHEN the Spotify_Metadata_Extractor retrieves artist and title, THE Source_Resolver SHALL search YouTube for "{artist} - {title} official music video" using the YouTubeResolver
3. IF the Spotify API call fails, THEN THE Music_Video_Command SHALL display an error message indicating the track metadata could not be retrieved
4. IF the YouTube search returns no results for the Spotify track metadata, THEN THE Music_Video_Command SHALL display a message indicating no music video was found for the track

### Requirement 7: Plain Text Search Resolution

**User Story:** As a Discord user, I want to type an artist or song name and have the bot find and play the official music video, so that I can quickly watch music videos without finding URLs.

#### Acceptance Criteria

1. WHEN the Query_Classifier returns "text_search", THE Source_Resolver SHALL search YouTube for "{query} official music video" using the YouTubeResolver
2. WHEN the YouTubeResolver returns a VideoSource from the search, THE Music_Video_Command SHALL route the VideoSource to the Video_Activity pipeline
3. IF the YouTubeResolver search returns no results, THEN THE Music_Video_Command SHALL display a message indicating no music video was found

### Requirement 8: Video Activity Session Management

**User Story:** As a Discord user, I want the music video to play immediately if no video is active, or be queued if one is already playing, so that playback is seamless.

#### Acceptance Criteria

1. WHEN a VideoSource is resolved and no Video_Activity session is active in the user's voice channel, THE Music_Video_Command SHALL launch a new Video_Activity session and begin playback
2. WHEN a VideoSource is resolved and a Video_Activity session is already active in the user's voice channel, THE Music_Video_Command SHALL enqueue the VideoSource to the existing session queue
3. WHEN a video is enqueued, THE Music_Video_Command SHALL respond with the queue position and video title
4. THE Music_Video_Command SHALL require the user to be in a voice channel before resolving any query
5. IF the Video_Activity fails to launch, THEN THE Music_Video_Command SHALL display an error message and clean up the resolved VideoSource

### Requirement 9: Source Priority for Search Queries

**User Story:** As a Discord user, I want the bot to prefer higher-quality native video sources when searching, so that I get the best available music video experience.

#### Acceptance Criteria

1. WHEN performing a Fallback_Search for a Tidal track, THE Source_Resolver SHALL first check Tidal for a native video before searching YouTube
2. WHEN multiple sources are available for the same content, THE Source_Resolver SHALL prefer Tidal native video over YouTube results
3. WHEN only YouTube is available as a source, THE Source_Resolver SHALL use YouTube without additional delay from checking unavailable sources

### Requirement 10: Error Handling and User Feedback

**User Story:** As a Discord user, I want clear error messages when something goes wrong, so that I know what happened and what I can try instead.

#### Acceptance Criteria

1. WHEN the user provides no query to the Music_Video_Command, THE Music_Video_Command SHALL respond with a usage hint indicating a URL or search query is required
2. WHEN a resolver encounters a network timeout, THE Music_Video_Command SHALL respond with an error indicating the source is temporarily unavailable
3. WHEN the user is not in a voice channel, THE Music_Video_Command SHALL respond with an error message before attempting any resolution
4. THE Music_Video_Command SHALL respond to all error conditions within 10 seconds of the command invocation
5. IF an unexpected error occurs during resolution, THEN THE Music_Video_Command SHALL log the error details and respond with a generic error message to the user

# Implementation Plan: Music Video Command

## Overview

Implement the `/play music_video` subcommand that classifies user input (URLs or text searches), resolves them to `VideoSource` objects via the appropriate sub-resolver (YouTube, Tidal, Spotify metadata → YouTube fallback), and routes them through the existing Video Activity pipeline. The implementation adds a pure classifier, a Spotify metadata extractor, and an orchestrating `MusicVideoResolver` that composes existing resolvers with fallback logic.

## Tasks

- [x] 1. Create MusicVideoQueryClassifier (pure classification layer)
  - [x] 1.1 Create `bot/video/music_video_resolver.py` with classifier enums and dataclasses
    - Define `MusicVideoSourceType` enum with values: youtube_direct, youtube_music, tidal_video, tidal_track, spotify_track, text_search
    - Define `MusicVideoClassification` frozen dataclass with fields: source_type, original_query, extracted_id
    - Implement `classify_music_video_query(query: str) -> MusicVideoClassification` as a pure function
    - Classification priority order: youtube.com/youtu.be → music.youtube.com → tidal.com/video → tidal.com/track → open.spotify.com/track → text_search (no `://`)
    - Extract video/track IDs from YouTube Music (`v` param), Tidal track (path segment), Spotify track (path segment)
    - Return `text_search` for any input without a URL scheme
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7_

  - [ ]* 1.2 Write property tests for classifier in `tests/test_music_video_classifier.py`
    - **Property 1: URL classification correctness**
    - Generate URLs with recognized provider domains (youtube.com, youtu.be, music.youtube.com, tidal.com/video, tidal.com/track, open.spotify.com/track) and verify correct MusicVideoSourceType is returned
    - **Validates: Requirements 1.1, 1.2, 1.3, 1.4, 1.5**

  - [ ]* 1.3 Write property test for non-URL classification
    - **Property 2: Non-URL input classification**
    - Generate arbitrary strings without `://` and verify TEXT_SEARCH is always returned
    - **Validates: Requirements 1.6**

  - [ ]* 1.4 Write property test for classification totality
    - **Property 3: Classification totality and uniqueness**
    - Generate arbitrary strings (including empty, malformed URLs, random bytes) and verify exactly one valid MusicVideoSourceType is returned without raising
    - **Validates: Requirements 1.7**

  - [ ]* 1.5 Write property test for YouTube Music ID round-trip
    - **Property 4: YouTube Music video ID round-trip**
    - Generate valid 11-char video ID strings (alphanumeric + `-_`), construct `music.youtube.com/watch?v={id}` URLs, classify, and verify extracted_id matches the original
    - **Validates: Requirements 3.1, 3.2**

  - [ ]* 1.6 Write property test for Tidal track ID round-trip
    - **Property 5: Tidal track ID round-trip**
    - Generate numeric track IDs (1 to 10^10), construct Tidal track URLs, classify, and verify extracted_id matches the original
    - **Validates: Requirements 5.1**

- [x] 2. Implement SpotifyMetadataExtractor
  - [x] 2.1 Add `SpotifyMetadataExtractor` class to `bot/video/music_video_resolver.py`
    - Define `TrackMetadata` frozen dataclass with fields: artist, title, isrc (optional)
    - Define `SpotifyMetadataError` exception class
    - Implement client_credentials OAuth2 flow using `cfg("spotify.client_id")` and `cfg("spotify.client_secret")`
    - Cache access tokens based on `expires_in` response field
    - Implement `extract(track_id: str) -> TrackMetadata` async method
    - Call Spotify Web API `GET /v1/tracks/{track_id}` for metadata
    - Apply 10s timeout on all Spotify API calls
    - Raise `SpotifyMetadataError` on auth failure, track not found, or network error
    - _Requirements: 6.1, 6.3, 10.2_

  - [ ]* 2.2 Write unit tests for SpotifyMetadataExtractor in `tests/test_music_video_resolver.py`
    - Mock Spotify API responses for successful metadata retrieval
    - Test auth failure produces SpotifyMetadataError
    - Test track-not-found produces SpotifyMetadataError
    - Test network timeout produces SpotifyMetadataError
    - _Requirements: 6.1, 6.3_

- [x] 3. Checkpoint - Ensure classifier and Spotify extractor tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 4. Implement MusicVideoResolver orchestrator
  - [x] 4.1 Add `MusicVideoResolver` class to `bot/video/music_video_resolver.py`
    - Define `MusicVideoResolverError` exception with `user_message` field
    - Implement `resolve(query: str) -> VideoSource` async method
    - Wire classification → dispatch → sub-resolver based on source_type
    - Instantiate and compose existing `YouTubeResolver` and `TidalResolver`
    - Instantiate `SpotifyMetadataExtractor` for spotify_track resolution
    - _Requirements: 2.1, 2.2, 2.3, 3.1, 3.2, 3.3, 4.1, 4.2, 4.3, 4.4_

  - [x] 4.2 Implement youtube_direct and youtube_music resolution paths
    - youtube_direct: pass full URL to `YouTubeResolver.resolve(url)`
    - youtube_music: construct `https://youtube.com/watch?v={extracted_id}` and pass to YouTubeResolver
    - youtube_music with no video ID: raise MusicVideoResolverError with user message
    - On YouTubeResolver failure: raise MusicVideoResolverError with appropriate user message
    - _Requirements: 2.1, 2.2, 2.3, 3.1, 3.2, 3.3_

  - [x] 4.3 Implement tidal_video resolution path with fallback
    - Pass URL to `TidalResolver.resolve_url(url)`
    - On non-recoverable TidalResolverError: raise MusicVideoResolverError
    - On recoverable TidalResolverError: fall back to YouTube search using video title from error context
    - _Requirements: 4.1, 4.2, 4.3, 4.4_

  - [x] 4.4 Implement tidal_track resolution path
    - Fetch track metadata from Tidal API (GET /v1/tracks/{track_id}) using TidalResolver's token
    - Search Tidal videos for "{artist} - {title}" (GET /v1/search/videos?query=X&limit=1)
    - If video found: resolve via TidalResolver.resolve_url
    - If no video: YouTube search "{artist} - {title} official music video"
    - If Tidal API fails: fallback YouTube search with available metadata
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 9.1, 9.2_

  - [x] 4.5 Implement spotify_track resolution path
    - Call `SpotifyMetadataExtractor.extract(track_id)` to get artist + title
    - Search YouTube for "{artist} - {title} official music video" using YouTubeResolver
    - On Spotify API failure: raise MusicVideoResolverError with "Could not retrieve track info from Spotify."
    - On YouTube search no results: raise MusicVideoResolverError with "No music video found for that query."
    - _Requirements: 6.1, 6.2, 6.3, 6.4_

  - [x] 4.6 Implement text_search resolution path
    - Search YouTube for "{query} official music video" using YouTubeResolver
    - On no results: raise MusicVideoResolverError with "No music video found for that query."
    - _Requirements: 7.1, 7.2, 7.3_

  - [ ]* 4.7 Write property test for metadata fallback search query format
    - **Property 6: Metadata fallback search query format**
    - Generate (artist, title) pairs and verify the YouTube search query is exactly "{artist} - {title} official music video"
    - **Validates: Requirements 5.4, 6.2**

  - [ ]* 4.8 Write property test for text search query format
    - **Property 7: Text search query format**
    - Generate non-empty text strings (without `://`) and verify the YouTube search query is exactly "{query} official music video"
    - **Validates: Requirements 7.1**

- [x] 5. Checkpoint - Ensure resolver logic tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 6. Integrate with command handler
  - [x] 6.1 Add `/play music_video` command handler to the Music cog or Video cog
    - Implement `play_music_video(interaction, query: str)` slash command
    - Voice channel check: require user in a voice channel before any resolution (return error if not)
    - Defer the interaction response
    - Call `MusicVideoResolver.resolve(query)`
    - Route resolved `VideoSource` to ActivityStreamer (launch new session or enqueue)
    - On enqueue: respond with queue position and video title
    - Handle `MusicVideoResolverError`: send `exc.user_message` as ephemeral followup
    - Handle unexpected exceptions: log full traceback, send generic error message
    - Clean up VideoSource file on Activity launch failure if `cleanup_on_finish=True`
    - _Requirements: 8.1, 8.2, 8.3, 8.4, 8.5, 10.1, 10.2, 10.3, 10.4, 10.5_

  - [ ]* 6.2 Write unit tests for command handler integration in `tests/test_music_video_resolver.py`
    - Mock MusicVideoResolver and ActivityStreamer
    - Test: user not in voice channel returns error before resolution
    - Test: successful resolve with no active session launches Activity
    - Test: successful resolve with active session enqueues and reports position
    - Test: MusicVideoResolverError displays user_message
    - Test: Activity launch failure triggers file cleanup
    - _Requirements: 8.1, 8.2, 8.3, 8.4, 8.5, 10.5_

- [x] 7. Final checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- Checkpoints ensure incremental validation
- Property tests validate universal correctness properties from the design document
- Unit tests validate specific examples and error paths
- The classifier is pure (no I/O) making it ideal for property-based testing with Hypothesis
- The resolver orchestrator composes existing `YouTubeResolver` and `TidalResolver` — no changes to those classes needed
- All resolution paths produce standard `VideoSource` objects consumed by the existing ActivityStreamer pipeline

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1"] },
    { "id": 1, "tasks": ["1.2", "1.3", "1.4", "1.5", "1.6", "2.1"] },
    { "id": 2, "tasks": ["2.2", "4.1"] },
    { "id": 3, "tasks": ["4.2", "4.3", "4.4", "4.5", "4.6"] },
    { "id": 4, "tasks": ["4.7", "4.8"] },
    { "id": 5, "tasks": ["6.1"] },
    { "id": 6, "tasks": ["6.2"] }
  ]
}
```

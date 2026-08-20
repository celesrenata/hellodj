# Implementation Plan: Video Sources

## Overview

Extend the video Activity streaming subsystem with three new source types (Tidal music videos, general video URLs via improved routing, and direct Discord file uploads). Implement a unified source router, per-source resolvers, and source-type-aware display formatting. All new sources produce standard `VideoSource` objects consumed by the existing `ActivityStreamer → HLSTranscodePipeline → Activity iframe` pipeline.

## Tasks

- [x] 1. Extend data models and project structure
  - [x] 1.1 Extend VideoSource and SessionStatus data models
    - Add `"tidal"` to the `VideoSource.source_type` Literal union in `bot/video/__init__.py`
    - Add `uploader: str | None = None` field to the `SessionStatus` dataclass
    - Ensure existing code continues to work with the extended type
    - _Requirements: 1.3, 3.4, 5.7, 6.3, 6.4_

- [x] 2. Implement TidalResolver
  - [x] 2.1 Create `bot/video/tidal_resolver.py` with URL resolution
    - Implement `TidalResolverError` exception class with `recoverable` flag
    - Implement `TidalResolver` class with `resolve_url()` method
    - Implement `extract_video_id()` to parse Tidal video URLs (tidal.com/browse/video/N, tidal.com/video/N, listen.tidal.com/video/N)
    - Implement `_fetch_video_metadata()` — GET Tidal API for video info (title, duration, artist)
    - Implement `_fetch_stream_url()` — fetch highest-quality video stream URL (≥720p preferred)
    - Implement `_download_video()` — download video file with 10-minute timeout
    - Produce `VideoSource` with `source_type="tidal"`, `cleanup_on_finish=True`, metadata containing artist, track_title, video_id
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.7, 1.8, 3.4_

  - [x] 2.2 Implement Tidal OAuth token management
    - Implement `_ensure_token()` — check expiry with 5-minute buffer, refresh if needed
    - Implement `_refresh_token()` — POST to `https://auth.tidal.com/v1/oauth2/token` with stored refresh token
    - Use `tidal.issuing_client_id` from credential store, fall back to tidalapi internal client ID
    - Update credential store on successful refresh (access_token, expiry, and refresh_token if new one provided)
    - Handle auth errors: expired refresh token → user-facing error, log at WARNING
    - Read credentials from `credentials.py` creds singleton using `tidal.*` key namespace
    - _Requirements: 1.5, 1.6, 9.1, 9.2, 9.3, 9.4, 9.5_

  - [x] 2.3 Implement Tidal search functionality
    - Implement `search()` method — strip `tidal:` prefix, validate query (1-200 chars, non-whitespace-only)
    - Search Tidal API for music videos, select first result
    - Resolve selected result through the same download flow as URL resolution
    - Handle error cases: no results, no credentials, empty query, unavailable video
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7_

  - [ ]* 2.4 Write property test for Tidal URL video ID extraction
    - **Property 1: Tidal URL Video ID Extraction**
    - Generate random URL strings with Tidal domains and various path patterns, verify correct ID extraction or None
    - **Validates: Requirements 1.1**

  - [ ]* 2.5 Write property test for Tidal video quality selection
    - **Property 2: Tidal Video Quality Selection**
    - Generate random non-empty lists of resolution heights, verify highest ≥720 is selected (or highest overall if none ≥720)
    - **Validates: Requirements 1.2**

  - [ ]* 2.6 Write property test for Tidal search query validation
    - **Property 4: Tidal Search Query Validation**
    - Generate whitespace-only strings (reject) and valid 1-200 char non-whitespace strings (accept)
    - **Validates: Requirements 2.1, 2.5**

  - [ ]* 2.7 Write property test for token refresh decision
    - **Property 10: Token Refresh Decision**
    - Generate random expiry timestamps and current times, verify refresh triggered iff T ≥ (E − 300)
    - **Validates: Requirements 9.2**

  - [ ]* 2.8 Write property test for client ID fallback selection
    - **Property 11: Client ID Fallback Selection**
    - Generate credential store states with/without issuing_client_id, verify correct selection
    - **Validates: Requirements 9.5**

- [x] 3. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 4. Implement UploadHandler
  - [x] 4.1 Create `bot/video/upload_handler.py` with upload processing
    - Implement `UploadHandlerError` exception class
    - Implement `UploadHandler` class with `process()` method
    - Implement `validate_type()` — check content_type (video/*) and extension fallback (mp4, mkv, webm, avi, mov, m4v)
    - Implement `validate_size()` — check attachment.size ≤ 500 MB, reject if size is None
    - Download attachment to temp directory
    - Implement `ffprobe_validate()` — run ffprobe with 10s timeout, confirm video stream exists, extract duration
    - Produce `VideoSource` with `source_type="upload"`, `cleanup_on_finish=True`, metadata containing uploader display name and original filename
    - Handle errors: unsupported format, too large, download failure, ffprobe timeout/rejection
    - Delete downloaded file on any validation failure after download
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 5.7, 5.8, 7.1, 7.2, 7.3, 7.4, 10.1, 10.2, 10.5_

  - [ ]* 4.2 Write property test for upload type validation
    - **Property 6: Upload Type Validation**
    - Generate filenames with supported/unsupported extensions and various MIME types, verify accept/reject
    - **Validates: Requirements 5.2, 7.1, 10.5**

  - [ ]* 4.3 Write property test for resolver cleanup invariant
    - **Property 9: Resolver Cleanup Invariant**
    - Generate VideoSource objects from TidalResolver and UploadHandler, verify `cleanup_on_finish=True`
    - **Validates: Requirements 3.4, 5.8**

- [x] 5. Implement Source Router
  - [x] 5.1 Create `bot/video/source_router.py` with classification logic
    - Implement `classify_input()` function with priority-ordered classification
    - Implement `is_url()` helper — detect URLs by scheme and netloc presence
    - Implement `extract_tidal_video_id()` — regex extraction of numeric ID from Tidal video URLs
    - Define `SourceType` literal: "youtube_url", "youtube_search", "tidal_url", "tidal_search", "general_url", "upload"
    - Define domain constants: `_YOUTUBE_DOMAINS`, `_TIDAL_DOMAINS`, `_TIDAL_VIDEO_PATH_RE`
    - Priority: attachment → YouTube URL → Tidal URL (with /video/ path) → tidal: prefix → URL with video ext → URL without video ext → YouTube search
    - _Requirements: 8.1, 8.2, 8.3, 8.4, 8.5, 8.6_

  - [ ]* 5.2 Write property test for source input classification
    - **Property 3: Source Input Classification**
    - Generate random inputs (URLs with various domains, prefixed strings, plain text) + attachment flags
    - Verify deterministic classification and correct priority order
    - **Validates: Requirements 8.1, 8.2, 8.3, 8.4, 8.5, 10.1, 10.4**

  - [ ]* 5.3 Write property test for URL title extraction
    - **Property 5: URL Title Extraction**
    - Generate random URLs with/without filename path segments, verify hostname fallback
    - **Validates: Requirements 4.3**

- [x] 6. Refactor VideoCog to use source router
  - [x] 6.1 Update `/video play` command in `bot/cogs/video.py`
    - Add `attachment` parameter to `/video play` slash command (optional `discord.Attachment`)
    - Import and use `classify_input()` from source router
    - Import `TidalResolver`, `UploadHandler` alongside existing `YouTubeResolver`, `URLDownloader`
    - Implement match/case dispatch: upload → UploadHandler, youtube_url/youtube_search → YouTubeResolver, tidal_url → TidalResolver.resolve_url, tidal_search → TidalResolver.search, general_url → URLDownloader with 10s timeout + YouTube fallback
    - Handle attachment-takes-priority rule (if both query and attachment provided, use attachment)
    - Catch all resolver errors and respond with ephemeral error messages including source type
    - _Requirements: 8.1, 8.2, 8.3, 8.4, 8.5, 8.6, 8.7, 10.1, 10.2, 10.3, 10.4, 10.5_

- [x] 7. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 8. Update Now Playing embed and session status
  - [x] 8.1 Update Now Playing embed with source-type-aware formatting
    - Tidal videos: display title as "Artist — Title" using metadata artist field
    - Upload videos: display "Uploaded by {display_name}" in embed footer
    - URL videos: display filename extracted from URL path (existing behavior)
    - Update queue listing to include "(uploaded by display_name)" for upload sources
    - _Requirements: 3.2, 4.3, 6.1, 6.2_

  - [x] 8.2 Update SessionStatus in activity backend
    - Populate `uploader` field from `source.metadata.get("uploader")` when `source_type == "upload"`
    - Set `uploader` to `None` for all other source types
    - Update `handle_status()` in `activity_backend.py` to include the uploader field in response
    - _Requirements: 6.3, 6.4_

  - [x] 8.3 Update Activity frontend to display uploader attribution
    - When status API returns non-null `uploader` field, display uploader attribution in player UI alongside video title
    - _Requirements: 6.5_

  - [ ]* 8.4 Write property test for upload attribution
    - **Property 7: Upload Attribution**
    - Generate random display names and video titles, verify metadata dict, embed text, and queue format
    - **Validates: Requirements 5.7, 6.1, 6.2, 10.3**

  - [ ]* 8.5 Write property test for status API uploader field
    - **Property 8: Status API Uploader Field**
    - Generate VideoSources of each type, verify uploader is non-null only for uploads
    - **Validates: Requirements 6.3, 6.4**

  - [ ]* 8.6 Write property test for Tidal Now Playing formatting
    - **Property 12: Tidal Now Playing Formatting**
    - Generate random artist/title strings, verify "Artist — Title" format or title-only when artist is empty
    - **Validates: Requirements 3.2**

- [x] 9. Final checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- Checkpoints ensure incremental validation
- Property tests validate universal correctness properties from the design document using Hypothesis (`@settings(max_examples=100)`)
- Unit tests validate specific examples and edge cases
- Existing components (YouTubeResolver, URLDownloader, ActivityStreamer, HLSTranscodePipeline, ActivityBackend, WebSocketHub) are already complete and do NOT need reimplementation
- The TidalResolver reads OAuth credentials from the existing `credentials.py` creds singleton using the `tidal.*` key namespace shared with the tidal-stream sidecar
- All property tests use the `hypothesis` library with `@settings(max_examples=100)`

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1"] },
    { "id": 1, "tasks": ["2.1", "4.1", "5.1"] },
    { "id": 2, "tasks": ["2.2", "2.4", "4.2", "5.2", "5.3"] },
    { "id": 3, "tasks": ["2.3", "2.5", "2.7", "2.8", "4.3"] },
    { "id": 4, "tasks": ["2.6", "6.1"] },
    { "id": 5, "tasks": ["8.1", "8.2"] },
    { "id": 6, "tasks": ["8.3", "8.4", "8.5", "8.6"] }
  ]
}
```

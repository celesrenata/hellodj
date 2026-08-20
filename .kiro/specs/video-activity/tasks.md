# Implementation Plan: Video Activity

## Overview

Replace the Go Live/screenshare video streaming with a Discord Activity (embedded iframe). The bot transcodes video to HLS via the existing Intel QSV pipeline, serves segments through an aiohttp HTTP server on port 8090, and launches a Discord Activity that plays the stream using hls.js. Audio is delivered through HLS (AAC) rather than the bot's voice connection.

This plan reuses existing components (`video/__init__.py`, `video/sources.py`, `video/gpu_probe.py`, `video/transcode.py`) and replaces the Go Live stack (`rtp_sender.py`, `go_live.py`, `streamer.py`) with Activity-based delivery.

## Prerequisites

- Discord Developer Portal: Register an Activity URL (`https://hellodj.celestium.life/activity/`) in the application settings. This is a manual step.
- `aiohttp` is already a bot dependency.
- `hypothesis` available for property-based tests.

## Tasks

- [x] 1. Add SessionStatus dataclass and update data models
  - [x] 1.1 Add `SessionStatus` dataclass to `bot/video/__init__.py`
    - Add the `SessionStatus` dataclass with fields: `state`, `video_title`, `video_duration`, `elapsed_seconds`, `playlist_url`, `queue_length`, `session_id`
    - Keep all existing models unchanged
    - _Requirements: 5.1_

- [x] 2. Implement HLS transcode pipeline
  - [x] 2.1 Create `bot/video/hls_transcode.py` with `HLSTranscodePipeline`
    - Based on existing `TranscodePipeline` but outputs HLS segments instead of piping H.264 to stdout
    - Use `-f hls -hls_time 4 -hls_list_size 0 -hls_segment_filename` for VOD-style complete playlist
    - Include `-c:a aac -b:a 128k` for audio encoding
    - Cap output resolution at 720p maximum
    - Retain QSV hardware decode for supported codecs, software decode fallback with `hwupload` + QSV encode
    - Output to `/tmp/hellodj_hls/{guild_id}/{session_id}/playlist.m3u8`
    - Include `asyncio.Event` (`ready`) that is set when the first segment file appears on disk
    - Include `wait_ready(timeout=30.0)` and `wait_complete()` methods
    - Include watchdog timeout (60s no new segments → kill)
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.7, 3.8_

  - [ ]* 2.2 Write property test: HLS pipeline command correctness (Property 4)
    - **Property 4: HLS pipeline command correctness**
    - Generate random codecs (from QSV-decodable set and outside) + resolutions → verify generated ffmpeg args include `-f hls`, `-hls_time 4`, `-c:v h264_qsv`, `-c:a aac -b:a 128k`, correct output path, and appropriate decode mode
    - **Validates: Requirements 3.1, 3.5, 3.7**

  - [ ]* 2.3 Write property test: Resolution capping at 720p (Property 3)
    - **Property 3: Resolution capping at 720p**
    - Generate random resolutions → verify output height never exceeds 720 pixels
    - Generate random format lists → verify selected quality ≤ 720p
    - **Validates: Requirements 2.4, 3.2**

  - [ ]* 2.4 Write unit tests for HLS pipeline
    - Test ffmpeg argument generation for various codecs (h264, hevc, vp9, av1)
    - Test QSV decode path vs software decode fallback path
    - Test bitrate calculation at different resolutions
    - Test output directory path construction
    - _Requirements: 3.1, 3.5_

- [x] 3. Implement session registry
  - [x] 3.1 Create `bot/video/session_registry.py` with `SessionRegistry`
    - Implement `register()`, `unregister()`, `get()`, `active_sessions()` methods
    - Implement grace period logic: `start_grace_period(guild_id, timeout=30.0)` starts a background task that unregisters the session after timeout
    - Implement `cancel_grace_period(guild_id)` for when viewers rejoin
    - Thread-safe (asyncio-safe) via standard dict + task management
    - _Requirements: 6.3_

- [x] 4. Implement Activity streamer
  - [x] 4.1 Create `bot/video/activity_streamer.py` with `ActivityStreamer`
    - Per-guild session manager replacing `VideoStreamer`
    - State machine: IDLE → RESOLVING → BUFFERING → STREAMING → STOPPING
    - Fields: `guild_id`, `channel_id`, `session_id` (UUID), `source`, `pipeline`, `queue`, `start_time`
    - Implement `play(source)`: if session active → enqueue; if idle → start new session
    - Implement `stop()`: kill pipeline, clean up HLS files, transition to IDLE
    - Implement `skip()`: if queue non-empty → play next; if empty → stop
    - Implement `enqueue(source)` with max capacity 50
    - Implement `get_elapsed_seconds()`: clamp `(now - start_time)` to `[0, duration]`
    - Implement `cleanup()`: delete session HLS directory
    - Auto-advance to next queue item when current video finishes (listen for pipeline completion)
    - Enforce 8-hour max session duration via background timer
    - _Requirements: 1.1, 1.2, 7.1, 7.2, 7.4, 8.1, 8.2, 8.3, 8.4, 8.5, 9.1, 9.3_

  - [ ]* 4.2 Write property test: Enqueue when session is active (Property 1)
    - **Property 1: Enqueue when session is active**
    - Generate random session states (not IDLE/ERROR) + valid VideoSource → verify `play()` appends to queue, queue length increases by one, no new Activity launch
    - **Validates: Requirements 1.2, 7.4**

  - [ ]* 4.3 Write property test: Skip advances or stops (Property 8)
    - **Property 8: Skip advances or stops**
    - Generate random queue states → verify skip dequeues next item or stops when empty
    - **Validates: Requirements 7.2**

  - [ ]* 4.4 Write property test: Queue ordering and capacity (Property 9)
    - **Property 9: Queue ordering and capacity**
    - Generate random enqueue sequences (up to 60 items) → verify FIFO order, cap at 50, rejection when full
    - **Validates: Requirements 8.1, 8.4, 8.5**

  - [ ]* 4.5 Write property test: Elapsed time tracking (Property 7)
    - **Property 7: Elapsed time tracking**
    - Generate random `start_time` + `query_time` pairs → verify elapsed = clamped `(query_time - start_time)`
    - **Validates: Requirements 6.1, 6.2**

- [x] 5. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 6. Implement Activity backend HTTP server
  - [x] 6.1 Create `bot/video/activity_backend.py` with `ActivityBackend`
    - aiohttp web application on port 8090
    - Route: `GET /activity/` → serve `index.html` from `activity_frontend/`
    - Route: `GET /activity/static/{filename}` → serve `app.js`, `style.css`
    - Route: `GET /activity/status/{guild_id}` → JSON response with `SessionStatus` fields
    - Route: `GET /activity/stream/{guild_id}/playlist.m3u8` → serve HLS playlist file
    - Route: `GET /activity/stream/{guild_id}/{segment}.ts` → serve HLS segment files
    - Authentication middleware: validate Activity session token on `/activity/stream/` and `/activity/status/` routes
    - Return HTTP 401 for missing/invalid tokens, HTTP 403 for cross-guild access, HTTP 404 for no active session
    - `start(port=8090)` and `stop()` lifecycle methods
    - Reference the `SessionRegistry` for session lookup
    - _Requirements: 4.1, 5.1, 5.2, 5.3, 5.4, 5.5, 10.1, 10.2, 10.3, 10.4_

  - [ ]* 6.2 Write property test: Authentication enforcement (Property 5)
    - **Property 5: Authentication enforcement**
    - Generate random auth scenarios (no token, invalid token, valid token for wrong guild, valid token for correct guild) → verify correct HTTP status codes (401, 403, 200)
    - **Validates: Requirements 5.4, 10.3, 10.4**

  - [ ]* 6.3 Write property test: Status API response completeness (Property 6)
    - **Property 6: Status API response completeness**
    - Generate random `SessionStatus` data with active sessions → verify response contains all required fields with correct types and constraints
    - **Validates: Requirements 5.1**

- [x] 7. Implement Activity launcher
  - [x] 7.1 Create `bot/video/activity_launcher.py` with `ActivityLauncher`
    - `launch(channel_id, application_id)` → POST to Discord API to start Activity in voice channel
    - `close(channel_id)` → Close the Activity session via Discord API
    - Handle rate limiting (respect `Retry-After`, retry once)
    - Handle API errors (4xx/5xx) and return descriptive error messages
    - Use the bot's HTTP session (aiohttp) for requests
    - _Requirements: 1.1, 1.4, 1.5_

- [x] 8. Implement Activity frontend
  - [x] 8.1 Create `bot/video/activity_frontend/index.html`
    - HTML page with full-viewport video element
    - Include hls.js from CDN
    - Include Discord Embedded App SDK from CDN
    - Link to `app.js` and `style.css`
    - Meta viewport for iframe dimensions
    - _Requirements: 4.1, 4.5_

  - [x] 8.2 Create `bot/video/activity_frontend/app.js`
    - Initialize Discord Embedded App SDK → obtain `guild_id`, `channel_id`, `instance_id`
    - Authenticate with backend using `instance_id` as session token
    - Fetch `/activity/status/{guild_id}` → get `playlist_url` + `elapsed_seconds`
    - Initialize hls.js with the playlist URL
    - Seek to `elapsed_seconds` on load for late-joiner sync
    - Display video title and duration overlay
    - Handle hls.js fatal errors → show error overlay
    - _Requirements: 4.2, 4.3, 4.4, 4.6, 6.2, 6.4, 10.1_

  - [x] 8.3 Create `bot/video/activity_frontend/style.css`
    - Full-viewport video styling (no scrollbars)
    - Video title overlay positioning
    - Error overlay styling
    - Dark theme appropriate for Discord iframe
    - _Requirements: 4.5_

- [x] 9. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 10. Update VideoCog to use ActivityStreamer
  - [x] 10.1 Rewrite `bot/cogs/video.py` to use Activity-based streaming
    - Replace `VideoStreamer` imports with `ActivityStreamer`, `SessionRegistry`, `ActivityLauncher`, `ActivityBackend`
    - `/video play`: resolve source → if no active session, launch Activity via `ActivityLauncher` then start `ActivityStreamer.play()`; if active session, enqueue
    - `/video stop`: call `ActivityStreamer.stop()` then `ActivityLauncher.close()`
    - `/video skip`: call `ActivityStreamer.skip()`
    - `/video queue`: display embed from streamer's queue
    - Remove `/video resolution` and `/video formats` commands (no longer applicable — resolution is fixed at 720p)
    - Send "Now Playing" embed when new video begins
    - Start `ActivityBackend` on cog load, stop on unload
    - Register/unregister streamers with `SessionRegistry`
    - Handle grace period: when all viewers leave voice channel, start grace period; if someone rejoins within 30s, cancel it
    - _Requirements: 1.1, 1.2, 1.3, 1.5, 7.1, 7.2, 7.3, 7.4, 7.5, 8.2, 8.3_

  - [ ]* 10.2 Write property test: URL source classification (Property 2)
    - **Property 2: URL source classification**
    - Generate random URL strings → verify YouTube domains classified as YouTube source regardless of extension; non-YouTube URLs with video extensions classified as direct download
    - **Validates: Requirements 2.3**

  - [ ]* 10.3 Write property test: Embed rendering completeness (Property 10)
    - **Property 10: Embed rendering completeness**
    - Generate random VideoSource and queue states → verify Now Playing embed contains title; queue embed lists all titles in order
    - **Validates: Requirements 7.3, 7.5**

- [x] 11. Implement session cleanup
  - [x] 11.1 Add startup cleanup and session cleanup logic
    - On bot startup: scan `/tmp/hellodj_hls/` for orphaned directories, remove any that don't correspond to active sessions
    - On session end: delete all files in `/tmp/hellodj_hls/{guild_id}/{session_id}/` and remove the directory
    - On transcode crash: clean up partial HLS output
    - Wire into `ActivityStreamer.cleanup()` and cog startup
    - _Requirements: 9.1, 9.2, 9.4_

  - [ ]* 11.2 Write property test: HLS file cleanup (Property 11)
    - **Property 11: HLS file cleanup**
    - Generate random directory structures under `/tmp/hellodj_hls/` → verify cleanup removes all files and directories for ended sessions; verify orphan cleanup on startup
    - **Validates: Requirements 9.1, 9.2**

- [x] 12. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 13. Kubernetes deployment changes
  - [x] 13.1 Update Kubernetes manifests for Activity backend
    - Add port 8090 to the bot container in `kube/deployment.yaml`
    - Add (or update) a Service exposing port 8090 for the bot
    - Add ingress rule: path `/activity/` → bot:8090 on the existing `hellodj.celestium.life` ingress
    - Keep `privileged: true` (still needed for QSV `/dev/dri` access)
    - _Requirements: 11.1, 11.2, 11.3, 11.4_

- [x] 14. Clean up deprecated Go Live files
  - [x] 14.1 Delete old Go Live streaming files
    - Delete `bot/video/rtp_sender.py` (RTP sending for Go Live — no longer needed)
    - Delete `bot/video/go_live.py` (Go Live protocol — no longer needed)
    - Delete `bot/video/streamer.py` (replaced by `activity_streamer.py`)
    - Remove any imports of these modules from other files
    - Verify no remaining references to deleted modules
    - _Requirements: N/A (cleanup)_

- [x] 15. Final checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- Checkpoints ensure incremental validation
- Property tests validate universal correctness properties from the design document
- Unit tests validate specific examples and edge cases
- The Activity backend runs in-process with the bot (no separate container)
- Discord Developer Portal Activity URL registration is a manual prerequisite
- The existing `video/transcode.py` is kept as reference but not used directly — `hls_transcode.py` is a new implementation based on it
- `video/sources.py` and `video/gpu_probe.py` are reused as-is

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1"] },
    { "id": 1, "tasks": ["2.1", "3.1"] },
    { "id": 2, "tasks": ["2.2", "2.3", "2.4", "4.1"] },
    { "id": 3, "tasks": ["4.2", "4.3", "4.4", "4.5", "7.1"] },
    { "id": 4, "tasks": ["6.1", "8.1", "8.3"] },
    { "id": 5, "tasks": ["6.2", "6.3", "8.2"] },
    { "id": 6, "tasks": ["10.1"] },
    { "id": 7, "tasks": ["10.2", "10.3", "11.1"] },
    { "id": 8, "tasks": ["11.2", "13.1"] },
    { "id": 9, "tasks": ["14.1"] }
  ]
}
```

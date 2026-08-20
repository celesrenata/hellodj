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
    - Fields: `guild_id`, `channel_id`, `session_id` (UUID), `source`, `pipeline`, `queue`, `history`, `start_time`
    - Implement `play(source)`: if session active → enqueue; if idle → start new session
    - Implement `stop()`: kill pipeline, clean up HLS files, transition to IDLE
    - Implement `skip()`: push current to history, if queue non-empty → play next; if empty → stop
    - Implement `previous()`: if history non-empty → push current to front of queue, pop history, play it; if empty → return False
    - Implement `enqueue(source)` with max capacity 50
    - Implement `get_elapsed_seconds()`: clamp `(now - start_time)` to `[0, duration]`
    - Implement `cleanup()`: delete session HLS directory
    - Maintain `history: list[VideoSource]` (LIFO, max 20 entries)
    - Auto-advance to next queue item when current video finishes (listen for pipeline completion)
    - Enforce 8-hour max session duration via background timer
    - _Requirements: 1.1, 1.2, 7.1, 7.2, 7.4, 7.6, 8.1, 8.2, 8.3, 8.4, 8.5, 8.6, 8.7, 8.8, 9.1, 9.3_

  - [x] 4.1a Harden ActivityStreamer for track-change race conditions
    - **Problem**: `skip()`, `previous()`, and `_auto_advance()` all transition the session to a new source. If they race (e.g., user skips while auto-advance fires), the session state becomes corrupt (double-play, orphaned pipelines, denied transitions).
    - Add an `asyncio.Lock` (`_transition_lock`) guarding all source-change operations (`skip`, `previous`, `_auto_advance`, `stop`)
    - In `skip()` and `previous()`: check state is STREAMING or BUFFERING before proceeding; if state is RESOLVING/STOPPING/IDLE/ERROR, return early with an appropriate error or False
    - In `_auto_advance()`: acquire lock, re-check that `self.state == StreamState.STREAMING` and `self.pipeline` matches the completed one (guard against stale task firing after a skip already replaced it)
    - On `_play_source()` failure (state → ERROR): if invoked from skip/previous, propagate the error rather than silently setting ERROR — allow the caller to try the next item or report to user
    - Handle `cleanup_on_finish=True` sources in history: mark history entries that can't be replayed (source file deleted), and in `previous()` skip over them or inform the user "Previous video unavailable (temp file cleaned up)"
    - Add state assertion at the top of `_play_source()`: if state is not IDLE/ERROR/BUFFERING (i.e., already streaming something), refuse to start — this catches the race where two play attempts overlap
    - Ensure `_cancel_background_tasks()` awaits task cancellation (current impl just calls `.cancel()` without awaiting — the task may still be running when the new source starts)
    - _Requirements: 7.2, 7.6, 8.6, 8.7_

  - [ ]* 4.2 Write property test: Enqueue when session is active (Property 1)
    - **Property 1: Enqueue when session is active**
    - Generate random session states (not IDLE/ERROR) + valid VideoSource → verify `play()` appends to queue, queue length increases by one, no new Activity launch
    - **Validates: Requirements 1.2, 7.4**

  - [ ]* 4.3 Write property test: Skip advances or stops (Property 8)
    - **Property 8: Skip advances or stops**
    - Generate random queue states → verify skip pushes current to history, dequeues next item or stops when empty
    - **Validates: Requirements 7.2, 8.6**

  - [ ]* 4.4 Write property test: Queue ordering and capacity (Property 9)
    - **Property 9: Queue ordering and capacity**
    - Generate random enqueue sequences (up to 60 items) → verify FIFO order, cap at 50, rejection when full
    - **Validates: Requirements 8.1, 8.4, 8.5**

  - [ ]* 4.5 Write property test: Elapsed time tracking (Property 7)
    - **Property 7: Elapsed time tracking**
    - Generate random `start_time` + `query_time` pairs → verify elapsed = clamped `(query_time - start_time)`
    - **Validates: Requirements 6.1, 6.2**

  - [ ]* 4.6 Write property test: Previous restores from history (Property 17)
    - **Property 17: Previous restores from history**
    - Generate random history stacks (0-20 items) + current source + queue → verify: non-empty history → current pushed to queue front, history popped, playback starts; empty history → returns False, session unchanged
    - **Validates: Requirements 7.6, 8.7, 8.8**

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
    - _Requirements: 4.1, 5.1, 5.2, 5.3, 5.4, 5.5, 15.1, 15.2, 15.3, 15.4_

  - [ ]* 6.2 Write property test: Authentication enforcement (Property 5)
    - **Property 5: Authentication enforcement**
    - Generate random auth scenarios (no token, invalid token, valid token for wrong guild, valid token for correct guild) → verify correct HTTP status codes (401, 403, 200)
    - **Validates: Requirements 5.4, 15.3, 15.4**

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
    - _Requirements: 4.2, 4.3, 4.4, 4.6, 6.2, 6.4, 15.1_

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
    - `/video skip` (alias: `/video next`): call `ActivityStreamer.skip()`
    - `/video previous` (alias: `/video last`): call `ActivityStreamer.previous()`, handle empty history error
    - `/video queue`: display embed from streamer's queue and history
    - Remove `/video resolution` and `/video formats` commands (no longer applicable — resolution is fixed at 720p)
    - Send "Now Playing" embed when new video begins
    - Start `ActivityBackend` on cog load, stop on unload
    - Register/unregister streamers with `SessionRegistry`
    - Handle grace period: when all viewers leave voice channel, start grace period; if someone rejoins within 30s, cancel it
    - _Requirements: 1.1, 1.2, 1.3, 1.5, 7.1, 7.2, 7.3, 7.4, 7.5, 7.6, 8.2, 8.3, 8.6, 8.7, 8.8_

  - [x] 10.1a Harden VideoCog error handling for skip/previous transitions
    - In `/video skip` and `/video previous` handlers: wrap `streamer.skip()` / `streamer.previous()` in try/except for `TransitionDeniedError` (new exception raised when state doesn't allow transition)
    - On `TransitionDeniedError`: respond with ephemeral message "Can't do that right now — video is loading" or similar
    - On `previous()` returning False: respond "No previous video available"
    - On `previous()` returning a source with `cleanup_on_finish=True` that was already cleaned: respond "Previous video no longer available (file was temporary)"
    - On `_play_source` ERROR after skip/previous: attempt to recover by trying the NEXT item in queue; if queue also empty, stop session and report "Playback failed"
    - Ensure the ⏮ button in `VideoControlView` handles all the same edge cases with appropriate ephemeral messages
    - _Requirements: 7.2, 7.6, 8.7, 8.8_

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
    - _Requirements: 16.1, 16.2, 16.3, 16.4_

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

- [x] 16. Implement WebSocket synchronization hub
  - [x] 16.1 Create `bot/video/ws_hub.py` with `WebSocketHub` and `PlaybackState`
    - Define `PlaybackState` dataclass: `playing` (bool), `position` (float), `last_update` (float monotonic), `subtitle_lang` (str|None), `audio_lang` (str|None)
    - Implement `handle_ws(request)`: upgrade HTTP → WebSocket, authenticate via token query param, register connection, send current `state` message on connect, listen for incoming messages
    - Implement `broadcast(guild_id, message, exclude=None)`: send JSON to all connected clients except the sender
    - Implement `broadcast_from_bot(guild_id, message)`: send JSON to ALL connected clients (used by bot-side controls like Now Playing embed buttons)
    - Implement `get_state(guild_id)` / `set_state(guild_id, state)`: server-authoritative state tracking
    - Implement `disconnect_all(guild_id)`: close all WebSocket connections for a guild on session end
    - Handle incoming messages: `play` → update state + broadcast, `pause` → update state + broadcast, `seek` → update state + broadcast, `subtitle_change` (if for_everyone) → update state + broadcast, `audio_change` (if for_everyone) → update state + broadcast
    - Include heartbeat/ping every 30s to detect stale connections
    - _Requirements: 10.1, 10.2, 10.3, 10.4, 10.5, 10.6, 10.7_

  - [x] 16.2 Register WebSocket route in `ActivityBackend`
    - Add route `GET /activity/ws/{guild_id}` → `ws_hub.handle_ws`
    - Pass the `SessionRegistry` to `WebSocketHub` for session validation
    - Authenticate WebSocket connections using the same token scheme as HTTP routes (token query param)
    - _Requirements: 10.1, 15.5_

  - [ ]* 16.3 Write property test: WebSocket broadcast exclusion (Property 12)
    - **Property 12: WebSocket sync broadcast**
    - Generate N mock clients (2-10), send a play/pause/seek from one → verify N-1 others receive it, sender does not
    - **Validates: Requirements 10.3, 10.4, 10.5**

  - [ ]* 16.4 Write property test: Late-joiner state (Property 13)
    - **Property 13: WebSocket late-joiner state**
    - Generate random PlaybackState → connect new client → verify it receives a `state` message with correct fields
    - **Validates: Requirements 10.6**

- [x] 17. Implement subtitle extraction in transcode pipeline
  - [x] 17.1 Add subtitle probe and extraction to `HLSTranscodePipeline`
    - Add `probe_subtitles(input_path)` method: run `ffprobe -show_streams -select_streams s` to detect subtitle tracks, return list of `{"lang": "en", "label": "English", "stream_index": 2}`
    - Add `extract_subtitles(input_path, subtitle_tracks)` method: for each subtitle track, run `ffmpeg -i input -map 0:s:{idx} -f webvtt output_dir/subtitles/{lang}.vtt`
    - Create `output_dir/subtitles/` directory alongside HLS segments
    - Call `extract_subtitles()` during `start()` after directory creation
    - Store discovered subtitle tracks as `self.subtitle_tracks: list[dict]`
    - Handle sources with no subtitles gracefully (empty list, no error)
    - _Requirements: 11.1, 11.7_

  - [x] 17.2 Add subtitle serving route to `ActivityBackend`
    - Add route `GET /activity/stream/{guild_id}/subtitles/{lang}.vtt` → serve WebVTT file
    - Validate `lang` param against known subtitle tracks for the session
    - Return 404 if subtitle language not available
    - _Requirements: 11.2_

  - [x] 17.3 Add `subtitles` field to status API response
    - Include `subtitles: list[dict]` in `SessionStatus` (e.g., `[{"lang": "en", "label": "English"}]`)
    - Populate from `pipeline.subtitle_tracks` when session is active
    - _Requirements: 11.3_

- [x] 18. Implement multi-audio track support in transcode pipeline
  - [x] 18.1 Add audio track probe to `HLSTranscodePipeline`
    - Add `probe_audio_tracks(input_path)` method: run `ffprobe -show_streams -select_streams a` to detect audio tracks, return list of `{"lang": "ja", "label": "Japanese", "stream_index": 1}`
    - Store discovered audio tracks as `self.audio_tracks: list[dict]`
    - _Requirements: 12.1_

  - [x] 18.2 Update `build_ffmpeg_args()` for multi-audio HLS output
    - When multiple audio tracks detected: produce a master playlist with `#EXT-X-MEDIA` audio group entries per language
    - Each audio track gets its own segments playlist: `audio_{lang}.m3u8`
    - Video becomes its own variant: `video.m3u8`
    - When only one audio track: keep current behavior (muxed A/V in single .ts segments)
    - _Requirements: 12.1_

  - [x] 18.3 Add `audio_tracks` field to status API response
    - Include `audio_tracks: list[dict]` in `SessionStatus` (e.g., `[{"lang": "ja", "label": "Japanese"}]`)
    - Populate from `pipeline.audio_tracks` when session is active
    - _Requirements: 12.2_

  - [x] 18.4 Update `handle_playlist` to serve master playlist with audio variants
    - When multi-audio: serve the master playlist that references per-language audio playlists
    - Add route for per-language audio playlists: `GET /activity/stream/{guild_id}/audio_{lang}.m3u8`
    - _Requirements: 12.1_

- [x] 19. Checkpoint - Ensure transcode + WebSocket tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 20. Update Activity frontend for WebSocket sync, subtitles, audio, and volume
  - [x] 20.1 Add WebSocket connection and sync logic to `app.js`
    - On HLS init: connect WebSocket to `/activity/ws/{guild_id}?token={instanceId}`
    - On receiving `play` message: call `videoEl.play()` and seek to `message.position`
    - On receiving `pause` message: call `videoEl.pause()`
    - On receiving `seek` message: set `videoEl.currentTime = message.position`
    - On receiving `state` message (late joiner): set playing/paused + seek to position
    - On receiving `subtitle_change` message: activate specified subtitle track
    - On receiving `audio_change` message: switch hls.js audio track
    - On user play/pause action: send WebSocket message instead of only local action
    - On user seek action (scrubber drag-end): send WebSocket `seek` message
    - Debounce seek messages during drag (send only on release)
    - _Requirements: 10.2, 10.3, 10.4, 10.5, 10.6_

  - [x] 20.2 Add volume controls to frontend
    - Add volume slider (range input, 0-100) to controls row
    - Add mute toggle button (🔊/🔇)
    - On volume change: set `videoEl.volume = value / 100` (local only, NO WebSocket message)
    - Persist volume in `localStorage` under key `hellodj_volume`
    - Load saved volume on init (default 80%)
    - _Requirements: 13.1, 13.2, 13.3, 13.4, 13.5_

  - [x] 20.3 Add subtitle selector to frontend
    - Add subtitle dropdown/button to controls row (hidden if no subtitles available)
    - Fetch available subtitles from status API `subtitles` field
    - On selection: add `<track>` element pointing to `/activity/stream/{guild_id}/subtitles/{lang}.vtt`
    - Add "for everyone" checkbox next to selector
    - If "for everyone" checked: send `subtitle_change` WebSocket message on selection
    - If not checked: apply locally only (no WebSocket message)
    - _Requirements: 11.4, 11.5, 11.6, 11.7_

  - [x] 20.4 Add audio language selector to frontend
    - Add audio track dropdown/button to controls row (hidden if single audio track)
    - Fetch available audio tracks from status API `audio_tracks` field
    - On selection: use hls.js `audioTrack` API to switch audio rendition
    - Add "for everyone" checkbox next to selector
    - If "for everyone" checked: send `audio_change` WebSocket message on selection
    - If not checked: apply locally only (no WebSocket message)
    - _Requirements: 12.3, 12.4, 12.5, 12.6_

  - [x] 20.5 Update `index.html` with new control elements
    - Add volume slider and mute button to controls row
    - Add subtitle selector with "for everyone" checkbox
    - Add audio language selector with "for everyone" checkbox
    - Keep existing layout; new controls go to the right of the time display
    - _Requirements: 11.4, 12.3, 13.1_

  - [x] 20.6 Update `style.css` for new controls
    - Style volume slider (thin, accent-colored fill)
    - Style dropdown menus for subtitle/audio selection (dark theme, Discord-appropriate)
    - Style "for everyone" checkboxes (small, inline)
    - Ensure controls don't overflow on small Activity iframe sizes
    - _Requirements: 4.5_

- [x] 21. Update Now Playing embed with seek bar and enhanced controls
  - [x] 21.1 Rewrite `VideoControlView` with seek bar and sync-aware buttons
    - Replace current button row with: ⏮ (previous), ⏪ (seek -10s), ⏯ (play/pause), ⏩ (seek +10s), ⏭ (next/skip), 🚫 (stop)
    - On ⏮ click: call `streamer.previous()`, if False → respond with "No previous video" ephemeral, else send Now Playing embed for new track
    - On ⏯ click: determine current state from WebSocketHub, broadcast `play` or `pause` to all Activity clients
    - On ⏪/⏩ click: compute new position from WebSocketHub state, broadcast `seek` to all Activity clients
    - On ⏭ click: call `streamer.skip()` (existing behavior)
    - On 🚫 click: call `streamer.stop()` (existing behavior)
    - _Requirements: 7.6, 14.2, 14.3, 14.6_

  - [x] 21.2 Implement `_build_seek_bar()` helper function
    - Generate text seek bar: 10-segment bar using `▬` (empty) and `🔘` (current position indicator)
    - Format: `▬🔘▬▬▬▬▬▬▬▬ 0:30 / 4:24`
    - Position = `floor(elapsed / duration * 10)` (clamp to [0, 9])
    - If duration unknown (0): show `▬▬▬▬▬▬▬▬▬▬ 0:00 / ???`
    - _Requirements: 14.1_

  - [x] 21.3 Implement periodic Now Playing embed update task
    - Background task runs every 30 seconds while session is active
    - Fetches current elapsed position from WebSocketHub state
    - Edits the original Now Playing message with updated seek bar
    - Stops when session ends or message is deleted
    - Store the message reference in the streamer/cog for editing
    - _Requirements: 14.5_

  - [x] 21.4 Wire WebSocketHub into VideoCog
    - Pass `WebSocketHub` reference to `VideoControlView` so buttons can broadcast
    - On session start: create PlaybackState in hub
    - On session stop: call `hub.disconnect_all(guild_id)` and clear state
    - On skip (new video starts): update hub state with position=0, playing=True, broadcast `state` to all clients
    - _Requirements: 10.2, 10.3, 10.4, 10.5_

- [x] 22. Update `SessionStatus` dataclass and status API
  - [x] 22.1 Extend `SessionStatus` with new fields
    - Add `subtitles: list[dict]` field (default empty list)
    - Add `audio_tracks: list[dict]` field (default empty list)
    - Add `playing: bool` field (default True)
    - Update `handle_status()` in ActivityBackend to populate these from pipeline and WebSocketHub
    - _Requirements: 5.1, 11.3, 12.2_

- [x] 23. Checkpoint - Ensure all new features pass integration
  - Ensure all tests pass, ask the user if questions arise.

- [x] 24. Update Kubernetes deployment for WebSocket support
  - [x] 24.1 Verify ingress supports WebSocket upgrade for `/activity/ws/`
    - Traefik (existing ingress controller) supports WebSocket by default
    - Ensure no annotation or timeout prevents long-lived WebSocket connections
    - Add annotation `traefik.ingress.kubernetes.io/router.middlewares` if needed for WS upgrade
    - Verify keepalive settings allow persistent connections
    - _Requirements: 16.2_

- [x] 25. Final checkpoint - All features integrated
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
- WebSocket sync uses aiohttp's built-in WebSocket support (no additional dependency)
- Volume is ALWAYS per-user — no "for everyone" option for volume
- Subtitle/audio "for everyone" broadcasts via WebSocket; without checkbox, changes are local-only
- The seek bar in the Now Playing embed uses Unicode block characters for visual rendering
- The Now Playing embed auto-updates every 30s — Discord rate limits embed edits to ~5/5s, so 30s is safe
- Multi-audio HLS uses `#EXT-X-MEDIA` for audio group selection — hls.js supports this natively
- Subtitle tracks are extracted as sidecar WebVTT files (not embedded in HLS segments) for language switching

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1"] },
    { "id": 1, "tasks": ["2.1", "3.1"] },
    { "id": 2, "tasks": ["2.2", "2.3", "2.4", "4.1", "4.1a"] },
    { "id": 3, "tasks": ["4.2", "4.3", "4.4", "4.5", "4.6", "7.1"] },
    { "id": 4, "tasks": ["6.1", "8.1", "8.3"] },
    { "id": 5, "tasks": ["6.2", "6.3", "8.2"] },
    { "id": 6, "tasks": ["10.1", "10.1a"] },
    { "id": 7, "tasks": ["10.2", "10.3", "11.1"] },
    { "id": 8, "tasks": ["11.2", "13.1"] },
    { "id": 9, "tasks": ["14.1"] },
    { "id": 10, "tasks": ["16.1", "16.2", "17.1"] },
    { "id": 11, "tasks": ["16.3", "16.4", "17.2", "17.3", "18.1"] },
    { "id": 12, "tasks": ["18.2", "18.3", "18.4"] },
    { "id": 13, "tasks": ["20.1", "20.2", "20.3", "20.4", "20.5", "20.6", "22.1"] },
    { "id": 14, "tasks": ["21.1", "21.2", "21.3", "21.4"] },
    { "id": 15, "tasks": ["24.1"] }
  ]
}
```

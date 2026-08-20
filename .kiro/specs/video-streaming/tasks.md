# Implementation Plan: Video Streaming

## Overview

This plan implements Discord Go Live video streaming for the HelloDJ bot, adding Intel QSV hardware-accelerated transcoding, YouTube/URL/upload video sources, and RTP video packet transmission over Discord's undocumented screenshare protocol. Implementation is in Python, leveraging the existing bot architecture (wavelink, player.py guild state, file_handler.py).

## Tasks

- [x] 1. Set up video module structure and data models
  - [x] 1.1 Create `bot/video/__init__.py` and core data models
    - Create `bot/video/` directory with `__init__.py`
    - Define `StreamState` enum (idle, resolving, buffering, streaming, stopping, error)
    - Define `Resolution` enum with width/height tuples and `from_height` classmethod
    - Define `SourceQuality` enum for YouTube quality selection (360p–2160p)
    - Define `FormatInfo` dataclass (height, codec, fps, filesize_approx, format_id)
    - Define `VideoSource` dataclass (source_type, file_path, title, duration_seconds, metadata, audio_url, cleanup_on_finish)
    - _Requirements: 6.1, 6.3, 7.1, 3.1_

  - [ ]* 1.2 Write property test for Resolution scaling (Property 7)
    - **Property 7: Resolution Scaling with Aspect Ratio Preservation**
    - Test that for any source dimensions and target resolution, output height equals target, width preserves aspect ratio (rounded to even), and letterbox padding fills the target frame
    - **Validates: Requirements 6.1, 6.3, 6.5**

  - [ ]* 1.3 Write property test for video file extension routing (Property 4)
    - **Property 4: Video File Extension Routing**
    - Test that `is_video_extension` returns True iff extension (case-insensitive) is in {mp4, mkv, webm, avi, mov, m4v}
    - **Validates: Requirements 4.1, 5.1**

  - [ ]* 1.4 Write property test for file size validation (Property 5)
    - **Property 5: File Size Validation**
    - Test that rejection occurs iff size exceeds threshold, and the message contains both actual and max size
    - **Validates: Requirements 4.4, 5.5**

- [x] 2. Implement GPU probe and Dockerfile changes
  - [x] 2.1 Create `bot/video/gpu_probe.py` — GPUProbe class
    - Implement `probe()`: check `/dev/dri/renderD*` existence, run `vainfo` subprocess, parse output
    - Implement `gpu_available` property and `require_gpu()` method (raises if unavailable)
    - Log probe result at INFO level on startup
    - Set internal `gpu_available` flag to false if probe fails
    - _Requirements: 1.2, 1.3_

  - [x] 2.2 Update `bot/Dockerfile` — add QSV-enabled ffmpeg and Intel runtime packages
    - Replace Debian `ffmpeg` with `jellyfin-ffmpeg7` (includes QSV/oneVPL) or equivalent custom build
    - Install `intel-media-va-driver-non-free`, `libmfx-gen1.2`, `libvpl2`, `vainfo`
    - Verify QSV works with a build-time test: `ffmpeg -hwaccels | grep qsv`
    - Add `bot/video/` to the COPY directive
    - _Requirements: 9.1, 9.2, 9.3, 9.4_

  - [x] 2.3 Update `kube/deployment.yaml` — add GPU resource limit
    - Add `intel.com/sriov-gpudevice: 1` to bot container `resources.limits`
    - Keep existing cpu/memory limits unchanged
    - _Requirements: 1.1, 1.4_

- [x] 3. Checkpoint — Ensure GPU probe and Dockerfile build
  - Ensure all tests pass, ask the user if questions arise.

- [x] 4. Implement transcode pipeline
  - [x] 4.1 Create `bot/video/transcode.py` — TranscodePipeline class
    - Implement `build_ffmpeg_args()`: construct ffmpeg CLI with QSV hwaccel decode, VPP scale, h264_qsv encode, Annex-B raw output to pipe:1
    - Implement `start()`: spawn ffmpeg subprocess with asyncio, configure input/output pipes
    - Implement `read_nal_unit()`: read H.264 NAL units from ffmpeg stdout (start-code delimited)
    - Implement `restart_at()`: kill current process, respawn with `-ss` seek to given timestamp + new resolution
    - Implement `stop()`: SIGKILL + wait in finally block, cleanup
    - Implement 60-second timeout watchdog task (abort if no output frames)
    - Handle QSV decode fallback: detect QSV decode error in stderr, retry with software decode + QSV encode
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 6.2, 6.4_

  - [ ]* 4.2 Write property test for ffmpeg command generation (Property 1)
    - **Property 1: ffmpeg Command Generation Correctness**
    - Test that `build_ffmpeg_args` includes QSV hwaccel flags for decodable codecs, software decode for non-QSV codecs, always includes `h264_qsv` encoder, and caps bitrate proportional to resolution
    - **Validates: Requirements 2.2, 2.3, 2.5**

- [x] 5. Implement RTP video sender and Go Live protocol
  - [x] 5.1 Create `bot/video/rtp_sender.py` — RTPVideoSender class
    - Implement `establish_session()`: send stream-create gateway opcode, connect to video voice UDP endpoint, receive SSRC
    - Implement `send_frame()`: packetize H.264 NAL units into RTP packets with correct header (version 2, PT 101, marker bit, timestamp increment = 90000/fps)
    - Implement `_fragment_nal()`: FU-A fragmentation for NAL units > 1200 bytes MTU
    - Implement `terminate_session()`: send stream-delete opcode, close UDP socket
    - Implement `reconnect()`: exponential backoff (1s, 2s, 4s, max 8s), up to 3 retries
    - _Requirements: 8.1, 8.2, 8.3, 8.4, 8.5, 8.6_

  - [ ]* 5.2 Write property test for RTP packetization (Property 10)
    - **Property 10: RTP Frame Rate Capping and Packetization**
    - Test that effective fps = min(source_fps, 60), NAL ≤ 1200 → single packet, NAL > 1200 → FU-A fragments ≤ 1200, sequence numbers monotonically increasing, timestamps increment by floor(90000/effective_fps)
    - **Validates: Requirements 8.2**

  - [ ]* 5.3 Write property test for reconnection backoff (Property 11)
    - **Property 11: Reconnection Exponential Backoff**
    - Test that retry occurs if attempts < 3, delay is 2^(attempt-1) capped at 8s, failure reported after 3 exhausted attempts
    - **Validates: Requirements 8.5**

- [x] 6. Checkpoint — Ensure transcode + RTP tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 7. Implement video sources (YouTube, URL, Upload)
  - [x] 7.1 Create `bot/video/sources.py` — YouTubeResolver, URLDownloader
    - Implement `YouTubeResolver.resolve()`: call yt-dlp with format selection based on SourceQuality, download video, return VideoSource
    - Implement `YouTubeResolver.query_formats()`: list available quality options (height, codec, filesize)
    - Implement `URLDownloader.download()`: validate URL (10s timeout, Content-Type check, 401/403 handling), download up to 100MB, return VideoSource
    - Implement video extension detection utility (`is_video_extension()`)
    - Implement file size validation utility
    - Implement yt-dlp error classification (unavailable, age-restricted, geo-restricted, network, unknown)
    - _Requirements: 3.1, 3.4, 3.7, 3.8, 5.1, 5.2, 5.3, 5.4, 5.5, 7.1, 7.2, 7.3, 7.4, 7.5_

  - [ ]* 7.2 Write property test for yt-dlp error classification (Property 3)
    - **Property 3: yt-dlp Error Classification**
    - Test that any yt-dlp error string maps to exactly one category, and the user message contains the classified reason text
    - **Validates: Requirements 3.4**

  - [ ]* 7.3 Write property test for YouTube quality selection (Property 8)
    - **Property 8: YouTube Quality Selection with Fallback**
    - Test that: selects max available height ≤ requested, if none selects minimum available, defaults to 1080p when unspecified, result is always a member of available set
    - **Validates: Requirements 7.1, 7.2, 7.3, 7.4**

  - [ ]* 7.4 Write property test for URL metadata extraction (Property 6)
    - **Property 6: URL Metadata Extraction**
    - Test that hostname matches URL domain (no scheme/port), filename matches last path segment before query params
    - **Validates: Requirements 5.2**

- [x] 8. Implement VideoStreamer orchestrator
  - [x] 8.1 Create `bot/video/streamer.py` — VideoStreamer class
    - Implement per-guild streaming session lifecycle (state machine: idle → resolving → buffering → streaming → stopping)
    - Implement `play()`: resolve source, start transcode pipeline, establish Go Live session, begin frame send loop
    - Implement `stop()`: terminate Go Live session, kill ffmpeg, cleanup temp files
    - Implement `change_resolution()`: restart ffmpeg at current timestamp with new resolution (within 5s)
    - Implement `gpu_available` property delegating to GPUProbe
    - Wire audio playback: route audio URL to Lavalink/wavelink for synchronized playback
    - Implement video queue processing (play next item on completion)
    - _Requirements: 3.2, 3.5, 4.3, 6.4, 8.3_

  - [x] 8.2 Implement now-playing embed builder in `bot/video/streamer.py`
    - YouTube: title truncated to 256 chars, channel name ≤ 256 chars, duration as HH:MM:SS
    - Upload: filename truncated to 128 chars, uploader Discord username
    - URL: hostname (no scheme/port), filename from path (no query params)
    - _Requirements: 3.3, 4.2, 5.2_

  - [ ]* 8.3 Write property test for now-playing formatting (Property 2)
    - **Property 2: Now-Playing Metadata Formatting**
    - Test truncation rules, presence of required fields per source type, prefix preservation
    - **Validates: Requirements 3.3, 4.2, 5.2**

  - [ ]* 8.4 Write property test for format list embed rendering (Property 9)
    - **Property 9: Format List Embed Rendering**
    - Test that for any non-empty list of FormatInfo entries, embed contains one entry per format with height, codec, and size
    - **Validates: Requirements 7.5**

- [x] 9. Checkpoint — Ensure VideoStreamer + sources pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 10. Implement VideoCog and integrate with existing bot
  - [x] 10.1 Create `bot/cogs/video.py` — VideoCog slash commands
    - Implement `/video play <query|url>` — resolve YouTube/URL, start streaming
    - Implement `/video stop` — stop current stream
    - Implement `/video resolution <value>` — change output resolution mid-stream
    - Implement `/video quality <value>` — set YouTube source quality for next play
    - Implement `/video formats <url>` — query and display available YouTube formats
    - Add `require_gpu` check as a command pre-check (ephemeral error if unavailable)
    - Add voice channel presence check (reject if user not in voice)
    - _Requirements: 1.3, 3.1, 3.6, 3.7, 6.1, 6.6, 7.5_

  - [x] 10.2 Modify `bot/player.py` — add video state to guild_state
    - Add `video_streamer: VideoStreamer | None` to guild state dict
    - Add `video_queue: list[VideoSource]` to guild state dict
    - Initialize both to None/[] in state creation
    - _Requirements: 3.2, 3.5_

  - [x] 10.3 Modify `bot/file_handler.py` — route video uploads to VideoStreamer
    - When `detect_type()` returns "video" and a VideoStreamer is available for the guild, route to `VideoStreamer.play()` instead of `extract_audio()`
    - Add file size validation (reject > 500MB with informative error)
    - Add voice channel presence check before accepting video uploads
    - Handle corrupted video detection (ffmpeg decode failure → reject)
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6_

  - [x] 10.4 Register VideoCog in `bot/bot.py` and wire GPUProbe startup
    - Add `bot/video/` import and cog load in bot startup
    - Call `GPUProbe.probe()` during bot startup, store result
    - Register cleanup of stale video temp files on startup (extend existing cleanup)
    - _Requirements: 1.2, 4.3_

- [x] 11. Final checkpoint — Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- Checkpoints ensure incremental validation
- Property tests validate universal correctness properties from the design document using Hypothesis
- Unit tests validate specific examples and edge cases
- The GPU probe (2.1) must be implemented before video commands to enable graceful degradation
- The RTP sender (5.1) implements the undocumented Discord Go Live protocol — reference Discord-RE/Discord-video-stream for signaling details
- ffmpeg must be QSV-capable (jellyfin-ffmpeg or custom build) — the stock Debian ffmpeg does NOT include QSV
- All video temp files are cleaned up within 60s of stream completion; stale files (>24h) cleaned on startup

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1"] },
    { "id": 1, "tasks": ["1.2", "1.3", "1.4", "2.1", "2.2", "2.3"] },
    { "id": 2, "tasks": ["4.1"] },
    { "id": 3, "tasks": ["4.2", "5.1"] },
    { "id": 4, "tasks": ["5.2", "5.3", "7.1"] },
    { "id": 5, "tasks": ["7.2", "7.3", "7.4", "8.1"] },
    { "id": 6, "tasks": ["8.2"] },
    { "id": 7, "tasks": ["8.3", "8.4", "10.1"] },
    { "id": 8, "tasks": ["10.2", "10.3", "10.4"] }
  ]
}
```

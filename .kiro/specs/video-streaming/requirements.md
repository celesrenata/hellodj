# Requirements Document

## Introduction

Video streaming capabilities for the HelloDJ Discord music bot, enabling the bot to broadcast video content into a Discord voice channel via the Go Live / screenshare mechanism. This feature adds GPU-accelerated video transcoding using Intel QSV (Quick Sync Video) through SR-IOV GPU device passthrough in Kubernetes, and supports YouTube video streaming, direct file uploads, and arbitrary URL playback — all with configurable resolution and quality selection.

## Glossary

- **Video_Streamer**: The video streaming subsystem of the HelloDJ bot responsible for acquiring, transcoding, and broadcasting video frames to Discord voice channels via the screenshare/Go Live protocol.
- **Transcoder**: The ffmpeg-based component that performs hardware-accelerated video encoding and decoding using Intel QSV, handling resolution changes and format conversion.
- **QSV**: Intel Quick Sync Video — a hardware video encoding/decoding engine built into Intel GPUs, accessed via the `intel.com/sriov-gpudevice` Kubernetes resource.
- **SR-IOV_GPU**: A Single Root I/O Virtualization virtual function of an Intel GPU, exposed to Kubernetes pods via the `intel.com/sriov-gpudevice` device plugin resource.
- **Go_Live**: Discord's screenshare/video broadcast feature within voice channels, used here to stream video content from the bot to channel participants.
- **Now_Playing_Window**: The visual overlay or embed displayed to viewers during video playback, showing metadata about the currently streaming content (title, uploader, resolution).
- **Source_Selector**: The component responsible for selecting video quality from YouTube (e.g., 720p, 1080p, 4K) via yt-dlp format selection.
- **Resolution_Scaler**: The transcoding pipeline stage that scales video output to a user-specified resolution (upscaling or downscaling) before streaming to Discord.
- **RTP_Video_Sender**: The component that encodes video frames into RTP packets compatible with Discord's voice/video UDP transport, enabling the bot to act as a screenshare source.
- **Upload_Handler**: The existing file upload processing system (`file_handler.py`), extended to support video playback through the streaming pipeline rather than audio-only extraction.

## Requirements

### Requirement 1: GPU Device Access in Kubernetes

**User Story:** As a cluster operator, I want the bot container to request Intel SR-IOV GPU resources, so that hardware-accelerated video transcoding is available at runtime.

#### Acceptance Criteria

1. THE Video_Streamer container specification SHALL request exactly 1 `intel.com/sriov-gpudevice` as a Kubernetes resource limit in the bot container's resource manifest
2. WHEN the bot container starts, THE Video_Streamer SHALL verify GPU accessibility by checking for the existence of a render device node (`/dev/dri/renderD*`) and executing a VA-API capability query (`vainfo`), logging the result at INFO level
3. IF the render device node is not present or the VA-API capability query fails, THEN THE Video_Streamer SHALL set an internal `gpu_available` flag to false and all video streaming commands SHALL return an ephemeral error embed stating "Video streaming unavailable: Intel GPU device not detected"
4. THE bot container SHALL remain schedulable without the GPU resource by using a separate scheduling strategy (e.g., resource requests of 0 with limits of 1, or a dedicated video sidecar with optional GPU), ensuring core audio functionality is not blocked by GPU unavailability

### Requirement 2: Hardware-Accelerated Video Transcoding

**User Story:** As a user, I want all video content to be processed through Intel QSV hardware acceleration, so that transcoding is fast and does not overload the bot's CPU.

#### Acceptance Criteria

1. THE Transcoder SHALL use ffmpeg compiled with QSV support (oneVPL or libmfx) for all video encoding and decoding operations
2. WHEN video content is received for streaming, THE Transcoder SHALL decode the source using QSV hardware acceleration (`-hwaccel qsv -hwaccel_output_format qsv`)
3. WHEN video content is transcoded for output, THE Transcoder SHALL encode using the QSV H.264 encoder (`h264_qsv`) producing an H.264 Baseline or Main profile bitstream at constrained VBR with a maximum bitrate of 8 Mbps for 1080p output
4. IF QSV hardware acceleration fails during a transcode operation, THEN THE Transcoder SHALL log the ffmpeg stderr output at ERROR level and report the failure to the user within 5 seconds with a message indicating "Hardware transcoding failed"
5. IF the source video container or codec is not supported by the QSV decoder (e.g., AV1 on older hardware), THEN THE Transcoder SHALL fall back to software decoding for that stream while still using QSV for encoding, and log the fallback at WARNING level
6. THE Transcoder SHALL abort any single transcode operation that has not produced output frames within 60 seconds, reporting a timeout error to the user

### Requirement 3: YouTube Video Streaming to Discord Go Live

**User Story:** As a user, I want to stream YouTube videos into a Discord voice channel's screenshare, so that everyone in the channel can watch the video together.

#### Acceptance Criteria

1. WHEN a user issues a video play command with a YouTube URL or search query, THE Video_Streamer SHALL resolve the query using yt-dlp, download the video stream within 30 seconds, and broadcast it to the voice channel via Go Live
2. WHILE streaming a YouTube video, THE Video_Streamer SHALL simultaneously play the audio track through the voice channel audio stream, synchronized with the video within the tolerance defined by the RTP_Video_Sender
3. WHILE streaming a YouTube video, THE Now_Playing_Window SHALL display the video title (truncated to 256 characters), channel name (truncated to 256 characters), and duration in HH:MM:SS format
4. IF yt-dlp fails to retrieve the YouTube video stream, THEN THE Video_Streamer SHALL report the error to the user with a message indicating the failure reason (video unavailable, age-restricted, geo-restricted, or network error)
5. WHEN a YouTube video finishes playing, THE Video_Streamer SHALL stop the Go Live broadcast within 3 seconds and proceed to the next queued item if one exists
6. IF the user issuing the video play command is not in a voice channel, THEN THE Video_Streamer SHALL reject the command with a message indicating the user must join a voice channel first
7. IF a YouTube search query returns no results, THEN THE Video_Streamer SHALL inform the user that no videos were found for the given query
8. IF the yt-dlp download exceeds 30 seconds without beginning playback, THEN THE Video_Streamer SHALL cancel the download and report a timeout error to the user

### Requirement 4: Uploaded File Video Playback

**User Story:** As a user, I want to upload video files directly to the bot and have them streamed to the voice channel's screenshare, so that I can share local video content with the channel.

#### Acceptance Criteria

1. WHEN a user uploads a file with a supported video extension (mp4, mkv, webm, avi, mov, m4v), THE Upload_Handler SHALL route the file to the Video_Streamer for screenshare playback instead of audio-only extraction
2. WHILE streaming an uploaded video, THE Now_Playing_Window SHALL display the filename (truncated to 128 characters if longer) and the Discord username of the person who uploaded the video
3. WHEN an uploaded video finishes playing, THE Video_Streamer SHALL delete the temporary video file from the uploads directory within 60 seconds of playback completion
4. IF the uploaded video file exceeds the configured maximum file size (default: 500 MB), THEN THE Upload_Handler SHALL reject the upload with a message indicating the file size, the maximum allowed size, and the supported size limit
5. IF the uploaded video file cannot be decoded by the Transcoder (corrupted container, missing codecs, or zero-length video stream), THEN THE Upload_Handler SHALL reject the file with a message indicating the file is not a playable video and delete the downloaded temporary file
6. IF the bot is not connected to a voice channel when a video file is uploaded, THEN THE Upload_Handler SHALL prompt the user to join a voice channel before uploading video content

### Requirement 5: URL Video Streaming

**User Story:** As a user, I want to stream video from arbitrary URLs with supported video filetypes, so that I can share video content hosted anywhere on the web.

#### Acceptance Criteria

1. WHEN a user provides a URL ending in a supported video extension (.mp4, .mkv, .webm, .avi, .mov, .m4v), THE Video_Streamer SHALL download the video and stream it to the voice channel via Go Live within 30 seconds of the command being issued for files up to 100 MB in size
2. WHEN streaming a URL video, THE Now_Playing_Window SHALL display the URL hostname and the filename extracted from the URL path (excluding query parameters)
3. IF the URL is unreachable after a connection timeout of 10 seconds or returns a Content-Type header that does not begin with "video/", THEN THE Video_Streamer SHALL report an error message to the user indicating whether the URL was unreachable or returned non-video content
4. IF the URL returns an HTTP 401 or 403 status code, THEN THE Video_Streamer SHALL inform the user that the URL is not publicly accessible and discard the request without retrying
5. IF the video file at the URL exceeds 100 MB in size, THEN THE Video_Streamer SHALL reject the request and inform the user of the maximum supported file size

### Requirement 6: Resolution Control

**User Story:** As a user, I want to change the resolution of the video being streamed, so that I can balance quality against bandwidth and performance.

#### Acceptance Criteria

1. WHEN a user specifies a target resolution from the supported set (480p, 720p, 1080p, 1440p, 2160p), THE Resolution_Scaler SHALL transcode the video output to that resolution's height (480, 720, 1080, 1440, or 2160 pixels) while computing the width from the source aspect ratio
2. THE Resolution_Scaler SHALL support both upscaling (e.g., 480p source to 720p output) and downscaling (e.g., 4K source to 1080p output) using the QSV VPP scaling filter
3. WHEN no resolution is specified by the user, THE Resolution_Scaler SHALL default to the source video's native resolution capped at 1080p (i.e., if source height exceeds 1080, output is scaled down to 1080p)
4. WHEN the resolution is changed mid-stream via a user command, THE Resolution_Scaler SHALL apply the new resolution within 5 seconds without restarting the video from the beginning, by restarting the ffmpeg transcode pipeline at the current timestamp
5. THE Resolution_Scaler SHALL maintain the source video's aspect ratio when scaling; if the user requests a resolution that would alter the aspect ratio, the output SHALL be padded with black letterbox bars to fill the target frame dimensions
6. IF a user specifies an unsupported resolution value (not in the supported set), THEN THE Resolution_Scaler SHALL reject the command with a message listing the supported resolutions

### Requirement 7: YouTube Quality Source Selection

**User Story:** As a user, I want to select the video quality downloaded from YouTube, so that I can choose between faster loading at lower quality or higher fidelity at higher quality.

#### Acceptance Criteria

1. WHEN a user specifies a YouTube source quality from the supported set (360p, 480p, 720p, 1080p, 1440p, 2160p), THE Source_Selector SHALL use yt-dlp format selection to download the best available video stream with height less than or equal to the specified value combined with the best available audio (`-f bestvideo[height<=N]+bestaudio/best[height<=N]`)
2. IF the requested quality is not available for the YouTube video, THEN THE Source_Selector SHALL select the next-best available quality below the requested height and inform the user via an embed message stating the actual quality selected (e.g., "Requested 1080p, streaming at 720p")
3. IF no video stream is available at or below the requested quality, THEN THE Source_Selector SHALL select the lowest available quality and inform the user of the actual quality
4. WHEN no source quality is specified, THE Source_Selector SHALL default to the best available quality up to 1080p
5. WHEN a user issues a quality-query command for a YouTube URL, THE Source_Selector SHALL present the available quality options (listing each available height with codec and approximate file size) as a Discord embed before download

### Requirement 8: Discord Go Live Protocol Integration

**User Story:** As a developer, I want the bot to establish and maintain a Go Live screenshare session in a Discord voice channel, so that video frames can be broadcast to all channel participants.

#### Acceptance Criteria

1. WHEN the Video_Streamer has video frames ready for broadcast and the bot is connected to a voice channel, THE RTP_Video_Sender SHALL establish a Go Live session by sending the Discord gateway Video/Stream signaling opcodes
2. WHEN the Go Live session is established, THE RTP_Video_Sender SHALL send H.264 encoded video frames as RTP packets over the Discord voice UDP connection at the source video's frame rate, not exceeding 60 fps
3. WHILE streaming video, THE RTP_Video_Sender SHALL synchronize audio and video timestamps to maintain lip-sync within 50ms
4. WHEN the bot disconnects from the voice channel or streaming stops, THE RTP_Video_Sender SHALL terminate the Go Live session by sending the stream-delete signaling opcode and ceasing all RTP packet transmission within 2 seconds
5. IF the Go Live session drops unexpectedly, THEN THE RTP_Video_Sender SHALL attempt to re-establish the session up to 3 times with exponential backoff starting at 1 second with a maximum delay of 8 seconds, and report failure to the user if all attempts are exhausted
6. IF the Go Live session is re-established after an unexpected drop, THEN THE RTP_Video_Sender SHALL resume streaming from the current playback position rather than restarting from the beginning

### Requirement 9: Docker Image with QSV-Enabled ffmpeg

**User Story:** As a cluster operator, I want the bot container image to include an ffmpeg build with Intel QSV support, so that hardware-accelerated transcoding works at runtime.

#### Acceptance Criteria

1. THE bot Dockerfile SHALL produce an image containing ffmpeg compiled with QSV support (via oneVPL or libmfx) and the Intel Media SDK or oneVPL runtime libraries
2. THE bot Dockerfile SHALL include the Intel GPU compute runtime packages required for QSV device access (`intel-media-va-driver-non-free` or `intel-media-va-driver`, `libmfx-gen1`, `libvpl2`, or equivalent packages for the target Debian/Ubuntu base)
3. WHEN the container starts with a GPU device mounted at `/dev/dri/renderD*`, THE Transcoder SHALL successfully execute `ffmpeg -hwaccels` and list `qsv` as an available hardware acceleration method
4. WHEN the container starts with a GPU device mounted, THE Transcoder SHALL successfully execute a test transcode (`ffmpeg -hwaccel qsv -f lavfi -i testsrc=duration=1:size=320x240:rate=30 -c:v h264_qsv -f null -`) completing without error, confirming end-to-end QSV pipeline functionality

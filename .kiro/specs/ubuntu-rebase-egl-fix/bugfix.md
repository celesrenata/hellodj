# Bugfix Requirements Document

## Introduction

Two bugs affecting the HelloDJ bot's GPU visualizer pipeline and audio playback routing:

**Bug 1 (Primary):** The bot container (based on `python:3.11-slim` / Debian trixie) ships Mesa 25.0.7 with libglvnd 1.7.0. This combination has broken EGL GBM platform dispatch — `eglGetPlatformDisplay(EGL_PLATFORM_GBM_KHR, gbm_device, NULL)` returns NULL with `EGL_BAD_PARAMETER`. The libglvnd dispatch layer doesn't properly route GBM platform requests to Mesa's vendor library, blocking all server-rendered GPU visualizer engines (audiovis, projectm, fosfora, varda) which require headless EGL + OpenGL 3.3 Core to render frames into the HLS pipeline. The current `eglGetPlatformDisplayEXT` workaround via `eglGetProcAddress` also fails in this environment.

**Bug 2 (Secondary):** YouTube URLs played via `/play` (audio mode, no explicit `mode:` override) are being incorrectly routed through the HLS Activity pipeline as music videos instead of through Lavalink audio. The unified queue entry gets `type: "music_video"` when it should have no `type` field (audio). This causes the visualizer to be set to DISABLED. The root cause is in the interplay between `router.py` and `classifier.py` — specifically, Rule 10 classifies unrecognized URLs (those without a known audio-domain match) as VIDEO by default, and certain YouTube URL variants may not match the hostname check in Rule 9.

## Bug Analysis

### Current Behavior (Defect)

#### Bug Condition 1: EGL GBM Platform Dispatch Failure on Debian Trixie

1.1 WHEN the bot container runs on `python:3.11-slim` (Debian trixie) with Mesa 25.0.7 and libglvnd 1.7.0 THEN `eglGetPlatformDisplayEXT(EGL_PLATFORM_GBM_KHR, gbm_device, NULL)` returns NULL/EGL_BAD_PARAMETER because glvnd's dispatch layer does not route GBM platform requests to Mesa's vendor library. The `__EGL_VENDOR_LIBRARY_FILENAMES` environment variable bypass (pointing directly to `/usr/lib/x86_64-linux-gnu/libEGL_mesa.so.0`) also fails because Mesa's vendor library does not export standard EGL entry points when loaded through glvnd.

1.2 WHEN any GPU visualizer engine (audiovis, projectm, fosfora, varda) attempts to create a headless EGL context via `EGLHeadlessContext.create()` on `/dev/dri/renderD133` THEN the engine fails with `EGLContextError("eglGetPlatformDisplayEXT failed (GBM)")` and no visualizer frames are rendered into the HLS pipeline, falling back to CPU-only mode (or no visualizer).

#### Bug Condition 2: PlaybackRouter Incorrect Video Classification

1.3 WHEN a user invokes `/play` with a YouTube URL whose hostname does not match the classifier's Rule 9 hostname list (currently: `youtube.com`, `www.youtube.com`, `m.youtube.com`, `youtu.be`, `www.youtu.be` — missing `youtube-nocookie.com`, `www.youtube-nocookie.com`, `music.youtube.com`, and any subdomain of `youtube.com`) AND no explicit `mode:` parameter is provided THEN the classifier falls through to Rule 10 which defaults unrecognized URLs to `ContentType.VIDEO` with `confidence="default"`

1.4 WHEN the classifier returns `ContentType.VIDEO` for a YouTube audio-intent request THEN the PlaybackRouter calls `_handle_video_play()` → `_start_video_session()` → `video_cog.video_play()` which creates a video Activity session, triggers `on_video_start()` on the visualizer registry setting it to DISABLED, and routes through the HLS pipeline instead of Lavalink audio playback

1.5 WHEN the classifier's Rule 10 returns `ContentType.VIDEO` as default for any unrecognized URL (even when user intent is audio playback) THEN audio-intent URLs that don't match an explicit audio-domain rule are incorrectly treated as video, because the fallback assumes unknown URLs are video content

### Expected Behavior (Correct)

#### Fix 1: EGL GBM Platform Dispatch Success on Ubuntu 26.04

2.1 WHEN the bot container uses Ubuntu 26.04 as its base image (which ships Mesa 25.x with a properly-integrated libglvnd) THEN `eglGetPlatformDisplay(EGL_PLATFORM_GBM_KHR, gbm_device, NULL)` SHALL return a non-NULL `EGLDisplay` handle and `eglInitialize()` SHALL populate major/minor version integers and return `EGL_TRUE`. Verification script:
```python
import ctypes, os
os.environ['MESA_LOADER_DRIVER_OVERRIDE'] = 'iris'
egl = ctypes.CDLL('libEGL.so.1')
gbm = ctypes.CDLL('libgbm.so.1')
gbm.gbm_create_device.restype = ctypes.c_void_p
fd = os.open('/dev/dri/renderD133', os.O_RDWR)
gbm_dev = gbm.gbm_create_device(fd)
egl.eglGetPlatformDisplay.restype = ctypes.c_void_p
display = egl.eglGetPlatformDisplay(0x31D7, ctypes.c_void_p(gbm_dev), None)
major, minor = ctypes.c_int(), ctypes.c_int()
assert egl.eglInitialize(ctypes.c_void_p(display), ctypes.byref(major), ctypes.byref(minor))
print(f"EGL {major.value}.{minor.value} — SUCCESS")
```

2.2 WHEN any GPU visualizer engine creates a headless EGL context via `EGLHeadlessContext.create()` on the rebased image targeting `/dev/dri/renderD133` (or any SR-IOV VF render node) THEN the engine SHALL successfully obtain an EGL display, bind EGL_OPENGL_API, select an EGL config with RENDERABLE_TYPE=EGL_OPENGL_BIT, create an OpenGL 3.3 Core context, attach a 1280×720 RGBA8 renderbuffer to an FBO, and `glCheckFramebufferStatus` SHALL return `GL_FRAMEBUFFER_COMPLETE`

#### Fix 2: Correct Audio Classification for YouTube URLs and Default Behavior

2.3 WHEN a URL with a YouTube-domain hostname (matching `youtube.com`, `www.youtube.com`, `m.youtube.com`, `youtu.be`, `www.youtu.be`, `music.youtube.com`, `youtube-nocookie.com`, or any hostname ending in `.youtube.com`) is passed to the classifier without an explicit `mode:` parameter THEN the classifier SHALL return `ContentType.AUDIO` with confidence `"default"` and source_hint `"youtube"`, regardless of the URL path structure

2.4 WHEN the classifier returns `ContentType.AUDIO` for a YouTube URL THEN the PlaybackRouter SHALL invoke `_handle_audio_play()` which routes the track through Lavalink, and the resulting queue entry SHALL NOT contain a `type` field (distinguishing it from video queue entries which carry `type: "music_video"`)

2.5 WHEN the classifier receives a URL that has a scheme (e.g. `https://`), does not match any known audio domain (YouTube, Spotify, Tidal, SoundCloud), and does not end in a recognized video file extension (`.mp4`, `.webm`, `.mkv`, `.avi`, `.mov`, `.m4v`) THEN the classifier SHALL return `ContentType.AUDIO` with confidence `"default"` and source_hint `"unknown_url"` — reversing Rule 10's current VIDEO default, since `/play`'s primary intent is audio playback and explicit `mode:video` exists for video requests

2.6 IF a YouTube URL contains tracking redirects, mobile share parameters, or query strings that do not alter the hostname THEN the system SHALL still match the URL against the YouTube hostname list and classify it as `ContentType.AUDIO`, preventing fallthrough to the unrecognized-URL default rule

### Unchanged Behavior (Regression Prevention)

3.1 WHEN FFmpeg 9 is invoked for HLS transcoding with QSV hardware acceleration THEN the system SHALL CONTINUE TO use the source-built FFmpeg 9 with libvpl, VA-API, libx264, libx265, libopus, libdav1d, and OpenSSL support (the HLS interleave fix must be preserved)

3.2 WHEN the container accesses `/dev/dri/renderD133` (Intel Meteor Lake iGPU SR-IOV VF) THEN the system SHALL CONTINUE TO use the iris driver for VA-API/QSV hardware video transcoding

3.3 WHEN a Tidal video URL (e.g. `https://tidal.com/browse/video/12345`) is played THEN the classifier SHALL CONTINUE TO classify it as VIDEO content and route through the HLS Activity pipeline

3.4 WHEN a Spotify URL (track, album, playlist, or artist — matching the pattern `https://open.spotify.com/...`) is played via `/play` THEN the classifier SHALL CONTINUE TO classify it as AUDIO and route through Lavalink

3.5 WHEN an explicit `mode:video` or `mode:music_video` parameter is provided to `/play` THEN the system SHALL CONTINUE TO route the request through the video/music_video pipeline regardless of URL type, with the classifier returning `ContentType.VIDEO` with confidence `"definite"` and source_hint `"mode_override"`

3.6 WHEN the init container runs `render_lavalink_config.py` THEN it SHALL CONTINUE TO have access to Python + cryptography in the bot image to read the encrypted SQLite credential store

3.7 WHEN Python package dependencies are installed THEN the system SHALL CONTINUE TO use Python 3.11 or later (3.11, 3.12, or 3.13) as the runtime version, verifiable via `python3 --version` inside the container

3.8 WHEN a URL not matching any known service pattern (Spotify, Tidal, YouTube, SoundCloud) ends with a video file extension (`.mp4`, `.webm`, `.mkv`, `.avi`, `.mov`, `.m4v`) is played THEN the classifier SHALL CONTINUE TO classify it as VIDEO content (Rule 8 preserved)

3.9 WHEN a plain text search query (no URL) is submitted to `/play` THEN the classifier SHALL CONTINUE TO classify it as AUDIO and search via Lavalink

3.10 WHEN libprojectm is used for Milkdrop preset rendering in the projectm visualizer engine THEN the system SHALL CONTINUE TO have libprojectm-dev available in the container

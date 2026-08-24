# Ubuntu Rebase & EGL Fix — Bugfix Design

## Overview

Two interrelated bugs block the HelloDJ bot's GPU visualizer pipeline and cause incorrect audio/video routing:

1. **EGL GBM Platform Dispatch Failure** — The current `python:3.11-slim` (Debian trixie) base image ships Mesa 25.0.7 with libglvnd 1.7.0 whose dispatch layer fails to route `eglGetPlatformDisplay(EGL_PLATFORM_GBM_KHR, ...)` to Mesa's vendor library. The existing `eglGetPlatformDisplayEXT` workaround via `eglGetProcAddress` also fails in this environment, blocking all GPU visualizer engines. The fix: rebase the Dockerfile onto Ubuntu 26.04 which ships Mesa 25.x with properly-integrated libglvnd, and simplify `egl_context.py` to call `eglGetPlatformDisplay` directly.

2. **PlaybackRouter Video Classification** — `classifier.py` Rule 9's YouTube hostname list is incomplete (missing `youtube-nocookie.com`, `music.youtube.com` in the general catch-all, subdomain patterns). Rule 10 defaults unrecognized URLs to VIDEO when `/play`'s intent is audio. The fix: expand hostname matching and flip Rule 10's default from VIDEO to AUDIO.

## Glossary

- **Bug_Condition (C)**: For Bug 1: running on Debian trixie with libglvnd 1.7.0 where GBM platform dispatch fails. For Bug 2: a YouTube URL whose hostname doesn't match the current Rule 9 list, OR any unrecognized URL falling through to Rule 10's VIDEO default.
- **Property (P)**: For Bug 1: `eglGetPlatformDisplay` returns a valid EGLDisplay and the full EGL→OpenGL 3.3 Core context creation succeeds. For Bug 2: YouTube URLs classify as AUDIO, unrecognized URLs default to AUDIO.
- **Preservation**: FFmpeg 9 source build (QSV/VPL, VA-API, x264, x265, opus, dav1d, OpenSSL), Python 3.11+, libprojectm, iris driver, Tidal VIDEO classification, Spotify AUDIO classification, explicit mode overrides, video-extension detection.
- **egl_context.py**: The `EGLHeadlessContext` class in `bot/video/visualizer_engines/egl_context.py` that creates a headless EGL/OpenGL 3.3 Core context on a DRM render node via GBM platform.
- **classifier.py**: The `classify()` function in `bot/playback/classifier.py` that routes user input to audio or video backend based on a priority-ordered rule chain.
- **GBM**: Generic Buffer Manager — the DRM buffer allocation API used as the EGL platform for headless GPU rendering without a display server.
- **libglvnd**: The GL Vendor-Neutral Dispatch library that routes EGL/GL calls to the correct vendor implementation (Mesa).

## Bug Details

### Bug Condition

The bug manifests in two independent conditions:

**Bug 1:** When the bot container runs on `python:3.11-slim` (Debian trixie) with Mesa 25.0.7 and libglvnd 1.7.0, the EGL dispatch layer cannot route GBM platform requests to Mesa. Both `eglGetPlatformDisplay` and the `eglGetPlatformDisplayEXT` workaround (obtained via `eglGetProcAddress`) return NULL/EGL_BAD_PARAMETER. All four GPU visualizer engines (audiovis, projectm, fosfora, varda) fail to initialize.

**Bug 2:** When a YouTube URL with hostname `youtube-nocookie.com`, `www.youtube-nocookie.com`, or any arbitrary subdomain of `youtube.com` is passed to `classify()` without explicit `mode:`, it falls through Rule 9's hostname set and hits Rule 10 which returns `ContentType.VIDEO`. Additionally, ANY unrecognized URL (no known audio-domain match, no video extension) defaults to VIDEO even when the user's intent is audio playback via `/play`.

**Formal Specification:**
```
FUNCTION isBugCondition(input)
  INPUT: input of type EGLContextCreateRequest | ClassifyRequest
  OUTPUT: boolean

  IF input.type == "egl_create":
    RETURN input.base_image == "python:3.11-slim"
           AND input.libglvnd_version <= "1.7.0"
           AND input.mesa_gbm_dispatch_broken == true
  
  IF input.type == "classify":
    LET hostname = parse_url(input.query).hostname
    LET known_youtube_hosts = {"youtube.com", "www.youtube.com", "m.youtube.com",
                               "youtu.be", "www.youtu.be"}
    LET is_youtube_domain = hostname ENDS_WITH ".youtube.com"
                            OR hostname IN {"youtube.com", "youtu.be", "www.youtu.be",
                                            "youtube-nocookie.com", "www.youtube-nocookie.com"}
    LET falls_through_rule9 = is_youtube_domain AND hostname NOT IN known_youtube_hosts
    LET is_unrecognized_url = has_scheme(input.query)
                              AND NOT matches_any_known_audio_domain(hostname)
                              AND NOT has_video_extension(input.query)
    
    RETURN (falls_through_rule9 OR is_unrecognized_url)
           AND input.mode == "auto"
END FUNCTION
```

### Examples

- **Bug 1 example**: `EGLHeadlessContext().create()` on Debian trixie → `EGLContextError("eglGetPlatformDisplayEXT failed (GBM)")` — visualizer renders nothing, HLS pipeline has no frames
- **Bug 2 example**: `classify("https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ")` → `ClassificationResult(VIDEO, "unknown_url", "default")` — should be AUDIO
- **Bug 2 example**: `classify("https://subdomain.youtube.com/watch?v=abc123")` → `ClassificationResult(VIDEO, "unknown_url", "default")` — should be AUDIO
- **Bug 2 example**: `classify("https://example.com/some-podcast-feed")` → `ClassificationResult(VIDEO, "unknown_url", "default")` — should be AUDIO (no video extension, /play intent is audio)
- **Correct behavior (no bug)**: `classify("https://tidal.com/browse/video/12345")` → `ClassificationResult(VIDEO, "tidal_video", "definite")` — this must be preserved

## Expected Behavior

### Preservation Requirements

**Unchanged Behaviors:**
- FFmpeg 9 source build with QSV (libvpl), VA-API, libx264, libx265, libopus, libdav1d, and OpenSSL must be preserved identically
- Python 3.11+ runtime (Ubuntu 26.04 ships Python 3.13; acceptable per requirement 3.7)
- `render_lavalink_config.py` init container must continue to have Python + cryptography available
- iris driver for Intel Meteor Lake iGPU VA-API/QSV hardware transcoding
- libprojectm-dev available for Milkdrop preset rendering
- Tidal video URLs (`/video/<id>` or `/browse/video/<id>`) continue classifying as VIDEO
- Spotify URLs continue classifying as AUDIO
- SoundCloud URLs continue classifying as AUDIO
- Explicit `mode:audio` / `mode:video` overrides continue working with confidence "definite"
- URLs ending in video extensions (`.mp4`, `.webm`, `.mkv`, `.avi`, `.mov`, `.m4v`) continue classifying as VIDEO
- Plain text search queries continue classifying as AUDIO
- Container UID/GID 1000, supplementalGroups [26], privileged: true for /dev/dri access

**Scope:**
All inputs that do NOT involve (a) the base image's EGL dispatch layer or (b) YouTube hostname matching / Rule 10 default should be completely unaffected by this fix. This includes:
- Mouse/keyboard interaction with Discord Activity frontend
- HLS transcode pipeline (ffmpeg command invocation)
- Lavalink config rendering
- All non-classifier playback routing logic
- Wake word / voice pipeline

## Hypothesized Root Cause

### Bug 1: EGL GBM Platform Dispatch

Based on the bug description and code analysis, the root causes are:

1. **Debian trixie's libglvnd 1.7.0 dispatch table misconfiguration**: The GBM platform enum (`EGL_PLATFORM_GBM_KHR = 0x31D7`) is not registered in libglvnd's internal platform dispatch table. When `eglGetPlatformDisplay` is called, libglvnd doesn't know which vendor library handles GBM → returns NULL.

2. **`eglGetPlatformDisplayEXT` workaround also broken**: Even though the current code fetches the function pointer via `eglGetProcAddress`, the returned function pointer routes through the same broken dispatch layer in this libglvnd version.

3. **Ubuntu 26.04's Mesa packaging solves this**: Ubuntu 26.04 ships Mesa 25.x built against a newer libglvnd (or with proper vendor JSON config in `/usr/share/glvnd/egl_vendor.d/`) that correctly routes GBM platform requests. The standard `eglGetPlatformDisplay` call works directly without any workaround.

### Bug 2: PlaybackRouter Classification

1. **Incomplete hostname set in Rule 9**: The current set `{"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be", "www.youtu.be"}` misses:
   - `youtube-nocookie.com` / `www.youtube-nocookie.com` (privacy-enhanced embeds)
   - `music.youtube.com` is handled by Rule 3 for AUDIO (definite) — this is correct
   - Arbitrary subdomains like `gaming.youtube.com`, `consent.youtube.com`

2. **Rule 10 default is wrong**: The fallback returns `ContentType.VIDEO` for any URL without a known audio-domain match or video extension. Since `/play`'s primary intent is audio playback (explicit `mode:video` exists for video), the default should be AUDIO. A user typing `/play https://some-podcast.example.com/episode-5` expects audio routing, not video.

## Correctness Properties

Property 1: Bug Condition - EGL Context Creation on Ubuntu 26.04

_For any_ render device path where a valid DRM render node exists (e.g., `/dev/dri/renderD128` or `/dev/dri/renderD133`), the fixed `EGLHeadlessContext.create()` running on Ubuntu 26.04 SHALL successfully obtain an EGLDisplay via `eglGetPlatformDisplay(EGL_PLATFORM_GBM_KHR, gbm_device, NULL)`, initialize it, bind OpenGL API, choose a config, create an OpenGL 3.3 Core context, and produce a complete framebuffer (GL_FRAMEBUFFER_COMPLETE).

**Validates: Requirements 2.1, 2.2**

Property 2: Bug Condition - YouTube and Unknown URL Classification

_For any_ URL whose hostname is a YouTube domain variant (including `youtube-nocookie.com`, subdomains of `youtube.com`, `youtu.be`) OR any unrecognized URL without a video file extension, when passed to `classify()` with `mode="auto"`, the fixed function SHALL return `ContentType.AUDIO` (not VIDEO).

**Validates: Requirements 2.3, 2.4, 2.5, 2.6**

Property 3: Preservation - Non-YouTube Known Service Classification

_For any_ URL matching Spotify (`open.spotify.com`), Tidal (`tidal.com`), or SoundCloud (`soundcloud.com`) domains, the fixed `classify()` function SHALL produce exactly the same `ClassificationResult` as the original function, preserving Tidal video detection, Spotify audio routing, and SoundCloud audio routing.

**Validates: Requirements 3.3, 3.4, 3.8**

Property 4: Preservation - Explicit Mode Override

_For any_ input with `mode="audio"` or `mode="video"`, the fixed `classify()` function SHALL return the same result as the original — `ContentType.AUDIO` or `ContentType.VIDEO` respectively with confidence "definite" and source_hint "mode_override".

**Validates: Requirements 3.5**

Property 5: Preservation - FFmpeg and System Dependencies

_For any_ container build from the fixed Dockerfile, FFmpeg 9 SHALL be available with QSV, VA-API, libx264, libx265, libopus, libdav1d, and OpenSSL support. Python SHALL be version 3.11 or later. libprojectm-dev SHALL be installed. The iris driver SHALL be usable via `/dev/dri/renderD*`.

**Validates: Requirements 3.1, 3.2, 3.7, 3.10**

## Fix Implementation

### Changes Required

#### File: `bot/Dockerfile`

**Full rewrite of base image and apt packages.**

1. **Change base image**: Replace `FROM python:3.11-slim` with `FROM ubuntu:26.04`
   - Ubuntu 26.04 ships Mesa 25.x with properly-integrated libglvnd
   - Ships Python 3.13 (satisfies requirement 3.7: Python 3.11+)

2. **Install Python and pip**: Ubuntu base doesn't include pip by default
   - `apt-get install python3 python3-pip python3-venv python3-dev`

3. **Install Mesa/EGL packages**: Ubuntu package names differ from Debian
   - `libegl1-mesa-dev`, `libgl1-mesa-dev`, `libgles2-mesa-dev`, `libgbm-dev`, `libdrm-dev`, `mesa-utils`
   - The key difference: Ubuntu 26.04's Mesa packages include proper `/usr/share/glvnd/egl_vendor.d/50_mesa.json` that registers Mesa as the GBM platform handler

4. **Install Intel VA-API/QSV packages**: Ubuntu equivalents
   - `intel-media-va-driver`, `libmfx-gen1.2`, `libvpl-dev`, `libva-dev`, `vainfo`

5. **Preserve FFmpeg 9 source build**: Identical `./configure` flags and build process

6. **Preserve libprojectm**: `libprojectm-dev`

7. **Preserve pip install layers**: Same three-stage requirements split (core → torch → AI)

8. **Symlink python3 → python**: Ensure `CMD ["python", "bot.py"]` works (Ubuntu installs as `python3`)

#### File: `bot/video/visualizer_engines/egl_context.py`

**Simplify EGL initialization to use `eglGetPlatformDisplay` directly.**

1. **Remove `eglGetProcAddress` / `eglGetPlatformDisplayEXT` workaround**: The entire block that fetches the function pointer via `eglGetProcAddress(b"eglGetPlatformDisplayEXT")` and casts to a CFUNCTYPE is no longer needed.

2. **Use `eglGetPlatformDisplay` directly**: Call `self._egl.eglGetPlatformDisplay(EGL_PLATFORM_GBM_KHR, gbm_device, None)` through the standard ctypes interface. Set proper `argtypes` and `restype` on the function.

3. **Remove `__EGL_VENDOR_LIBRARY_FILENAMES` environment override**: Ubuntu 26.04's libglvnd dispatch works correctly without manually pointing to Mesa's vendor library. The `MESA_LOADER_DRIVER_OVERRIDE=iris` hint can remain as it's still useful for SR-IOV VF selection.

4. **Update module docstring**: Reflect that we now use standard `eglGetPlatformDisplay` on Ubuntu 26.04 with GBM platform (no longer referencing EGL_MESA_platform_surfaceless or the EXT workaround).

#### File: `bot/playback/classifier.py`

**Fix YouTube hostname matching and default classification.**

1. **Expand Rule 9 hostname matching**: Replace the static hostname set with a function that matches:
   - Exact matches: `youtube.com`, `www.youtube.com`, `m.youtube.com`, `youtu.be`, `www.youtu.be`, `youtube-nocookie.com`, `www.youtube-nocookie.com`
   - Suffix match: any hostname ending in `.youtube.com` (catches `gaming.youtube.com`, `consent.youtube.com`, future subdomains)
   - Note: `music.youtube.com` is already handled by Rule 3 (AUDIO definite) which has higher priority

2. **Move Rule 9 above Rule 8**: No — Rule 8 (video extension) should remain higher priority. A URL like `https://youtube.com/download/video.mp4` should still be VIDEO. Keep rule order.

3. **Flip Rule 10 default**: Change from `ContentType.VIDEO` to `ContentType.AUDIO` with `source_hint="unknown_url"` and `confidence="default"`. This aligns with `/play`'s primary intent being audio — explicit `mode:video` exists for video requests.

4. **Update docstring Rule 10 description**: Document that unrecognized URLs default to AUDIO since `/play`'s intent is audio.

## Testing Strategy

### Validation Approach

The testing strategy follows a two-phase approach: first, surface counterexamples that demonstrate the bug on unfixed code, then verify the fix works correctly and preserves existing behavior.

### Exploratory Bug Condition Checking

**Goal**: Surface counterexamples that demonstrate the bugs BEFORE implementing the fix. Confirm or refute the root cause analysis.

**Test Plan (Bug 1 — EGL)**: Cannot easily run EGL tests outside the container, but the bug is deterministic on `python:3.11-slim`. Verification: build the rebased image and run the EGL verification script from requirement 2.1 inside the container against a render node.

**Test Plan (Bug 2 — Classifier)**: Write unit tests that call `classify()` with YouTube URL variants that currently misclassify. Run on UNFIXED code to observe failures.

**Test Cases**:
1. **youtube-nocookie.com test**: `classify("https://www.youtube-nocookie.com/embed/abc")` → expects AUDIO, currently returns VIDEO (will fail on unfixed code)
2. **Subdomain test**: `classify("https://gaming.youtube.com/watch?v=abc")` → expects AUDIO, currently returns VIDEO (will fail on unfixed code)
3. **Unknown URL default test**: `classify("https://example.com/podcast/ep5")` → expects AUDIO, currently returns VIDEO (will fail on unfixed code)
4. **music.youtube.com test**: `classify("https://music.youtube.com/watch?v=abc")` → expects AUDIO definite — this ALREADY works (Rule 3), confirms no regression

**Expected Counterexamples**:
- `classify("https://youtube-nocookie.com/embed/dQw4w9WgXcQ")` → `ClassificationResult(VIDEO, "unknown_url", "default")` — wrong, should be AUDIO
- `classify("https://consent.youtube.com/redirect?q=...")` → `ClassificationResult(VIDEO, "unknown_url", "default")` — wrong, should be AUDIO

### Fix Checking

**Goal**: Verify that for all inputs where the bug condition holds, the fixed function produces the expected behavior.

**Pseudocode:**
```
FOR ALL input WHERE isBugCondition(input) DO
  IF input.type == "classify":
    result := classify_fixed(input.query, mode=input.mode)
    ASSERT result.content_type == ContentType.AUDIO
    ASSERT result.confidence == "default"
    ASSERT result.source_hint IN {"youtube", "unknown_url"}
  IF input.type == "egl_create":
    ctx := EGLHeadlessContext(input.render_device)
    ctx.create()
    ASSERT ctx.is_valid == true
END FOR
```

### Preservation Checking

**Goal**: Verify that for all inputs where the bug condition does NOT hold, the fixed function produces the same result as the original function.

**Pseudocode:**
```
FOR ALL input WHERE NOT isBugCondition(input) DO
  IF input.type == "classify":
    ASSERT classify_original(input.query, mode=input.mode) == classify_fixed(input.query, mode=input.mode)
END FOR
```

**Testing Approach**: Property-based testing is recommended for preservation checking because:
- It generates many random URLs and mode combinations automatically
- It catches edge cases in URL parsing that manual tests would miss
- It provides strong guarantees that Tidal video, Spotify, SoundCloud, explicit mode overrides, and video-extension detection are all unchanged

**Test Plan**: Observe behavior on UNFIXED code first for Spotify, Tidal, SoundCloud, video-extension URLs, and explicit modes, then write property-based tests capturing that behavior.

**Test Cases**:
1. **Tidal Video Preservation**: Verify `classify("https://tidal.com/browse/video/12345")` continues to return VIDEO definite
2. **Spotify Preservation**: Verify all `open.spotify.com` URLs continue returning AUDIO definite
3. **Video Extension Preservation**: Verify URLs ending in `.mp4`, `.webm`, etc. continue returning VIDEO definite
4. **Mode Override Preservation**: Verify `mode="video"` always returns VIDEO definite regardless of URL
5. **Plain Text Preservation**: Verify non-URL search queries continue returning AUDIO default

### Unit Tests

- Test `classify()` with expanded YouTube hostname variants (nocookie, subdomains)
- Test `classify()` with unrecognized URLs → verify AUDIO default
- Test all existing rules continue working (Tidal video, Spotify, SoundCloud, video ext)
- Test `EGLHeadlessContext.create()` code path uses `eglGetPlatformDisplay` directly (mock ctypes)

### Property-Based Tests

- Generate random YouTube-domain URLs (random subdomains + paths) and verify all classify as AUDIO
- Generate random non-YouTube URLs without video extensions and verify classify as AUDIO (default)
- Generate random Spotify/Tidal/SoundCloud URLs and verify classification matches original behavior
- Generate random video-extension URLs and verify they still classify as VIDEO

### Integration Tests

- Build Docker image from rebased Dockerfile and verify FFmpeg 9 with `ffmpeg -hwaccels | grep qsv`
- Run EGL verification script inside container against `/dev/dri/renderD133`
- Test full classify → PlaybackRouter flow with YouTube nocookie URLs
- Verify `render_lavalink_config.py` still runs successfully in the rebased image

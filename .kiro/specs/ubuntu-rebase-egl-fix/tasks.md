# Implementation Plan

## Overview

Bugfix implementation for two issues: (1) EGL GBM platform dispatch failure on Debian trixie — rebase Dockerfile to Ubuntu 26.04 and simplify egl_context.py to use `eglGetPlatformDisplay` directly; (2) PlaybackRouter video classification — expand YouTube hostname matching in Rule 9 and flip Rule 10 default from VIDEO to AUDIO.

## Tasks

- [x] 1. Write bug condition exploration test
  - **Property 1: Bug Condition** - YouTube Hostname Fallthrough & Rule 10 VIDEO Default
  - **CRITICAL**: This test MUST FAIL on unfixed code — failure confirms the bug exists
  - **DO NOT attempt to fix the test or the code when it fails**
  - **NOTE**: This test encodes the expected behavior — it will validate the fix when it passes after implementation
  - **GOAL**: Surface counterexamples that demonstrate the classifier bug exists
  - **Scoped PBT Approach**: Scope the property to concrete failing cases:
    - `classify("https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ")` — hostname not in Rule 9 set
    - `classify("https://youtube-nocookie.com/watch?v=abc123")` — hostname not in Rule 9 set
    - `classify("https://gaming.youtube.com/watch?v=abc123")` — subdomain falls through Rule 9
    - `classify("https://consent.youtube.com/redirect?q=...")` — subdomain falls through Rule 9
    - `classify("https://example.com/podcast/episode-5")` — unrecognized URL, no video ext
    - `classify("https://somepodcast.fm/episode/123")` — unrecognized URL defaults to VIDEO
  - Use Hypothesis to generate YouTube-domain URLs with arbitrary subdomains (e.g., `st.from_regex(r"[a-z]+", fullmatch=True)` + `.youtube.com`) and assert `classify()` returns `ContentType.AUDIO`
  - Also generate arbitrary non-video-extension URLs and assert `classify()` returns `ContentType.AUDIO` (not VIDEO)
  - The bug condition from design: `isBugCondition(input)` where hostname is a YouTube domain NOT in the current Rule 9 set, OR any unrecognized URL without video extension
  - Expected behavior: all such inputs should return `ContentType.AUDIO` with confidence `"default"`
  - Run test on UNFIXED code
  - **EXPECTED OUTCOME**: Test FAILS (this is correct — it proves the bug exists)
  - Document counterexamples found (e.g., `classify("https://youtube-nocookie.com/embed/abc")` returns `ClassificationResult(VIDEO, "unknown_url", "default")` instead of AUDIO)
  - Mark task complete when test is written, run, and failure is documented
  - _Requirements: 1.3, 1.4, 1.5, 2.3, 2.5_

- [x] 2. Write preservation property tests (BEFORE implementing fix)
  - **Property 2: Preservation** - Existing Classification Rules Unchanged
  - **IMPORTANT**: Follow observation-first methodology
  - **Observe on UNFIXED code:**
    - `classify("https://open.spotify.com/track/abc")` → `ClassificationResult(AUDIO, "spotify", "definite")`
    - `classify("https://tidal.com/browse/video/12345")` → `ClassificationResult(VIDEO, "tidal_video", "definite")`
    - `classify("https://tidal.com/browse/track/67890")` → `ClassificationResult(AUDIO, "tidal", "definite")`
    - `classify("https://soundcloud.com/artist/track")` → `ClassificationResult(AUDIO, "soundcloud", "definite")`
    - `classify("https://music.youtube.com/watch?v=abc")` → `ClassificationResult(AUDIO, "youtube_music", "definite")`
    - `classify("https://youtube.com/watch?v=abc")` → `ClassificationResult(AUDIO, "youtube", "default")`
    - `classify("https://example.com/video.mp4")` → `ClassificationResult(VIDEO, "direct_video", "definite")`
    - `classify("some search query", mode="video")` → `ClassificationResult(VIDEO, "mode_override", "definite")`
    - `classify("some search query", mode="audio")` → `ClassificationResult(AUDIO, "mode_override", "definite")`
    - `classify("just a text search")` → `ClassificationResult(AUDIO, "search", "default")`
  - **Write property-based tests with Hypothesis:**
    - Property: for all Spotify URLs (`open.spotify.com/*`), classify returns `AUDIO` definite with source_hint `"spotify"`
    - Property: for all Tidal video URLs (`tidal.com/(browse/)?video/\d+`), classify returns `VIDEO` definite with source_hint `"tidal_video"`
    - Property: for all Tidal non-video URLs, classify returns `AUDIO` definite with source_hint `"tidal"`
    - Property: for all SoundCloud URLs (`soundcloud.com/*`), classify returns `AUDIO` definite with source_hint `"soundcloud"`
    - Property: for all URLs ending in video extensions (`.mp4`, `.webm`, `.mkv`, `.avi`, `.mov`, `.m4v`), classify returns `VIDEO` definite with source_hint `"direct_video"`
    - Property: for any query with `mode="video"`, classify returns `VIDEO` definite with source_hint `"mode_override"`
    - Property: for any query with `mode="audio"`, classify returns `AUDIO` definite with source_hint `"mode_override"`
    - Property: for all plain text queries (no URL scheme), classify returns `AUDIO` default with source_hint `"search"`
    - Property: for `music.youtube.com` URLs, classify returns `AUDIO` definite with source_hint `"youtube_music"`
  - Run tests on UNFIXED code
  - **EXPECTED OUTCOME**: Tests PASS (this confirms baseline behavior to preserve)
  - Mark task complete when tests are written, run, and passing on unfixed code
  - _Requirements: 3.3, 3.4, 3.5, 3.8, 3.9_

- [x] 3. Fix for PlaybackRouter Video Classification (classifier.py)

  - [x] 3.1 Expand Rule 9 YouTube hostname matching
    - Replace the static hostname tuple with a helper function `_is_youtube_domain(hostname: str) -> bool`
    - Match exact hosts: `youtube.com`, `www.youtube.com`, `m.youtube.com`, `youtu.be`, `www.youtu.be`, `youtube-nocookie.com`, `www.youtube-nocookie.com`
    - Match suffix: any hostname ending in `.youtube.com` (catches `gaming.youtube.com`, `consent.youtube.com`, future subdomains)
    - Note: `music.youtube.com` is already handled by Rule 3 (higher priority) so it won't reach Rule 9
    - Keep Rule 9 below Rule 8 (video extension detection has higher priority)
    - _Bug_Condition: isBugCondition(input) where hostname is a YouTube domain NOT in the current Rule 9 set_
    - _Expected_Behavior: classify returns ContentType.AUDIO with confidence "default" and source_hint "youtube"_
    - _Preservation: Rules 1–8 unchanged, Rule 9 is a superset of previous behavior_
    - _Requirements: 1.3, 2.3, 2.6_

  - [x] 3.2 Flip Rule 10 default from VIDEO to AUDIO
    - Change `content_type=ContentType.VIDEO` to `content_type=ContentType.AUDIO` in Rule 10
    - Keep `source_hint="unknown_url"` and `confidence="default"`
    - Update the Rule 10 docstring to explain: unrecognized URLs default to AUDIO since `/play`'s primary intent is audio playback; explicit `mode:video` exists for video requests
    - _Bug_Condition: isBugCondition(input) where URL has scheme, no known audio-domain match, no video extension_
    - _Expected_Behavior: classify returns ContentType.AUDIO with confidence "default" and source_hint "unknown_url"_
    - _Preservation: Video extension detection (Rule 8) still returns VIDEO; explicit mode overrides still work_
    - _Requirements: 1.5, 2.5_

  - [x] 3.3 Verify bug condition exploration test now passes
    - **Property 1: Expected Behavior** - YouTube Hostname Fallthrough & Rule 10 AUDIO Default
    - **IMPORTANT**: Re-run the SAME test from task 1 — do NOT write a new test
    - The test from task 1 encodes the expected behavior (YouTube domains → AUDIO, unrecognized URLs → AUDIO)
    - When this test passes, it confirms the expected behavior is satisfied
    - Run bug condition exploration test from step 1
    - **EXPECTED OUTCOME**: Test PASSES (confirms bug is fixed)
    - _Requirements: 2.3, 2.5_

  - [x] 3.4 Verify preservation tests still pass
    - **Property 2: Preservation** - Existing Classification Rules Unchanged
    - **IMPORTANT**: Re-run the SAME tests from task 2 — do NOT write new tests
    - Run preservation property tests from step 2
    - **EXPECTED OUTCOME**: Tests PASS (confirms no regressions)
    - Confirm Spotify, Tidal video, Tidal audio, SoundCloud, video extensions, mode overrides, plain text, and music.youtube.com all still classify correctly
    - _Requirements: 3.3, 3.4, 3.5, 3.8, 3.9_

- [x] 4. Fix for EGL GBM Platform Dispatch (egl_context.py)

  - [x] 4.1 Simplify EGL initialization to use eglGetPlatformDisplay directly
    - Remove the `eglGetProcAddress(b"eglGetPlatformDisplayEXT")` workaround block (lines that fetch function pointer via eglGetProcAddress and cast to CFUNCTYPE)
    - Replace with direct `self._egl.eglGetPlatformDisplay(EGL_PLATFORM_GBM_KHR, gbm_device, None)` call
    - Set proper `argtypes` and `restype` on `self._egl.eglGetPlatformDisplay`: `argtypes = [ctypes.c_uint, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)]`, `restype = ctypes.c_void_p`
    - Remove the `__EGL_VENDOR_LIBRARY_FILENAMES` environment override (Ubuntu 26.04's libglvnd dispatch works without it)
    - Keep `MESA_LOADER_DRIVER_OVERRIDE=iris` (still needed for SR-IOV VF selection)
    - Update module docstring: reference standard `eglGetPlatformDisplay` on Ubuntu 26.04 with GBM platform, remove mention of `EGL_MESA_platform_surfaceless` and the EXT workaround
    - Remove `EGL_PLATFORM_SURFACELESS_MESA` constant (no longer used)
    - _Bug_Condition: eglGetPlatformDisplayEXT via eglGetProcAddress fails on Debian trixie libglvnd 1.7.0_
    - _Expected_Behavior: eglGetPlatformDisplay(EGL_PLATFORM_GBM_KHR, gbm_device, NULL) returns valid EGLDisplay on Ubuntu 26.04_
    - _Preservation: GBM device creation, eglInitialize, eglBindAPI, context creation, FBO creation all unchanged_
    - _Requirements: 1.1, 1.2, 2.1, 2.2_

  - [x] 4.2 Update error messages
    - Change error message from `"eglGetPlatformDisplayEXT failed (GBM)"` to `"eglGetPlatformDisplay failed (GBM)"`
    - Remove error for `eglGetProcAddress(eglGetPlatformDisplayEXT) returned NULL` (no longer applicable)
    - _Requirements: 2.1_

- [x] 5. Rebase bot/Dockerfile from python:3.11-slim to Ubuntu 26.04

  - [x] 5.1 Change base image and install Python
    - Replace `FROM python:3.11-slim` with `FROM ubuntu:26.04`
    - Add `DEBIAN_FRONTEND=noninteractive` env for non-interactive apt
    - Install Python and pip: `python3`, `python3-pip`, `python3-venv`, `python3-dev`
    - Create symlink `python3 → python` so `CMD ["python", "bot.py"]` works
    - _Requirements: 2.1, 3.7_

  - [x] 5.2 Install Mesa/EGL and Intel GPU packages
    - Install: `libegl1-mesa-dev`, `libgl1-mesa-dev`, `libgles2-mesa-dev`, `libgbm-dev`, `libdrm-dev`, `mesa-utils`
    - Ubuntu 26.04's Mesa packages include proper `/usr/share/glvnd/egl_vendor.d/50_mesa.json` for GBM platform dispatch
    - Install Intel VA-API/QSV: `intel-media-va-driver`, `libmfx-gen1.2`, `libvpl-dev`, `libva-dev`, `vainfo`
    - Install libprojectm: `libprojectm-dev`
    - _Requirements: 2.1, 2.2, 3.1, 3.2, 3.10_

  - [x] 5.3 Preserve FFmpeg 9 source build
    - Keep identical `./configure` flags: `--enable-libvpl`, `--enable-vaapi`, `--enable-libx264`, `--enable-libx265`, `--enable-libopus`, `--enable-libdav1d`, `--enable-openssl`, `--enable-gpl`, `--enable-nonfree`
    - Build deps: `build-essential`, `nasm`, `pkg-config`, `libx264-dev`, `libx265-dev`, `libopus-dev`, `libdav1d-dev`, `libssl-dev`, `wget`, `ca-certificates`
    - Verify ffmpeg 9 builds successfully with all codecs
    - _Requirements: 3.1_

  - [x] 5.4 Preserve pip install layers and application setup
    - Maintain three-stage requirements split (core → torch → AI) for registry push efficiency
    - Install yt-dlp for YouTube video downloading
    - Copy stickers directory for whiteboard feature
    - Ensure `CMD ["python", "bot.py"]` entry point works
    - Verify `render_lavalink_config.py` has access to Python + cryptography
    - _Requirements: 3.6, 3.7_

- [x] 6. Checkpoint — Ensure all tests pass
  - Run the full test suite: `pytest tests/ -v`
  - Confirm bug condition exploration test (Property 1) PASSES on fixed code
  - Confirm preservation property tests (Property 2) PASS on fixed code
  - Verify no regressions in existing test suite
  - If any test fails, diagnose and fix before marking complete
  - Ask the user if questions arise


## Task Dependency Graph

```json
{
  "waves": [
    ["1", "2"],
    ["3", "4"],
    ["5"],
    ["6"]
  ]
}
```

## Notes

- Bug 1 (EGL) verification requires building the Docker image and running inside the container against a render node — cannot be unit-tested outside the container
- Bug 2 (Classifier) is fully unit-testable with Hypothesis property-based tests
- Tasks 4 and 5 (EGL fix + Dockerfile rebase) are independent of task 3 (classifier fix) but both feed into the checkpoint
- The Dockerfile rebase (task 5) must be done AFTER the egl_context.py changes (task 4) since the simplified EGL code depends on Ubuntu 26.04's working libglvnd dispatch
- Property-based tests use the Hypothesis library (already in test dependencies)

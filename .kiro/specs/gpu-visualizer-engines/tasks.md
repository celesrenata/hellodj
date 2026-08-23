# Implementation Plan: GPU Visualizer Engines

## Overview

This plan implements GPU-accelerated visualizer engines for HelloDJ's Discord Activity. Four engine implementations (projectM, AudioVis, Fosfora, Varda) render audio-reactive visualizations on Intel Meteor Lake iGPUs via EGL headless contexts, delivering HLS streams to Activity viewers. The system includes per-engine configuration, preset save/load, factory presets, GPU resource scheduling (SR-IOV VF allocation), and graceful degradation.

## Prerequisites

- Existing `video/visualizer_manager.py` with VisualizerManager state machine (DISABLED, IDLE_NO_VIEWERS, STARTING, ACTIVE, SUSPENDING, ERROR)
- Existing `video/visualizer_engines/base.py` with VisualizerRenderer ABC and AudioFeatures/TrackMetadata data classes
- Existing `video/visualizer_engines/__init__.py` with ENGINE_REGISTRY and create_engine() factory
- Existing `video/audio_feature_bus.py` with subscriber-gated FFT/beat/BPM pipeline
- Existing `video/hls_transcode.py` with HLSTranscodePipeline (video source HLS)
- Existing `video/gpu_probe.py` with GPU detection
- Existing `guild_settings.py` with get/set_visualizer_engine, VALID_VISUALIZER_ENGINES
- Existing `cogs/visualizer.py` with `/visualizer engine` command
- Intel Meteor Lake iGPU with SR-IOV (8 VFs) on gremlin nodes
- Mesa iris driver for OpenGL 3.3 Core, EGL surfaceless platform support
- libprojectM 4.x available in container
- `hypothesis` for property-based tests

## Tasks

- [x] 1. Phase 1: Foundation (no GPU needed for testing)
  - [x] 1.1 Create GPU Resource Scheduler
    - Create `bot/video/gpu_scheduler.py` with `GPUResourceScheduler` class
    - Define `VFAllocation` dataclass (guild_id, engine_type, allocated_at)
    - Implement `allocate(guild_id, engine_type)` — assign VF slot, raise `GPUCapacityExceededError` if full
    - Implement `release(guild_id)` — free VF slot, no-op if not allocated
    - Implement `is_allocated(guild_id)` query and `active_sessions`/`available_vfs` properties
    - Set `MAX_VISUALIZER_VFS = 7` (reserve 1 of 8 VFs for video transcode pipeline)
    - Handle re-allocation for same guild (release old before allocating new)
    - Create `tests/test_gpu_scheduler.py` with full coverage
    - _Requirements: Req 4 (AC 1-5), Req 12 (AC 4)_

  - [x] 1.2 Create Engine Configuration Schema + Validation
    - Create `bot/video/visualizer_engines/config_schema.py`
    - Define `ENGINE_CONFIG_SCHEMAS` dict with settings for: projectm (preset_category, blend_duration, preset_duration, brightness, sensitivity), audiovis (style, color_scheme, fft_bins, glow_intensity, background_opacity), fosfora (particle_count, gravity, emission_style, color_mode, trail_length), varda (shader_name, color_intensity, speed, complexity), dvd (speed, hue_shift, icon_size)
    - Each setting has type (string/float/int/bool/choice), default, and constraints (min/max/choices)
    - Implement `validate_config_value(engine, setting, value)` — returns normalized value or raises ValueError
    - Implement `get_default_config(engine)` — returns dict of all defaults
    - Implement `get_setting_schema(engine, setting)` — returns schema dict for autocomplete
    - Create `tests/test_config_schema.py` with validation tests (valid inputs, type errors, range violations, unknown keys)
    - _Requirements: Req 14 (AC 1, 2, 5)_

  - [x]* 1.3 Write property test for config schema validation (Property 5)
    - **Property 5: Config Validation**
    - Generate random values within schema ranges → verify acceptance
    - Generate random values outside ranges → verify rejection with ValueError
    - Generate random unknown engine/setting names → verify rejection
    - Verify roundtrip: validate(default) == default for all engines
    - **Validates: Requirements 14.1, 14.2**

  - [x] 1.4 Create Factory Presets Data Model
    - Create `bot/video/visualizer_engines/factory_presets.py`
    - Define `FACTORY_PRESETS` dict with all presets: projectm (milkdrop-classic, psychedelic, chill, trippy, geometric, space, energy, minimal), audiovis (spectrum-bars, full-spectrum, waveform, waterfall, circular, vinyl, neon-city), fosfora (stardust, fireworks, aurora, vortex, rain, nebula, pulse), varda (fractal-zoom, tunnel, plasma, voronoi-pulse, raymarched-orbs, kaleidoscope, neon-grid, star-field, liquid-metal, cosmic-web), dvd (classic, fast, slow, no-hue)
    - Each preset: `{"engine": str, "config": dict, "factory": True}`
    - Implement `is_factory_preset(name)`, `get_factory_preset(name)`, `list_factory_presets(engine=None)`
    - Create `tests/test_factory_presets.py` — verify all preset configs validate against schema
    - _Requirements: Req 16 (AC 1-7)_

  - [x]* 1.5 Write property test for factory preset immutability (Property 6)
    - **Property 6: Factory Preset Immutability**
    - For all factory preset names → verify `is_factory_preset()` returns True
    - For all factory presets → verify config validates against ENGINE_CONFIG_SCHEMAS
    - For random non-factory names → verify `is_factory_preset()` returns False
    - **Validates: Requirements 16.1, 16.2**

  - [x] 1.6 Extend Guild Settings (visualizer_config + visualizer_presets)
    - Extend `bot/guild_settings.py` with:
    - `get_visualizer_config(guild_id, engine)` — returns merged defaults + stored overrides
    - `set_visualizer_config(guild_id, engine, setting, value)` — validates via schema, stores
    - `get_visualizer_presets(guild_id)` — returns dict of user-saved presets
    - `save_visualizer_preset(guild_id, name, preset_data)` — stores engine + config as named preset
    - `delete_visualizer_preset(guild_id, name)` — removes user preset; raises ValueError for factory presets
    - `load_visualizer_preset(guild_id, name)` — checks user presets first, then factory
    - Ensure existing settings (mode, visualizer_engine) are unaffected
    - Create `tests/test_guild_settings_visualizer.py` with CRUD tests
    - _Requirements: Req 14 (AC 1, 3), Req 15 (AC 1-7)_

  - [x] 1.7 Create Discord Command Interface (config/preset subcommands)
    - Extend `bot/cogs/visualizer.py` with:
    - `/visualizer config <engine> <setting> <value>` — validate and store; confirmation embed
    - `/visualizer settings [engine]` — display current config as embed
    - `/visualizer preset save <name>` — capture current engine + config as named preset
    - `/visualizer preset load <name>` — apply preset (engine + config); autocomplete preset names
    - `/visualizer preset list` — show all presets (factory + user) with engine type
    - `/visualizer preset delete <name>` — remove user preset; error on factory preset
    - `/visualizer projectm list-categories` — list available category folders with preset counts
    - Add autocomplete for engine names, setting names, preset names, category names
    - Hot-reload config when engine is active (where technically possible)
    - Create `tests/test_visualizer_cog_commands.py` with command validation tests
    - _Requirements: Req 14 (AC 1-5), Req 15 (AC 1-7), Req 17 (AC 4, 5)_

- [x] 2. Phase 2: EGL + Pipeline (needs GPU for integration test)
  - [x] 2.1 Create EGL Headless Context Module
    - Create `bot/video/visualizer_engines/egl_context.py`
    - Implement `EGLHeadlessContext` class with ctypes bindings to libEGL.so.1 and libGL.so.1
    - `create(width=1280, height=720)` — EGL display (surfaceless platform), OpenGL 3.3 Core context, offscreen FBO with RGBA8 renderbuffer
    - `make_current()` — bind context (surfaceless, no surface)
    - `read_pixels()` — glReadPixels returns exactly 3,686,400 bytes (1280×720×4 RGBA)
    - `destroy()` — release FBO, renderbuffer, EGL context, EGL display within 500ms
    - `is_valid` property for state checking
    - Define `EGLContextError` exception for all failure modes
    - Use `EGL_MESA_platform_surfaceless` (no X11/Wayland dependency)
    - Create `tests/test_egl_context.py` — mocked ctypes for CI; docstring for GPU integration test
    - _Requirements: Req 2 (AC 1-5)_

  - [x]* 2.2 Write property test for frame size consistency (Property 3)
    - **Property 3: Frame Size Consistency**
    - Mock EGLHeadlessContext to produce frames → verify every frame is exactly 3,686,400 bytes
    - Generate random width/height combinations → verify `width * height * 4` formula
    - **Validates: Requirements 3.1, 13.1**

  - [x] 2.3 Create GPU Engine Base Class
    - Create `bot/video/visualizer_engines/gpu_engine_base.py`
    - `GPUEngineBase(VisualizerRenderer)` — shared base for all server-rendered GPU engines
    - `activate()` — creates EGLHeadlessContext, calls `_on_gl_ready()` subclass hook
    - `suspend()` — destroys EGL context (zero GPU while suspended per Req 12 AC 4)
    - `resume()` — re-creates context via `activate()`
    - `stop()` — destroys context and all resources
    - `render_frames()` — async generator yielding RGBA frames at 30fps with sleep timing
    - `on_audio_features(features)` — atomic reference swap (non-blocking, ~47fps safe)
    - Properties: `is_client_side=False`, `consumes_gpu_while_suspended=False`
    - Subclass hooks: `_on_gl_ready(metadata)`, `_render_gl_frame(features)`
    - Add `on_audio_features()` method to VisualizerRenderer ABC in `base.py` (default no-op)
    - Create `tests/test_gpu_engine_base.py` — tests with mocked EGL context
    - _Requirements: Req 2 (AC 1-5), Req 11 (AC 4), Req 12 (AC 4)_

  - [x]* 2.4 Write property test for zero GPU when suspended (Property 2)
    - **Property 2: Zero GPU When No Viewers**
    - Generate random activate/suspend sequences → verify EGL context is None after suspend
    - Verify `consumes_gpu_while_suspended` is always False for GPUEngineBase subclasses
    - **Validates: Requirements 12.1, 12.3, 12.4**

  - [x] 2.5 Extend HLS Pipeline with start_visualizer()
    - Extend `bot/video/hls_transcode.py` with:
    - `start_visualizer()` — spawn ffmpeg accepting rawvideo RGBA on stdin, encoding via h264_qsv to HLS
    - `_build_visualizer_ffmpeg_args()` — construct command: `-f rawvideo -pixel_format rgba -video_size 1280x720 -framerate 30 -i pipe:0` → `format=nv12,hwupload=extra_hw_frames=64` → `h264_qsv` preset fast → `-f hls -hls_time 2 -hls_list_size 10 -hls_flags delete_segments+independent_segments`
    - `write_frame(data: bytes)` — write to ffmpeg stdin pipe
    - Output: `/tmp/hellodj_hls/{guild_id}/viz/playlist.m3u8` with segment files
    - Signal readiness when first segment appears (segment watcher)
    - `-r 30` ensures frame duplication if engine is slower than 30fps
    - Flush and finalize on stop
    - Create `tests/test_hls_visualizer_pipeline.py` — test arg construction, mock subprocess
    - _Requirements: Req 3 (AC 1-5), Req 13 (AC 1-5)_

  - [x] 2.6 Wire VisualizerRegistry + GPU Scheduler into VisualizerManager
    - Modify `bot/video/visualizer_manager.py`:
    - `_start_engine()` calls `gpu_scheduler.allocate(guild_id, engine_type)` before EGL context creation
    - `_stop_engine()` and `_execute_suspension()` call `gpu_scheduler.release(guild_id)`
    - `GPUCapacityExceededError` → remain in IDLE_NO_VIEWERS with "GPU capacity exceeded" warning log
    - AudioFeatureBus subscribe on engine activate, unsubscribe on suspend/stop (within 100ms)
    - `render_frames()` output piped to `hls_pipeline.write_frame()` in render loop task
    - Signal HLS readiness to WebSocket hub when first segment appears
    - Modify `bot/video/visualizer_engines/__init__.py`:
    - Remove `vgalizer` from ENGINE_REGISTRY
    - Add `_RANDOM_POOL_ENGINES = ["projectm", "audiovis", "fosfora", "varda"]`
    - Modify `bot/guild_settings.py` — remove `"vgalizer"` from VALID_VISUALIZER_ENGINES; treat legacy vgalizer configs as default
    - Create `tests/test_visualizer_manager_gpu.py` — integration tests with mocked GPU components
    - _Requirements: Req 1 (AC 1-3), Req 4 (AC 2-4), Req 10 (AC 1-2), Req 12 (AC 1-4)_

  - [x]* 2.7 Write property test for VF allocation invariant (Property 1)
    - **Property 1: VF Allocation Invariant**
    - Generate random allocate/release sequences → verify `active_sessions <= MAX_VISUALIZER_VFS` always holds
    - Generate allocations beyond capacity → verify GPUCapacityExceededError raised and no over-allocation
    - **Validates: Requirements 4.1, 4.2, 4.3, 4.5**

- [x] 3. Phase 3: Engine Implementations (each independent)
  - [x] 3.1 Implement Varda Engine (simplest — single fullscreen fragment shader)
    - Rewrite `bot/video/visualizer_engines/varda.py` as `VardaEngine(GPUEngineBase)`
    - `_on_gl_ready()` — compile vertex shader (fullscreen triangle) + fragment shader from file
    - If shader compilation fails → fallback to `plasma.glsl`, log compilation error
    - `_render_gl_frame()` — upload audio as 512×2 texture (row 0: waveform, row 1: FFT), set uniforms (iTime, iResolution, iChannel0, iBeat, iBPM, iBandEnergy[7]), draw fullscreen triangle
    - Beat pulse decays from 1.0 → 0.0 over ~300ms
    - `on_track_change()` — select new shader from pool, crossfade over 2 seconds
    - Create `bot/video/visualizer_engines/shaders/varda_vertex.glsl` — fullscreen triangle
    - Create `bot/video/visualizer_engines/shaders/plasma.glsl` — default fallback shader
    - Configurable: shader_name, color_intensity, speed, complexity
    - Create `tests/test_varda_engine.py` — mocked GL tests (shader load, fallback, uniform setting)
    - _Requirements: Req 8 (AC 1-6)_

  - [x] 3.2 Implement AudioVis Engine (spectrum bars/waveform shaders)
    - Rewrite `bot/video/visualizer_engines/audiovis.py` as `AudioVisEngine(GPUEngineBase)`
    - `_on_gl_ready()` — compile vertex + per-style fragment shader (bars, waveform, waterfall, circular)
    - `_render_gl_frame()` — upload FFT as 1D texture, set uniforms (iTime, iResolution, iBeat, iBPM, iBandEnergy, iFFT, iFFTBins), draw fullscreen quad
    - Beat pulse: brightness boost + bar expansion decaying over 200ms
    - Render track title/artist as text overlay into frame
    - Create shader files: `audiovis_vert.glsl`, `audiovis_bars.glsl`, `audiovis_waveform.glsl`, `audiovis_waterfall.glsl`, `audiovis_circular.glsl` in `shaders/` directory
    - Configurable: style, color_scheme, fft_bins, glow_intensity, background_opacity
    - Create `tests/test_audiovis_engine.py` — mocked GL tests
    - _Requirements: Req 6 (AC 1-5)_

  - [x] 3.3 Implement Fosfora Engine (particle system with transform feedback)
    - Rewrite `bot/video/visualizer_engines/fosfora.py` as `FosforaEngine(GPUEngineBase)`
    - `_on_gl_ready()` — compile transform feedback program + render program, allocate ping-pong VBOs (44 bytes/particle × up to 10,000)
    - `_render_gl_frame()` — physics pass (transform feedback: gravity, drag, lifetime decay), swap buffers, render pass (additive blending point sprites, color cycling by BPM)
    - Beat detection → burst emission from center, velocity ∝ intensity
    - Band energy drives continuous emission rate
    - `suspend()` — release all GPU particle buffers and shader programs
    - Create shader files: `fosfora_physics.vert`, `fosfora_render.vert`, `fosfora_render.frag` in `shaders/`
    - Configurable: particle_count, gravity, emission_style, color_mode, trail_length
    - Create `tests/test_fosfora_engine.py` — mocked GL tests
    - _Requirements: Req 7 (AC 1-6)_

  - [x] 3.4 Implement projectM Engine (libprojectM binding + preset loading)
    - Rewrite `bot/video/visualizer_engines/projectm.py` as `ProjectMEngine(GPUEngineBase)`
    - `_on_gl_ready()` — load libprojectM-4.so via ctypes, call `projectm_create()`, set window size (1280×720), configure preset/blend durations, beat sensitivity, shuffle
    - `_render_gl_frame()` — feed FFT data via `projectm_pcm_add_float()`, call `projectm_opengl_render_frame()`
    - `on_track_change()` — `projectm_select_random_preset()` with smooth 3s blend
    - `_resolve_preset_path()` — resolve to category subfolder when preset_category != "all"
    - `suspend()` — destroy GL context (frees GPU memory); `resume()` recreates
    - Configurable: preset_category, blend_duration, preset_duration, brightness, sensitivity
    - Create `tests/test_projectm_engine.py` — mocked libprojectM ctypes tests
    - _Requirements: Req 5 (AC 1-6), Req 17 (AC 1-3)_

- [x] 4. Phase 4: Integration
  - [x] 4.1 Wire Audio Data Flow (voice_recv → AudioFeatureBus → Engine)
    - Modify `bot/video/visualizer_manager.py`:
    - `_start_engine()` subscribes engine's `on_audio_features` callback to AudioFeatureBus within 100ms
    - `_stop_engine()`/`_execute_suspension()` unsubscribes within 100ms
    - Verify AudioFeatureBus auto-starts processing loop on first subscriber (within 100ms)
    - Verify AudioFeatures dispatched include: FFT (512 bins), beat flag, BPM, 7-band energy
    - Verify queue-full condition drops oldest frame (never blocks voice_recv)
    - Create `tests/test_audio_data_flow.py` — integration test: mock PCM → bus → engine receives features
    - _Requirements: Req 1 (AC 1-5)_

  - [x] 4.2 End-to-End Integration (engine start → HLS → frontend playback)
    - Wire complete path in `bot/video/visualizer_manager.py`:
    - Implement `_render_loop()` asyncio task reading from `render_frames()` and writing to HLS pipeline stdin
    - Signal readiness to WebSocket hub when first HLS segment appears on disk
    - Frontend begins HLS.js playback on readiness signal
    - Validate startup within 2 seconds of first viewer connecting
    - Verify `/activity/stream/{gid}/viz/playlist.m3u8` serves valid HLS playlist
    - Verify last viewer disconnect → 10s debounce → engine suspension → GPU release
    - Create `tests/test_e2e_visualizer_gpu.py` — integration test with mocked ffmpeg
    - _Requirements: Req 3 (AC 1-5), Req 12 (AC 2-3), Req 13 (AC 1-5)_

  - [x] 4.3 Bundled Presets + Docker Image Update
    - Update `bot/Dockerfile`:
    - Add `libegl1-mesa-dev`, `libgl1-mesa-dev`, `libgles2-mesa-dev`, `libprojectm-dev` packages
    - COPY `bot/data/presets/projectm/` with category subfolders (Abstract, Fluid Motion, Geometric, Simple, Space, Trippy, Classic, Energy) containing ≥50 .milk presets
    - COPY `bot/data/presets/varda/` with all 10 GLSL shader files
    - COPY `bot/video/visualizer_engines/shaders/` with all shader files
    - Create all 10 Varda GLSL shaders following Shadertoy uniform convention (iTime, iResolution, iChannel0, iBeat, iBPM, iBandEnergy)
    - Verify Docker image builds, shaders compile against OpenGL 3.3 Core
    - _Requirements: Req 5 (AC 6), Req 8 (AC 5), Req 16 (AC 3-6)_

  - [x] 4.4 Implement Engine Feasibility Gate + Random Pool
    - Modify `bot/video/visualizer_engines/__init__.py`:
    - Implement `get_available_engines()` that consults GPUProbe — if no GPU, only "dvd" available
    - Random pool: `_RANDOM_POOL_ENGINES = ["projectm", "audiovis", "fosfora", "varda"]`
    - Modify `bot/video/visualizer_manager.py`:
    - `_select_random_engine()` — no consecutive repeat; try next on failure; all fail → DVD fallback
    - When GPUProbe detects no GPU, disable all server-rendered engines
    - Modify `bot/cogs/visualizer.py` — autocomplete shows only engines from `get_available_engines()`
    - Modify `bot/guild_settings.py` — treat `vgalizer` as default engine in `get_visualizer_engine()`
    - Create `tests/test_random_pool.py` — no-repeat, fallback chain, GPU-unavailable tests
    - _Requirements: Req 9 (AC 1-4), Req 10 (AC 1-4)_

  - [x]* 4.5 Write property test for engine registry consistency (Property 7)
    - **Property 7: Engine Registry Consistency**
    - Verify `VALID_VISUALIZER_ENGINES ⊇ ENGINE_REGISTRY.keys() ∪ {"random", "off"}`
    - Verify `get_available_engines()` ⊆ VALID_VISUALIZER_ENGINES
    - Verify all random pool engines are in ENGINE_REGISTRY
    - **Validates: Requirements 10.1, 10.3**

- [x] 5. Phase 5: Polish
  - [x] 5.1 Implement Graceful Degradation + Error Recovery
    - Modify `bot/video/visualizer_manager.py`:
    - Implement `_handle_render_error()` — stop server resources → release GPU VF → ERROR state → activate DVD fallback
    - Wrap `_render_loop()` in exception handler; exceptions never propagate to bot main event loop
    - GPU device loss detected within 5s (broken pipe / GL error / no new segments)
    - Modify `bot/video/visualizer_engines/gpu_engine_base.py` — exception isolation in render loop
    - Modify `bot/video/ws_hub.py` — emit error notification to viewers on ERROR state
    - Varda shader compile failure → fallback shader (not ERROR state)
    - Create `tests/test_graceful_degradation.py` — tests for GPU error, shader fail, device loss, capacity exceeded
    - _Requirements: Req 11 (AC 1-5)_

  - [x]* 5.2 Write property test for graceful degradation (Property 4)
    - **Property 4: Graceful Degradation**
    - Generate random engine failure scenarios → verify DVD fallback always activates
    - Verify bot main event loop never receives unhandled exception from render loop
    - Verify ERROR state → viewer notification sent via WebSocket
    - **Validates: Requirements 11.1, 11.3, 11.4, 11.5**

  - [x] 5.3 Implement Viewer-Driven Demand Rendering (debounce, suspend/resume)
    - Modify `bot/video/visualizer_manager.py`:
    - Zero viewers → no GPU context, no render loop
    - First viewer + audio playing → engine starts within 2 seconds
    - Last viewer disconnect → 10-second debounce timer
    - Reconnect within 10s → cancel debounce (no suspension)
    - After 10s → engine suspended: EGL context destroyed, GPU VF released
    - Modify `bot/video/ws_hub.py` — track viewer count per guild; emit count changes to VisualizerManager
    - Create `tests/test_demand_rendering.py` — debounce timing, zero-GPU verification, transient disconnect tests
    - _Requirements: Req 12 (AC 1-4)_

  - [x] 5.4 Implement projectM Preset Category Management
    - Modify `bot/video/visualizer_engines/projectm.py`:
    - `_resolve_preset_path()` with category validation against actual directory contents
    - `get_available_categories()` class method returning category names + preset counts
    - Category change takes effect on next preset transition
    - Modify `bot/cogs/visualizer.py`:
    - `/visualizer projectm list-categories` reads directory, displays as embed with counts
    - `/visualizer config projectm preset_category` autocomplete suggests category folder names
    - Invalid category → error listing available categories
    - Create `tests/test_projectm_categories.py`
    - _Requirements: Req 17 (AC 1-5)_

- [x] 6. Checkpoint — All phases complete
  - Ensure all tests pass, ask the user if questions arise.
  - Verify: all 4 GPU engines render frames, HLS pipeline delivers to frontend, demand rendering conserves GPU, graceful degradation handles all failure modes, config/preset commands work end-to-end, Docker image builds with all dependencies

## Notes

- Tasks marked with `*` are property-based tests (optional, can be skipped for faster MVP)
- Phase 1 requires zero GPU hardware — all logic is pure Python, testable in CI
- Phase 2 needs GPU for integration tests but unit tests use mocked ctypes
- Phase 3 engines are independent of each other — can be implemented in parallel
- Phase 4 wires everything together — requires at least one engine from Phase 3
- Phase 5 adds robustness and polish — can be deployed incrementally
- The existing VisualizerManager state machine and AudioFeatureBus are unchanged; this spec extends them with GPU engine support
- `vgalizer` is removed (no feasible GPU implementation) — legacy configs treated as default
- All rendering at 720p30 via EGL surfaceless (no display server, no X11)
- ctypes used for EGL/GL/libprojectM bindings (stdlib, no extra dependencies)
- GPU VF scheduler is single-node (tracks allocations on current pod's node only)

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "1.2"] },
    { "id": 1, "tasks": ["1.3", "1.4", "2.1"] },
    { "id": 2, "tasks": ["1.5", "1.6", "2.3"] },
    { "id": 3, "tasks": ["1.7", "2.2", "2.4", "2.5"] },
    { "id": 4, "tasks": ["2.6", "2.7"] },
    { "id": 5, "tasks": ["3.1", "3.2", "3.3", "3.4"] },
    { "id": 6, "tasks": ["4.1", "4.2", "4.3"] },
    { "id": 7, "tasks": ["4.4", "4.5"] },
    { "id": 8, "tasks": ["5.1", "5.3"] },
    { "id": 9, "tasks": ["5.2", "5.4"] },
    { "id": 10, "tasks": ["6"] }
  ]
}
```

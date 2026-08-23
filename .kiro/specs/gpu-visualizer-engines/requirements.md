# Requirements Document

## Introduction

This specification defines the GPU-accelerated visualizer engine system for HelloDJ's Discord Activity. The system renders audio-reactive visualizations driven by real-time audio features (FFT, beat detection, BPM) and delivers them as HLS streams to Activity viewers. Each engine runs on Intel Meteor Lake iGPUs (QSV/VA-API) in the cluster, with multi-guild isolation via SR-IOV virtual functions.

The existing infrastructure provides: an `AudioFeatureBus` producing ~47fps audio analysis frames, a `VisualizerManager` state machine managing per-guild lifecycle, a `VisualizerRenderer` ABC defining the engine interface, an HLS transcode pipeline with QSV encoding, and a working DVD screensaver (client-side reference). This spec covers making the remaining engines operational with GPU acceleration.

## Glossary

- **Engine**: A visualizer implementation conforming to the VisualizerRenderer interface that produces visual output reacting to audio features.
- **AudioFeatureBus**: The existing subscriber-gated pipeline that performs FFT, beat detection, BPM estimation, and 7-band energy analysis on PCM audio at ~47fps.
- **VisualizerManager**: The per-guild state machine that coordinates engine lifecycle, viewer demand, and HLS streaming.
- **HLS_Pipeline**: The existing ffmpeg QSV transcode pipeline that encodes raw video frames into HLS segments for Activity viewers.
- **QSV**: Intel Quick Sync Video — hardware-accelerated video encode/decode via the Intel Media SDK/oneVPL.
- **VA-API**: Video Acceleration API — Linux-standard interface for hardware video processing on Intel GPUs.
- **SR-IOV_VF**: Single Root I/O Virtualization Virtual Function — one of 8 virtual GPU slices exposed by Intel Meteor Lake iGPU SR-IOV, enabling multi-tenant GPU isolation.
- **EGL_Context**: An EGL-based OpenGL/OpenGL ES rendering context created without a display server, enabling headless GPU rendering in containers.
- **projectM**: Open-source library (libprojectM) implementing Milkdrop-compatible audio visualization presets using OpenGL shaders.
- **Render_Node**: A DRM render node device (`/dev/dri/renderD*`) providing GPU access without requiring display privileges.
- **Frame_Pipe**: The mechanism by which a server-rendered engine delivers raw RGBA frames (1280x720x4 bytes) to the HLS_Pipeline for encoding.
- **GPU_Scheduler**: The component responsible for allocating SR-IOV_VF resources across concurrent guild visualizer sessions.

## Requirements

### Requirement 1: Audio Data Flow Integration

**User Story:** As the system, I want to wire the AudioFeatureBus output into active visualizer engines, so that engines receive real-time audio analysis data for reactive rendering.

#### Acceptance Criteria

1. WHEN a server-rendered Engine activates, THE VisualizerManager SHALL subscribe the Engine's audio callback to the AudioFeatureBus within 100ms.
2. WHEN a server-rendered Engine is suspended or stopped, THE VisualizerManager SHALL unsubscribe the Engine's audio callback from the AudioFeatureBus within 100ms.
3. WHILE an Engine is active with zero AudioFeatureBus subscribers aside from itself, THE AudioFeatureBus SHALL start its processing loop within 100ms of the subscription.
4. WHEN the AudioFeatureBus dispatches an AudioFeatures frame, THE subscribed Engine SHALL receive FFT (512 bins), beat flag, BPM estimate, and 7-band energy values.
5. IF the AudioFeatureBus queue is full, THEN THE AudioFeatureBus SHALL drop the oldest frame rather than blocking the voice_recv thread.

### Requirement 2: Headless GPU Rendering Context

**User Story:** As a developer, I want engines to create EGL-based headless OpenGL contexts on the Intel iGPU, so that GPU-accelerated rendering works in the containerized Kubernetes environment without a display server.

#### Acceptance Criteria

1. WHEN a server-rendered Engine initializes, THE Engine SHALL create an EGL_Context using the available Render_Node without requiring X11 or Wayland.
2. THE EGL_Context SHALL support OpenGL 3.3 Core or OpenGL ES 3.1 for shader-based rendering.
3. WHEN the Engine renders a frame, THE Engine SHALL draw into an offscreen framebuffer (FBO) at 1280x720 resolution and read pixels back as RGBA bytes.
4. IF no Render_Node is available at initialization time, THEN THE Engine SHALL raise an initialization error and THE VisualizerManager SHALL transition to the ERROR state.
5. WHEN the Engine is stopped, THE Engine SHALL destroy the EGL_Context and release the associated GPU memory within 500ms.

### Requirement 3: Frame Pipeline to HLS

**User Story:** As a viewer watching the Activity, I want the visualizer output to appear as a smooth HLS video stream, so that I see audio-reactive visuals in real-time.

#### Acceptance Criteria

1. WHEN a server-rendered Engine is active, THE VisualizerManager SHALL pipe raw RGBA frames from the Engine's render_frames() iterator into the HLS_Pipeline's ffmpeg stdin.
2. THE HLS_Pipeline SHALL encode frames using h264_qsv at 720p, 30fps, with 2-second HLS segments written to the tmpfs volume.
3. WHEN the first HLS segment is written to disk, THE VisualizerManager SHALL signal readiness to the WebSocket hub so Activity viewers can begin playback.
4. WHILE the Engine produces frames slower than 30fps, THE HLS_Pipeline SHALL duplicate the last frame to maintain constant output rate.
5. IF the Engine fails to produce a frame within 2 seconds, THEN THE VisualizerManager SHALL transition to the ERROR state and notify connected viewers.

### Requirement 4: GPU Resource Management

**User Story:** As a cluster operator, I want multiple guilds to share the Intel iGPU safely, so that one guild's visualizer does not starve other guilds or the video transcoding pipeline.

#### Acceptance Criteria

1. THE GPU_Scheduler SHALL track active visualizer sessions and the SR-IOV_VF slots in use on the current node.
2. WHEN a new Engine session requests GPU resources, THE GPU_Scheduler SHALL allocate one SR-IOV_VF to that session if a free VF is available.
3. IF all SR-IOV_VF slots are occupied, THEN THE GPU_Scheduler SHALL reject the new Engine session and THE VisualizerManager SHALL remain in IDLE_NO_VIEWERS state with a "GPU capacity exceeded" log message.
4. WHEN an Engine session is stopped or suspended, THE GPU_Scheduler SHALL release its SR-IOV_VF allocation within 1 second.
5. THE GPU_Scheduler SHALL reserve at least 1 SR-IOV_VF for the video transcoding pipeline and not allocate it to visualizer sessions.

### Requirement 5: projectM Engine (Milkdrop Presets)

**User Story:** As a listener, I want to see Milkdrop-style audio visualizations (like classic Winamp visuals), so that I get a nostalgic, immersive music experience.

#### Acceptance Criteria

1. WHEN the projectM Engine is activated, THE Engine SHALL initialize libprojectM with an EGL_Context and load a preset from the configured preset directory.
2. WHEN AudioFeatures are received, THE projectM Engine SHALL feed PCM-equivalent audio data and beat information into the libprojectM render pipeline.
3. THE projectM Engine SHALL render preset frames at 30fps to the offscreen FBO and yield RGBA frame data via render_frames().
4. WHEN a track changes, THE projectM Engine SHALL transition to a new randomly-selected preset with a smooth blend over 3 seconds.
5. WHILE the projectM Engine is suspended, THE Engine SHALL destroy the OpenGL context to release GPU memory and recreate it on resume.
6. THE projectM Engine SHALL include at least 50 bundled Milkdrop-compatible presets covering diverse visual styles.

### Requirement 6: AudioVis Engine (Spectrum/Waveform Analyzer)

**User Story:** As a listener, I want to see detailed spectrum analysis and waveform visualizations rendered with GPU acceleration, so that I get smooth, high-fidelity audio displays.

#### Acceptance Criteria

1. WHEN the AudioVis Engine is activated, THE Engine SHALL initialize an EGL_Context and compile GLSL shaders for spectrum bar rendering, waveform display, and frequency waterfall.
2. WHEN AudioFeatures are received, THE AudioVis Engine SHALL update the spectrum bars (7-band or full 512-bin FFT) and waveform display in the shader uniforms.
3. THE AudioVis Engine SHALL render at 30fps using GPU-accelerated fragment shaders for smooth gradient fills, glow effects, and anti-aliased lines.
4. WHEN a beat is detected, THE AudioVis Engine SHALL trigger a visual pulse effect (brightness boost and bar expansion) that decays over 200ms.
5. THE AudioVis Engine SHALL display the current track title and artist as text rendered into the frame.

### Requirement 7: Fosfora Engine (GPU Particle System)

**User Story:** As a listener, I want to see fluid, organic particle animations reacting to the music, so that I experience an immersive audio-driven visual show.

#### Acceptance Criteria

1. WHEN the Fosfora Engine is activated, THE Engine SHALL initialize an EGL_Context and allocate GPU buffers for a particle system supporting up to 10000 particles.
2. WHEN AudioFeatures are received, THE Fosfora Engine SHALL update particle emission rate based on band_energy and apply velocity impulses on beat detection.
3. THE Fosfora Engine SHALL simulate particle physics (gravity, drag, lifetime decay) using GPU compute or vertex shader transform feedback at 30fps.
4. WHEN a beat is detected, THE Fosfora Engine SHALL emit a burst of particles from the center with velocities proportional to the beat intensity.
5. THE Fosfora Engine SHALL render particles with additive blending and colour cycling driven by the current BPM value.
6. WHILE the Fosfora Engine is suspended, THE Engine SHALL release all GPU particle buffers and shader programs.

### Requirement 8: Varda Engine (GPU Shader Visualizer)

**User Story:** As a listener, I want to see high-quality, shader-driven audio-reactive visuals (similar to Shadertoy), so that I experience creative generative art driven by the music.

#### Acceptance Criteria

1. WHEN the Varda Engine is activated, THE Engine SHALL initialize an EGL_Context and load a GLSL fragment shader from the configured shader directory.
2. WHEN AudioFeatures are received, THE Varda Engine SHALL pass FFT data, beat flag, BPM, band_energy, and elapsed time as shader uniforms.
3. THE Varda Engine SHALL render the full-screen fragment shader at 30fps to the offscreen FBO.
4. WHEN a track changes, THE Varda Engine SHALL select a new shader from the available pool (random or sequential) with a crossfade transition over 2 seconds.
5. THE Varda Engine SHALL include at least 10 bundled GLSL shader presets that produce distinct audio-reactive visual patterns.
6. IF a shader fails to compile, THEN THE Varda Engine SHALL fall back to a default shader and log the compilation error.

### Requirement 9: Random Engine Behaviour

**User Story:** As a listener, I want the "random" engine setting to rotate through available GPU engines each track, so that I get visual variety without manual configuration.

#### Acceptance Criteria

1. WHEN the random Engine mode is active and a new track begins, THE VisualizerManager SHALL select a different engine from the pool of implemented server-rendered engines.
2. THE VisualizerManager SHALL not repeat the same engine consecutively unless only one engine is available in the pool.
3. WHEN the selected engine fails to initialize, THE VisualizerManager SHALL attempt the next engine in the pool until one succeeds or all have been tried.
4. IF all engines in the random pool fail to initialize, THEN THE VisualizerManager SHALL fall back to the DVD client-side engine.

### Requirement 10: Engine Feasibility Gate

**User Story:** As a developer, I want engines that cannot be GPU-accelerated on Intel Meteor Lake to be removed from the valid engine list, so that users only see working options.

#### Acceptance Criteria

1. THE system SHALL remove "vgalizer" from VALID_VISUALIZER_ENGINES if no GPU-acceleratable implementation is feasible.
2. WHEN an engine is removed from VALID_VISUALIZER_ENGINES, THE guild_settings module SHALL treat existing guild configurations referencing that engine as equivalent to the default engine.
3. THE /visualizer command autocomplete SHALL only display engines that have a working implementation registered in ENGINE_REGISTRY.
4. WHEN the GPUProbe detects no available GPU at startup, THE VisualizerManager SHALL disable all server-rendered engines and only permit client-side engines (dvd).

### Requirement 11: Graceful Degradation

**User Story:** As a listener, I want the visualizer to gracefully handle GPU errors without crashing the bot, so that audio playback continues even if the visualizer fails.

#### Acceptance Criteria

1. IF an Engine encounters a GPU error during rendering, THEN THE VisualizerManager SHALL transition to the ERROR state, stop the HLS_Pipeline, and log the error.
2. WHEN the VisualizerManager enters the ERROR state, THE WebSocket hub SHALL notify connected viewers with an error message.
3. IF a server-rendered Engine fails, THEN THE VisualizerManager SHALL fall back to the DVD client-side engine for the current session.
4. THE Engine rendering loop SHALL NOT propagate unhandled exceptions to the bot's main event loop.
5. WHEN the GPU device becomes unavailable mid-session (device hot-unplug or driver crash), THE VisualizerManager SHALL detect the failure within 5 seconds and execute graceful degradation.

### Requirement 12: Viewer-Driven Demand Rendering

**User Story:** As a cluster operator, I want visualizer engines to consume zero GPU resources when no viewers are connected, so that cluster GPU capacity is preserved for other workloads.

#### Acceptance Criteria

1. WHILE zero viewers are connected to a guild's Activity, THE VisualizerManager SHALL NOT allocate a GPU context or start the render loop for that guild.
2. WHEN the first viewer connects and audio is playing, THE VisualizerManager SHALL start the Engine and begin frame production within 2 seconds.
3. WHEN the last viewer disconnects, THE VisualizerManager SHALL suspend the Engine after a 10-second debounce period to handle transient disconnects.
4. WHILE the Engine is suspended, THE Engine SHALL hold zero GPU allocations (EGL context destroyed, buffers freed).

### Requirement 13: HLS Pipeline for Visualizer Frames

**User Story:** As a developer, I want a specialised ffmpeg pipeline that accepts raw RGBA frames on stdin and produces HLS output, so that server-rendered engines can reuse the existing HLS delivery infrastructure.

#### Acceptance Criteria

1. THE HLS_Pipeline for visualizers SHALL accept raw RGBA frames (1280x720, 4 bytes/pixel) on stdin using the rawvideo input format.
2. THE HLS_Pipeline SHALL encode video using h264_qsv with the "fast" preset at a bitrate appropriate for 720p30.
3. THE HLS_Pipeline SHALL produce 2-second HLS segments with independent keyframes at segment boundaries.
4. THE HLS_Pipeline SHALL use the hwupload_qsv filter to transfer raw frames from system memory to QSV surfaces for hardware encoding.
5. WHEN the Engine's render_frames() iterator is exhausted or the Engine is stopped, THE HLS_Pipeline SHALL flush remaining frames and finalize the HLS playlist.

### Requirement 14: Per-Engine Configuration via Discord Commands

**User Story:** As a server admin, I want to configure each visualizer engine's settings through Discord slash commands, so that I can customise the visual experience without editing files.

#### Acceptance Criteria

1. THE bot SHALL provide a `/visualizer config <engine> <setting> <value>` slash command for modifying engine-specific parameters.
2. THE `/visualizer config` command SHALL validate values against the engine's schema and respond with an error for invalid inputs.
3. WHEN a configuration is changed while the engine is active, THE VisualizerManager SHALL hot-reload the setting without restarting the HLS pipeline (where technically possible).
4. THE bot SHALL provide a `/visualizer settings [engine]` slash command that displays the current configuration for the specified engine (or the active engine if omitted) as a Discord embed.
5. Engine-specific configurable parameters SHALL include:
   - **projectM**: `preset_category` (folder name), `blend_duration` (1-10s), `preset_duration` (10-300s), `brightness` (0.5-2.0), `sensitivity` (0.5-2.0)
   - **audiovis**: `style` (bars|waveform|waterfall|circular), `color_scheme` (name), `fft_bins` (7|32|64|128|512), `glow_intensity` (0-1.0), `background_opacity` (0-1.0)
   - **fosfora**: `particle_count` (1000-10000), `gravity` (0-2.0), `emission_style` (burst|stream|rain|fountain), `color_mode` (spectrum|mono|gradient), `trail_length` (0-1.0)
   - **varda**: `shader_name` (from available pool), `color_intensity` (0.5-2.0), `speed` (0.25-4.0), `complexity` (low|medium|high)
   - **dvd**: `speed` (0.5-3.0), `hue_shift` (true|false), `icon_size` (10-30% of viewport)

### Requirement 15: Preset Save/Load System

**User Story:** As a server admin, I want to save and load named preset configurations for each engine, so that I can quickly switch between curated looks without reconfiguring every setting.

#### Acceptance Criteria

1. THE bot SHALL provide a `/visualizer preset save <name>` command that captures the current engine type and all its configuration settings as a named preset.
2. THE bot SHALL provide a `/visualizer preset load <name>` command that applies a saved preset (restoring engine type + all settings) immediately.
3. THE bot SHALL provide a `/visualizer preset list` command that shows all saved presets for the guild with their engine type and a brief description.
4. THE bot SHALL provide a `/visualizer preset delete <name>` command to remove a saved preset.
5. Presets SHALL be stored per-guild in the encrypted credential store (same DB as other guild settings) and survive bot restarts.
6. THE `/visualizer preset load` command SHALL support autocomplete, suggesting available preset names.
7. IF the engine type in a loaded preset is unavailable (GPU down, engine removed), THEN the bot SHALL respond with an error suggesting available alternatives.

### Requirement 16: Bundled Factory Presets

**User Story:** As a user, I want pre-configured factory presets for each engine available out-of-the-box, so that I can enjoy curated visual experiences without manual configuration.

#### Acceptance Criteria

1. THE system SHALL ship with factory presets that cannot be deleted but can be overridden by guild-saved presets of the same name.
2. Factory presets SHALL be available for all engines and loadable via `/visualizer preset load <name>`.
3. THE following factory presets SHALL be included for **projectM**:
   - `milkdrop-classic` — Original Milkdrop 2 presets (nostalgia, geometric morphing)
   - `psychedelic` — High-complexity fractals and kaleidoscopes from the Cream of the Crop "Abstract" folder
   - `chill` — Low-motion, ambient presets from the "Fluid Motion" and "Simple" categories
   - `trippy` — Fast-morphing, high-saturation presets from "Trippy / Psychedelic" folder
   - `geometric` — Clean geometric patterns from "Geometric" and "Sacred Geometry" categories
   - `space` — Cosmic/nebula/space-themed presets from "Space" category
   - `energy` — High-motion presets that react aggressively to bass/beats
   - `minimal` — Simple, clean waveforms and low-element presets

   Source: [projectM-visualizer/presets-cream-of-the-crop](https://github.com/projectM-visualizer/presets-cream-of-the-crop) (9,795 presets sorted into themed folders by ISOSCELES; LGPL-2.1 compatible)

4. THE following factory presets SHALL be included for **audiovis**:
   - `spectrum-bars` — Classic vertical frequency bars, neon gradient, 7-band
   - `full-spectrum` — 128-bin hi-res spectrum with glow, dark background
   - `waveform` — Oscilloscope-style center waveform with bass-driven pulse
   - `waterfall` — Scrolling frequency waterfall (time descends, frequency on X axis)
   - `circular` — Radial spectrum arranged in a circle, beat-reactive radius
   - `vinyl` — Rotating disc with spectrum carved into the grooves
   - `neon-city` — Retro synthwave bars with grid perspective floor

5. THE following factory presets SHALL be included for **fosfora**:
   - `stardust` — Gentle particle rain from top, beat triggers upward bursts
   - `fireworks` — Center explosions on every beat, particles fade with gravity
   - `aurora` — Slow-flowing horizontal bands of particles (northern lights style)
   - `vortex` — Spiral emission with bass-driven rotation speed
   - `rain` — Downward-streaming particles that splash on beats
   - `nebula` — Dense cloud particles with color cycling and gentle drift
   - `pulse` — Radial pulse waves emanating from center on each beat

6. THE following factory presets SHALL be included for **varda** (GLSL shader presets):
   - `fractal-zoom` — Infinite Mandelbrot zoom driven by time and bass
   - `tunnel` — Beat-reactive neon tunnel fly-through (distance mapped to spectrum)
   - `plasma` — Classic plasma effect with audio-driven color cycling
   - `voronoi-pulse` — Voronoi cells with bass-reactive cell size and hue rotation
   - `raymarched-orbs` — Raymarched floating orbs with beat-triggered deformation
   - `kaleidoscope` — Mirror-pattern with FFT-driven complexity and rotation
   - `neon-grid` — Retro 80s grid perspective with bass-rippled surface
   - `star-field` — 3D star field with bass-accelerated speed and beat-flash
   - `liquid-metal` — Reflective metallic surface with spectrum-driven displacement
   - `cosmic-web` — Neural-network-like connections reacting to frequency bands

   Each varda shader SHALL follow the Shadertoy-compatible uniform convention: `iTime`, `iResolution`, `iChannel0` (512x2 audio texture with row 0 = waveform, row 1 = FFT spectrum), plus custom uniforms `iBeat` (float, 0-1 decaying beat pulse), `iBPM` (float), `iBandEnergy[7]` (float array).

7. THE following factory presets SHALL be included for **dvd** (client-side):
   - `classic` — Default circular guild icon bounce, hue shift on edge hit
   - `fast` — Higher speed (2.5x), more frequent color changes
   - `slow` — Very slow drift (0.5x speed), soothing ambient movement
   - `no-hue` — Normal speed, no hue-rotate filter (preserves original icon colors)

### Requirement 17: projectM Preset Management

**User Story:** As a server admin, I want to choose which category of Milkdrop presets to use, so that the visualizations match my server's vibe.

#### Acceptance Criteria

1. THE projectM Engine SHALL organize bundled presets into named categories matching the Cream of the Crop folder structure (Abstract, Fluid Motion, Geometric, Trippy, Space, Simple, etc.).
2. THE `/visualizer config projectm preset_category <name>` command SHALL restrict preset selection to the named category.
3. THE `/visualizer config projectm preset_category all` command SHALL enable random selection across all categories.
4. THE bot SHALL provide `/visualizer projectm list-categories` to show all available preset categories with their preset counts.
5. WHEN a user specifies a non-existent category name, THE command SHALL respond with an error listing available categories.

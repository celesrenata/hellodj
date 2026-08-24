# Requirements Document

## Introduction

Expand the AudioVis visualizer engine with 12+ visually impressive GLSL fragment shader presets across four aesthetic categories (psychedelic, aggressive, ambient, retro/synthwave). All presets render at 1280×720 @ 30fps on Intel Meteor Lake iGPU (Mesa iris) using the existing uniform interface (iTime, iResolution, iBeat, iBPM, iBandEnergy[7], iFFT, iFFTBins, iGlowIntensity, iBgOpacity). Beat drops trigger dramatic visual state changes, and all shaders exhibit continuous smooth motion rather than static patterns that pulse.

## Glossary

- **AudioVis_Engine**: The GPU-accelerated visualizer engine class (`AudioVisEngine`) that compiles GLSL fragment shaders, uploads FFT data as a 1D texture, and renders fullscreen quads at 30fps via EGL headless context.
- **Shader_Preset**: A single GLSL 330 core fragment shader file (`audiovis_{name}.glsl`) that implements a complete visual effect using the standard uniform interface.
- **Beat_Pulse**: The `iBeat` uniform value (float 0.0–1.0) that spikes to 1.0 on beat detection and decays to 0.0 over 200ms (~6 frames at 30fps).
- **Band_Energy**: The `iBandEnergy[7]` uniform array containing RMS energy values for 7 frequency bands (sub-bass through brilliance).
- **FFT_Texture**: The `iFFT` sampler1D uniform containing 512 magnitude bins from the current audio frame, uploaded each render tick.
- **Style_Category**: A logical grouping of shader presets by aesthetic mood (psychedelic, aggressive, ambient, retro).
- **Frame_Budget**: The maximum time allowed for a single frame render (33.3ms at 30fps target).
- **Discord_User**: A person interacting with the bot via Discord slash commands or the Activity UI.

## Requirements

### Requirement 1: Shader Preset Library

**User Story:** As a Discord_User, I want access to 12 or more visually distinct shader presets beyond the existing four (bars, waveform, circular, waterfall), so that I can choose visualizations that match the mood of the music.

#### Acceptance Criteria

1. THE AudioVis_Engine SHALL support a minimum of 12 new Shader_Preset styles in addition to the existing 4 styles (bars, waveform, circular, waterfall)
2. WHEN a Discord_User selects a valid style name via `/visualizer config audiovis style {name}`, THE AudioVis_Engine SHALL load and compile the corresponding fragment shader file `audiovis_{name}.glsl` from the shaders directory
3. IF a Discord_User selects an invalid style name, THEN THE AudioVis_Engine SHALL respond with an error message listing all available styles grouped by Style_Category

### Requirement 2: Style Categories

**User Story:** As a Discord_User, I want shader presets organized into aesthetic categories, so that I can browse by mood rather than memorizing individual style names.

#### Acceptance Criteria

1. THE AudioVis_Engine SHALL organize all Shader_Presets into exactly four Style_Categories: psychedelic, aggressive, ambient, and retro
2. WHEN a Discord_User queries available styles, THE AudioVis_Engine SHALL present them grouped by Style_Category with the category name as a heading
3. THE AudioVis_Engine SHALL assign each Shader_Preset to exactly one Style_Category

### Requirement 3: Psychedelic Category Presets

**User Story:** As a Discord_User, I want trippy/psychedelic visualizations (kaleidoscope fractals, plasma tunnels, hypnotic spirals), so that the visuals feel immersive and mind-bending during music playback.

#### Acceptance Criteria

1. THE AudioVis_Engine SHALL include a "kaleidoscope" Shader_Preset that renders mirrored fractal geometry with rotational symmetry driven by iTime, where iBeat triggers symmetry-fold increases and iBandEnergy controls color cycling speed
2. THE AudioVis_Engine SHALL include a "plasma" Shader_Preset that renders a flowing plasma tunnel effect with sinusoidal color mixing, where iBandEnergy[0..2] (bass bands) drives tunnel depth pulsing and iBeat triggers color inversion flashes
3. THE AudioVis_Engine SHALL include a "fractal" Shader_Preset that renders a Mandelbrot or Julia set with continuous zoom driven by iTime, where iBeat triggers zoom acceleration and iBandEnergy maps to color palette rotation
4. THE AudioVis_Engine SHALL include a "hypnotic" Shader_Preset that renders concentric rotating rings with wave-distortion, where each ring's rotation speed responds to a different iBandEnergy band and iBeat causes ring expansion bursts

### Requirement 4: Aggressive Category Presets

**User Story:** As a Discord_User, I want hard-hitting glitchy visualizations (digital corruption, lightning, shatter effects), so that the visuals match the energy of aggressive or electronic music.

#### Acceptance Criteria

1. THE AudioVis_Engine SHALL include a "glitch" Shader_Preset that renders digital corruption artifacts (RGB channel splitting, block displacement, scanline noise), where iBeat triggers maximum distortion intensity and iBandEnergy[0] (sub-bass) controls block displacement magnitude
2. THE AudioVis_Engine SHALL include a "storm" Shader_Preset that renders branching electrical arc patterns across the viewport, where iBeat spawns new arc clusters from random origin points and iBandEnergy[5..6] (presence/brilliance) control arc branching complexity
3. THE AudioVis_Engine SHALL include a "shatter" Shader_Preset that renders a glass/mirror surface that fractures on beat detection, where iBeat triggers a new fracture event radiating from center and the fracture pattern decays over time proportional to 1.0/iBPM

### Requirement 5: Ambient Category Presets

**User Story:** As a Discord_User, I want calm ambient visualizations (aurora, nebula, ocean caustics), so that the visuals provide a relaxing backdrop during chill music.

#### Acceptance Criteria

1. THE AudioVis_Engine SHALL include an "aurora" Shader_Preset that renders flowing curtains of light resembling northern lights, where iBandEnergy[2..4] (low-mid through upper-mid) control curtain wave amplitude and color shifting responds to the overall energy sum
2. THE AudioVis_Engine SHALL include a "nebula" Shader_Preset that renders volumetric cosmic gas clouds with slow rotation, where FFT_Texture drives local cloud density variation and iBeat triggers gentle brightness surges across the cloud field
3. THE AudioVis_Engine SHALL include a "ocean" Shader_Preset that renders underwater caustic light patterns with gentle wave motion, where iBandEnergy[0..1] (sub-bass/bass) controls wave height and iBeat triggers ripple expansion events from the viewport center
4. THE AudioVis_Engine SHALL include a "fireflies" Shader_Preset that renders floating luminous particles with soft glow trails, where the number of visible particles scales with overall audio energy and iBeat causes particles to scatter outward from center

### Requirement 6: Retro/Synthwave Category Presets

**User Story:** As a Discord_User, I want synthwave/retro visualizations (neon grids, VHS effects, cyber tunnels), so that the visuals evoke 80s/outrun aesthetics.

#### Acceptance Criteria

1. THE AudioVis_Engine SHALL include a "synthwave" Shader_Preset that renders a perspective grid floor with a sunset gradient horizon and wireframe mountains, where iBandEnergy[0..1] (bass) drives mountain height oscillation and iBeat triggers horizon flash effects
2. THE AudioVis_Engine SHALL include a "retrowave" Shader_Preset that renders VHS-style visual artifacts (chromatic aberration, horizontal scanlines, tape warping) over geometric neon shapes, where iBeat intensifies all distortion parameters and iBandEnergy controls neon shape pulsing
3. THE AudioVis_Engine SHALL include a "cyber" Shader_Preset that renders a forward-moving neon wireframe tunnel, where tunnel forward speed responds to iBandEnergy[0] (sub-bass), tunnel shape complexity responds to iBandEnergy[3..4] (mid/upper-mid), and iBeat triggers tunnel geometry morphing

### Requirement 7: Audio Reactivity Depth

**User Story:** As a Discord_User, I want beat drops to trigger dramatic visual state changes rather than just brightness pulsing, so that the visualizations feel tightly synchronized to the music.

#### Acceptance Criteria

1. WHEN iBeat transitions from 0.0 to 1.0, each Shader_Preset SHALL trigger at least one structural visual change (geometry transformation, pattern reset, symmetry change, or color palette shift) in addition to any brightness or intensity modulation
2. WHILE iBeat is greater than 0.5 (first ~100ms after beat detection), each Shader_Preset SHALL exhibit a visually distinct "peak" state that differs from the resting state in at least two visual parameters (color, geometry, scale, or speed)
3. THE AudioVis_Engine SHALL pass all 7 iBandEnergy values to Shader_Presets, and each Shader_Preset SHALL utilize a minimum of 3 distinct iBandEnergy bands to drive independent visual parameters

### Requirement 8: Continuous Motion

**User Story:** As a Discord_User, I want all shader presets to exhibit smooth continuous motion even when no audio is playing, so that the visualizations never appear static or frozen.

#### Acceptance Criteria

1. WHILE no audio data is received (all iBandEnergy values equal 0.0 and FFT_Texture contains all zeros), each Shader_Preset SHALL render continuously evolving visuals driven by iTime with perceptible motion at every frame
2. THE AudioVis_Engine SHALL maintain a monotonically increasing iTime uniform that increments by wall-clock elapsed time each frame, providing the base animation driver for all Shader_Presets
3. WHEN audio data resumes after silence, each Shader_Preset SHALL blend audio-reactive parameters into the continuous motion within 10 frames (333ms) without jarring visual discontinuities

### Requirement 9: Performance Constraints

**User Story:** As a system operator, I want all shader presets to render within the 30fps frame budget on Intel Meteor Lake iGPU (Mesa iris), so that the visualizer does not drop frames or stall the HLS pipeline.

#### Acceptance Criteria

1. THE AudioVis_Engine SHALL render each Shader_Preset frame in under 30ms (leaving 3.3ms headroom within the 33.3ms Frame_Budget) on Intel Meteor Lake integrated GPU at 1280×720 resolution
2. THE AudioVis_Engine SHALL limit per-pixel fragment shader loop iterations to a maximum of 128 iterations for fractal/raymarching presets to bound worst-case GPU execution time
3. IF a Shader_Preset frame exceeds the Frame_Budget for 3 consecutive frames, THEN THE AudioVis_Engine SHALL log a performance warning including the style name and measured frame time

### Requirement 10: Shader Compilation Validation

**User Story:** As a system operator, I want shader compilation failures to be handled gracefully, so that a broken shader preset does not crash the visualizer for the entire guild.

#### Acceptance Criteria

1. IF a Shader_Preset fails to compile (GLSL syntax error or unsupported feature), THEN THE AudioVis_Engine SHALL log the compilation error, fall back to the "bars" style, and continue rendering without interruption
2. WHEN the AudioVis_Engine initializes, THE AudioVis_Engine SHALL validate that the requested style's shader file exists in the shaders directory before attempting compilation
3. IF the shader file for the requested style does not exist, THEN THE AudioVis_Engine SHALL log a warning and fall back to the "bars" style

### Requirement 11: Style Registry Extension

**User Story:** As a developer, I want the style registration to be data-driven and extensible, so that adding new shader presets requires only adding a GLSL file and a registry entry without modifying rendering logic.

#### Acceptance Criteria

1. THE AudioVis_Engine SHALL maintain a style registry mapping each style name to its Style_Category and shader filename
2. WHEN a new Shader_Preset GLSL file is added to the shaders directory and registered in the style registry, THE AudioVis_Engine SHALL support it without any changes to the rendering pipeline, uniform upload logic, or frame readback code
3. THE AudioVis_Engine SHALL expose the complete style registry (names and categories) to the Discord command layer for autocomplete and validation

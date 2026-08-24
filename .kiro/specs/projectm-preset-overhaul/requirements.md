# Requirements Document

## Introduction

Overhaul the projectM visualizer engine's preset library by replacing all 56 AI-generated stub `.milk` files with hundreds of curated, high-quality community Milkdrop presets from established authors. Disable the libprojectM "floating M" logo/watermark overlay that appears during rendering. Verify end-to-end rendering on the Intel Meteor Lake iGPUs (Mesa iris, EGL headless, SR-IOV VFs) in the gremlin cluster. The DVD engine remains the default (zero GPU cost); projectM is opt-in via `/visualizer engine projectm`.

## Glossary

- **ProjectM_Engine**: The server-side visualizer engine class (`bot/video/visualizer_engines/projectm.py`) that binds to libprojectM 4.x via ctypes and renders Milkdrop presets into an EGL headless FBO at 1280×720@30fps
- **libprojectM**: The projectM 4.x shared library (`libprojectM-4.so`) installed via `libprojectm-dev` that parses `.milk` preset files and renders audio-reactive OpenGL visualizations
- **Preset_Directory**: The directory tree at `bot/data/presets/projectm/` (mapped to `/app/data/presets/projectm/` in the container) where `.milk` files are organized into category subdirectories
- **Stub_Preset**: An AI-generated minimal `.milk` file (~59 lines) with only basic `per_frame` equations and no `per_pixel` or `warp_` shader sections — visually trivial
- **Community_Preset**: A real Milkdrop preset authored by established community members, typically 200-800+ lines with complex `per_pixel` equations, composite shaders, and warp shaders
- **Cream_Of_The_Crop**: The curated "best of" community preset collections maintained by the projectM project (projectm-presets-cream-of-the-crop, cream_of_the_crop repositories)
- **Logo_Overlay**: The "floating M" projectM logo/watermark texture rendered by libprojectM as an overlay on top of preset output during visualization
- **DVD_Engine**: The zero-GPU client-side visualizer engine that renders entirely in the browser with no server-side GPU usage, remains the default
- **Gremlin_Cluster**: The Kubernetes cluster on nodes gremlin-1 through gremlin-4 (10.1.1.12-15) with Intel Meteor Lake iGPUs, SR-IOV enabled (8 VFs per node), Mesa iris OpenGL driver
- **EGL_Headless**: Surfaceless EGL platform rendering where no display surface exists — requires explicit `glViewport` after FBO creation
- **FBO**: OpenGL Framebuffer Object used for off-screen rendering; the ProjectM_Engine renders into this and reads pixels back via `glReadPixels` for the ffmpeg HLS pipeline
- **Preset_Category**: A subdirectory within the Preset_Directory grouping presets by visual style (e.g., Abstract, Trippy, Geometric, Energy)

## Requirements

### Requirement 1: Replace Stub Presets with Curated Community Presets

**User Story:** As a guild member watching the visualizer, I want to see stunning Milkdrop visualizations with complex per-pixel equations, warp shaders, and composite shaders, so that the visuals are genuinely psychedelic and impressive rather than trivial color-cycling loops.

#### Acceptance Criteria

1. THE Preset_Directory SHALL contain Community_Presets sourced from Cream_Of_The_Crop collections and established projectM community preset repositories (github.com/projectM-visualizer/presets-cream-of-the-crop or equivalent)
2. THE Preset_Directory SHALL contain a minimum of 200 Community_Presets across all category subdirectories
3. THE Preset_Directory SHALL NOT contain any Stub_Presets — files are classified as stubs if they have fewer than 80 lines AND contain no `per_pixel` section AND contain no `warp_` or `comp_` shader sections; all such files SHALL be removed before adding Community_Presets
4. WHEN the ProjectM_Engine loads the Preset_Directory, THE presets SHALL include works from at least 5 distinct established authors, identifiable by author name in the preset filename convention (e.g., "Geiss - ...", "Flexi - ...", "Rovastar - ...") from the set: Geiss, Flexi, Rovastar, Zylot, Eo.S., martin, cope, Idiot24-7, shifter, Phat, Krash, Unchained, or equivalent recognized community contributors
5. THE Preset_Directory SHALL organize presets into at least 5 category subdirectories with a minimum of 10 presets per category, matching visual style groupings for use with the `preset_category` configuration option
6. WHEN a Community_Preset contains `per_pixel` equation sections, warp shaders (`warp_1`, `warp_2`, etc.), or composite shaders (`comp_1`, `comp_2`, etc.), THE preset file SHALL be included unmodified to preserve intended visual behavior

### Requirement 2: Disable the projectM Logo Overlay

**User Story:** As a guild member watching the visualizer, I want no projectM branding or watermark logos appearing over the visualization, so that the visual output is clean and unobstructed.

#### Acceptance Criteria

1. WHEN the ProjectM_Engine renders frames, THE rendered output SHALL NOT contain the projectM "floating M" Logo_Overlay texture at any position in the frame
2. WHEN the ProjectM_Engine initializes libprojectM, THE ProjectM_Engine SHALL configure the texture search path to exclude any directory containing the projectM logo texture file (typically `projectM.png` or `texture_projectM.png`), preventing the library from loading the Logo_Overlay asset
3. IF libprojectM exposes a `projectm_set_toast_message` function symbol, THEN THE ProjectM_Engine SHALL call it with an empty string during initialization to suppress preset title text overlay rendering on the output
4. THE ProjectM_Engine SHALL NOT modify the libprojectM shared library binary to achieve logo removal — only API-level configuration, texture path manipulation, or omission of logo texture files from the container image SHALL be used
5. WHEN the Docker image is built, THE image SHALL NOT include the projectM logo texture file (`projectM.png`, `texture_projectM.png`, or equivalent branding assets) in any directory scanned by libprojectM for textures

### Requirement 3: End-to-End Rendering Verification on Intel Meteor Lake

**User Story:** As the system operator, I want to verify that projectM renders non-black, visually active frames on the Intel Meteor Lake iGPUs in the gremlin cluster, so that the engine actually produces visible output through the full pipeline.

#### Acceptance Criteria

1. WHEN the ProjectM_Engine renders a frame after receiving audio data via `projectm_pcm_add_float`, THE FBO SHALL contain non-black pixel data where at least 1% of pixels (9,216 of 921,600 at 1280×720) have any RGB channel value greater than zero
2. WHILE the EGL surfaceless context is active on an Intel Meteor Lake iGPU (Mesa iris driver), THE ProjectM_Engine SHALL call `projectm_opengl_render_frame` and `glGetError()` SHALL return `GL_NO_ERROR` after the call completes
3. WHEN Community_Presets with HLSL shader sections (`warp_` or `comp_` blocks) are loaded, THE libprojectM GLSL translator SHALL compile the translated shaders on the Mesa iris OpenGL 4.6 context, or fall back to non-shader rendering without crashing the process
4. WHILE audio data is being fed to the ProjectM_Engine at a rate of at least one `projectm_pcm_add_float` call per 33ms, THE ProjectM_Engine SHALL produce rendered frames at an average rate of 30fps (±2fps measured over any 5-second window), matching the GPUEngineBase TARGET_FPS
5. WHEN the ProjectM_Engine initializes the EGL headless FBO, THE ProjectM_Engine SHALL call `glViewport(0, 0, width, height)` before the first render pass to ensure fragment rasterization occurs on the surfaceless context

### Requirement 4: Preset Quality Curation Standards

**User Story:** As a guild member, I want the preset selection to be curated for maximum visual impact, so that every preset looks impressive rather than being a random dump of mediocre content.

#### Acceptance Criteria

1. AT LEAST 75% of presets in the Preset_Directory SHALL contain either a `per_pixel` equation section OR at least one `warp_`/`comp_` shader section, ensuring the majority use advanced rendering techniques beyond basic per-frame parameters
2. THE Preset_Directory SHALL NOT include presets that lack any reference to audio-reactive variables (`bass`, `mid`, `treb`, `bass_att`, `mid_att`, `treb_att`, or `beat`) in their per_frame or per_pixel equations — all included presets must visibly respond to audio
3. AT LEAST 50% of presets SHALL contain two or more of the following visual complexity traits (detectable by file content inspection): motion trails (fDecay < 0.99), warp shader sections, composite shader sections, more than 5 per_frame equations, per_pixel equations referencing `rad` or `ang`
4. THE Preset_Directory SHALL contain presets organized into at least 5 distinct category subdirectories with a minimum of 10 presets per category to maintain variety during shuffle playback

### Requirement 5: DVD Engine Default Preservation

**User Story:** As the system operator, I want DVD to remain the default visualizer engine for all guilds, so that no GPU resources are consumed unless a guild explicitly opts in to projectM.

#### Acceptance Criteria

1. THE `DEFAULT_VISUALIZER_ENGINE` constant in `guild_settings.py` SHALL remain set to `"dvd"`
2. WHILE the DVD_Engine is active for a guild, THE system SHALL allocate zero server-side GPU resources (no EGL context creation, no FBO allocation, no OpenGL render calls) and the `DVDEngine.is_client_side` property SHALL return `True`
3. WHEN `get_visualizer_engine()` is called for a guild that has no stored `visualizer_engine` value, THE guild_settings module SHALL return `"dvd"`
4. IF a guild's stored `visualizer_engine` value is not present in `VALID_VISUALIZER_ENGINES` or equals the removed legacy value `"vgalizer"`, THEN THE guild_settings module SHALL return `"dvd"` instead of the invalid value

### Requirement 6: Docker Image Build and Deployment

**User Story:** As the system operator, I want the updated preset library packaged into the Docker image and deployable to the gremlin cluster without manual file transfers, so that preset changes ship with the image.

#### Acceptance Criteria

1. WHEN the Docker image is built, THE Dockerfile `COPY` directive SHALL include all Community_Presets from `bot/data/presets/projectm/` into `/app/data/presets/projectm/` in the image, and the resulting directory SHALL contain at least 200 `.milk` files
2. THE total size of the Preset_Directory SHALL remain under 50MB to avoid excessive Docker image layer bloat
3. WHEN deployed, THE ProjectM_Engine SHALL resolve the Preset_Directory at the path specified by the `PRESET_DIR` constant (`/app/data/presets/projectm/`) and `Path(PRESET_DIR).is_dir()` SHALL return True
4. THE preset files SHALL be readable by the bot process running as UID 1000 (matching the SecurityContext configuration) with POSIX permissions of at least 644

### Requirement 7: Preset Shuffle and Track-Change Transitions

**User Story:** As a guild member watching the visualizer, I want presets to change automatically with smooth blending transitions, so that the visual experience stays varied without jarring hard-cuts.

#### Acceptance Criteria

1. WHEN the ProjectM_Engine is configured, THE ProjectM_Engine SHALL enable preset shuffle mode via `projectm_set_shuffle_enabled(handle, True)` so that presets are selected in random order rather than sequentially
2. WHEN a track changes and the ProjectM_Engine handle is active, THE ProjectM_Engine SHALL trigger a soft-cut transition to a new random preset with the configured blend duration (default 3.0 seconds, configurable from 1.0 to 10.0 seconds via `projectm_set_soft_cut_duration`)
3. WHILE no track change occurs, THE ProjectM_Engine SHALL auto-advance to a new random preset via soft-cut transition after the configured preset_duration elapses (default 30 seconds, configurable from 10 to 300 seconds via `projectm_set_preset_duration`)
4. IF the ProjectM_Engine handle is not initialized when a track change occurs, THEN THE ProjectM_Engine SHALL skip the preset transition without raising an error

### Requirement 8: Preset Compatibility with libprojectM 4.x

**User Story:** As the system operator, I want all bundled presets to be parseable by the Ubuntu-packaged libprojectM 4.x without crashes, so that the engine remains stable during extended playback sessions.

#### Acceptance Criteria

1. WHEN a Community_Preset fails to parse (malformed equations, unsupported shader syntax), THE libprojectM library SHALL skip the preset and continue rendering remaining valid presets without interrupting audio-reactive playback — the process SHALL NOT crash or abort
2. THE Preset_Directory SHALL only include presets in the `.milk` format compatible with Milkdrop 2.x / projectM 4.x; `.prjm` files MAY be included only if they are also indexed and selectable by `projectm_set_preset_path` without error
3. IF a preset contains shader code that fails GLSL compilation on the Mesa iris driver, THEN THE libprojectM library SHALL fall back to rendering the preset using per-frame and per-pixel equations without the warp/composite shader passes rather than halting
4. THE preset collection SHALL be tested to verify at least 90% of included presets (and no fewer than 180 presets in absolute count) are selectable and renderable without parse errors on libprojectM 4.x as packaged in Ubuntu
5. WHILE the ProjectM_Engine is rendering with Community_Presets, THE ProjectM_Engine SHALL operate for at least 60 continuous minutes of audio-fed playback with preset auto-advancement without crashing or requiring a restart

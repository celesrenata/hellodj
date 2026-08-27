# Requirements Document

## Introduction

Upgrade the projectM visualizer engine from AI-generated stub presets to real community Milkdrop presets, verify end-to-end rendering on Intel Meteor Lake iGPUs, and replace the DVD bouncing logo in the Activity frontend with a minimal track info display. DVD remains the default engine (zero GPU cost for idle guilds).

## Glossary

- **ProjectM_Engine**: The server-side visualizer engine class (`bot/video/visualizer_engines/projectm.py`) that binds to libprojectM via ctypes and renders Milkdrop presets into an EGL headless FBO
- **libprojectM**: The projectM 4.x shared library (`libprojectM-4.so`) that parses `.milk` preset files and renders audio-reactive visualizations using OpenGL
- **Preset**: A `.milk` file conforming to the Milkdrop preset format, containing per-frame equations, per-pixel equations, warp shaders, and composite shaders that define visual behavior
- **Stub_Preset**: An AI-generated minimal `.milk` file (~59 lines) with only trivial per_frame equations and no per_pixel, warp shader, or composite shader sections
- **Community_Preset**: A real Milkdrop preset authored by the community (Geiss, Flexi, Rovastar, Zylot, Eo.S, etc.) typically 200-800+ lines with complex per_pixel equations and HLSL shader sections
- **DVD_Engine**: The zero-GPU client-side visualizer engine that renders entirely in the browser with no server-side GPU usage
- **Activity_Frontend**: The single-page HTML/JS application loaded in Discord's Activity iframe that displays video, whiteboard, visualizer, and lyrics overlay
- **Bouncing_Logo**: The DVD screensaver animation in the Activity_Frontend that renders the bot avatar image bouncing around the screen with hue rotation
- **Track_Info_Display**: A minimal static display showing current track title and artist on a dark background
- **Gremlin_Cluster**: The Kubernetes cluster on nodes 10.1.1.12-15 with Intel Meteor Lake iGPUs, SR-IOV (8 VFs), Mesa iris driver, EGL surfaceless platform
- **Preset_Directory**: The directory `/app/data/presets/projectm/` where `.milk` preset files are stored, organized into category subdirectories
- **FBO**: OpenGL Framebuffer Object used for headless off-screen rendering

## Requirements

### Requirement 1: Replace Stub Presets with Community Presets

**User Story:** As a guild member watching the visualizer, I want to see high-quality Milkdrop visualizations with complex per-pixel equations and shader effects, so that the visuals are genuinely impressive rather than trivial color-cycling loops.

#### Acceptance Criteria

1. THE Preset_Directory SHALL contain Community_Presets sourced from official projectM community preset collections (cream-of-the-crop, milkdrop-presets, projectm-presets-cream-of-the-crop)
2. THE Preset_Directory SHALL contain a minimum of 200 Community_Presets organized into category subdirectories
3. THE Preset_Directory SHALL NOT contain any Stub_Presets (files with fewer than 80 lines and no `per_pixel` or `warp_` shader sections)
4. WHEN the ProjectM_Engine loads presets, THE ProjectM_Engine SHALL have access to presets from at least 5 distinct authors (Geiss, Flexi, Rovastar, Zylot, Eo.S, martin, Idiot, shifter, or equivalent established community authors)
5. THE Preset_Directory SHALL organize presets into category subdirectories for use with the `preset_category` configuration option

### Requirement 2: ProjectM Library Loading

**User Story:** As the system operator, I want the projectM engine to reliably load libprojectM on the production cluster, so that the engine initializes without runtime errors.

#### Acceptance Criteria

1. WHEN the ProjectM_Engine initializes, THE ProjectM_Engine SHALL load `libprojectM-4.so` or `libprojectM.so.4` from the system library path
2. IF libprojectM is not found at any expected path, THEN THE ProjectM_Engine SHALL log a descriptive error message including the library name and OS error detail
3. WHEN libprojectM is loaded, THE ProjectM_Engine SHALL configure ctypes function signatures for all projectM API calls (create, destroy, set_window_size, pcm_add_float, opengl_render_frame, set_preset_path, select_random_preset)
4. THE ProjectM_Engine SHALL verify that `projectm_create()` returns a non-NULL handle before proceeding with configuration

### Requirement 3: Preset Parsing on Real Presets

**User Story:** As the system operator, I want libprojectM to correctly parse complex community presets with per_pixel equations and HLSL shader sections, so that the visualization renders the intended effects.

#### Acceptance Criteria

1. WHEN the ProjectM_Engine calls `projectm_set_preset_path` with the Preset_Directory, THE libprojectM library SHALL scan and index all `.milk` files in the directory tree
2. WHEN a Community_Preset contains `per_pixel` equation sections, THE libprojectM library SHALL parse the equations without crashing the process
3. WHEN a Community_Preset contains `warp_` or `comp_` HLSL shader sections, THE libprojectM library SHALL compile the shaders or gracefully fall back to non-shader rendering
4. IF a preset fails to parse, THEN THE libprojectM library SHALL skip the preset and continue operating with remaining valid presets

### Requirement 4: Frame Rendering Verification on Intel Meteor Lake

**User Story:** As the system operator, I want to verify that projectM renders non-black frames on the Intel Meteor Lake iGPUs in the gremlin cluster, so that I know the engine actually produces visible output end-to-end.

#### Acceptance Criteria

1. WHEN the ProjectM_Engine renders a frame after receiving audio data, THE FBO SHALL contain pixel values where at least 1% of pixels have a luminance value greater than zero (non-black output)
2. WHILE the EGL surfaceless context is active on an Intel Meteor Lake iGPU, THE ProjectM_Engine SHALL call `glViewport(0, 0, 1280, 720)` before each render pass to ensure fragments are rasterized
3. WHEN `projectm_opengl_render_frame` is called, THE ProjectM_Engine SHALL render into the currently bound FBO without requiring a display surface
4. THE ProjectM_Engine SHALL produce rendered frames at 30fps when audio data is being fed via `projectm_pcm_add_float`

### Requirement 5: DVD Frontend Visual Replacement

**User Story:** As a guild member viewing the Activity, I want to see a clean track info display instead of the bouncing bot avatar logo, so that the idle state looks professional rather than distracting.

#### Acceptance Criteria

1. WHEN the DVD_Engine is active, THE Activity_Frontend SHALL display the Track_Info_Display instead of the Bouncing_Logo
2. THE Track_Info_Display SHALL show the current track title and artist name centered on a dark background
3. WHEN no track is playing, THE Track_Info_Display SHALL show the bot name or a minimal idle indicator on a dark background
4. THE Activity_Frontend SHALL NOT render any bouncing, moving, or animated logo elements when the DVD_Engine is active
5. THE Track_Info_Display SHALL use text styling consistent with the existing Activity_Frontend design system (font family, text color, sizing hierarchy)

### Requirement 6: DVD Engine Default Preservation

**User Story:** As the system operator, I want DVD to remain the default visualizer engine for all guilds, so that idle guilds consume zero GPU resources.

#### Acceptance Criteria

1. THE DVD_Engine SHALL remain the default value for `DEFAULT_VISUALIZER_ENGINE` in guild_settings.py
2. WHILE the DVD_Engine is active, THE DVD_Engine SHALL consume zero server-side GPU resources (no OpenGL context, no FBO allocation, no render calls)
3. WHEN a new guild joins, THE guild_settings SHALL assign the DVD_Engine as the visualizer engine without requiring manual configuration

### Requirement 7: Build and Deployment

**User Story:** As the system operator, I want the updated presets and frontend changes packaged into the Docker image and deployed to the gremlin cluster, so that the changes are live in production.

#### Acceptance Criteria

1. WHEN the Docker image is built, THE Dockerfile SHALL copy all Community_Presets from `bot/data/presets/projectm/` into `/app/data/presets/projectm/` in the image
2. WHEN the image is deployed to the Gremlin_Cluster, THE Preset_Directory SHALL be readable by the bot process (UID 1000)
3. THE updated image SHALL be tagged and pushed to `registry.celestium.life/hellodj/bot` with a descriptive tag
4. WHEN deployed, THE ProjectM_Engine SHALL resolve the Preset_Directory path at `/app/data/presets/projectm/` matching the `PRESET_DIR` constant

### Requirement 8: Preset Shuffle and Transitions

**User Story:** As a guild member watching the visualizer, I want presets to change automatically and blend smoothly, so that the visual experience stays varied and transitions are not jarring.

#### Acceptance Criteria

1. THE ProjectM_Engine SHALL enable preset shuffle mode so that presets are selected randomly rather than sequentially
2. WHEN a track changes, THE ProjectM_Engine SHALL trigger a soft-cut transition to a new random preset with a blend duration of 3.0 seconds
3. WHILE no track change occurs, THE ProjectM_Engine SHALL auto-advance to a new preset after the configured preset_duration (default 30 seconds)

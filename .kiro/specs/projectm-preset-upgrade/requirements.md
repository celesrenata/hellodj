# Requirements Document

## Introduction

Replace the 56 AI-generated stub Milkdrop .milk preset files with real, battle-tested community presets from the projectM cream-of-the-crop collection and other high-quality sources. Verify the projectM engine renders correctly on Intel Meteor Lake iGPUs (Mesa iris driver with SR-IOV VFs). Change the default visualizer engine from "dvd" (bouncing logo) to "random" (cycles through GPU-accelerated engines).

## Glossary

- **ProjectM_Engine**: The visualizer engine at `bot/video/visualizer_engines/projectm.py` that wraps libprojectM 4.x via ctypes to render Milkdrop-compatible audio visualizations
- **Preset**: A `.milk` file containing Milkdrop preset configuration including per-frame equations, per-pixel equations, composite shaders, and warp shaders
- **Stub_Preset**: An AI-generated placeholder preset file (~59 lines) containing only basic per_frame equations with no per_pixel, composite shader, or warp shader sections
- **Community_Preset**: A real Milkdrop preset authored by the community (artists like Geiss, Flexi, Rovastar, Zylot, Aderrasi) typically 200-800+ lines with complex rendering equations
- **Cream_of_the_Crop**: The curated collection of high-quality community presets maintained at `projectm-visualizer/presets-cream-of-the-crop` on GitHub (~2000+ presets)
- **Preset_Category**: A subdirectory under the preset root directory used to group presets by visual style
- **Preset_Directory**: The path `/app/data/presets/projectm` where libprojectM loads preset files at runtime
- **Default_Engine**: The visualizer engine used when no guild-specific engine is configured, stored as `DEFAULT_VISUALIZER_ENGINE` in `guild_settings.py`
- **Random_Engine**: The meta-engine that cycles through GPU-accelerated engines: drift, projectm, audiovis, fosfora, varda
- **Gremlin_Node**: One of four Kubernetes worker nodes (gremlin-1 through gremlin-4) with Intel Meteor Lake iGPUs running Mesa iris driver with SR-IOV virtual functions
- **libprojectM**: The shared library (`libprojectM-4.so`) providing the Milkdrop-compatible rendering engine via its C API
- **Factory_Preset**: A curated engine configuration in `factory_presets.py` that references a Preset_Category by name

## Requirements

### Requirement 1: Replace Stub Presets with Community Presets

**User Story:** As a bot operator, I want real community Milkdrop presets instead of trivial AI-generated stubs, so that the projectM visualizer produces visually impressive, battle-tested audio visualizations.

#### Acceptance Criteria

1. WHEN the bot image is built, THE Preset_Directory SHALL contain Community_Presets sourced from the Cream_of_the_Crop collection and other high-quality community preset repositories
2. WHEN the bot image is built, THE Preset_Directory SHALL NOT contain any Stub_Presets
3. THE Preset_Directory SHALL contain a minimum of 200 Community_Presets across all categories
4. WHEN a Community_Preset is included, THE Preset file SHALL contain at least one of: per_pixel equations, a composite shader (`comp_shader_*` section), or a warp shader (`warp_shader_*` section)

### Requirement 2: Organize Presets into Categories

**User Story:** As a bot operator, I want presets organized into meaningful categories, so that the factory preset system and category-based selection continue to work correctly.

#### Acceptance Criteria

1. THE Preset_Directory SHALL contain categorized subdirectories that align with the Preset_Category names referenced by Factory_Presets in `factory_presets.py`
2. WHEN a Factory_Preset references a Preset_Category, THE corresponding subdirectory SHALL exist and contain at least 10 Community_Presets
3. THE Preset_Directory SHALL retain the existing category names (Abstract, Classic, Energy, Fluid Motion, Geometric, Simple, Space, Trippy) or provide updated Factory_Preset entries for any renamed or replaced categories
4. WHEN the `get_available_categories()` class method is called, THE ProjectM_Engine SHALL return a dictionary with accurate preset counts for each populated category

### Requirement 3: Verify ProjectM Engine on Intel Meteor Lake iGPUs

**User Story:** As a bot operator, I want confirmation that the projectM engine renders correctly on my Intel Meteor Lake iGPUs, so that I can confidently use projectM as a production visualizer.

#### Acceptance Criteria

1. WHEN the ProjectM_Engine is initialized on a Gremlin_Node, THE engine SHALL successfully load libprojectM via ctypes without errors
2. WHEN the ProjectM_Engine renders a frame with audio data, THE engine SHALL produce a non-black, non-uniform framebuffer output
3. WHEN the ProjectM_Engine loads a Community_Preset, THE engine SHALL not crash or log shader compilation errors
4. IF libprojectM fails to load on a Gremlin_Node, THEN THE ProjectM_Engine SHALL log a descriptive error message including the library path attempted and the OS error

### Requirement 4: Change Default Visualizer Engine

**User Story:** As a bot operator, I want the default visualizer engine changed from "dvd" to "random", so that new guilds see GPU-accelerated visualizations instead of a bouncing logo.

#### Acceptance Criteria

1. THE Default_Engine SHALL be set to "random" instead of "dvd"
2. WHEN a guild has no explicitly configured visualizer engine, THE system SHALL use "random" as the engine selection
3. WHEN a guild has an explicitly configured engine (including "dvd"), THE system SHALL respect the guild-specific configuration
4. WHEN the Default_Engine is "random", THE Random_Engine SHALL cycle through the pool of GPU-accelerated engines (drift, projectm, audiovis, fosfora, varda)

### Requirement 5: Preset Loading Robustness

**User Story:** As a bot operator, I want the projectM engine to handle preset loading failures gracefully, so that a single corrupt or incompatible preset does not crash the visualizer.

#### Acceptance Criteria

1. IF a Community_Preset fails to parse or causes a shader compilation error, THEN THE ProjectM_Engine SHALL skip the failing preset and select another preset from the pool
2. IF the Preset_Directory is empty or missing, THEN THE ProjectM_Engine SHALL log a warning and raise an initialization error rather than rendering with no presets
3. WHEN the ProjectM_Engine is configured with a specific Preset_Category that contains no valid presets, THE engine SHALL fall back to loading presets from all categories

### Requirement 6: Docker Image Preset Bundling

**User Story:** As a bot operator, I want presets bundled in the Docker image at build time, so that presets are available immediately without runtime downloads or network dependencies.

#### Acceptance Criteria

1. WHEN the bot Docker image is built, THE Dockerfile SHALL copy Community_Presets into the image at the path that maps to Preset_Directory at runtime (`/app/data/presets/projectm`)
2. THE bundled presets SHALL NOT require network access at runtime to function
3. WHEN updating presets, THE process SHALL involve rebuilding the Docker image with the new preset files and deploying via the existing Kubernetes rolling update mechanism

# Design Document: projectM Preset Overhaul

## Overview

This feature replaces all 56 AI-generated stub `.milk` presets with curated community presets from the official [projectM-visualizer/presets-cream-of-the-crop](https://github.com/projectM-visualizer/presets-cream-of-the-crop) repository (9,795 presets curated by ISOSCELES), disables the libprojectM logo overlay via the texture search paths API, and verifies end-to-end rendering on the Intel Meteor Lake iGPUs.

The approach is purely additive to the existing `ProjectMEngine` — the engine's render loop, audio feed, shuffle, and track-change blend logic all remain unchanged. Changes are limited to:
1. Replacing preset files in `bot/data/presets/projectm/`
2. Adding a `projectm_set_texture_search_paths()` call during initialization (logo suppression)
3. Adding a one-shot verification script for CI/manual validation

## Architecture

```mermaid
graph TD
    A[presets-cream-of-the-crop repo<br/>9,795 presets] -->|curate_presets.py| B[bot/data/presets/projectm/<br/>200-400 curated .milk files]
    B -->|Docker COPY| C[/app/data/presets/projectm/<br/>in container]
    C -->|projectm_set_preset_path| D[libprojectM 4.x]
    D -->|projectm_opengl_render_frame| E[EGL FBO 1280x720]
    
    F[projectm_set_texture_search_paths<br/>empty list] -->|prevents logo load| D
    
    G[verify_projectm.py] -->|EGL headless + render| E
    G -->|glReadPixels check| H[Non-black frame assertion]
```

### Preset Sourcing Strategy

The `presets-cream-of-the-crop` repo is the official default preset pack for projectM releases since 2022. It contains presets sorted into thematic folders by the community curator ISOSCELES. This is the canonical source — no need to scrape or aggregate from other sources.

**Selection from the 9,795 presets:**
- Run a curation script (`scripts/curate_presets.py`) that filters by quality criteria
- Target 200–400 presets (keeps Docker image under 50MB, provides variety without bloat)
- Organize into 6–8 category subdirectories matching the existing factory preset config names

### Logo Suppression Strategy

**Research findings from the libprojectM 4.x API headers:**

The `projectm_set_texture_search_paths(instance, paths, count)` function (defined in `parameters.h`, available since 4.0.0) configures where libprojectM looks for texture files. The "floating M" logo is rendered from a texture file (`projectM.png` or similar) that the library loads from its texture search paths.

**Suppression approach (dual-layered):**
1. **API-level**: Call `projectm_set_texture_search_paths(handle, &empty_path, 1)` with an empty/nonexistent directory (e.g., `/dev/null` or `/tmp/empty`). This prevents the library from finding any logo texture.
2. **Image-level**: Ensure no `projectM.png` or `texture_projectM.png` file is present in the Docker image at any path libprojectM might scan (typically `/usr/share/projectM/textures/` or similar).

**Note on `projectm_set_toast_message`**: This function does NOT exist in the libprojectM 4.x API. The preset title toast overlay was an older feature. In 4.x, there is no text overlay rendering — the logo is purely texture-based.

### Rendering Verification Strategy

A standalone Python script (`scripts/verify_projectm.py`) that:
1. Creates an EGL headless context (reusing `egl_context.py`)
2. Loads libprojectM, creates instance, configures with real presets
3. Feeds synthetic audio data (sine wave PCM)
4. Renders N frames
5. Reads pixels via `glReadPixels` and asserts non-black output (≥1% non-zero pixels)
6. Reports GL errors via `glGetError()`

This can run in CI (inside the Docker image on a gremlin node) or manually.

## Components and Interfaces

### 1. Curation Script (`scripts/curate_presets.py`)

A build-time script (not shipped in the image) that:
- Clones or reads from `presets-cream-of-the-crop`
- Filters presets by quality criteria:
  - Must contain `bass`, `mid`, `treb`, `bass_att`, `mid_att`, or `treb_att` (audio-reactive)
  - Prefers presets with `per_pixel` sections, `warp_` shaders, or `comp_` shaders
  - Rejects files under 80 lines with no `per_pixel`/`warp_`/`comp_` sections (stub-like)
- Ensures at least 5 distinct established authors (Geiss, Flexi, Rovastar, Zylot, Eo.S., etc.)
- Assigns presets to categories based on source folder names or filename heuristics
- Copies selected presets into `bot/data/presets/projectm/<Category>/`

**Interface:**
```python
# scripts/curate_presets.py
# Usage: python scripts/curate_presets.py --source /path/to/cream-of-the-crop --output bot/data/presets/projectm/
# Options:
#   --max-presets 350    (target count)
#   --min-presets 200    (minimum required)
#   --max-size-mb 50     (total size limit)
```

### 2. ProjectMEngine Changes (`bot/video/visualizer_engines/projectm.py`)

Minimal changes to `_configure_instance()`:
- Add `projectm_set_texture_search_paths` call with empty directory to suppress logo
- Add function signature setup for `projectm_set_texture_search_paths`

```python
# New in _setup_function_signatures():
lib.projectm_set_texture_search_paths.restype = None
lib.projectm_set_texture_search_paths.argtypes = [
    ctypes.c_void_p,           # instance
    ctypes.POINTER(ctypes.c_char_p),  # texture_search_paths (char**)
    ctypes.c_size_t,           # count
]

# New in _configure_instance():
def _suppress_logo(self) -> None:
    """Prevent libprojectM from loading logo texture."""
    empty_path = ctypes.c_char_p(b"/dev/null")
    paths_array = (ctypes.c_char_p * 1)(empty_path)
    self._lib.projectm_set_texture_search_paths(
        self._pm_handle,
        paths_array,
        ctypes.c_size_t(1),
    )
    log.debug("projectM: texture search paths set to /dev/null (logo suppressed)")
```

### 3. Verification Script (`scripts/verify_projectm.py`)

Standalone script for manual/CI rendering verification:

```python
# scripts/verify_projectm.py
# Usage: python scripts/verify_projectm.py [--preset-dir /app/data/presets/projectm] [--frames 30]
# Exit code: 0 if rendering produces non-black frames, 1 otherwise
# Outputs: frame stats (% non-zero pixels), GL errors, preset parse results
```

### 4. Dockerfile Changes

No structural changes to the Dockerfile. The existing `COPY data/presets/projectm/ ./data/presets/projectm/` directive already handles the preset directory. The content of that directory simply changes from stubs to real presets.

Additionally, verify no projectM logo textures exist in the image by inspecting typical paths (`/usr/share/projectM/`, `/usr/share/projectm/`).

## Data Models

### Preset Directory Structure

```
bot/data/presets/projectm/
├── Abstract/          # 30-60 presets — color fields, morphing shapes, organic flow
├── Classic/           # 30-60 presets — traditional Milkdrop looks (Geiss originals)
├── Energy/            # 20-40 presets — high-energy, beat-reactive, aggressive
├── Fluid Motion/      # 20-40 presets — flowing, liquid, smooth animations
├── Geometric/         # 20-40 presets — structured patterns, fractals, symmetry
├── Space/             # 20-40 presets — cosmic, starfield, nebula themes
├── Trippy/            # 30-60 presets — psychedelic, kaleidoscope, intense color
└── Simple/            # 10-20 presets — lighter presets for lower GPU load
```

**Total target**: 200–400 `.milk` files across all categories.

### Category Mapping (Cream-of-the-Crop → HelloDJ)

The cream-of-the-crop repo has its own folder structure. The curation script maps source folders to our categories:

| Source Folder Pattern | Target Category |
|---|---|
| `*abstract*`, `*organic*`, `*blob*` | Abstract |
| `*classic*`, `*geiss*`, `*milkdrop*` | Classic |
| `*energy*`, `*beat*`, `*intense*` | Energy |
| `*fluid*`, `*water*`, `*flow*` | Fluid Motion |
| `*geometric*`, `*fractal*`, `*math*` | Geometric |
| `*space*`, `*cosmic*`, `*star*` | Space |
| `*trippy*`, `*psychedelic*`, `*kaleid*` | Trippy |
| (fallback — simpler presets) | Simple |

### Preset Quality Criteria (Filter Rules)

A preset is included if it passes ALL of:
1. **Audio-reactive**: Contains at least one of `bass`, `mid`, `treb`, `bass_att`, `mid_att`, `treb_att`
2. **Not a stub**: Has ≥80 lines OR contains `per_pixel` OR contains `warp_`/`comp_` shader sections
3. **File format**: Extension is `.milk` (or `.prjm` if also parseable)

A preset is **preferred** (scored higher for selection) if it has:
- `per_pixel` equation section (complex per-pixel transforms)
- `warp_` or `comp_` shader sections (GPU shaders)
- `fDecay` < 0.99 (motion trails)
- More than 5 `per_frame` equations
- `per_pixel` references to `rad` or `ang` (polar coordinates)

### Author Identification

Authors are identified from filenames using the convention `"Author - Preset Name.milk"`. The curation script extracts author names and ensures representation from at least 5 of: Geiss, Flexi, Rovastar, Zylot, Eo.S., martin, cope, Idiot24-7, shifter, Phat, Krash, Unchained.


## Correctness Properties

*A property is a characteristic or behavior that should hold true across all valid executions of a system — essentially, a formal statement about what the system should do. Properties serve as the bridge between human-readable specifications and machine-verifiable correctness guarantees.*

### Property 1: No stub presets in directory

*For any* `.milk` file in the Preset_Directory, it SHALL NOT be classifiable as a stub — meaning it must have ≥80 lines, OR contain a `per_pixel` section, OR contain a `warp_`/`comp_` shader section.

**Validates: Requirements 1.3**

### Property 2: Preset file integrity preservation

*For any* preset file that contains `per_pixel` equation sections, `warp_` shaders, or `comp_` shaders, the file in the Preset_Directory SHALL be byte-identical to the corresponding source file from the cream-of-the-crop collection (no modifications applied during curation).

**Validates: Requirements 1.6**

### Property 3: Audio-reactive variable presence

*For any* `.milk` file in the Preset_Directory, the file content SHALL contain at least one audio-reactive variable reference from the set: `bass`, `mid`, `treb`, `bass_att`, `mid_att`, `treb_att`.

**Validates: Requirements 4.2**

### Property 4: Non-black frame rendering

*For any* valid PCM float buffer (512 samples, values in [-1.0, 1.0]) fed to the ProjectM_Engine, rendering a frame after audio input SHALL produce an FBO with at least 1% of pixels having any RGB channel value greater than zero.

**Validates: Requirements 3.1**

### Property 5: GL error-free rendering

*For any* sequence of audio data feed + render frame calls on an active ProjectM_Engine instance, `glGetError()` SHALL return `GL_NO_ERROR` after each `projectm_opengl_render_frame` call completes.

**Validates: Requirements 3.2**

### Property 6: Shader preset resilience

*For any* preset file containing `warp_` or `comp_` shader sections, loading it via `projectm_load_preset_file` and rendering one frame SHALL NOT crash the process or raise an unhandled exception.

**Validates: Requirements 3.3**

### Property 7: DVD engine default fallback

*For any* guild_id where the stored `visualizer_engine` value is either missing, not present in `VALID_VISUALIZER_ENGINES`, or equals the legacy value `"vgalizer"`, `get_visualizer_engine()` SHALL return `"dvd"`.

**Validates: Requirements 5.3, 5.4**

### Property 8: Track change triggers soft-cut when active

*For any* `TrackMetadata` value, calling `on_track_change()` while the ProjectM_Engine handle is active (non-None) SHALL invoke `projectm_select_random_preset` with `hard_cut=False`.

**Validates: Requirements 7.2**

### Property 9: Track change safe when inactive

*For any* `TrackMetadata` value, calling `on_track_change()` while the ProjectM_Engine handle is None SHALL complete without raising an exception or error.

**Validates: Requirements 7.4**

## Error Handling

### Preset Parse Failures

libprojectM 4.x handles malformed presets internally — it logs a warning and skips to the next preset without crashing. The `ProjectMEngine` does not need additional error handling here. The playlist library's `projectm_playlist_set_retry_count` (if using the playlist API) ensures failed presets are retried up to 500 times before being removed from rotation.

**Our responsibility**: Ensure the curated presets minimize parse failures by pre-filtering during curation. The requirement specifies ≥90% of presets must load successfully.

### Texture Path Errors

If `projectm_set_texture_search_paths` is called with a nonexistent path (e.g., `/dev/null`), libprojectM gracefully handles this — it simply finds no textures, which is the desired behavior for logo suppression. No error is raised.

### Missing Preset Directory

If `PRESET_DIR` doesn't exist at runtime (e.g., misconfigured volume mount):
- `_resolve_preset_path()` already handles this — it returns the base path regardless
- `projectm_set_preset_path` with a nonexistent path causes libprojectM to have no presets loaded
- The engine renders a blank/idle state (the "idle://" preset with a black background if logo textures are suppressed)
- The engine does NOT crash — it simply produces empty/black frames

### GL Context Loss

If the EGL context is lost (device reset, driver error):
- `GPUEngineBase` handles this via its `suspend()`/`resume()` lifecycle
- The ProjectMEngine's `suspend()` calls `_destroy_projectm()` before the base class destroys the EGL context
- On `resume()`, the full initialization chain re-runs (`_on_gl_ready` → `_load_library` → `_create_instance` → `_configure_instance`)

### Symbol Resolution Failures

If the Ubuntu-packaged libprojectM doesn't expose `projectm_set_texture_search_paths` (unlikely for 4.x but possible for older packages):
- Wrap the call in a try/except `AttributeError`
- Log a warning and continue without logo suppression
- The engine still functions correctly, just with the logo visible

## Testing Strategy

### Property-Based Tests (Hypothesis)

The project already uses Hypothesis for PBT. Property tests will validate:

- **Properties 1, 3** (preset file quality): Generate random indices into the preset file list, check stub criteria and audio variable presence. Run with `@settings(max_examples=200)` to cover the full set.
- **Property 2** (file integrity): Requires access to the source repo at test time — better as a one-time curation validation step (unit test with fixed data).
- **Properties 4, 5, 6** (rendering): Require a GPU context — these are integration/hardware tests, not pure PBT. However, Property 4 can use Hypothesis to generate random PCM buffers.
- **Property 7** (DVD default): Pure function test. Generate random guild IDs and random invalid engine strings. `@settings(max_examples=100)`.
- **Properties 8, 9** (track change): Test the `on_track_change` method with randomly generated `TrackMetadata` values and mock libprojectM calls. `@settings(max_examples=100)`.

**PBT Library**: `hypothesis` (already in project dependencies)
**Minimum iterations**: 100 per property test

**Tag format**: `# Feature: projectm-preset-overhaul, Property N: <property text>`

### Unit Tests

- `test_projectm_logo_suppression`: Verify `projectm_set_texture_search_paths` is called during `_configure_instance` with a non-logo path
- `test_projectm_toast_symbol_check`: Verify graceful handling when `projectm_set_toast_message` symbol doesn't exist
- `test_default_visualizer_engine_constant`: Assert `DEFAULT_VISUALIZER_ENGINE == "dvd"`
- `test_dvd_engine_is_client_side`: Assert `DVDEngine().is_client_side is True`
- `test_shuffle_enabled_on_configure`: Verify `projectm_set_shuffle_enabled(handle, True)` called

### Integration Tests (Require GPU / Docker)

- `test_render_non_black_frame`: Full EGL + libprojectM render pipeline, assert non-black output
- `test_preset_parse_rate`: Load all presets one by one, count successes, assert ≥90%
- `test_60min_stability`: Long-running render with auto-advance (manual / CI nightly)
- `test_30fps_throughput`: Sustained render loop, measure average FPS over 5 seconds
- `test_no_logo_in_output`: Render 100 frames, inspect for logo-shaped pixel patterns

### Smoke Tests (Build Verification)

- `test_preset_count`: Assert ≥200 `.milk` files in preset directory
- `test_category_count`: Assert ≥5 subdirectories with ≥10 presets each
- `test_author_diversity`: Assert ≥5 distinct recognized authors in filenames
- `test_preset_size_limit`: Assert total directory size < 50MB
- `test_file_permissions`: Assert all preset files are readable (permissions ≥644)
- `test_no_logo_texture_in_image`: Assert no `projectM.png` in Docker image

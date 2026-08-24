# Implementation Plan: projectM Preset Overhaul

## Overview

Replace all 56 AI-generated stub `.milk` presets with 200–400 curated community presets from the cream-of-the-crop repo, suppress the libprojectM logo overlay via texture search path manipulation, and add a rendering verification script. The curation script and engine changes proceed in parallel, converging for integration testing.

## Tasks

- [x] 1. Create preset curation script
  - [x] 1.1 Implement `scripts/curate_presets.py` with CLI interface
    - Create the script with argparse handling `--source`, `--output`, `--max-presets`, `--min-presets`, `--max-size-mb` options
    - Implement Git clone logic to fetch `projectM-visualizer/presets-cream-of-the-crop` if `--source` not provided
    - Implement preset file discovery (recursive `.milk` file search)
    - Implement quality filter: audio-reactive variable check (`bass`, `mid`, `treb`, `bass_att`, `mid_att`, `treb_att`)
    - Implement stub rejection: skip files with <80 lines AND no `per_pixel` AND no `warp_`/`comp_` sections
    - Implement scoring: prefer presets with `per_pixel`, warp/comp shaders, `fDecay < 0.99`, >5 `per_frame` equations, polar refs (`rad`/`ang`)
    - Implement author extraction from filename convention `"Author - Preset Name.milk"`
    - Ensure minimum 5 distinct authors from recognized set (Geiss, Flexi, Rovastar, Zylot, Eo.S., etc.)
    - Implement category mapping from source folder names to target categories (Abstract, Classic, Energy, Fluid Motion, Geometric, Space, Trippy, Simple)
    - Copy selected presets unmodified to output directory organized by category
    - Validate minimum 200 presets selected, total size under 50MB
    - Print summary report (preset count per category, author breakdown, total size)
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 4.1, 4.2, 4.3, 4.4, 6.2_

  - [ ]* 1.2 Write property tests for preset curation output (Properties 1, 2, 3)
    - **Property 1: No stub presets in directory** — For any `.milk` file in Preset_Directory, it SHALL NOT be classifiable as a stub (≥80 lines OR contains `per_pixel` OR contains `warp_`/`comp_` shader section)
    - **Property 2: Preset file integrity preservation** — For any preset with `per_pixel`/`warp_`/`comp_` sections, the file SHALL be byte-identical to the source
    - **Property 3: Audio-reactive variable presence** — For any `.milk` file in Preset_Directory, content SHALL contain at least one audio-reactive variable (`bass`, `mid`, `treb`, `bass_att`, `mid_att`, `treb_att`)
    - **Validates: Requirements 1.3, 1.6, 4.2**

- [x] 2. Populate preset directory with curated presets
  - [x] 2.1 Run curation script to replace stub presets
    - Remove all existing stub preset files from `bot/data/presets/projectm/` subdirectories
    - Execute `scripts/curate_presets.py` targeting `bot/data/presets/projectm/` with `--max-presets 350 --min-presets 200`
    - Verify output: ≥200 `.milk` files, ≥5 category subdirectories with ≥10 presets each, ≥5 distinct authors
    - Verify total directory size < 50MB
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 6.1, 6.2_

  - [ ]* 2.2 Write unit tests for preset directory smoke checks
    - Test preset count ≥200 `.milk` files
    - Test ≥5 subdirectories with ≥10 presets each
    - Test ≥5 distinct recognized authors in filenames
    - Test total directory size < 50MB
    - Test all preset files readable (permissions ≥644)
    - _Requirements: 1.2, 1.4, 1.5, 6.2, 6.4_

- [x] 3. Add logo suppression to ProjectMEngine
  - [x] 3.1 Add `projectm_set_texture_search_paths` ctypes signature to `_setup_function_signatures()`
    - Add restype (None) and argtypes (`c_void_p`, `POINTER(c_char_p)`, `c_size_t`) for `projectm_set_texture_search_paths`
    - _Requirements: 2.2_

  - [x] 3.2 Implement `_suppress_logo()` method and integrate into `_configure_instance()`
    - Create `_suppress_logo()` method that calls `projectm_set_texture_search_paths(handle, [b"/dev/null"], 1)`
    - Wrap in try/except `AttributeError` for graceful fallback if symbol not found
    - Call `_suppress_logo()` from `_configure_instance()` after other configuration
    - Log debug message on success, warning on fallback
    - _Requirements: 2.1, 2.2, 2.4_

  - [x] 3.3 Add `projectm_set_toast_message` check with graceful fallback
    - Check if `projectm_set_toast_message` symbol exists on the loaded library
    - If present, call with empty string to suppress preset title toast
    - If absent (expected for 4.x), log debug and continue without error
    - _Requirements: 2.3_

  - [ ]* 3.4 Write unit tests for logo suppression and toast handling
    - Test `projectm_set_texture_search_paths` is called during `_configure_instance` with `/dev/null` path
    - Test graceful handling when `projectm_set_texture_search_paths` symbol doesn't exist (AttributeError)
    - Test graceful handling when `projectm_set_toast_message` symbol doesn't exist
    - Test `projectm_set_shuffle_enabled(handle, True)` is called during configuration
    - _Requirements: 2.1, 2.2, 2.3, 7.1_

- [x] 4. Checkpoint — Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 5. DVD engine default and track-change validation
  - [x] 5.1 Verify DVD engine default preservation (no code changes needed)
    - Confirm `DEFAULT_VISUALIZER_ENGINE == "dvd"` in `guild_settings.py`
    - Confirm `DVDEngine.is_client_side` returns `True`
    - Confirm `get_visualizer_engine()` returns `"dvd"` for missing/invalid values
    - _Requirements: 5.1, 5.2, 5.3, 5.4_

  - [ ]* 5.2 Write property tests for DVD default and track-change behavior (Properties 7, 8, 9)
    - **Property 7: DVD engine default fallback** — For any guild_id where stored `visualizer_engine` is missing, invalid, or `"vgalizer"`, `get_visualizer_engine()` SHALL return `"dvd"`
    - **Property 8: Track change triggers soft-cut when active** — For any `TrackMetadata`, calling `on_track_change()` with active handle SHALL invoke `projectm_select_random_preset` with `hard_cut=False`
    - **Property 9: Track change safe when inactive** — For any `TrackMetadata`, calling `on_track_change()` with handle=None SHALL complete without raising
    - **Validates: Requirements 5.3, 5.4, 7.2, 7.4**

- [x] 6. Create rendering verification script
  - [x] 6.1 Implement `scripts/verify_projectm.py`
    - Create standalone script with argparse (`--preset-dir`, `--frames`, `--verbose`)
    - Reuse EGL headless context creation pattern from existing `egl_context.py`
    - Load libprojectM, create instance, configure with preset directory
    - Generate synthetic PCM audio data (sine wave, 512 float samples)
    - Feed audio via `projectm_pcm_add_float` and render N frames via `projectm_opengl_render_frame`
    - Read pixels via `glReadPixels` and assert ≥1% non-zero pixels (non-black output)
    - Check `glGetError()` returns `GL_NO_ERROR` after each render call
    - Report: frame stats, GL errors, preset load results
    - Exit code 0 if rendering succeeds, 1 otherwise
    - _Requirements: 3.1, 3.2, 3.4, 3.5_

  - [ ]* 6.2 Write property tests for rendering verification (Properties 4, 5, 6)
    - **Property 4: Non-black frame rendering** — For any valid PCM float buffer (512 samples, [-1.0, 1.0]), rendering SHALL produce FBO with ≥1% non-zero pixels
    - **Property 5: GL error-free rendering** — For any audio feed + render sequence, `glGetError()` SHALL return `GL_NO_ERROR`
    - **Property 6: Shader preset resilience** — For any preset with `warp_`/`comp_` shaders, loading and rendering SHALL NOT crash
    - **Validates: Requirements 3.1, 3.2, 3.3**
    - _Note: These require GPU context — mark as integration tests that run in Docker/CI only_

- [x] 7. Docker image logo texture exclusion
  - [x] 7.1 Verify and document no projectM logo texture in Docker image paths
    - Check that no `projectM.png`, `texture_projectM.png`, or equivalent branding assets exist in `/usr/share/projectM/`, `/usr/share/projectm/`, or any libprojectM texture directory in the Docker image
    - Add a comment to Dockerfile or CI check confirming this verification
    - If logo texture exists in base image paths, add an explicit `RUN rm -f` in Dockerfile
    - _Requirements: 2.5, 6.1_

  - [ ]* 7.2 Write unit test for no logo texture in image
    - Test that `projectM.png` and `texture_projectM.png` do not exist in common libprojectM texture paths
    - _Requirements: 2.5_

- [x] 8. Update factory_presets.py category references if needed
  - [x] 8.1 Verify factory preset category names match actual directory names
    - Confirm `factory_presets.py` references (Abstract, Classic, Energy, Fluid Motion, Geometric, Space, Trippy, Simple) match the subdirectory names created by the curation script
    - Update any mismatched category names if the curation script produces different folder names
    - _Requirements: 1.5, 4.4_

- [x] 9. Final checkpoint — Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- Checkpoints ensure incremental validation
- Property tests validate universal correctness properties from the design document
- Unit tests validate specific examples and edge cases
- The curation script (task 1) and engine changes (task 3) are independent and can proceed in parallel
- Rendering verification (task 6) requires both presets and engine changes to be complete
- Properties 4, 5, 6 require a GPU context and should be treated as integration tests (CI/Docker only)

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "3.1"] },
    { "id": 1, "tasks": ["1.2", "3.2", "3.3"] },
    { "id": 2, "tasks": ["2.1", "3.4", "5.1"] },
    { "id": 3, "tasks": ["2.2", "5.2", "8.1"] },
    { "id": 4, "tasks": ["6.1", "7.1"] },
    { "id": 5, "tasks": ["6.2", "7.2"] }
  ]
}
```

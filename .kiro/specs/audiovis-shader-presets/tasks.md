# Implementation Plan: AudioVis Shader Presets

## Overview

Expand the AudioVis engine with 12 new GLSL fragment shader presets across 4 aesthetic categories (psychedelic, aggressive, ambient, retro). Replace the hardcoded STYLES tuple with a data-driven registry, add fallback/error handling, and update the Discord command autocomplete.

## Tasks

- [x] 1. Replace STYLES tuple with STYLE_REGISTRY dict in audiovis.py — Replace the `STYLES = ("bars", "waveform", "waterfall", "circular")` tuple with a `STYLE_REGISTRY` dict mapping each style name to `{"category": str, "file": str}`. Add `get_valid_styles()` and `get_styles_by_category()` helper functions. Update `__init__` to validate style against `STYLE_REGISTRY.keys()` instead of `STYLES`. Update `_on_gl_ready` to load shader via `STYLE_REGISTRY[self._style]["file"]` instead of `f"audiovis_{self._style}.glsl"`. (Req 11 AC 1-2)
- [x] 2. Add shader compilation fallback logic — Wrap the `_compile_program` call in `_on_gl_ready` with try/except. On `RuntimeError` (compilation failure), log the error and fall back to "bars" shader. Before compilation, check that the shader file exists; if missing, log warning and fall back to "bars". Add `_slow_frame_count` field and 3-frame performance warning in `render_frames()`. (Req 10 AC 1-3, Req 9 AC 3)
- [x] 3. Update config_schema.py with all 16 style choices — Add all style names from `STYLE_REGISTRY` to the `audiovis.style.choices` validation list in `bot/video/visualizer_engines/config_schema.py` so that `/visualizer config audiovis style {name}` accepts the new presets. (Req 1 AC 2)
- [x] 4. Update visualizer cog autocomplete to show categories — Modify the style value autocomplete in `bot/cogs/visualizer.py` to import `get_styles_by_category()` from audiovis and present choices as `[category] name` labels, filtered by the user's current input. (Req 2 AC 2, Req 1 AC 3)
- [x] 5. Write audiovis_kaleidoscope.glsl — Psychedelic mirrored fractal geometry with polar UV folding. Sector count N increases on iBeat (6→10). Layered noise colored with cosine palette. iBandEnergy[0..2] drives color cycling. iTime rotates pattern. (Req 3 AC 1, Req 7 AC 1-3, Req 8 AC 1)
- [x] 6. Write audiovis_plasma.glsl — Multi-frequency sinusoidal color mixing. Frequencies modulated by iBandEnergy[0..2]. Beat triggers color inversion flash blended over 6 frames. Continuous flow from iTime. (Req 3 AC 2, Req 7 AC 1-3, Req 8 AC 1)
- [x] 7. Write audiovis_fractal.glsl — Julia set with continuous zoom via exp(-iTime*0.3). Max 128 iterations. Julia constant c orbits slowly. Beat accelerates zoom. Color via smooth iteration count + cosine palette driven by iBandEnergy[3..5]. (Req 3 AC 3, Req 7 AC 1-3, Req 8 AC 1, Req 9 AC 2)
- [x] 8. Write audiovis_hypnotic.glsl — Concentric rotating rings. Each ring rotates at speed from iBandEnergy[ring%7]. Ring thickness oscillates with sin(iTime+ring). Beat expands all radii. Moiré interference. HSV color shift. (Req 3 AC 4, Req 7 AC 1-3, Req 8 AC 1)
- [x] 9. Write audiovis_glitch.glsl — Block displacement (8x8 grid), RGB channel splitting (offset from iBandEnergy[0]), scanline noise flicker. Beat multiplies all distortion by 4x. Baseline jitter from iTime between beats. (Req 4 AC 1, Req 7 AC 1-3, Req 8 AC 1)
- [x] 10. Write audiovis_storm.glsl — FBM noise arcs (4-8 instances). Beat spawns new arc cluster. iBandEnergy[5..6] controls FBM octave count (2-5). Exponential glow falloff from arc distance. Dark cloud background. Arcs fade over time. (Req 4 AC 2, Req 7 AC 1-3, Req 8 AC 1)
- [x] 11. Write audiovis_shatter.glsl — Voronoi tessellation with 20 seed points. Beat injects new seeds radiating from center. Fracture decay proportional to 1/iBPM. Cell borders rendered as bright cracks. Cells offset by fracture intensity. Seeds drift slowly with iTime. (Req 4 AC 3, Req 7 AC 1-3, Req 8 AC 1)
- [x] 12. Write audiovis_aurora.glsl — 3-5 layered FBM noise curtains. Amplitude from iBandEnergy[2..4]. Slow vertical drift via iTime. Green→cyan→purple color spectrum. Beat triggers brightness surge. Additive layer blending. (Req 5 AC 1, Req 7 AC 1-3, Req 8 AC 1)
- [x] 13. Write audiovis_nebula.glsl — 4-octave FBM volumetric fog with slow rotation. FFT texture drives local density. Deep blues/purples with star highlights. Beat triggers brightness surge. iBandEnergy[1..3] modulates movement speed. (Req 5 AC 2, Req 7 AC 1-3, Req 8 AC 1, Req 9 AC 2)
- [x] 14. Write audiovis_ocean.glsl — Caustic light patterns from 3 rotated sine grids. iBandEnergy[0..1] controls wave frequency. Beat triggers circular ripple from center. Dark blue depth gradient. Continuous sway from iTime UV offsets. (Req 5 AC 3, Req 7 AC 1-3, Req 8 AC 1)
- [x] 15. Write audiovis_fireflies.glsl — Distance-field particle glow (max 32 particles). Count scales with audio energy sum. Beat scatters particles outward. Brownian noise drift for continuous motion. Warm gold/amber/green palette via per-particle hash. (Req 5 AC 4, Req 7 AC 1-3, Req 8 AC 1, Req 9 AC 2)
- [x] 16. Write audiovis_synthwave.glsl — Perspective grid floor scrolling forward via iTime. 1D noise mountain silhouette at horizon with height from iBandEnergy[0..1]. Sunset gradient (orange→magenta→purple). Beat triggers horizon flash. Sun circle pulsing with bass. (Req 6 AC 1, Req 7 AC 1-3, Req 8 AC 1)
- [x] 17. Write audiovis_retrowave.glsl — VHS artifacts (chromatic aberration, scanlines, tape warp) over neon SDF shapes. Beat multiplies all distortion by 3x. iBandEnergy[2..4] controls shape pulsing. Continuous tape-roll from iTime. Hot pink/blue/purple neon palette. (Req 6 AC 2, Req 7 AC 1-3, Req 8 AC 1)
- [x] 18. Write audiovis_cyber.glsl — Polar→tube UV wireframe tunnel. Forward speed from iBandEnergy[0]. Edge count from iBandEnergy[3..4]. Beat triggers geometry morph (hex→octagon→circle). Neon edge glow cycling cyan/magenta/yellow. Continuous scroll from iTime. (Req 6 AC 3, Req 7 AC 1-3, Req 8 AC 1)
- [x] 19. Build, deploy, and verify all shaders compile on target GPU — Build bot image, deploy to cluster, verify all 16 styles compile without errors on the Intel Meteor Lake iGPU (Mesa iris). Check logs for any shader compilation warnings. Test 3-4 styles via the Activity UI to confirm non-black visible output. (Req 9 AC 1, Req 10 AC 1-3)

## Task Dependency Graph

```json
{
  "waves": [
    {"tasks": [1, 3]},
    {"tasks": [2, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18], "dependsOn": [1, 3]},
    {"tasks": [19], "dependsOn": [2, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18]}
  ]
}
```

## Notes

Tasks 1-4 are infrastructure (Python changes). Tasks 5-18 are independent shader files (can be written in parallel). Task 19 is the final integration test requiring all prior tasks.

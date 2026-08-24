# Design Document — AudioVis Shader Presets

## Overview

Expand the AudioVis engine from 4 hardcoded styles to 16 total styles (4 existing + 12 new) organized into aesthetic categories. The implementation is data-driven: a style registry maps names to categories and shader files, and the rendering pipeline remains unchanged — every shader uses the same uniform interface.

**Target hardware:** Intel Meteor Lake iGPU (Mesa iris), 1280×720 @ 30fps via EGL headless context.

---

## 1. Style Registry (Req 11)

Replace the current `STYLES` tuple in `audiovis.py` with a data-driven dict registry:

```python
STYLE_REGISTRY: dict[str, dict[str, str]] = {
    # Classic (existing)
    "bars":         {"category": "classic",     "file": "audiovis_bars.glsl"},
    "waveform":     {"category": "classic",     "file": "audiovis_waveform.glsl"},
    "waterfall":    {"category": "classic",     "file": "audiovis_waterfall.glsl"},
    "circular":     {"category": "classic",     "file": "audiovis_circular.glsl"},
    # Psychedelic (Req 3)
    "kaleidoscope": {"category": "psychedelic", "file": "audiovis_kaleidoscope.glsl"},
    "plasma":       {"category": "psychedelic", "file": "audiovis_plasma.glsl"},
    "fractal":      {"category": "psychedelic", "file": "audiovis_fractal.glsl"},
    "hypnotic":     {"category": "psychedelic", "file": "audiovis_hypnotic.glsl"},
    # Aggressive (Req 4)
    "glitch":       {"category": "aggressive",  "file": "audiovis_glitch.glsl"},
    "storm":        {"category": "aggressive",  "file": "audiovis_storm.glsl"},
    "shatter":      {"category": "aggressive",  "file": "audiovis_shatter.glsl"},
    # Ambient (Req 5)
    "aurora":       {"category": "ambient",     "file": "audiovis_aurora.glsl"},
    "nebula":       {"category": "ambient",     "file": "audiovis_nebula.glsl"},
    "ocean":        {"category": "ambient",     "file": "audiovis_ocean.glsl"},
    "fireflies":    {"category": "ambient",     "file": "audiovis_fireflies.glsl"},
    # Retro (Req 6)
    "synthwave":    {"category": "retro",       "file": "audiovis_synthwave.glsl"},
    "retrowave":    {"category": "retro",       "file": "audiovis_retrowave.glsl"},
    "cyber":        {"category": "retro",       "file": "audiovis_cyber.glsl"},
}
```

### Design decisions

- **Req 11 AC 1**: Each entry maps style name → `{category, file}`. Adding a new preset requires only a GLSL file + one dict entry.
- **Req 11 AC 2**: Uniform upload logic, VAO creation, FFT texture upload, and pixel readback remain untouched. The only variable is which `.glsl` file is loaded in `_on_gl_ready`.
- **Req 11 AC 3**: The registry is exposed as a module-level constant. The cog's autocomplete reads it to present styles grouped by category.
- **Req 2 AC 1–3**: Five categories (classic, psychedelic, aggressive, ambient, retro). Each preset belongs to exactly one category.

### Helper functions

```python
def get_valid_styles() -> list[str]:
    """Return all style names from the registry."""
    return list(STYLE_REGISTRY.keys())

def get_styles_by_category() -> dict[str, list[str]]:
    """Return styles grouped by category for autocomplete display."""
    grouped: dict[str, list[str]] = {}
    for style, meta in STYLE_REGISTRY.items():
        grouped.setdefault(meta["category"], []).append(style)
    return grouped
```

---

## 2. Shader Architecture (Req 1, Req 8, Req 11)

### Uniform interface (unchanged)

All 16 fragment shaders receive the same uniforms — no changes to `_cache_uniform_locations` or `_render_gl_frame`:

```glsl
uniform float     iTime;            // Wall-clock elapsed (seconds)
uniform vec2      iResolution;      // Viewport size (1280, 720)
uniform float     iBeat;            // 0.0–1.0 decaying beat pulse
uniform float     iBPM;             // Estimated BPM (60–200)
uniform float     iBandEnergy[7];   // 7-band RMS energy
uniform sampler1D iFFT;             // 512-bin FFT magnitude
uniform int       iFFTBins;         // Display bin count
uniform float     iGlowIntensity;   // Glow amount (0–1)
uniform float     iBgOpacity;       // Background opacity (0–1)
```

### Self-contained shaders (no #include)

OpenGL 3.3 Core has no `#include` directive. Each shader file is **fully self-contained** with its own utility functions inlined at the top. Common patterns duplicated per-shader:

| Utility | Used by |
|---------|---------|
| `hash(vec2)` / `noise(vec2)` | All except bars/waveform |
| `fbm(vec2, octaves)` | nebula, aurora, storm |
| `rotate2d(angle)` | kaleidoscope, hypnotic, cyber, synthwave |
| `sdf_circle(p, r)` | fireflies, circular |
| `voronoi(uv)` | shatter, ocean |
| `palette(t)` (cosine palette) | fractal, plasma, hypnotic |

This is intentional — duplicating ~20 lines of noise functions per shader avoids build-system complexity, keeps each file independently testable, and has zero runtime cost.

### Vertex shader (shared)

All audiovis styles share `audiovis_vert.glsl` (unchanged):

```glsl
#version 330 core
layout(location = 0) in vec2 aPos;
out vec2 vUV;
void main() {
    vUV = aPos * 0.5 + 0.5;
    gl_Position = vec4(aPos, 0.0, 1.0);
}
```

---

## 3. Shader Design Patterns per Category

### 3.1 Psychedelic (Req 3)

#### kaleidoscope (Req 3 AC 1)
**Algorithm:** UV folding + polar coordinate rotation.
- Convert UV to polar `(r, θ)`.
- Fold `θ` into N sectors: `θ = mod(θ, TAU/N)` then mirror.
- N (sector count) increases on beat: `N = 6 + int(iBeat * 4.0)`.
- Inner pattern: layered noise fields colored with cosine palette.
- `iBandEnergy[0..2]` drives color cycling speed.
- `iTime` rotates the entire pattern.
- Beat triggers symmetry fold increase (structural change per Req 7 AC 1).

#### plasma (Req 3 AC 2)
**Algorithm:** Multi-frequency sinusoidal color mixing (classic plasma).
- `color.r = sin(uv.x * f1 + iTime) + sin(uv.y * f2 + iTime * 0.7)`
- `color.g = sin(uv.x * f3 - iTime * 1.3) + sin(dist * f4 + iTime)`
- Frequencies `f1–f4` modulated by `iBandEnergy[0..2]` (bass drives tunnel depth pulsing).
- Beat triggers color inversion flash: `color = 1.0 - color` blended over ~6 frames.
- Continuous flow via `iTime` multiplication on sin arguments (Req 8 AC 1).

#### fractal (Req 3 AC 3)
**Algorithm:** Julia set iteration with continuous zoom.
- Zoom factor: `exp(-iTime * 0.3)` — continuous zoom driven by time.
- Julia constant `c` orbits slowly: `c = vec2(sin(iTime*0.1)*0.7, cos(iTime*0.13)*0.5)`.
- Max 128 iterations (Req 9 AC 2).
- Beat triggers zoom acceleration: `zoom *= 1.0 + iBeat * 2.0`.
- Color from iteration count via cosine palette; palette rotation speed maps to `iBandEnergy[3..5]`.
- Uses smooth iteration count for anti-banding.

#### hypnotic (Req 3 AC 4)
**Algorithm:** Concentric ring modulation with wave distortion.
- Distance from center → ring index.
- Each ring rotates at speed proportional to `iBandEnergy[ring_index % 7]`.
- Ring thickness oscillates with `sin(iTime + ring_index)`.
- Beat causes ring expansion burst: all radii shift outward by `iBeat * 0.3`.
- Interference pattern between rings creates moiré illusion.
- Colors shift through HSV spectrum driven by ring depth + time.

### 3.2 Aggressive (Req 4)

#### glitch (Req 4 AC 1)
**Algorithm:** Block displacement + RGB channel splitting + scanline noise.
- Divide screen into 8×8 blocks.
- Per-block hash determines displacement offset (horizontal shift, vertical flip).
- RGB channels sampled at different UV offsets (chromatic aberration): `offset = iBandEnergy[0] * 0.05`.
- Scanline noise: horizontal stripes flicker based on `hash(floor(uv.y * 100.0) + iTime)`.
- Beat triggers maximum distortion: all parameters multiplied by `1.0 + iBeat * 3.0`.
- Between beats, distortion decays to a subtle baseline jitter (Req 8 AC 1).

#### storm (Req 4 AC 2)
**Algorithm:** Branching noise arcs (lightning simulation).
- Arc path: FBM noise along Y with X as seed: `x_offset = fbm(vec2(y * 4.0, iTime + seed))`.
- Multiple arc instances (4–8) with random origin points.
- Beat spawns new arc cluster from random origin (Req 7 AC 1).
- `iBandEnergy[5..6]` (presence/brilliance) controls branching complexity (FBM octave count: 2–5).
- Glow: distance-to-arc → exponential falloff brightness.
- Background: dark storm clouds via low-frequency noise.
- Arcs fade over time: `alpha *= exp(-age * 2.0)`.

#### shatter (Req 4 AC 3)
**Algorithm:** Voronoi tessellation simulating glass fractures.
- Voronoi cell calculation with ~20 seed points.
- Beat triggers new fracture event: injects new seed points radiating from center.
- Fracture pattern decays proportional to `1.0 / iBPM` (longer decay at slower tempos).
- Cells offset from original UV by distance-from-center × fracture intensity.
- Edge detection (cell borders) rendered as bright white cracks.
- Within cells: shifted/refracted copy of a gradient background.
- Continuous evolution via slowly drifting seed points (Req 8 AC 1).

### 3.3 Ambient (Req 5)

#### aurora (Req 5 AC 1)
**Algorithm:** Layered noise curtains (northern lights).
- 3–5 vertical curtain layers, each a horizontal band of FBM noise.
- Curtain wave amplitude driven by `iBandEnergy[2..4]` (low-mid through upper-mid).
- Slow vertical drift via `iTime * 0.1`.
- Colors: green → cyan → purple spectrum, color shift responds to sum of all band energies.
- Additive blending between layers.
- Beat triggers brightness surge + slight horizontal wave acceleration.
- Base motion from `iTime` ensures continuous animation even without audio (Req 8 AC 1).

#### nebula (Req 5 AC 2)
**Algorithm:** Fractal Brownian Motion (FBM) volumetric fog.
- 4-octave FBM noise in 2D, sampled at multiple scales.
- FFT texture drives local cloud density: `density += texture(iFFT, uv.x).r * 0.3`.
- Slow rotation: UV rotated by `iTime * 0.02`.
- Color: deep blues/purples with bright star-like point highlights.
- Beat triggers gentle brightness surge: `brightness *= 1.0 + iBeat * 0.4`.
- `iBandEnergy[1..3]` modulates cloud movement speed.
- FBM octaves capped at 4 for performance (Req 9 AC 2).

#### ocean (Req 5 AC 3)
**Algorithm:** Caustic light refraction (underwater light patterns).
- Caustic pattern: sum of 3 rotated sine-wave grids at different frequencies.
- `caustic = max(sin(uv.x*f + iTime) + sin(uv.y*f*1.3 + iTime*0.7), 0.0)` — layered.
- `iBandEnergy[0..1]` (sub-bass/bass) controls wave height via frequency modulation.
- Beat triggers ripple expansion: circular wave from center, decays over 1 second.
- Background: dark blue gradient with depth-based brightness attenuation.
- Gentle continuous sway via time-offset UV coordinates (Req 8 AC 1).

#### fireflies (Req 5 AC 4)
**Algorithm:** Distance-field particle glow (soft luminous points).
- N particle positions computed per-frame: `pos[i] = vec2(hash_x(i, iTime), hash_y(i, iTime))`.
- N (visible count) scales with overall audio energy: `N = 8 + int(energy_sum * 20.0)`.
- Each particle: `glow = 0.01 / distance(uv, pos[i])` — inverse distance glow.
- Beat causes particles to scatter outward from center (radial velocity burst).
- Particle motion: Brownian drift via noise-based velocity (continuous, Req 8 AC 1).
- Warm color palette (gold, amber, soft green) via per-particle hash.
- Loop capped at 32 particles max for performance.

### 3.4 Retro (Req 6)

#### synthwave (Req 6 AC 1)
**Algorithm:** Perspective grid + sunset horizon + wireframe mountains.
- Floor grid: perspective-projected horizontal/vertical lines receding to horizon.
- Grid scroll: `z_offset += iTime * 0.5` (forward motion).
- Mountains: 1D noise silhouette at horizon line, height oscillates with `iBandEnergy[0..1]`.
- Sunset gradient: horizontal bands of orange → magenta → purple → dark.
- Beat triggers horizon flash: white glow at vanishing point, decays over 6 frames.
- Sun: large circle at horizon, pulsing size with bass energy.
- `iBandEnergy[0..1]` drives mountain height variation.

#### retrowave (Req 6 AC 2)
**Algorithm:** VHS artifacts + neon geometric shapes.
- Base layer: rotating/pulsing neon shapes (triangles, circles) — simple SDF primitives.
- VHS overlay: chromatic aberration (RGB offset), horizontal scanlines, tape warping (sinusoidal UV distortion).
- Beat intensifies all distortion parameters: `distortion *= 1.0 + iBeat * 2.0`.
- `iBandEnergy[2..4]` controls neon shape pulse intensity.
- Continuous tape-roll effect via `iTime` driving scanline position.
- Color: hot pink, electric blue, neon purple palette.

#### cyber (Req 6 AC 3)
**Algorithm:** Forward-moving neon wireframe tunnel.
- Tunnel: polar coordinates → tube UV mapping: `tube_uv = vec2(atan(uv.y, uv.x), 1.0/length(uv))`.
- Forward motion: `tube_uv.y += iTime * speed` where `speed` responds to `iBandEnergy[0]`.
- Wireframe grid on tunnel surface with neon edge glow.
- Shape complexity: number of longitudinal edges responds to `iBandEnergy[3..4]`.
- Beat triggers tunnel geometry morph: cross-section transitions (hex → octagon → circle).
- Neon edge coloring cycles through cyan/magenta/yellow.
- Continuous forward scroll even without audio (Req 8 AC 1).

---

## 4. Performance Guardrails (Req 9)

| Constraint | Value | Rationale |
|-----------|-------|-----------|
| Max loop iterations | 128 | Bounds fractal/raymarch worst-case. Applied to `fractal` Julia set loop. |
| FBM octaves | 4 max | Caps `nebula`, `aurora`, `storm` FBM noise. |
| Firefly particle count | 32 max | Loop over distance-field particles. |
| Voronoi seed points | 20 max | `shatter` cell computation. |
| Texture-dependent branching | Avoided | FFT texture reads are unconditional; branching only on uniform values. |
| Noise function | Value noise / simplex approx | Simple `fract(sin(dot(p, ...)))` hash — avoids expensive Perlin with gradient tables. |
| Target frame time | <20ms | On Intel Meteor Lake iGPU at 1280×720 (leaves 13.3ms headroom in 33.3ms budget). |

### Performance monitoring (Req 9 AC 3)

```python
# In _render_gl_frame or render_frames loop:
elapsed = time.monotonic() - t0
if elapsed > FRAME_BUDGET:
    self._slow_frame_count += 1
    if self._slow_frame_count >= 3:
        log.warning(
            "AudioVis '%s': %d consecutive slow frames (%.1fms avg)",
            self._style, self._slow_frame_count, elapsed * 1000,
        )
else:
    self._slow_frame_count = 0
```

---

## 5. Error Handling / Fallback (Req 10)

### Shader file missing (Req 10 AC 2–3)

In `_on_gl_ready`, before attempting compilation:

```python
frag_file = STYLE_REGISTRY.get(self._style, {}).get("file")
frag_path = SHADER_DIR / frag_file if frag_file else None

if not frag_path or not frag_path.exists():
    log.warning(
        "Shader file missing for style '%s' (expected %s), falling back to 'bars'",
        self._style, frag_file,
    )
    self._style = "bars"
    frag_file = STYLE_REGISTRY["bars"]["file"]
    frag_path = SHADER_DIR / frag_file
```

### Compilation failure (Req 10 AC 1)

Wrap `_compile_program` in try/except:

```python
try:
    self._shader_program = self._compile_program(gl, vert_src, frag_src)
except RuntimeError as exc:
    log.error(
        "Shader compilation failed for style '%s': %s — falling back to 'bars'",
        self._style, exc,
    )
    self._style = "bars"
    frag_src = self._load_shader_source(STYLE_REGISTRY["bars"]["file"])
    self._shader_program = self._compile_program(gl, vert_src, frag_src)
```

Both paths converge on `"bars"` as the known-good fallback, ensuring the visualizer always renders something.

---

## 6. Command Interface (Req 1 AC 2–3, Req 2 AC 2)

### Existing command (unchanged)

```
/visualizer config audiovis style {name}
```

The command flow is:
1. User types `/visualizer config audiovis style` → Discord triggers `_setting_autocomplete`.
2. User types a value → Discord triggers value autocomplete (currently not implemented for free-text choices).
3. `guild_settings.set_visualizer_config(guild_id, "audiovis", "style", value)` validates via `config_schema.py`.

### Autocomplete changes

Update `_setting_autocomplete` / style value autocomplete in `visualizer.py` to show styles **grouped by category**:

```python
# When setting == "style" and engine == "audiovis":
from video.visualizer_engines.audiovis import get_styles_by_category

grouped = get_styles_by_category()
choices = []
for category, styles in sorted(grouped.items()):
    for style in sorted(styles):
        label = f"[{category}] {style}"
        if current_lower in style or current_lower in category:
            choices.append(app_commands.Choice(name=label, value=style))
```

### VALID_VISUALIZER_ENGINES

Unchanged. `audiovis` remains a single engine entry. The style is a sub-config of the engine, not a separate engine registration.

---

## 7. Files Changed

| File | Change |
|------|--------|
| `bot/video/visualizer_engines/audiovis.py` | Replace `STYLES` tuple with `STYLE_REGISTRY` dict. Add `get_valid_styles()` and `get_styles_by_category()` helpers. Add fallback logic in `_on_gl_ready` (try/except around compilation). Add `_slow_frame_count` tracking. |
| `bot/video/visualizer_engines/config_schema.py` | Update `audiovis.style.choices` list to include all 16 style names from the registry. |
| `bot/video/visualizer_engines/shaders/audiovis_kaleidoscope.glsl` | New — psychedelic kaleidoscope |
| `bot/video/visualizer_engines/shaders/audiovis_plasma.glsl` | New — psychedelic plasma tunnel |
| `bot/video/visualizer_engines/shaders/audiovis_fractal.glsl` | New — psychedelic Julia/Mandelbrot |
| `bot/video/visualizer_engines/shaders/audiovis_hypnotic.glsl` | New — psychedelic concentric rings |
| `bot/video/visualizer_engines/shaders/audiovis_glitch.glsl` | New — aggressive digital corruption |
| `bot/video/visualizer_engines/shaders/audiovis_storm.glsl` | New — aggressive lightning arcs |
| `bot/video/visualizer_engines/shaders/audiovis_shatter.glsl` | New — aggressive voronoi fracture |
| `bot/video/visualizer_engines/shaders/audiovis_aurora.glsl` | New — ambient aurora curtains |
| `bot/video/visualizer_engines/shaders/audiovis_nebula.glsl` | New — ambient FBM cosmic fog |
| `bot/video/visualizer_engines/shaders/audiovis_ocean.glsl` | New — ambient caustic refraction |
| `bot/video/visualizer_engines/shaders/audiovis_fireflies.glsl` | New — ambient particle glow |
| `bot/video/visualizer_engines/shaders/audiovis_synthwave.glsl` | New — retro perspective grid |
| `bot/video/visualizer_engines/shaders/audiovis_retrowave.glsl` | New — retro VHS + neon |
| `bot/video/visualizer_engines/shaders/audiovis_cyber.glsl` | New — retro wireframe tunnel |
| `bot/cogs/visualizer.py` | Update style value autocomplete to show category groupings |

---

## 8. Audio Reactivity Contract (Req 7, Req 8)

Every new shader MUST satisfy:

1. **Structural beat response** (Req 7 AC 1): When `iBeat` spikes to 1.0, at least one geometry/pattern/symmetry change occurs (not just brightness).
2. **Peak state** (Req 7 AC 2): While `iBeat > 0.5` (~first 100ms), two or more visual parameters differ from resting state.
3. **Band diversity** (Req 7 AC 3): Each shader uses ≥3 distinct `iBandEnergy` bands for independent visual parameters.
4. **Continuous motion** (Req 8 AC 1): With all audio inputs at zero, `iTime` alone drives perceptible frame-to-frame change.
5. **Smooth resumption** (Req 8 AC 3): Audio-reactive parameters are multiplied into the time-based animation (additive blending), so there's no discontinuity when audio starts/stops.

### Band assignment convention

| Band index | Name | Typical shader role |
|-----------|------|---------------------|
| 0 | Sub-bass | Large-scale displacement, tunnel speed, wave height |
| 1 | Bass | Mountain height, secondary displacement |
| 2 | Low-mid | Color shift speed, curtain amplitude |
| 3 | Mid | Pattern complexity, shape morph |
| 4 | Upper-mid | Secondary complexity, particle velocity |
| 5 | Presence | Arc branching, high-frequency detail |
| 6 | Brilliance | Sparkle intensity, edge glow |

---

## 9. Testing Strategy

### Compilation smoke test

A CI/dev script that attempts `glCompileShader` on each `.glsl` file against a Mesa software renderer (llvmpipe) to catch syntax errors without requiring iGPU hardware.

### Visual verification

Manual inspection via the Activity UI or a headless frame-dump tool that renders 60 frames of each style and writes PNG thumbnails for visual review.

### Performance benchmark

Frame-dump 300 frames per shader on target hardware, measure p95 render time, flag any shader exceeding 20ms.

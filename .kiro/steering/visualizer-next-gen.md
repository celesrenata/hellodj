# Next-Gen Visualizer Engine: "Drift"

inclusion: manual

## Why Current Visualizers Look Bad

The current engines (AudioVis, Varda, Fosfora) are **single-pass fragment shaders** — each frame is computed independently with no memory of the previous frame. This is the fundamental architectural gap between our visualizers and Milkdrop/Synesthesia.

What makes Milkdrop look incredible is its **iterative frame feedback loop**: each frame takes the PREVIOUS frame's output, warps it (zoom, rotate, displace per-pixel), then composites new visual elements on top. This creates organic, evolving trails and motion that single-pass shaders physically cannot produce.

Additionally, our HLS encoding pipeline uses `global_quality 28` with `maxrate 3000k` — this is too aggressive for detailed visual content. Raising quality is free (the iGPU has headroom).

## Architecture: Multipass Feedback Rendering

The engine ("Drift") uses a **ping-pong FBO feedback loop** with multiple render passes per frame:

```
┌─────────────────────────────────────────────────────┐
│  Frame N                                            │
│                                                     │
│  1. WARP PASS (vertex shader on NxN mesh)           │
│     - Read FBO_prev as texture                      │
│     - Displace mesh vertices by audio-driven eqs    │
│     - Per-vertex: zoom, rotation, translation       │
│     - Outputs warped previous frame to FBO_current  │
│                                                     │
│  2. DARKEN/DECAY PASS                               │
│     - Multiply FBO_current by decay factor (0.96)   │
│     - Prevents infinite brightness accumulation     │
│                                                     │
│  3. COMPOSITE PASS (fragment shader)                │
│     - Draw new elements on top of warped frame:     │
│       • Audio waveform (oscilloscope line)          │
│       • Spectrum shapes (radial, bars)              │
│       • Particles (bright dots at beat)             │
│       • Custom per-pixel shader effects             │
│     - Additive or alpha blend onto FBO_current      │
│                                                     │
│  4. POST-PROCESS PASS (optional)                    │
│     - Gaussian bloom (2-pass separable blur)        │
│     - Tone mapping                                  │
│     - Film grain (subtle)                           │
│     - Outputs to final read FBO                     │
│                                                     │
│  5. Swap: FBO_current → FBO_prev for next frame     │
│                                                     │
│  6. glReadPixels → pipe to ffmpeg                   │
└─────────────────────────────────────────────────────┘
```

## Infrastructure Mapping

| Component | What We Have | What We Use |
|-----------|-------------|-------------|
| GPU | Intel Meteor Lake iGPU, OpenGL 4.6, SR-IOV VFs | EGL headless on VF render node |
| Framebuffers | 2 FBOs with RGBA8 color attachments | Ping-pong feedback (current/prev) |
| Warp mesh | OpenGL 3.3 VBO/VAO with dynamic vertex updates | 48×36 vertex grid (1728 quads) |
| Blur | Separable Gaussian needs 2 extra FBOs | 2 half-res FBOs for bloom |
| Encoding | QSV h264_qsv already in pipeline | Raise quality: CRF 22, maxrate 6000k |
| Delivery | HLS 2s segments | Keep — possibly drop to 1s segments for lower latency |
| Audio | AudioFeatureBus @ 47fps (beat, FFT, 7-band, BPM) | All of it, plus smoothed interpolation |

## Key Rendering Techniques

### 1. Warp Mesh (Like Milkdrop's Per-Vertex Equations)

A `48×36` grid of vertices covering the viewport. Each vertex has UV coordinates pointing into the previous frame's texture. Every frame, we compute new UVs per-vertex based on audio:

```glsl
// Per-vertex warp driven by audio
vec2 warpedUV = baseUV;

// Zoom (center zoom driven by bass)
float zoom = 1.0 + u_bass * 0.03;
warpedUV = (warpedUV - 0.5) / zoom + 0.5;

// Rotation (driven by mid energy)
float angle = u_mids * 0.01;
mat2 rot = mat2(cos(angle), -sin(angle), sin(angle), cos(angle));
warpedUV = (rot * (warpedUV - 0.5)) + 0.5;

// Per-vertex displacement (creates organic flow patterns)
warpedUV.x += sin(baseUV.y * 6.28 + u_time) * u_highs * 0.01;
warpedUV.y += cos(baseUV.x * 6.28 + u_time) * u_bass * 0.01;
```

When the mesh is rendered with the previous frame's texture, this creates the fluid motion trails that define Milkdrop's aesthetic.

### 2. Frame Feedback with Decay

The warped previous frame is multiplied by a decay factor (0.94–0.98). Higher decay = longer trails. Audio energy modulates decay:

```glsl
float decay = 0.96 - u_bass * 0.02;  // More bass → faster fade
vec4 warped = texture(u_prev_frame, warpedUV) * decay;
```

### 3. Composite Shapes

After warping, new visual elements are drawn on top with additive blending:

- **Oscilloscope wave**: The raw audio waveform rendered as a glowing line (GL_LINE_STRIP with geometry shader for thickness, or distance-field in fragment shader)
- **Spectrum ring**: FFT bins drawn as a radial pattern emanating from center
- **Beat particles**: Burst of bright points on transients
- **Per-pixel shader**: Optional full-screen fragment shader for color grading / distortion

### 4. Bloom Post-Process

Two-pass separable Gaussian blur on a half-resolution FBO, blended back with the main image:

```
Main FBO (1280×720) → downsample to Blur FBO A (640×360)
Blur FBO A → horizontal blur → Blur FBO B
Blur FBO B → vertical blur → Blur FBO A
Composite: final = main + bloom_fbo * bloom_intensity
```

### 5. Preset System

Like Milkdrop, presets define the parameters:

```python
PRESET = {
    "name": "Cosmic Drift",
    "warp": {
        "zoom": {"base": 1.01, "bass_mod": 0.03, "beat_mod": 0.05},
        "rotation": {"base": 0.002, "mids_mod": 0.01},
        "displacement": {"x_freq": 2.0, "y_freq": 3.0, "amplitude": 0.008},
    },
    "decay": {"base": 0.96, "bass_mod": -0.02},
    "composite": {
        "wave": {"enabled": True, "thickness": 3.0, "color": [0.2, 0.8, 1.0]},
        "spectrum_ring": {"enabled": True, "radius": 0.3, "glow": 1.5},
        "particles": {"on_beat": True, "count": 50, "size": 4.0},
    },
    "bloom": {"intensity": 0.3, "radius": 8},
    "shader": "drift_cosmic",  # optional per-pixel composite shader
}
```

Presets crossfade by interpolating all numeric parameters over 3 seconds.

## Encoding Quality Fix

Current ffmpeg command uses `global_quality 28` which is too lossy for detailed visuals. New parameters:

```
-c:v h264_qsv -profile:v high -preset veryslow
-global_quality 20 -look_ahead 1 -look_ahead_depth 60
-maxrate 6000k -bufsize 10000k
-g 30 -force_key_frames expr:gte(t,n_forced*1)
```

Changes:
- `global_quality`: 28 → 20 (much higher quality, still reasonable file size)
- `maxrate`: 3000k → 6000k (allows more detail)
- `bufsize`: 5000k → 10000k (smoother rate control)
- Key frames every 1s instead of 2s (less quality drop at segment boundaries)
- `preset veryslow`: better compression efficiency (the iGPU has capacity)

## Resolution

Keep 1280×720 for now — Discord's Activity iframe caps rendering at this anyway, and the H.264 bitrate would need to be very high for 1080p to look good over HLS. The quality gain comes from the rendering technique (feedback loop), not raw pixel count.

## Implementation Plan

### Phase 1: DriftEngine (GPUEngineBase subclass)
- Ping-pong FBO pair (2 color textures + VAO for full-screen quad)
- Warp mesh: 48×36 vertex grid with per-frame UV update
- Frame decay pass
- Basic waveform composite (distance-field line in fragment shader)
- Single preset hardcoded

### Phase 2: Bloom + Composite Shapes
- Half-res FBO pair for separable Gaussian blur
- Spectrum ring composite
- Beat particle burst
- Additive blend compositing

### Phase 3: Preset System + Crossfade
- Preset data model (Python dicts → uniform values)
- Linear interpolation between presets over configurable duration
- 10-15 factory presets with distinct aesthetics
- Auto-advance on track change or timed interval

### Phase 4: Per-Pixel Composite Shaders
- Optional fragment shader applied during composite pass
- Supports simple Shadertoy-like syntax (iTime, iResolution, iBeat, etc.)
- Swappable per-preset
- Community preset loading from `/app/data/presets/drift/`

### Phase 5: Encoding Pipeline Upgrade
- Raise QSV quality parameters
- Optionally support 1s HLS segments for lower latency
- Explore HEVC (h265_qsv) for better quality/bitrate ratio if client support allows

## What Makes This Different From Everything Else

| Feature | AudioVis/Varda | ProjectM | Drift (Ours) |
|---------|---------------|----------|--------------|
| Frame feedback | ❌ | ✅ (via libprojectM) | ✅ (native) |
| Multipass render | ❌ | ✅ | ✅ |
| Per-pixel warp mesh | ❌ | ✅ | ✅ |
| Bloom post-process | ❌ | ❌ | ✅ |
| Audio smoothing | Basic | FFT only | Smoothed + BPM-synced |
| Preset crossfade | ❌ | ✅ (libprojectM) | ✅ (native, parametric) |
| Custom presets | Limited (shader files) | .milk files | Python dicts + GLSL |
| Resolution control | Fixed 720p | Fixed 720p | Configurable |
| Encoding quality | global_quality 28 | global_quality 28 | global_quality 20 |
| Dependency | None | libprojectM.so | None (pure OpenGL) |

The key advantage over ProjectM: we own the entire pipeline. No C library dependency, no .milk file parser, no HLSL→GLSL translation. Pure OpenGL 3.3+ that we fully control, optimized for our specific GPU (Intel Meteor Lake) and delivery format (HLS via QSV).

## Files to Create

```
bot/video/visualizer_engines/drift.py           — DriftEngine class
bot/video/visualizer_engines/drift_presets.py   — Factory presets
bot/video/visualizer_engines/shaders/drift_warp.vert    — Warp mesh vertex shader
bot/video/visualizer_engines/shaders/drift_warp.frag    — Warp mesh fragment (texture sample)
bot/video/visualizer_engines/shaders/drift_decay.frag   — Decay pass
bot/video/visualizer_engines/shaders/drift_composite.vert — Composite vertex
bot/video/visualizer_engines/shaders/drift_composite.frag — Composite fragment (waves, shapes)
bot/video/visualizer_engines/shaders/drift_bloom_h.frag — Horizontal blur
bot/video/visualizer_engines/shaders/drift_bloom_v.frag — Vertical blur
bot/video/visualizer_engines/shaders/drift_final.frag   — Final compositing (main + bloom)
```

## Reference Projects

- [Butterchurn](https://github.com/jberg/butterchurn) — WebGL MilkDrop reimplementation (great architectural reference for the feedback loop)
- [Milkslop](https://hebberd.co.nz/engineering/milkslop/milkslop) — TypeScript/WebGL2 MilkDrop port (modern clean implementation)
- [Synesthesia.live](https://synesthesia.live) — commercial GLSL visualizer (SSF format reference)
- [projectM](https://github.com/projectM-visualizer/projectm) — C++ Milkdrop reimpl (already integrated but as black-box library)

# Session Context — Visualizer Audio Pipe + Rendering Fixes (2026-08-24)

inclusion: manual

## What was accomplished this session

### Audio Pipe Integration (Lavalink → Visualizer)

1. **Fixed infinite retry loop** — `on_track_start` no longer resets `track_retries` during active retry cycle (`_retrying` flag)
2. **Session resume filter** — stale video HLS URLs (`/hls/video`) filtered from auto-resume to prevent unplayable tracks
3. **AudioPipeSession O_RDWR priming** — FIFO primed with O_RDWR so Lavalink's FileOutputStream open doesn't block
4. **close_primer()** — primer fd closed after Lavalink connects, so pipe reader is sole data consumer
5. **Shared hls-tmp volume** — mounted in BOTH bot and lavalink containers so Lavalink can see the FIFO
6. **Jackson @JsonCreator fix** — AudioPipeRequest in Lavalink needed explicit Jackson annotations for deserialization
7. **Pipe reader loop** — reads PCM from FIFO, feeds AudioFeatureBus; auto-reconnects across track changes
8. **Lavalink confirmed writing** — 18664 frames, 0 errors on first test; TeeAudioFilter + hot-swap working

### Visualizer Rendering Fixes

9. **glViewport fix** — THE black screen root cause. Headless EGL has no default viewport (0x0), so no fragments were rasterized
10. **VBO + vertex attribute** — Mesa iris needs actual vertex attributes bound (gl_VertexID alone doesn't work)
11. **Vertex shader rewrite** — reads from `layout(location=0) in vec2 aPos` instead of `positions[gl_VertexID]`
12. **glVertexAttribPointer ctypes fix** — needs explicit argtypes + `c_void_p(0)` for offset (not Python None)

### New Shader Styles

13. **audiovis_waveform.glsl** — glowing oscilloscope with reflections
14. **audiovis_circular.glsl** — radial spectrum bars from center ring
15. **audiovis_waterfall.glsl** — spectrogram heatmap with scroll decay
16. **Guild config wiring** — `_create_engine_instance` now passes guild settings to engine constructor

### Other Fixes

17. **5s suspension debounce** — reduced from 10s
18. **Resilient pipe reader** — auto-recreates FIFO and re-enables Lavalink pipe on track transitions

## What still needs work

### 1. Track Transition Crash (Lavalink OpusEncoder)
When the audio pipe's `PipeAwareFilterFactory` hot-swaps into the filter chain, the old chain's `close()` releases the `OpusEncoder`'s native resources while the new chain still references the same output filter. Causes `NativeResourceHolder.checkNotReleased` in `OpusEncoder.encode`. Needs a lavaplayer-side fix to not close shared downstream filters during hot-swap.

### 2. Better Visualizer Shaders
Current shaders (bars, waveform, circular, waterfall) are functional but basic. Need Milkdrop-quality presets: kaleidoscope fractals, particle explosions on beats, plasma tunnels, metaball fields, etc.

### 3. ProjectM / Fosfora / Varda Engines
These are separate engine classes with their own rendering approaches (projectM uses libprojectM, Fosfora uses transform feedback particles, Varda is a Shadertoy-compatible runner). Currently registered but untested with the new audio pipe.

### 4. Tidal Token Refresh
`tidal-refresh: failed (status=401)` — the Tidal OAuth token is expired/invalid. Needs re-auth.

## Key architecture notes for next session

- **Audio pipe flow**: Lavalink TeeAudioFilter → FIFO (`/tmp/hellodj_hls/{gid}/viz/audio.pipe`) → `_pipe_reader_loop` → `AudioFeatureBus.feed_pcm()` → `_compute_features()` → `engine.on_audio_features()`
- **FIFO lifecycle**: Created by `AudioPipeSession.start()` with O_RDWR primer. Primer closed after `enable_pipe` succeeds. Reader opens O_RDONLY. On track end (EOF), reader re-creates FIFO and re-enables pipe.
- **Shared volume**: `hls-tmp` (emptyDir Memory 2Gi) mounted at `/tmp/hellodj_hls` in BOTH bot and lavalink containers
- **glViewport**: MUST be called after FBO creation in headless EGL — no surface = no default viewport
- **Image tags**: Bot `viz-audio-pipe-2026-08-24`, Lavalink `audio-pipe-2026-08-24`
- **Kustomize override**: Bot tag in `kube/kustomization.yaml` still `viz-audio-pipe-2026-08-24`
- **Lavalink.jar**: Rebuilt with `@JsonCreator` fix, committed to `kube/lavalink/Lavalink.jar`

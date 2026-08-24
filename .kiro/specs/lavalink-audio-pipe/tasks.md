# Implementation Plan: Lavalink Audio Pipe

## Overview

Implements a named FIFO bridge between Lavalink's DSP filter chain and FFmpeg's HLS transcoding pipeline. Spans three repos: lavaplayer (Java PCM sink), Lavalink (Kotlin REST API), and hellodj bot (Python pipe lifecycle + FFmpeg integration).

## Tasks

- [x] 1. Implement PipePcmSink interface and UnixFifoPcmSink in lavaplayer — Create `PipePcmSink.java` interface and `UnixFifoPcmSink.java` implementation in `lavaplayer/main/src/main/java/com/sedmelluq/discord/lavaplayer/track/playback/`. Opens named FIFO via FileOutputStream, writes s16le PCM frames (3840 bytes/20ms frame), tracks frame/error counts, handles blocking writes (natural rate-limiting), disconnects gracefully after 50 consecutive errors.
- [x] 2. Implement TeeAudioFilter in lavaplayer — Create `TeeAudioFilter.java` implementing `FloatPcmAudioFilter` in `lavaplayer/main/src/main/java/com/sedmelluq/discord/lavaplayer/filter/`. Converts float PCM to s16le bytes, writes to PipePcmSink, passes samples through to next filter. Sits at end of pipe filter chain (after all non-timing filters). Thread-safe: audio thread writes, connection state changes from another thread.
- [x] 3. Add FilterChain.withoutTimescale() in Lavalink — Add method to `LavalinkServer/src/main/java/lavalink/server/player/filters/FilterChain.kt` that returns a new FilterChain with `timescale = null` and all other filters preserved. Used for building the pipe-specific filter chain.
- [x] 4. Add audio pipe management to LavalinkPlayer — Modify `LavalinkServer/src/main/java/lavalink/server/player/LavalinkPlayer.kt`: add `pipeSink`/`pipeFilterChain` fields, implement `enableAudioPipe(path)`, `disableAudioPipe()`, `getAudioPipeStatus()`, `rebuildPipeFilterChain()`. Hook into the existing `filters` setter so filter updates propagate to pipe chain.
- [x] 5. Add audio pipe REST endpoints to Lavalink — Add `POST /v4/sessions/{sessionId}/players/{guildId}/audiopipe` and `DELETE` endpoints to `PlayerRestHandler.kt`. Create `AudioPipeRequest` and `AudioPipeStatus` data classes. Include `audioPipe` field in player state GET response. Return HTTP 400 if FIFO path doesn't exist.
- [x] 6. Create AudioPipeSession module in hellodj bot — New `bot/video/audio_pipe.py` with `AudioPipeSession` class. `start()` creates FIFO via `os.mkfifo()` at `/tmp/hellodj_hls/{guild_id}/{session_id}/audio.pipe`. `stop()` unlinks. Add startup cleanup to glob and unlink orphaned pipe files.
- [x] 7. Create LavalinkPipeClient in hellodj bot — New `bot/video/lavalink_pipe_client.py` with REST client for Lavalink pipe endpoints. `enable_pipe()` POSTs socketPath, `disable_pipe()` DELETEs, `get_pipe_status()` reads player state. Uses localhost:2333 with auth from credential store, session_id from wavelink node.
- [x] 8. Add audio pipe input mode to HLS transcode pipeline — Modify `bot/video/hls_transcode.py`: add `audio_pipe_path` and `timescale_speed` parameters to `_build_streaming_ffmpeg_args()` and `start_streaming()`. When pipe path set: input becomes `-f s16le -ar 48000 -ac 2 -i {path}`, map `0:v:0` + `1:a:0`. When speed != 1.0: add `setpts=PTS/{speed}` to video filter and `-af atempo={speed}` for audio. Implement `_build_atempo_chain()` helper for speeds outside [0.5, 2.0].
- [x] 9. Wire audio pipe into ActivityStreamer._play_source — In `_play_source()`: check guild filters, create AudioPipeSession if active, call LavalinkPipeClient.enable_pipe(), pass pipe path to pipeline.start_streaming(). In `_stop_internal()`/`skip()`/`previous()`: disable pipe and stop session. Replace the old `_start_lavalink_audio`/`_stop_lavalink_audio` methods.
- [x] 10. Handle filter changes during active video playback — In `bot/cogs/filters.py`: detect if video pipe session is active. Non-timing filters: no action (Lavalink auto-propagates). Timescale changes: restart HLS pipeline with new speed. Filter reset: disable pipe, restart pipeline with source audio. New filters on unfiltered video: enable pipe, restart pipeline. Broadcast `filter_sync` WS for frontend feedback.
- [x] 11. Revert unsafe Lavalink audio routing from previous commit — Remove `_start_lavalink_audio`/`_stop_lavalink_audio` from activity_streamer.py. Remove `lavalink_audio` WS case and `_lavalinkAudioActive` logic from app.js. Revert `_handle_audio_play_pause` video-active guard removal in ws_hub.py. Keep `filter_sync` WS message and seek-sync change.
- [x] 12. Build and test custom Lavalink image with pipe support — Build lavaplayer + Lavalink JARs with new classes. Package as Docker image `registry.celestium.life/hellodj/lavalink:<tag>`. Verify: play track → enable pipe → confirm PCM writes to FIFO. Verify: disable pipe → normal playback continues. Verify: filter update propagates to pipe output.
- [x] 13. End-to-end integration test — Test: video with EQ → HLS has filtered audio. Nightcore 1.25x → pipeline restarts, both video+audio sped up. Filter reset → falls back to source audio. Kill FIFO → graceful fallback. Skip during pipe → clean transition. Discord VC receives full filters simultaneously.

## Task Dependency Graph

```json
{
  "waves": [
    {"tasks": [1, 2, 3, 6, 7, 8, 11]},
    {"tasks": [4]},
    {"tasks": [5]},
    {"tasks": [12, 9]},
    {"tasks": [10]},
    {"tasks": [13]}
  ]
}
```

```
1 (PipePcmSink) ──┐
                   ├──► 4 (LavalinkPlayer integration)
2 (TeeAudioFilter)─┘           │
                               ├──► 5 (REST endpoints) ──► 12 (Build image)
3 (withoutTimescale) ──────────┘                                  │
                                                                  │
6 (AudioPipeSession) ──┐                                          │
                       ├──► 9 (Wire into ActivityStreamer) ◄───────┘
7 (PipeClient) ────────┘           │
                                   │
8 (FFmpeg pipe mode) ──────────────┘
                                   │
10 (Filter changes) ◄─────────────┘
                                   │
11 (Revert old approach) ──────────┘
                                   │
13 (E2E test) ◄───────────────────┘
```

## Notes

- Tasks 1-5 are Java/Kotlin work in the lavaplayer and Lavalink repos
- Tasks 6-11 are Python work in the hellodj bot repo
- Task 11 should be done early (can be done in parallel with Java work) to remove the broken dual-output approach from the previous commit
- Task 12 requires Tasks 1-5 complete; Task 13 requires all tasks complete
- The lavaplayer fork is at `celesrenata/lavaplayer` (no specific branch documented — use default or create `audio-pipe`)
- The Lavalink fork is at `celesrenata/Lavalink` branch `dev`

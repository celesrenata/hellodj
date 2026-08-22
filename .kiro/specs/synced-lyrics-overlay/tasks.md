# Implementation Plan: Synced Lyrics Overlay

## Overview

Implement synchronized lyrics overlay in three phases: (1) Core Pipeline MVP with LRCLIB provider, LRC parser, even-distribution timing, LyricsOverlay frontend, and WebSocket broadcast; (2) Full Resolution + Karaoke with Genius fallback, word-level LRC parsing, beat-snap timing via AudioFeatureBus, and karaoke highlight; (3) Controls + Commands with `/lyrics overlay:on|off`, Activity toggle button, localStorage persistence, and responsive mode.

## Prerequisites

- Existing `bot/video/ws_hub.py` with `broadcast_from_bot()` and per-guild connection tracking
- Existing `bot/video/activity_frontend/` with `app.js`, `style.css`, `index.html`
- Existing `bot/player.py` with `_on_track_start_callback` and `get_state()` helpers
- Existing `bot/cogs/lyrics.py` with Genius API integration
- `aiohttp` available for HTTP requests
- `hypothesis` available for property-based tests
- AudioFeatureBus from activity-visualizer spec (optional for Phase 2 beat snapping)

## Tasks

- [x] 1. Phase 1: Core Pipeline (MVP) — LRCLIB + Even Timing + Overlay + Broadcast
  - [x] 1.1 Create `bot/video/lyrics_models.py` with data model classes
    - Define `TimedWord` dataclass (time_ms, text) with `to_dict()` method
    - Define `TimedLine` dataclass (time_ms, text, words: list[TimedWord] | None) with `to_dict()` method
    - Define `TimedLyrics` dataclass (track_id, sync_type, duration_s, lines: list[TimedLine]) with `to_dict()` and `to_ws_message()` methods
    - Define `LyricsState` dataclass (enabled, current_lyrics, current_track_key)
    - `sync_type` is a Literal["lrc_synced", "lrc_word", "beat_estimated"]
    - _Requirements: 6.4_

  - [x] 1.2 Create `bot/video/lrclib_provider.py` with LRCLIB.net API client and LRC parser
    - Implement `LRCLIBProvider` class with `fetch(artist, title, duration_s) -> TimedLyrics | None`
    - Query `GET https://lrclib.net/api/get?artist_name={artist}&track_name={title}&duration={seconds}`
    - Set User-Agent header to `HelloDJ/1.0 (https://hellodj.celestium.life)`
    - Enforce 5-second timeout on the HTTP request
    - Handle 404 (no match) → return None
    - Handle `syncedLyrics` field → call `parse_lrc()` → return TimedLyrics with sync_type="lrc_synced"
    - Handle `plainLyrics` only → return raw text (caller handles timing)
    - Handle `instrumental: true` → return None (caller broadcasts unavailable)
    - Implement `parse_lrc(lrc_text: str) -> list[TimedLine]` for line-level timestamps `[mm:ss.xx]`
    - Handle both 2-digit (centiseconds) and 3-digit (milliseconds) LRC timestamp formats
    - Log errors at debug level, never raise to caller
    - _Requirements: 1.1, 1.2, 1.3, 8.1, 8.2, 8.3, 8.4, 8.5_

  - [x]* 1.3 Write property test for LRC round-trip (Property 1)
    - **Property 1: LRC round-trip**
    - Generate random valid LRC strings → parse → format back → parse again → verify equivalent TimedLine arrays
    - Timestamps match within 10ms due to format precision
    - **Validates: Requirements 8.6**

  - [x] 1.4 Create `bot/video/beat_timing.py` with even-distribution timing algorithm
    - Implement `compute_beat_timing(plain_text: str, duration_s: float, audio_bus=None) -> list[TimedLine]`
    - Split text into non-empty lines
    - Weight each line by character count / total characters
    - Compute cumulative start times proportionally across duration
    - Return list of TimedLine with words=None
    - Handle edge cases: empty text → return [], zero total chars → equal distribution
    - Phase 1: ignore `audio_bus` parameter (always even distribution)
    - _Requirements: 2.1, 2.3, 2.4_

  - [x]* 1.5 Write property test for beat timing monotonicity (Property 2)
    - **Property 2: Beat timing monotonicity**
    - Generate random plain text + durations → verify output `time_ms` values are non-decreasing
    - For all i: `lines[i].time_ms <= lines[i+1].time_ms`
    - **Validates: Requirements 2.1**

  - [x]* 1.6 Write property test for beat timing bounds (Property 3)
    - **Property 3: Beat timing bounds**
    - Generate random plain text + durations → verify all `time_ms` values fall within `[0, duration_ms]`
    - No line starts before 0 or after song ends
    - **Validates: Requirements 2.1, 2.5**

  - [x] 1.7 Create `bot/video/lyrics_service.py` with LyricsService orchestrator
    - Implement per-guild `LyricsService` class following VisualizerManager pattern
    - Constructor takes guild_id and ws_hub reference
    - Implement `on_track_change(guild_id, metadata)` — auto-fetch when enabled
    - Implement `fetch_and_broadcast()` — resolution chain with LRCLIB-only (Phase 1)
    - Implement LRU cache (OrderedDict, max 50 entries, evict oldest on overflow)
    - Guard: skip timing for duration <= 0 or > 86400 (live streams)
    - Wrap entire fetch in try/except — never propagate exceptions to audio pipeline
    - Broadcast `lyrics_data` via ws_hub on success
    - Broadcast `lyrics_unavailable` on failure/no results
    - Implement `get_lyrics_service(guild_id)` factory function
    - _Requirements: 1.1, 1.4, 1.5, 2.4, 2.5, 9.3, 9.4, 9.5_

  - [x]* 1.8 Write property test for cache LRU invariant (Property 4)
    - **Property 4: Cache LRU invariant**
    - Generate random put sequences → verify `len(cache) <= cache_max` always holds
    - Verify eviction removes the least recently used entry
    - **Validates: Requirements 1.1**

  - [x] 1.9 Extend `bot/video/ws_hub.py` with lyrics broadcast and late-joiner sync
    - Add `lyrics_data` message broadcast support
    - Add `lyrics_unavailable` message broadcast support
    - On new client connect: if lyrics overlay is enabled and current_lyrics exists, send `lyrics_data` to the joining client
    - Client uses existing playback position from state message to determine active line (no separate position sync)
    - _Requirements: 6.1, 6.2, 6.3_

  - [x] 1.10 Register LyricsService callback in player track-start chain
    - Chain LyricsService `on_track_change` with existing `_on_track_start_callback`
    - Forward to original callback first (VisualizerManager), then lyrics
    - Wrap lyrics callback in try/except so failures never affect audio
    - _Requirements: 9.5_

  - [x]* 1.11 Write property test for audio independence (Property 7)
    - **Property 7: Audio independence**
    - Generate random LyricsService failures → verify player.py state is never modified
    - Verify no exception from LyricsService propagates to the track-start callback caller
    - **Validates: Requirements 9.5**

  - [x] 1.12 Implement `LyricsOverlay` class in `bot/video/activity_frontend/app.js`
    - Create `LyricsOverlay` class with constructor accepting container element
    - Build DOM: `.lyrics-overlay` with `.lyrics-line.prev`, `.lyrics-line.current`, `.lyrics-line.next`
    - Implement `setLyricsData(payload)` — store lines array and sync_type
    - Implement `clearLyrics()` — clear all state and hide overlay
    - Implement `updatePosition(currentTimeMs)` — binary search for active line, render 3-line display
    - Implement `showUnavailable()` — show "No lyrics available", auto-dismiss after 3s
    - Implement `_findLineIndex(timeMs)` — binary search returning largest index where `time_ms <= timeMs`
    - Implement `_renderLines(idx)` — update prev/current/next text, trigger animation class
    - Wire `updatePosition` to hls.js video `timeupdate` event
    - Handle seek (binary search recalculates on new position) and pause (timeupdate stops naturally)
    - _Requirements: 3.1, 3.2, 3.3, 3.6, 6.5, 6.6_

  - [x]* 1.13 Write property test for binary search correctness (Property 6)
    - **Property 6: Binary search correctness**
    - Generate random sorted TimedLine arrays + random timeMs values
    - Verify `_findLineIndex(timeMs)` returns the largest index where `lines[i].time_ms <= timeMs`, or -1 if none
    - **Validates: Requirements 6.6**

  - [x] 1.14 Add lyrics overlay CSS to `bot/video/activity_frontend/style.css`
    - `.lyrics-overlay`: absolute, bottom: 80px, flex column, center-aligned, pointer-events: none, z-index: 5
    - `.lyrics-line`: white-space nowrap, overflow hidden, text-overflow ellipsis, max-width 90%, text-align center
    - `.lyrics-line.prev`, `.lyrics-line.next`: opacity 0.4, font-size 0.9em, color #ccc
    - `.lyrics-line.current`: opacity 1.0, font-size 1.3em, color #fff, font-weight 500
    - `.lyrics-line.current.animate`: `lyrics-slide-up` animation (translateY 8px → 0, opacity 0.7 → 1)
    - Text shadow for readability over video: `0 1px 4px rgba(0,0,0,0.8)`
    - Font family: 'Inter', sans-serif
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 7.1_

  - [x] 1.15 Add WebSocket message routing for lyrics messages in frontend `app.js`
    - Route `lyrics_data` → `lyricsOverlay.setLyricsData(payload)`
    - Route `lyrics_unavailable` → `lyricsOverlay.showUnavailable()`
    - Ensure lyrics messages are handled regardless of current playback mode (video or visualizer)
    - _Requirements: 6.3_

- [x] 2. Checkpoint — Phase 1 complete
  - Ensure all tests pass, ask the user if questions arise.
  - Verify: LRCLIB fetch works, LRC parsing correct, even-distribution timing produces valid output, LyricsOverlay renders 3-line display, WebSocket broadcast delivers lyrics to clients, late-joiner receives current lyrics

- [x] 3. Phase 2: Full Resolution + Karaoke — Genius + Word-Level + Beat-Snap
  - [x] 3.1 Create `bot/video/genius_provider.py` — extract shared module from cog
    - Extract `_fetch_lyrics()` and `_extract_from_html()` from `bot/cogs/lyrics.py` into shared module
    - Implement `GeniusProvider` class with `fetch(title, artist) -> str | None`
    - Use Genius API access token from config/credential store
    - Return plain text lyrics or None
    - Enforce 5-second timeout
    - Log errors at debug level, never raise to caller
    - _Requirements: 1.1, 1.4, 9.3_

  - [x] 3.2 Update `bot/cogs/lyrics.py` to delegate to `genius_provider.py`
    - Replace inline `_fetch_lyrics()` / `_extract_from_html()` with calls to `GeniusProvider`
    - Preserve existing embed behavior for `/lyrics` without `overlay` option
    - Ensure no functional regression for chat-embed lyrics display
    - _Requirements: 5.3_

  - [x] 3.3 Integrate Genius fallback into LyricsService resolution chain
    - Initialize `GeniusProvider` lazily with access token from config
    - After LRCLIB returns None: attempt Genius fetch
    - If Genius returns plain text: pass to `compute_beat_timing()` → broadcast result with sync_type="beat_estimated"
    - If Genius also fails: broadcast `lyrics_unavailable`
    - Combined timeout budget: 5s LRCLIB + 5s Genius = 10s max
    - _Requirements: 1.1, 1.4, 1.5_

  - [x] 3.4 Add word-level LRC parsing to `lrclib_provider.py`
    - Extend `parse_lrc()` to detect word-level timestamps: `<mm:ss.xx>word`
    - Parse inline word timestamps into `TimedWord` objects per line
    - Clean display text by removing word-timestamp tags
    - Set sync_type to "lrc_word" when word-level data is present
    - _Requirements: 1.6_

  - [x] 3.5 Implement beat-snap timing in `bot/video/beat_timing.py`
    - When `audio_bus` is provided and has subscribers: collect beat timestamps
    - Implement `_snap_to_beats(line_starts, beat_timestamps, tolerance_ms=500)` using binary search
    - Snap each line start to nearest beat within ±500ms tolerance
    - If no beat within tolerance, keep original even-distribution time
    - If AudioFeatureBus unavailable or zero subscribers: use even distribution (existing fallback)
    - _Requirements: 2.2, 2.3, 9.1_

  - [x] 3.6 Implement karaoke highlight in frontend `LyricsOverlay`
    - Add `_renderKaraoke(words, currentTimeMs)` method
    - When `syncType === 'lrc_word'` and current line has `words` array: render word spans
    - Each word gets `.lyrics-word` class; words with `time_ms <= currentTimeMs` get `.active`
    - Progressive highlight updates on each `updatePosition` call
    - _Requirements: 3.5_

  - [x] 3.7 Add karaoke CSS styles to `style.css`
    - `.lyrics-word`: transition color 0.15s ease, color rgba(255,255,255,0.5)
    - `.lyrics-word.active`: color #fff
    - _Requirements: 3.5_

- [x] 4. Checkpoint — Phase 2 complete
  - Ensure all tests pass, ask the user if questions arise.
  - Verify: Genius fallback works when LRCLIB has no results, word-level LRC parsing produces TimedWord arrays, beat-snap improves timing alignment, karaoke highlight progressively illuminates words

- [x] 5. Phase 3: Controls + Commands — Toggle + Slash Command + Persistence + Responsive
  - [x] 5.1 Extend `/lyrics` command in `bot/cogs/lyrics.py` with `overlay` parameter
    - Add optional `overlay: Literal["on", "off"] | None` parameter to `/lyrics` command
    - On `overlay:on`: verify a track is playing, enable LyricsService, trigger fetch_and_broadcast, broadcast `lyrics_overlay_enable`
    - On `overlay:off`: disable LyricsService, broadcast `lyrics_overlay_disable`
    - On `overlay:on` with nothing playing: respond ephemeral "Nothing is playing right now."
    - On no `overlay` option: preserve existing chat embed behavior
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5_

  - [x] 5.2 Add `lyrics_overlay_enable` / `lyrics_overlay_disable` WS message handling in ws_hub
    - Broadcast `{"type": "lyrics_overlay_enable"}` to all guild clients on command
    - Broadcast `{"type": "lyrics_overlay_disable"}` to all guild clients on command
    - _Requirements: 5.1, 5.2_

  - [x] 5.3 Implement frontend toggle button in `index.html` and `app.js`
    - Add `<button id="lyrics-toggle">` to Activity controls bar (styled like whiteboard toggle)
    - On click: toggle `lyricsOverlay.enable()` / `lyricsOverlay.disable()`
    - Toggle button active state class
    - _Requirements: 4.1, 4.2_

  - [x] 5.4 Implement localStorage persistence for toggle preference
    - On enable: `localStorage.setItem('hellodj_lyrics_enabled', 'true')`
    - On disable: `localStorage.setItem('hellodj_lyrics_enabled', 'false')`
    - On page load: read saved preference and apply
    - _Requirements: 4.3_

  - [x] 5.5 Implement `forceEnable()` / `forceDisable()` / `clearForce()` in LyricsOverlay
    - Handle `lyrics_overlay_enable` WS message → `forceEnable()`
    - Handle `lyrics_overlay_disable` WS message → `forceDisable()`
    - `forcedState` takes precedence over local toggle (`isVisible` getter)
    - When force cleared (e.g., track ends): revert to local preference
    - _Requirements: 4.4, 4.5_

  - [x]* 5.6 Write property test for force override precedence (Property 8)
    - **Property 8: Force override precedence**
    - Generate random sequences of local toggle + force enable/disable events
    - Verify: when `forcedState !== null`, visibility equals `forcedState` regardless of local toggle
    - Verify: when `forcedState === null`, visibility equals local `enabled` state
    - **Validates: Requirements 4.4, 4.5**

  - [x] 5.7 Add responsive single-line mode for narrow viewports
    - CSS media query: `@media (max-width: 400px)` hides `.lyrics-line.prev` and `.lyrics-line.next`
    - Current line font-size reduced to 1.0em in narrow mode
    - _Requirements: 7.4, 7.5_

  - [x] 5.8 Verify z-index and pointer-events coexistence
    - Confirm z-index order: video/visualizer (1) < lyrics (5) < whiteboard (10) < controls (20)
    - Confirm `pointer-events: none` on `.lyrics-overlay` allows click-through
    - Verify whiteboard draws over lyrics without obstruction
    - _Requirements: 7.1, 7.2, 7.3_

- [x] 6. Final checkpoint
  - Ensure all tests pass, ask the user if questions arise.
  - Verify: `/lyrics overlay:on|off` toggles for all viewers, local toggle persists across refresh, force override takes precedence, responsive mode displays single line on narrow viewport, z-index layering correct, full end-to-end flow works (song starts → lyrics fetched → overlay renders → toggle/command controls)

## Notes

- Tasks marked with `*` are property-based tests (optional, can be skipped for faster MVP)
- Each task references specific requirements from `requirements.md` for traceability
- Checkpoints between phases ensure incremental validation
- Phase 1 is a fully functional MVP — LRCLIB covers the majority of popular tracks with pre-synced timestamps
- Phase 2 adds fallback breadth (Genius) and quality (karaoke, beat-snap) — can ship independently
- Phase 3 adds user/admin control surface — low risk, mostly frontend
- Audio independence is enforced at every boundary — LyricsService failures never touch Lavalink playback
- Python (server) and JavaScript/CSS (frontend) throughout
- The LyricsService follows the same per-guild, in-process pattern as VisualizerManager
- No GPU resources consumed — all rendering is client-side CSS/JS

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1"] },
    { "id": 1, "tasks": ["1.2", "1.4"] },
    { "id": 2, "tasks": ["1.3", "1.5", "1.6", "1.14"] },
    { "id": 3, "tasks": ["1.7", "1.9", "1.10"] },
    { "id": 4, "tasks": ["1.8", "1.11", "1.12", "1.15"] },
    { "id": 5, "tasks": ["1.13"] },
    { "id": 6, "tasks": ["3.1"] },
    { "id": 7, "tasks": ["3.2", "3.3", "3.4", "3.5"] },
    { "id": 8, "tasks": ["3.6", "3.7"] },
    { "id": 9, "tasks": ["5.1", "5.2"] },
    { "id": 10, "tasks": ["5.3", "5.4", "5.5", "5.7"] },
    { "id": 11, "tasks": ["5.6", "5.8"] }
  ]
}
```

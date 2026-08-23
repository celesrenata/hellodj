# Implementation Plan: Unified Search Autocomplete

## Overview

Implement a parallel multi-provider search engine (`bot/search/` package) that fans out queries to Spotify, Tidal, and YouTube via wavelink, deduplicates results by ISRC and normalized metadata, and formats up to 25 autocomplete choices within Discord's 3-second timeout. Additionally, implement a rich Activity-based search UI panel with progressive WebSocket-driven results, expandable provider groups, filters, and queue integration. Both interfaces share the same `UnifiedSearchEngine` backend.

## Tasks

- [x] 1. Set up search package structure and data models
  - [x] 1.1 Create `bot/search/__init__.py` with package exports and `bot/search/models.py` with `SearchResult`, `ProviderResult`, `TrackGroup`, and `CacheEntry` dataclasses
    - Define all fields per the design: title, artist, album, release_year, duration_ms, artwork_url, isrc, provider, track_id, variant_type, normalized_key, has_music_video
    - Include `CacheEntry.is_expired(ttl)` method
    - _Requirements: 18.1_

- [x] 2. Implement normalized key generation and variant detection
  - [x] 2.1 Create `bot/search/deduplicator.py` with `normalize_key(artist, title)` and `detect_variant(title)` functions
    - Implement regex-based stripping: remaster annotations, featuring credits, trailing year patterns
    - Lowercase, collapse whitespace, trim, concatenate as `{artist}:{title}`
    - Implement word-boundary variant detection for "Live", "Remix", "Acoustic", "Music Video"
    - Ensure substring matches (e.g., "Oliver", "Premixed") do NOT trigger variant classification
    - _Requirements: 4.1, 4.2, 4.3, 4.5, 4.6_

  - [ ]* 2.2 Write property test for normalized key determinism (Property 6)
    - **Property 6: Normalized Key Determinism and Canonicalization**
    - Verify output is entirely lowercase, no leading/trailing whitespace, no consecutive whitespace, no remaster/feat/year patterns, format is `{artist}:{title}`
    - **Validates: Requirements 4.1, 4.2, 4.3**

  - [ ]* 2.3 Write property test for variant detection at word boundaries (Property 7)
    - **Property 7: Variant Detection at Word Boundaries Only**
    - Verify detect_variant returns a type only when "Live", "Remix", "Acoustic", or "Music Video" appears as a complete word, not as substring
    - **Validates: Requirements 4.5, 4.6**

- [x] 3. Implement ISRC-based deduplication
  - [x] 3.1 Add `Deduplicator.deduplicate(results, guild_source_provider)` static method to `bot/search/deduplicator.py`
    - Compute dedup keys: ISRC (+ variant_type if variant), or normalized_key fallback
    - Group by key, retain highest-priority provider version (respecting guild preference)
    - Record `available_providers` for Activity UI Track_Groups
    - Implement slot redistribution when a provider returns zero results without error
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 2.4, 2.5, 10.5_

  - [ ]* 3.2 Write property test for deduplication priority retention (Property 4)
    - **Property 4: Deduplication Retains Highest Priority Per Key**
    - For each unique dedup key, exactly one result remains from the highest-priority provider
    - **Validates: Requirements 3.1, 3.2, 3.4**

  - [ ]* 3.3 Write property test for variant preservation (Property 5)
    - **Property 5: Variant Tracks Preserved as Distinct Entries**
    - Two tracks sharing ISRC where one is a variant are preserved as separate entries
    - **Validates: Requirements 3.3**

  - [ ]* 3.4 Write property test for slot redistribution invariants (Property 3)
    - **Property 3: Slot Redistribution Invariants**
    - Verify redistributed slots sum to ≤25, only increase for providers with results, remainder goes to highest-priority, never assign slots to zero-result providers
    - **Validates: Requirements 2.4, 2.5**

  - [ ]* 3.5 Write property test for guild source provider ordering (Property 12)
    - **Property 12: Guild Source Provider Ordering**
    - Results ordered with guild's preferred provider first, remaining in default priority
    - **Validates: Requirements 10.5**

- [x] 4. Implement result cache
  - [x] 4.1 Create `bot/search/cache.py` with `ResultCache` class
    - LRU eviction with max capacity 200 entries
    - TTL-based expiration (60 seconds)
    - Cache key normalization: lowercase, strip, collapse whitespace
    - Incorporate filter parameters into cache key for isolation
    - Per-process in-memory storage (no external dependencies)
    - _Requirements: 7.1, 7.2, 7.3, 7.4, 7.5, 7.6, 18.6_

  - [ ]* 4.2 Write property test for cache key isolation (Property 13)
    - **Property 13: Cache Key Isolation**
    - Same text with different case/whitespace produces same key; different filters produce different keys
    - **Validates: Requirements 7.1, 18.6**

- [x] 5. Implement URL detection
  - [x] 5.1 Create `bot/search/url_detector.py` with `URLDetector.detect(query)` static method
    - Recognize: spotify.com/track, tidal.com/track, tidal.com/browse/track, youtube.com/watch, youtu.be/, soundcloud.com/
    - Return `(platform_name, url)` tuple or None
    - Handle URLs with query parameters, fragments, additional path segments
    - _Requirements: 8.1, 8.2, 8.3_

  - [ ]* 5.2 Write property test for URL detection correctness (Property 11)
    - **Property 11: URL Detection Correctness**
    - Strings starting with http(s):// matching recognized patterns return non-None; non-matching return None
    - **Validates: Requirements 8.1, 8.2, 8.3**

- [x] 6. Implement choice formatter and value encoding
  - [x] 6.1 Create `bot/search/formatter.py` with `ChoiceFormatter` class
    - `format_choices(results, max_choices=25)` → list of `app_commands.Choice[str]`
    - Format: `{icon} {artist} - {title} ({M:SS})` with 🎬 indicator for music videos
    - Truncate title first (append "…") to stay within 100 characters
    - Duration formatting: `M:SS` for <60min, `H:MM:SS` for ≥60min, omit if unavailable
    - `encode_value(provider, track_id)` → `{prefix}:{track_id}`, truncate to 100 chars
    - `decode_value(value)` → `(lavalink_prefix, track_id)` or `(None, raw_value)`
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 6.1, 6.2, 6.3, 6.4, 6.5_

  - [ ]* 6.2 Write property test for formatted choice length invariant (Property 8)
    - **Property 8: Formatted Choice Length Invariant**
    - For any SearchResult, formatted choice name ≤ 100 chars, encoded value ≤ 100 chars
    - **Validates: Requirements 5.3, 6.2, 8.4**

  - [ ]* 6.3 Write property test for duration formatting correctness (Property 9)
    - **Property 9: Duration Formatting Correctness**
    - `H:MM:SS` when ≥3,600,000ms, `M:SS` when <3,600,000ms, same total seconds as input
    - **Validates: Requirements 5.4, 5.5**

  - [ ]* 6.4 Write property test for value encoding round-trip (Property 10)
    - **Property 10: Value Encoding Round-Trip**
    - For values where total encoded length ≤ 100 chars, encode then decode yields original provider + track_id
    - **Validates: Requirements 6.1, 6.3, 6.5**

- [x] 7. Checkpoint — Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 8. Implement UnifiedSearchEngine
  - [x] 8.1 Create `bot/search/engine.py` with `UnifiedSearchEngine` class
    - `__init__` with cache_capacity=200, cache_ttl=60.0
    - `search()` method: query threshold gate (≥2 non-whitespace chars), URL detection bypass, cache lookup, parallel provider dispatch via asyncio.gather with 2s per-provider timeout
    - Provider configs: spotify=spsearch/10 results, tidal=tdsearch/8, youtube=ytsearch/7
    - Convert wavelink.Playable results to SearchResult objects
    - Call deduplicator, apply guild source_provider ordering
    - Store successful results in cache
    - Enforce 2800ms total pipeline budget with 2000ms search phase + 300ms dedup/format
    - Handle graceful degradation: exclude failed providers, log at WARNING
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 2.1, 2.2, 2.3, 9.1, 9.2, 9.3, 9.4, 18.1, 18.4, 18.5, 18.7_

  - [x] 8.2 Implement `search_streaming()` method on `UnifiedSearchEngine`
    - Use `asyncio.as_completed` pattern to fire `on_provider_result` callback as each provider responds
    - Apply filters (provider, content_type, sort_order) before returning
    - Deduplicate after all providers respond (or timeout)
    - _Requirements: 17.2, 17.7, 18.1, 18.5_

  - [ ]* 8.3 Write property test for query threshold gate (Property 1)
    - **Property 1: Query Threshold Gate**
    - Searches dispatch only for ≥2 non-whitespace chars; fewer returns empty without provider calls
    - **Validates: Requirements 1.1, 1.4**

  - [ ]* 8.4 Write property test for graceful degradation (Property 2)
    - **Property 2: Graceful Degradation Preserves Successful Results**
    - Final results contain all from successful providers, none from failed providers
    - **Validates: Requirements 2.1, 2.2**

  - [ ]* 8.5 Write property test for filter application (Property 14)
    - **Property 14: Filter Application Correctness**
    - When provider filter is not "all", only results from matching provider appear
    - **Validates: Requirements 18.5**

- [x] 9. Integrate with PlaybackCog autocomplete
  - [x] 9.1 Add `@play.autocomplete("query")` handler in `bot/cogs/playback.py`
    - Instantiate or reference shared `UnifiedSearchEngine` instance
    - Call `engine.search(query, guild_id=interaction.guild_id)`
    - Format results with `ChoiceFormatter.format_choices()`
    - Handle URL detection: return single choice `🔗 {platform_name} URL`
    - _Requirements: 10.1, 10.2, 10.5_

  - [x] 9.2 Update play command handler in `bot/cogs/playback.py` for value decoding
    - Use `ChoiceFormatter.decode_value()` to split prefix and track_id
    - Resolve via `wavelink.Playable.search()` with decoded Lavalink prefix
    - Fall through to PlaybackRouter if decode fails or resolution fails
    - Pass raw text to PlaybackRouter when value doesn't match encoding format
    - _Requirements: 10.2, 10.3, 10.4, 6.3, 6.4, 8.5_

- [x] 10. Checkpoint — Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 11. Implement WebSocket search handlers
  - [x] 11.1 Add `search_request`, `search_cancel`, `search_play`, and `search_enqueue` message handlers to `bot/video/ws_hub.py`
    - `search_request`: call `engine.search_streaming()` with `on_provider_result` callback that sends `search_partial_result` to client
    - Track active search by `request_id`; cancel previous on new request
    - Send `search_complete` when all providers done or timed out
    - `search_play`: decode provider + track_id, delegate to PlaybackRouter, send `search_play_ack`
    - `search_enqueue`: decode provider + track_id, add to unified queue, send `search_enqueue_ack` with position
    - `search_cancel`: cancel in-flight search tasks for given request_id
    - Handle errors: send `search_error` message on failure
    - _Requirements: 17.1, 17.2, 17.3, 17.4, 17.5, 17.6, 17.7, 17.8, 16.1, 16.3_

- [x] 12. Implement Activity search panel frontend
  - [x] 12.1 Create `bot/video/activity_frontend/search_panel.js` with search UI logic
    - Search mode activation via search icon/button
    - Text input with autofocus, 300ms debounce before sending query
    - Send `search_request` via WebSocket with query, filters, request_id (UUID v4)
    - Handle `search_partial_result`: render results progressively without clearing previous
    - Handle `search_complete`: remove loading indicators
    - Handle `search_error`: display error message
    - Discard stale results when new query submitted (match by request_id)
    - Loading spinner per pending provider badge
    - Cancel pending search on input clear or mode exit
    - _Requirements: 11.1, 11.2, 11.3, 11.4, 11.5, 11.6, 11.7, 17.1, 17.3, 17.4, 17.6_

  - [x] 12.2 Create `bot/video/activity_frontend/search_panel.css` with search panel styles
    - Dark glassmorphism styling consistent with existing Activity UI
    - Result rows: 48×48 album art, full title/artist/album/year/duration display
    - Provider badges as colored circles (🟢🔵🔴🟠) with opacity states
    - Expandable Track_Group controls
    - Filter bar styling (provider, content type, sort order)
    - Loading skeletons and spinner animations
    - Queue section with drag-to-reorder visual feedback
    - Responsive layout for Activity iframe dimensions
    - _Requirements: 12.1, 12.2, 12.3, 12.4, 12.5, 14.1, 14.4_

  - [x] 12.3 Implement expandable provider groups and result rendering in `search_panel.js`
    - Group results into Track_Groups using ISRC/normalized key dedup logic (client-side grouping from partial results)
    - Display highest-priority provider as primary entry per group
    - Collapsible expand control when ≥2 providers have the track
    - Reveal other providers on expand, listed in priority order
    - Preserve variants as separate groups
    - Default to collapsed state
    - _Requirements: 13.1, 13.2, 13.3, 13.4, 13.5, 13.6_

  - [x] 12.4 Implement provider badges with click-to-play
    - Render badges for all available providers on each Track_Group primary entry
    - Full opacity for displayed provider, 0.5 for others
    - Click badge → send `search_play` via WebSocket for that provider's version
    - Handle `search_play_ack`: show success/error indication
    - _Requirements: 14.1, 14.2, 14.3, 14.4, 14.5_

  - [x] 12.5 Implement search filters (provider, content type, sort order)
    - Provider filter: All, Spotify, Tidal, YouTube, SoundCloud (default: All)
    - Content type filter: Tracks, Albums, Playlists, Videos (default: Tracks, single select)
    - Sort order: Relevance, Duration, Year (default: Relevance)
    - Re-issue search on filter change if query is present
    - Persist filter selections for Activity session duration
    - _Requirements: 15.1, 15.2, 15.3, 15.4, 15.5, 15.6_

  - [x] 12.6 Implement queue integration (play, enqueue, reorder)
    - Click result → send `search_play` (interrupt current playback)
    - Long-press (500ms) or right-click → context menu with "Add to Queue"
    - Enqueue → send `search_enqueue`, show transient confirmation (2s)
    - Queue section: track title, artist, duration, provider icon per entry
    - Drag-to-reorder → send updated queue order via WebSocket
    - Error handling: display error indicator on failure, preserve queue state
    - _Requirements: 16.1, 16.2, 16.3, 16.4, 16.5, 16.6_

  - [x] 12.7 Wire search panel into existing Activity frontend (`index.html`, `app.js`)
    - Add search icon/button to existing Activity UI controls
    - Import search_panel.js and search_panel.css
    - Connect to existing WebSocket connection instance
    - Ensure search mode coexists with whiteboard/visualizer/lyrics modes
    - _Requirements: 11.1, 11.2, 11.3_

- [x] 13. Checkpoint — Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 14. Integration tests
  - [ ]* 14.1 Write integration tests for autocomplete end-to-end flow
    - Mock wavelink.Playable.search to return provider results
    - Verify full pipeline: search → dedup → format → choices
    - Test cache hit prevents provider dispatch
    - Test timing budget compliance with artificial delays
    - Test URL bypass flow
    - _Requirements: 1.1, 7.4, 8.1, 9.1_

  - [ ]* 14.2 Write integration tests for WebSocket search flow
    - Test search_request → partial_result → complete message sequence
    - Test search cancellation discards stale results
    - Test search_play → play_ack with mocked PlaybackRouter
    - Test search_enqueue → enqueue_ack with queue position
    - _Requirements: 17.1, 17.2, 17.6, 17.7_

  - [ ]* 14.3 Write unit tests for URL detection patterns
    - Test each recognized pattern: spotify, tidal, tidal/browse, youtube, youtu.be, soundcloud
    - Test non-matching URLs return None
    - Test URL value truncation at 100 chars
    - _Requirements: 8.1, 8.3, 8.4_

  - [ ]* 14.4 Write unit tests for choice formatting edge cases
    - Test formatting with/without duration, with/without music video indicator
    - Test title truncation with "…" when exceeding 100 chars
    - Test value encoding with very long track IDs
    - Test decode fallthrough for unrecognized formats
    - _Requirements: 5.1, 5.2, 5.3, 5.6, 6.4_

- [x] 15. Final checkpoint — Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- Checkpoints ensure incremental validation
- Property tests validate universal correctness properties from the design document
- Unit tests validate specific examples and edge cases
- The `bot/search/` package is self-contained — all pure logic (normalization, dedup, formatting, caching) is testable without Discord or wavelink dependencies
- Frontend tasks (12.x) depend on WebSocket handlers (11.1) being in place
- The shared `UnifiedSearchEngine` instance should be created once in the bot's setup and passed to both the PlaybackCog autocomplete handler and the WebSocket hub

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1"] },
    { "id": 1, "tasks": ["2.1", "4.1", "5.1"] },
    { "id": 2, "tasks": ["2.2", "2.3", "3.1", "4.2", "5.2", "6.1"] },
    { "id": 3, "tasks": ["3.2", "3.3", "3.4", "3.5", "6.2", "6.3", "6.4"] },
    { "id": 4, "tasks": ["8.1"] },
    { "id": 5, "tasks": ["8.2", "8.3", "8.4", "8.5", "9.1", "9.2"] },
    { "id": 6, "tasks": ["11.1"] },
    { "id": 7, "tasks": ["12.1", "12.2"] },
    { "id": 8, "tasks": ["12.3", "12.4", "12.5", "12.6"] },
    { "id": 9, "tasks": ["12.7"] },
    { "id": 10, "tasks": ["14.1", "14.2", "14.3", "14.4"] }
  ]
}
```

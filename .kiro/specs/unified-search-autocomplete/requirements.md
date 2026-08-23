# Requirements Document

## Introduction

Replace the current single-provider search in the `/play` command's autocomplete with a parallel multi-provider search engine that fans out to Spotify, Tidal, and YouTube simultaneously via Lavalink's wavelink API, deduplicates results by ISRC and normalized metadata, and presents up to 25 formatted choices within Discord's 3-second autocomplete timeout.

Additionally, provide a rich Activity-based search UI panel inside the existing Discord Activity iframe (port 8090) that delivers the full search experience beyond what Discord's 100-character autocomplete allows: expandable provider groups, album art thumbnails, full metadata display, provider badges, search filters, and WebSocket-driven live progressive results. Both interfaces share the same UnifiedSearchEngine backend.

## Glossary

- **Search_Engine**: The unified multi-provider search module that orchestrates parallel queries, deduplication, formatting, and caching for the `/play` autocomplete
- **Provider**: A music source backend accessible through Lavalink — specifically Spotify (spsearch), Tidal (tdsearch), and YouTube (ytsearch)
- **ISRC**: International Standard Recording Code — a 12-character alphanumeric identifier shared by the same recording across platforms
- **Deduplicator**: The component that identifies duplicate tracks across providers using ISRC and normalized metadata keys
- **Normalized_Key**: A fallback deduplication identifier derived from lowercase artist and title with remaster tags, year suffixes, and featuring credits stripped
- **Choice_Formatter**: The component that renders search results into Discord autocomplete choices respecting the 100-character name and value limits
- **Provider_Icon**: An emoji prefix indicating result source — 🟢 Spotify, 🔵 Tidal, 🔴 YouTube, 🟠 SoundCloud
- **Value_Encoding**: A compact string format encoding provider prefix and track ID within 100 characters (e.g., `sp:4uLU6hMCjMI75M1A2tKUQC`, `td:123456789`, `yt:dQw4w9WgXcQ`)
- **Result_Cache**: An in-memory LRU cache keyed by query prefix that stores formatted search results to reduce Lavalink load during rapid typing
- **URL_Detector**: The component that identifies recognizable platform URLs and bypasses search entirely
- **Variant**: A distinct version of a track (live, remix, acoustic, music video) that is preserved as a separate entry during deduplication
- **Activity_Search_Panel**: The rich HTML/CSS search interface rendered inside the Discord Activity iframe, providing full metadata display, expandable groups, and progressive WebSocket-driven results
- **Provider_Badge**: A small colored indicator showing which providers have a track available — clickable to play that provider's specific version
- **Track_Group**: A set of results representing the same recording across multiple providers, displayed as a collapsible group with the best version as the primary entry

## Requirements

### Requirement 1: Parallel Multi-Provider Search

**User Story:** As a user, I want the `/play` autocomplete to search Spotify, Tidal, and YouTube simultaneously, so that I get comprehensive results from all available sources without waiting for sequential searches.

#### Acceptance Criteria

1. WHEN autocomplete fires with a query of 2 or more non-whitespace characters, THE Search_Engine SHALL dispatch searches to Spotify (spsearch), Tidal (tdsearch), and YouTube (ytsearch) in parallel using asyncio.gather
2. THE Search_Engine SHALL use wavelink.Playable.search() for all provider queries without introducing additional HTTP clients
3. WHILE searches are in progress, THE Search_Engine SHALL enforce a 2-second timeout per provider via asyncio.wait_for
4. WHEN a query contains fewer than 2 non-whitespace characters (including empty or whitespace-only input), THE Search_Engine SHALL return an empty choice list without dispatching any searches
5. THE Search_Engine SHALL request exactly 10 results from Spotify, 8 from Tidal, and 7 from YouTube per query, for a combined maximum of 25 choices
6. IF a provider returns fewer results than requested, THEN THE Search_Engine SHALL include all results returned by that provider without redistributing its unused slots to other providers

### Requirement 2: Graceful Degradation on Provider Failure

**User Story:** As a user, I want autocomplete to still show results even when one or more providers are unavailable, so that my experience is not blocked by a single service outage.

#### Acceptance Criteria

1. IF a provider search times out or raises an exception, THEN THE Search_Engine SHALL exclude that provider's results and include results from all remaining successful providers in the response without surfacing an error to the user
2. IF all provider searches fail, THEN THE Search_Engine SHALL return an empty choice list with no error indication in the autocomplete response
3. THE Search_Engine SHALL log each provider failure at WARNING level with the provider name, exception type, and exception message
4. IF a provider returns zero results but does not error, THEN THE Search_Engine SHALL redistribute that provider's original slot allocation proportionally among providers that did return results, rounding fractional slots down and assigning any remainder to the highest-priority provider (Spotify > Tidal > YouTube), up to a combined maximum of 25 choices
5. IF only one provider returns results, THEN THE Search_Engine SHALL fill the response with up to 25 results from that single provider

### Requirement 3: ISRC-Based Deduplication

**User Story:** As a user, I want to see one entry per song rather than the same track repeated from multiple providers, so that the 25-choice limit shows maximum variety.

#### Acceptance Criteria

1. WHEN multiple providers return tracks with the same non-null ISRC that are not Variant tracks, THE Deduplicator SHALL retain only the highest-priority provider's version (priority: Spotify > Tidal > YouTube) and suppress duplicates from lower-priority providers
2. WHEN tracks lack an ISRC or have a null ISRC, THE Deduplicator SHALL fall back to Normalized_Key comparison to identify duplicates
3. THE Deduplicator SHALL preserve Variant tracks (live, remix, acoustic, music video) as separate entries even when the base ISRC matches, by appending the variant type to the deduplication key
4. WHEN a track has an ISRC but the same ISRC appears from only one provider, THE Deduplicator SHALL include it without modification

### Requirement 4: Normalized Key Generation

**User Story:** As a developer, I want a deterministic normalization function for track metadata, so that deduplication works reliably across providers with inconsistent title formatting.

#### Acceptance Criteria

1. THE Deduplicator SHALL generate a Normalized_Key by lowercasing the artist name and track title, collapsing consecutive whitespace to a single space, trimming leading and trailing whitespace, and concatenating as `{artist}:{title}`
2. THE Deduplicator SHALL strip the following patterns (case-insensitive) from the title during normalization: " - Remaster", " - Remastered YYYY", " (Remastered YYYY)", " (feat. ...)", " (ft. ...)" where YYYY is any 4-digit number in the range 1900–2099 and "..." extends to the closing delimiter
3. THE Deduplicator SHALL strip trailing year patterns matching " (YYYY)" or " [YYYY]" from the title, where YYYY is a 4-digit number in the range 1900–2099
4. FOR ALL tracks sharing the same ISRC across providers, normalizing their metadata and comparing keys SHALL produce identical Normalized_Key values
5. THE Deduplicator SHALL treat titles containing the whole words "Live", "Remix", "Acoustic", or "Music Video" (case-insensitive, matched at word boundaries) as Variant tracks that do not match the base recording
6. WHEN a title contains a substring that includes a variant keyword within a larger word (e.g., "Oliver", "Premixed"), THE Deduplicator SHALL NOT classify the track as a Variant

### Requirement 5: Choice Display Formatting

**User Story:** As a user, I want search results formatted clearly with provider icons and duration, so that I can quickly identify the source and length of each track.

#### Acceptance Criteria

1. THE Choice_Formatter SHALL prefix each choice name with the appropriate Provider_Icon (🟢 Spotify, 🔵 Tidal, 🔴 YouTube, 🟠 SoundCloud)
2. THE Choice_Formatter SHALL format the choice name as: `{icon} {artist} - {title} ({M:SS})` with the 🎬 indicator inserted after the icon when the track has an associated music video
3. THE Choice_Formatter SHALL truncate the title portion first (appending "…") to ensure the total choice name does not exceed 100 characters including icon, artist, duration, and any indicators
4. WHEN the track duration exceeds 59 minutes, THE Choice_Formatter SHALL format duration as `H:MM:SS` (e.g., `1:02:15`)
5. THE Choice_Formatter SHALL format duration as minutes and seconds in `M:SS` format (e.g., `3:45`, `12:01`) for tracks under 60 minutes
6. WHEN duration metadata is unavailable, THE Choice_Formatter SHALL omit the duration portion entirely from the choice name

### Requirement 6: Value Encoding and Decoding

**User Story:** As a developer, I want a compact encoding for track identifiers in autocomplete values, so that the play handler can resolve the selected track without ambiguity.

#### Acceptance Criteria

1. THE Choice_Formatter SHALL encode the choice value as `{prefix}:{track_id}` where prefix is `sp` (Spotify), `td` (Tidal), `yt` (YouTube), or `sc` (SoundCloud)
2. IF the encoded value would exceed 100 characters, THEN THE Choice_Formatter SHALL truncate the track_id portion so that the total encoded value is exactly 100 characters
3. WHEN a user selects an autocomplete choice, THE Play_Handler SHALL split the value on the first `:` character and decode the prefix into the appropriate Lavalink search prefix (sp → spsearch, td → tdsearch, yt → ytsearch, sc → scsearch)
4. IF the encoded value does not contain a recognized prefix (sp, td, yt, sc) or does not match the `{prefix}:{track_id}` format, THEN THE Play_Handler SHALL treat the raw value as a direct search query and fall through to the existing search flow
5. THE Choice_Formatter SHALL guarantee that encoding a track identifier and then decoding the result produces the original provider and track_id for all values where the total encoded length does not exceed 100 characters (round-trip property)

### Requirement 7: Result Caching

**User Story:** As a user, I want autocomplete to respond quickly when I'm typing, so that results appear without noticeable lag even when refining my query.

#### Acceptance Criteria

1. THE Result_Cache SHALL store formatted choice lists keyed by the query string normalized to lowercase with leading/trailing whitespace stripped and internal whitespace collapsed to a single space
2. THE Result_Cache SHALL have a maximum capacity of 200 entries using LRU eviction
3. THE Result_Cache SHALL expire entries after 60 seconds, treating any entry older than 60 seconds as a cache miss on access
4. WHEN a cache hit occurs for a non-expired entry, THE Search_Engine SHALL return the cached choices without dispatching any provider searches
5. THE Result_Cache SHALL be per-process in-memory storage (no external dependencies)
6. WHEN a search completes successfully with one or more results, THE Search_Engine SHALL store the formatted choice list in the Result_Cache keyed by the normalized query string

### Requirement 8: URL Detection and Bypass

**User Story:** As a user, I want to paste a URL into the play command and see it recognized immediately without triggering a text search, so that direct links play without delay.

#### Acceptance Criteria

1. WHEN the query starts with `http://` or `https://` and contains a recognized platform domain and path pattern (Spotify, Tidal, YouTube, or SoundCloud), THE URL_Detector SHALL bypass search entirely and return the result without dispatching any provider searches
2. WHEN a URL is detected, THE Search_Engine SHALL return a single formatted choice with the name `🔗 {platform_name} URL` where platform_name is one of "Spotify", "Tidal", "YouTube", or "SoundCloud" matching the detected domain
3. THE URL_Detector SHALL recognize the following URL patterns: spotify.com/track, tidal.com/track, tidal.com/browse/track, youtube.com/watch, youtu.be/, soundcloud.com/ — matching any URL that contains these domain and path prefixes regardless of additional path segments, query parameters, or fragments
4. THE URL_Detector SHALL encode the full URL as the choice value, truncating from the end of the string to 100 characters if the URL exceeds that length
5. WHEN a user selects a URL-type autocomplete choice, THE Play_Handler SHALL pass the raw URL directly to wavelink.Playable.search() for resolution instead of decoding it as a prefix:id value

### Requirement 9: Timing Budget Compliance

**User Story:** As a user, I want autocomplete to always respond within Discord's 3-second limit, so that I never see a timeout error or missing suggestions.

#### Acceptance Criteria

1. THE Search_Engine SHALL complete the full autocomplete pipeline (search, dedup, format, respond) within 2800ms of receiving the autocomplete interaction
2. WHILE the parallel search phase is in progress, THE Search_Engine SHALL abort remaining provider searches after 2000ms and proceed with available results
3. THE Search_Engine SHALL allocate no more than 300ms for deduplication and formatting after the search phase completes
4. IF the total pipeline elapsed time reaches 2700ms before formatting is complete, THEN THE Search_Engine SHALL immediately return the results that have completed deduplication and formatting at that point, skipping any unprocessed results

### Requirement 10: Integration with Existing Play Command

**User Story:** As a user, I want the autocomplete to work seamlessly with the existing `/play` command, so that selecting a result plays the track through the existing playback pipeline.

#### Acceptance Criteria

1. THE Search_Engine SHALL register as the autocomplete handler for the `query` parameter of the `/play` command in PlaybackCog
2. WHEN a user selects an autocomplete choice whose value matches the Value_Encoding format (`{prefix}:{track_id}`), THE Play_Handler SHALL decode the prefix into the corresponding Lavalink search prefix and resolve the track using wavelink.Playable.search()
3. IF the Play_Handler fails to resolve a selected autocomplete value to a playable track, THEN THE Play_Handler SHALL fall through to the existing search-and-pick flow in PlaybackRouter using the original query text
4. WHEN a user submits text that does not match the Value_Encoding format, THE Play_Handler SHALL delegate the query to PlaybackRouter.play() without modification
5. THE Search_Engine SHALL order deduplicated results so that the provider matching the guild's current source_provider setting (as stored in player guild state) appears first, followed by remaining providers in default priority order (Spotify > Tidal > YouTube)

### Requirement 11: Activity Search Panel

**User Story:** As a user, I want a rich search interface inside the Discord Activity, so that I can browse results with full metadata, album art, and expandable groups that Discord's native autocomplete cannot provide.

#### Acceptance Criteria

1. THE Activity_Search_Panel SHALL render as a "Search" mode accessible via a search icon/button in the existing Activity frontend UI
2. WHEN the user activates the search mode, THE Activity_Search_Panel SHALL display a text input field with autofocus and a scrollable results area
3. THE Activity_Search_Panel SHALL communicate with the bot backend exclusively via the existing WebSocket connection (ws_hub)
4. WHEN the user types in the search input with 2 or more non-whitespace characters, THE Activity_Search_Panel SHALL send the query to the backend after a 300ms debounce interval
5. THE Activity_Search_Panel SHALL display a loading indicator while results are being fetched
6. WHEN the user clears the search input or navigates away from search mode, THE Activity_Search_Panel SHALL clear all displayed results and cancel any pending search requests
7. IF the WebSocket connection is unavailable when a search is attempted, THE Activity_Search_Panel SHALL display an error message indicating the connection issue

### Requirement 12: Rich Result Display

**User Story:** As a user, I want each search result to show album art, full track title, artist, album, year, duration, and provider badges, so that I can make informed choices without the 100-character truncation of Discord autocomplete.

#### Acceptance Criteria

1. THE Activity_Search_Panel SHALL display each result with: album art thumbnail, track title, artist name, album name, release year, duration, and Provider_Badge indicators
2. THE Activity_Search_Panel SHALL render album art thumbnails at 48×48 CSS pixels, and IF the artwork URL is unavailable or fails to load, THEN THE Activity_Search_Panel SHALL display a generic placeholder image at the same dimensions
3. THE Activity_Search_Panel SHALL display track titles and artist names at full length, wrapping text to additional lines rather than truncating when content exceeds the available row width
4. THE Activity_Search_Panel SHALL format duration as `M:SS` (e.g., `3:45`, `12:01`)
5. WHEN album name or release year is unavailable from the provider, THE Activity_Search_Panel SHALL omit that field rather than displaying a placeholder

### Requirement 13: Expandable Provider Groups

**User Story:** As a user, I want results grouped by canonical track so I can see one entry per song with the option to expand and pick a specific provider's version.

#### Acceptance Criteria

1. THE Activity_Search_Panel SHALL group results into Track_Groups using the same ISRC/Normalized_Key deduplication logic as the autocomplete
2. THE Activity_Search_Panel SHALL display the highest-priority provider's version (Spotify > Tidal > YouTube) as the primary entry for each Track_Group
3. WHEN a Track_Group contains results from 2 or more providers, THE Activity_Search_Panel SHALL display a collapsible expand control on the primary entry indicating the number of additional providers available
4. WHEN the user expands a Track_Group, THE Activity_Search_Panel SHALL reveal the same track from all other providers that returned it, listed in priority order (Spotify > Tidal > YouTube)
5. THE Activity_Search_Panel SHALL preserve Variant tracks (live, remix, acoustic, music video) as separate Track_Groups
6. THE Activity_Search_Panel SHALL render all Track_Groups in collapsed state by default, showing only the primary entry until the user activates the expand control

### Requirement 14: Provider Badges

**User Story:** As a user, I want to see colored badges on each result showing which providers have it, so that I can quickly identify availability and click a badge to play a specific version.

#### Acceptance Criteria

1. THE Activity_Search_Panel SHALL display Provider_Badges as colored circular indicators (🟢 Spotify, 🔵 Tidal, 🔴 YouTube, 🟠 SoundCloud) on each Track_Group's primary entry, rendered in priority order (Spotify, Tidal, YouTube, SoundCloud) from left to right
2. THE Activity_Search_Panel SHALL show Provider_Badges for ALL providers that have the track available, not only the displayed provider
3. WHEN the user clicks a Provider_Badge, THE Activity_Search_Panel SHALL send a play command via the existing WebSocket connection to initiate playback of that specific provider's version of the track
4. THE Activity_Search_Panel SHALL visually distinguish the currently-displayed provider's badge from the other available providers by rendering it at full opacity (1.0) while rendering other provider badges at reduced opacity (0.5)
5. IF playback initiation fails after a Provider_Badge click, THEN THE Activity_Search_Panel SHALL display an error indication to the user and leave the current playback state unchanged

### Requirement 15: Search Filters

**User Story:** As a user, I want filter controls for provider, content type, and sort order, so that I can narrow results to exactly what I'm looking for.

#### Acceptance Criteria

1. THE Activity_Search_Panel SHALL provide a provider filter with options: All, Spotify, Tidal, YouTube, SoundCloud, with "All" selected by default
2. THE Activity_Search_Panel SHALL provide a content type filter with options: Tracks, Albums, Playlists, Videos, with "Tracks" selected by default, allowing only one content type selection at a time
3. THE Activity_Search_Panel SHALL provide a sort order control with options: Relevance (provider's native ranking order), Duration (shortest first), Year (newest first)
4. WHEN a filter is changed and a non-empty search query is present, THE Activity_Search_Panel SHALL re-issue the current search query with the updated filter parameters
5. IF a filter is changed and no search query has been entered, THEN THE Activity_Search_Panel SHALL store the updated filter selection without issuing a search
6. THE Activity_Search_Panel SHALL persist filter selections for the duration of the Activity session (reset on Activity close)

### Requirement 16: Queue Integration from Activity Search

**User Story:** As a user, I want to play immediately or add to queue directly from the Activity search panel, so that I can manage playback without switching back to Discord slash commands.

#### Acceptance Criteria

1. WHEN the user clicks a search result, THE Activity_Search_Panel SHALL send a play command to the bot backend via WebSocket that interrupts any currently playing track and begins playback of the selected track
2. WHEN the user long-presses (500ms or more) or right-clicks a search result, THE Activity_Search_Panel SHALL display a context menu with an "Add to Queue" option
3. WHEN "Add to Queue" is selected, THE Activity_Search_Panel SHALL enqueue the track via WebSocket without interrupting current playback, and SHALL display a transient confirmation indicator (visible for at least 2 seconds) on the triggering result
4. THE Activity_Search_Panel SHALL display a queue section showing the current queue with track title, artist name, duration, and Provider_Icon per entry, with drag-to-reorder capability
5. WHEN the user reorders the queue via drag, THE Activity_Search_Panel SHALL send the updated queue order to the bot backend via WebSocket
6. IF a play or enqueue WebSocket command fails or the WebSocket connection is unavailable, THEN THE Activity_Search_Panel SHALL display an error indicator on the affected result and SHALL NOT remove any previously displayed queue state

### Requirement 17: WebSocket Live Search

**User Story:** As a user, I want search results to appear progressively as providers respond, so that I see fast providers' results immediately without waiting for slow ones.

#### Acceptance Criteria

1. WHEN a search query is submitted, THE Activity_Search_Panel SHALL send a `search_request` message to the bot backend via WebSocket
2. WHEN a provider returns results for the active search query, THE Search_Engine SHALL send a `search_partial_result` message to the Activity_Search_Panel containing that provider's results
3. WHEN the first provider responds, THE Activity_Search_Panel SHALL render those results without waiting for remaining providers
4. WHEN additional providers respond, THE Activity_Search_Panel SHALL merge new results into the existing display (re-grouping Track_Groups as needed) without clearing previously shown results
5. WHILE providers have not yet responded for the active search query, THE Activity_Search_Panel SHALL display a loading spinner on each pending provider's badge, and remove the spinner when that provider's results arrive or when the provider fails
6. WHEN a new search query is submitted while a previous search is still streaming results, THE Activity_Search_Panel SHALL discard any further incoming results from the previous query and clear the display before rendering results for the new query
7. WHEN all providers have either responded or failed for the active search query, THE Search_Engine SHALL send a `search_complete` message to the Activity_Search_Panel to signal that no further results will arrive
8. IF a provider fails or times out during streaming, THEN THE Activity_Search_Panel SHALL mark that provider's badge as unavailable and continue displaying results from the remaining providers

### Requirement 18: Shared Search Engine Backend

**User Story:** As a developer, I want both the autocomplete handler and the Activity search panel to use the same backend search engine, so that deduplication logic and provider management are not duplicated.

#### Acceptance Criteria

1. THE Search_Engine SHALL expose a unified search method that accepts a query string and optional filter parameters, and returns structured results containing: track title, artist name, album name, release year, duration in milliseconds, album art URL, ISRC, provider identifier, and provider-specific track ID
2. THE Choice_Formatter SHALL consume the unified search results and produce truncated 100-character Discord autocomplete choices
3. THE Activity_Search_Panel backend handler SHALL consume the same unified search results and forward all returned metadata fields (including album art URLs) to the WebSocket client without truncation
4. WHEN the autocomplete handler calls the unified search method without filter parameters, THE Search_Engine SHALL query all configured providers and return unfiltered results ordered by provider priority (Spotify > Tidal > YouTube)
5. WHEN the Activity_Search_Panel calls the unified search method with filter parameters (provider, content type, sort order), THE Search_Engine SHALL apply those filters before returning results
6. THE Search_Engine SHALL use cache keys that incorporate both the query string and any applied filter parameters, so that filtered and unfiltered queries for the same text are cached independently
7. IF the unified search method raises an exception, THEN both the autocomplete handler and the Activity_Search_Panel backend handler SHALL return an empty result set to their respective consumers

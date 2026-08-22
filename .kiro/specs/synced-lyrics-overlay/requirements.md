# Requirements Document

## Introduction

This feature adds synchronized lyrics display overlaid on the HelloDJ Discord Activity viewport (video player or visualizer). Lyrics are fetched from external sources (LRCLIB.net for time-synced LRC data, Genius API for plain text fallback), timing is computed server-side, and the overlay renders client-side via CSS/JS. Two control methods are provided: an in-Activity toggle button and a `/lyrics overlay` slash command option that broadcasts state to all viewers.

## Glossary

- **Activity_Frontend**: The HTML/JS/CSS application rendered inside the Discord Activity iframe, served by the Activity_Backend
- **Lyrics_Overlay**: A semi-transparent CSS/JS layer rendered on the Activity_Frontend that displays synchronized lyrics text above the video/visualizer but below the whiteboard and controls
- **Lyrics_Service**: A server-side module responsible for fetching lyrics from external sources, computing timing data, and delivering synced lyrics payloads via WebSocket
- **LRCLIB_Provider**: A component that queries the LRCLIB.net free API to retrieve time-synced LRC lyrics (line-level or word-level timestamps)
- **Genius_Provider**: The existing Genius API integration that retrieves plain text lyrics without timing information
- **LRC_Format**: A lyric file format where each line is prefixed with a timestamp (e.g., `[00:12.34]Line of lyrics`), optionally with word-level timestamps
- **Beat_Estimated_Timing**: A timing strategy that distributes plain text lyrics across the song duration, aligning line transitions to detected beats from the Audio_Feature_Bus when available
- **Audio_Feature_Bus**: The subscriber-gated audio analysis pipeline (from activity-visualizer spec) providing FFT, beat detection, and BPM data
- **WebSocket_Hub**: The per-guild WebSocket connection manager that synchronizes playback state and lyrics overlay state across connected clients
- **Lyrics_State**: The per-guild server-side state tracking whether the lyrics overlay is enabled, which song's lyrics are loaded, and the timing payload
- **Karaoke_Highlight**: A word-by-word progressive highlight effect applied when word-level sync data is available from LRCLIB

## Requirements

### Requirement 1: Lyrics Source Resolution

**User Story:** As a viewer, I want the bot to automatically find the best available lyrics for the current song, so that I see synced lyrics without manual intervention.

#### Acceptance Criteria

1. WHEN a song starts playing AND the lyrics overlay is enabled, THE Lyrics_Service SHALL attempt to fetch lyrics from LRCLIB_Provider first, then fall back to Genius_Provider if LRCLIB returns no results
2. WHEN the LRCLIB_Provider receives a query, THE LRCLIB_Provider SHALL search using the track title and artist name extracted from the current queue entry metadata
3. WHEN the LRCLIB_Provider returns a synced LRC result, THE Lyrics_Service SHALL use the LRC timestamps directly without further timing computation
4. WHEN the LRCLIB_Provider returns no results AND the Genius_Provider returns plain text lyrics, THE Lyrics_Service SHALL compute Beat_Estimated_Timing for line transitions
5. IF both the LRCLIB_Provider and Genius_Provider return no results, THEN THE Lyrics_Service SHALL broadcast a `lyrics_unavailable` message to connected clients
6. WHEN the LRCLIB_Provider returns word-level synced lyrics, THE Lyrics_Service SHALL preserve word-level timestamp data in the payload sent to clients

### Requirement 2: Beat-Estimated Timing Computation

**User Story:** As a viewer, I want plain text lyrics to advance in time with the music, so that the overlay feels synchronized even without pre-synced timestamps.

#### Acceptance Criteria

1. WHEN the Lyrics_Service receives plain text lyrics without timestamps, THE Lyrics_Service SHALL distribute lyrics lines across the total song duration proportionally by line length (character count)
2. WHILE the Audio_Feature_Bus has at least one subscriber providing beat detection data, THE Lyrics_Service SHALL snap line transition timestamps to the nearest detected beat boundary
3. WHEN the Audio_Feature_Bus is unavailable or has zero subscribers, THE Lyrics_Service SHALL fall back to evenly distributing lines across the song duration without beat alignment
4. THE Lyrics_Service SHALL compute all timing data server-side and deliver the complete timed lyrics payload to clients via a single WebSocket message
5. WHEN a song's total duration is unknown or exceeds 24 hours (live stream), THE Lyrics_Service SHALL not attempt timing computation and SHALL broadcast a `lyrics_unavailable` message

### Requirement 3: Lyrics Overlay Rendering

**User Story:** As a viewer, I want lyrics to display as a readable overlay on the Activity viewport, so that I can follow along with the music while watching the video or visualizer.

#### Acceptance Criteria

1. THE Lyrics_Overlay SHALL render as a semi-transparent panel positioned at the bottom of the Activity_Frontend viewport
2. THE Lyrics_Overlay SHALL display the current lyric line with full opacity, the previous line above it with reduced opacity, and the next line below it with reduced opacity
3. WHEN a line transition occurs, THE Lyrics_Overlay SHALL animate the transition using a smooth vertical scroll effect
4. THE Lyrics_Overlay SHALL use a z-index that places it above the video player and visualizer but below the whiteboard overlay and playback controls
5. WHEN word-level sync data is available, THE Lyrics_Overlay SHALL apply a Karaoke_Highlight effect that progressively illuminates each word as its timestamp is reached
6. THE Lyrics_Overlay SHALL render using client-side CSS and JavaScript without requiring server-side GPU resources
7. WHILE the lyrics overlay is enabled AND no lyrics are available for the current track, THE Lyrics_Overlay SHALL display a brief "No lyrics available" message that auto-dismisses after 3 seconds

### Requirement 4: Activity Toggle Control

**User Story:** As a viewer, I want a button in the Activity to toggle lyrics on and off, so that I can control my own viewing experience.

#### Acceptance Criteria

1. THE Activity_Frontend SHALL display a "Lyrics" toggle button in the controls area, styled consistently with existing toggle buttons (whiteboard toggle)
2. WHEN a viewer clicks the Lyrics toggle button, THE Activity_Frontend SHALL show or hide the Lyrics_Overlay locally for that viewer only
3. THE Activity_Frontend SHALL persist the viewer's local lyrics toggle preference in browser localStorage so it survives page refreshes
4. WHEN the lyrics overlay is force-enabled via the `/lyrics overlay:on` command, THE Activity_Frontend SHALL activate the overlay regardless of the local toggle state
5. WHEN the lyrics overlay is force-disabled via the `/lyrics overlay:off` command, THE Activity_Frontend SHALL deactivate the overlay regardless of the local toggle state

### Requirement 5: Slash Command Overlay Control

**User Story:** As a server member, I want a `/lyrics` command option to toggle the overlay for everyone, so that I can share the lyrics experience with all viewers in the Activity.

#### Acceptance Criteria

1. WHEN a user invokes `/lyrics overlay:on`, THE Bot SHALL broadcast a `lyrics_overlay_enable` message via WebSocket_Hub to all connected viewers for the guild
2. WHEN a user invokes `/lyrics overlay:off`, THE Bot SHALL broadcast a `lyrics_overlay_disable` message via WebSocket_Hub to all connected viewers for the guild
3. WHEN a user invokes `/lyrics` without the `overlay` option, THE Bot SHALL display lyrics as a text embed in the Discord chat channel (preserving existing behavior)
4. THE `/lyrics` command SHALL accept an optional `overlay` parameter with allowed values `on` and `off`
5. WHEN the `/lyrics overlay:on` command is invoked AND no song is currently playing, THE Bot SHALL respond with an ephemeral message indicating nothing is playing

### Requirement 6: WebSocket Lyrics Sync Protocol

**User Story:** As a late-joining viewer, I want to receive the current lyrics state when I connect, so that my overlay is immediately in sync with other viewers.

#### Acceptance Criteria

1. WHEN a WebSocket client connects to a guild session where the lyrics overlay is enabled, THE WebSocket_Hub SHALL send the current Lyrics_State including the full timed lyrics payload and current playback position
2. WHEN a new song starts playing AND the lyrics overlay is enabled, THE Lyrics_Service SHALL fetch lyrics for the new track and broadcast the updated timed payload to all connected clients
3. WHEN lyrics are successfully resolved for a track, THE WebSocket_Hub SHALL broadcast a `lyrics_data` message containing the complete array of timed lines (and word-level timestamps if available)
4. THE `lyrics_data` WebSocket message SHALL include: the track identifier, an array of lyric lines with start timestamps, sync type (lrc_line, lrc_word, or beat_estimated), and total song duration in seconds
5. WHEN playback is paused, THE Lyrics_Overlay SHALL pause line advancement at the current position
6. WHEN playback is seeked to a new position, THE Activity_Frontend SHALL recalculate the current lyric line based on the new playback position

### Requirement 7: Overlay Z-Index and Layout Coexistence

**User Story:** As a viewer, I want the lyrics overlay to coexist with the video, visualizer, whiteboard, and controls without interfering with interactive elements.

#### Acceptance Criteria

1. THE Activity_Frontend SHALL enforce the following z-index order from bottom to top: video/visualizer → Lyrics_Overlay → whiteboard → playback controls
2. THE Lyrics_Overlay SHALL not intercept pointer events for areas outside the visible lyrics text, allowing click-through to underlying video controls
3. WHILE the whiteboard overlay is active, THE Lyrics_Overlay SHALL remain visible beneath the whiteboard layer without obstructing whiteboard drawing
4. THE Lyrics_Overlay SHALL be responsive and adapt its font size and panel height to the Activity_Frontend viewport dimensions
5. WHEN the Activity_Frontend viewport width is below 400 pixels, THE Lyrics_Overlay SHALL display only the current line (single-line mode) to conserve space

### Requirement 8: LRCLIB API Integration

**User Story:** As the system, I want a reliable integration with LRCLIB.net, so that time-synced lyrics are available for the majority of popular tracks.

#### Acceptance Criteria

1. WHEN querying LRCLIB.net, THE LRCLIB_Provider SHALL use the `GET /api/get` endpoint with `artist_name`, `track_name`, and `duration` parameters
2. WHEN the LRCLIB API returns a response with a `syncedLyrics` field, THE LRCLIB_Provider SHALL parse the LRC-formatted string into an array of timed lines
3. WHEN the LRCLIB API returns a response with only a `plainLyrics` field (no synced data), THE LRCLIB_Provider SHALL pass the plain text to the Beat_Estimated_Timing computation
4. IF the LRCLIB API returns an HTTP error or times out (within 5 seconds), THEN THE LRCLIB_Provider SHALL log the error and fall through to the Genius_Provider without blocking playback
5. THE LRCLIB_Provider SHALL include a `User-Agent` header identifying the bot (e.g., `HelloDJ/1.0`) per LRCLIB API etiquette guidelines
6. FOR ALL valid LRC strings, parsing then formatting back to LRC then parsing again SHALL produce an equivalent timed line array (round-trip property)

### Requirement 9: Graceful Degradation

**User Story:** As a viewer, I want the lyrics overlay to degrade gracefully when components are unavailable, so that partial functionality is still provided rather than errors.

#### Acceptance Criteria

1. WHEN the Audio_Feature_Bus is unavailable, THE Lyrics_Service SHALL compute timing using even distribution across song duration without beat alignment
2. WHEN both lyrics providers fail to return results, THE Lyrics_Overlay SHALL display "No lyrics available" and auto-dismiss without disrupting the Activity experience
3. IF the Lyrics_Service encounters an unhandled exception during lyrics fetch or timing computation, THEN THE Lyrics_Service SHALL log the error and broadcast `lyrics_unavailable` to clients without affecting audio playback or video streaming
4. WHEN a track has no duration metadata available, THE Lyrics_Service SHALL skip timing computation and broadcast `lyrics_unavailable`
5. THE Lyrics_Service lifecycle SHALL operate independently from the Lavalink audio playback pipeline, ensuring lyrics failures never interrupt music playback

# Lavalink Music Source Integration — Requirements

## Overview

Wire up HelloDJ's Lavalink instance to support all major music streaming platforms:
Spotify, Tidal, Deezer, YouTube, YouTube Music, Apple Music, YouTube Videos, and
Tidal Music Videos. Each source should be searchable, playable, and switchable via
the existing `/source` command.

## Requirements

### REQ-1: Spotify Integration
**Priority:** High
**Status:** Partial (metadata lookup via lavasrc `spsearch:` → YouTube audio fallback)

**Acceptance Criteria:**
- [ ] Spotify track URLs (`open.spotify.com/track/...`) resolve and play audio
- [ ] Spotify playlist URLs load all tracks into the queue
- [ ] Spotify album URLs load all tracks into the queue
- [ ] `spsearch:` prefix searches Spotify catalog and returns results
- [ ] `/source spotify` sets the guild's default provider to Spotify
- [ ] Audio is resolved via YouTube (lavasrc's ISR flow) or native Spotify streams if available
- [ ] Spotify client credentials (SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET) authenticate the plugin
- [ ] Error handling: graceful fallback to YouTube search if Spotify resolution fails

### REQ-2: Tidal Integration
**Priority:** High
**Status:** Partial (lavasrc `tdsearch:` + custom tidal.py client for video)

**Acceptance Criteria:**
- [ ] Tidal track URLs (`tidal.com/track/...`) resolve and play audio
- [ ] Tidal playlist URLs load all tracks into the queue
- [ ] Tidal album URLs load all tracks into the queue
- [ ] `tdsearch:` prefix searches Tidal catalog
- [ ] `/source tidal` sets the guild's default provider to Tidal
- [ ] Tidal credentials (TIDAL_CLIENT_ID, TIDAL_CLIENT_SECRET) authenticate the plugin
- [ ] Country code configurable via TIDAL_COUNTRY_CODE (default: US)
- [ ] Search limit configurable via TIDAL_SEARCH_LIMIT (default: 10)
- [ ] Error handling: graceful fallback to YouTube if Tidal resolution fails

### REQ-3: Deezer Integration
**Priority:** Medium
**Status:** Not implemented

**Acceptance Criteria:**
- [ ] Deezer track URLs (`deezer.com/track/...`) resolve and play audio
- [ ] Deezer playlist URLs load all tracks into the queue
- [ ] Deezer album URLs load all tracks into the queue
- [ ] `dzsearch:` prefix searches Deezer catalog
- [ ] `/source deezer` sets the guild's default provider to Deezer
- [ ] lavasrc plugin configured with Deezer source enabled
- [ ] No API key required (Deezer's public API via lavasrc)
- [ ] Error handling: graceful fallback to YouTube if Deezer resolution fails

### REQ-4: YouTube Integration
**Priority:** High
**Status:** Working (youtube-source plugin with OAuth + poToken)

**Acceptance Criteria:**
- [ ] YouTube video URLs play audio
- [ ] YouTube playlist URLs load all tracks into the queue
- [ ] `ytsearch:` prefix searches YouTube
- [ ] `/source youtube` sets the guild's default provider to YouTube
- [ ] YouTube OAuth refresh token pushed to plugin on startup
- [ ] poToken/visitorData pushed for bot-detection bypass
- [ ] Remote cipher server configured for signature deciphering
- [ ] Client priority: MUSIC → TV → TVHTML5_SIMPLY → ANDROID_VR → WEB → WEBEMBEDDED
- [ ] Age-restricted content accessible via OAuth (TV client)

### REQ-5: YouTube Music Integration
**Priority:** High
**Status:** Working (youtube-source plugin MUSIC client)

**Acceptance Criteria:**
- [ ] YouTube Music URLs (`music.youtube.com/...`) play audio
- [ ] `ytmsearch:` prefix searches YouTube Music catalog
- [ ] `/source youtube_music` sets the guild's default provider to YouTube Music
- [ ] Higher audio quality than standard YouTube (audio-only streams preferred)
- [ ] YouTube Music playlists and albums resolve correctly

### REQ-6: Apple Music Integration
**Priority:** Medium
**Status:** Not implemented

**Acceptance Criteria:**
- [ ] Apple Music track URLs (`music.apple.com/...`) resolve and play audio
- [ ] Apple Music playlist URLs load all tracks into the queue
- [ ] Apple Music album URLs load all tracks into the queue
- [ ] `amsearch:` prefix searches Apple Music catalog
- [ ] `/source apple_music` sets the guild's default provider to Apple Music
- [ ] lavasrc plugin configured with Apple Music source enabled
- [ ] Apple Music media API token configured (APPLE_MUSIC_MEDIA_API_TOKEN)
- [ ] Country code configurable (APPLE_MUSIC_COUNTRY_CODE, default: US)
- [ ] Error handling: graceful fallback to YouTube if Apple Music resolution fails

### REQ-7: YouTube Video Playback (Audio from Video)
**Priority:** High
**Status:** Working (inherent to YouTube source)

**Acceptance Criteria:**
- [ ] Any YouTube video URL plays its audio track in voice
- [ ] Video metadata (title, thumbnail, duration) displayed in now-playing embed
- [ ] Direct video ID resolution via `allowDirectVideoIds: true`
- [ ] Livestream URLs detected and handled (continuous play, no track-end)
- [ ] Shorts URLs (`youtube.com/shorts/...`) resolve correctly

### REQ-8: Tidal Music Videos
**Priority:** Medium
**Status:** Partial (custom tidal.py fetches video URL, /stream posts to text channel)

**Acceptance Criteria:**
- [ ] `/stream <query>` searches Tidal for the track's music video
- [ ] If a music video exists: downloads via yt-dlp and posts to text channel as embedded video
- [ ] Audio from the video plays simultaneously in voice channel
- [ ] If no video: falls back to audio-only + YouTube link in text channel
- [ ] Video file size check: if > Discord attachment limit, post as link embed instead
- [ ] Tidal video stream URL resolution via custom tidal.py client
- [ ] Error handling: timeout, download failure, and codec issues handled gracefully

### REQ-9: Source Switching (`/source` command)
**Priority:** High
**Status:** Working (youtube, youtube_music, soundcloud, spotify, tidal)

**Acceptance Criteria:**
- [ ] `/source` command accepts: youtube, youtube_music, soundcloud, spotify, tidal, deezer, apple_music
- [ ] Source preference persisted per-guild (survives restart via session.py)
- [ ] Source affects all subsequent `/play` and autoplay searches
- [ ] Direct URLs bypass source preference (URL type detected automatically)
- [ ] Autocomplete lists all available/configured sources

### REQ-10: Unified Error Handling & Fallback Chain
**Priority:** High
**Status:** Partial

**Acceptance Criteria:**
- [ ] If the primary source fails, fallback to YouTube search automatically
- [ ] Fallback logged at WARNING level with the original error
- [ ] User notified when fallback is used (subtle embed footer note)
- [ ] Track blacklist (`data/track_blacklist.json`) honored across all sources
- [ ] Rate-limit / auth errors surface a clear user-facing message
- [ ] All source errors tracked in the debug framework (debug.py)

## Non-Functional Requirements

### NFR-1: Configuration
- All credentials via environment variables (never hardcoded)
- Provider enable/disable toggles: PROVIDER_YOUTUBE, PROVIDER_SPOTIFY, PROVIDER_TIDAL, PROVIDER_DEEZER, PROVIDER_APPLE_MUSIC
- Missing credentials = source disabled gracefully (not a crash)

### NFR-2: Performance
- Source resolution should complete within 10 seconds (timeout + fallback)
- Playlist loading should be chunked (don't block the queue for 500-track playlists)
- Token refresh should be lazy and cached (no token fetch per-request)

### NFR-3: Observability
- Every source resolution logged via debug.py `get_debug_logger("source")`
- Events: `source_resolve_start`, `source_resolve_success`, `source_resolve_fallback`, `source_resolve_fail`
- Timing: elapsed_ms for each resolution
- Token lifecycle: `token_refresh`, `token_expired`, `token_cached`

### NFR-4: Lavalink Plugin Compatibility
- youtube-source plugin: v1.18.2+ (current)
- lavasrc plugin: v4.2.0+ (current, supports Spotify/Tidal/Deezer/Apple Music)
- Both plugins must be declared in `lavalink/application.yml`
- Plugin JARs auto-downloaded by Lavalink on startup (via dependency declaration)

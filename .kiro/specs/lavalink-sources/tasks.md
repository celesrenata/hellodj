# Lavalink Music Source Integration — Tasks

## Task 1: Update Lavalink application.yml for Deezer and Apple Music
- [ ] Add `deezer: true` to `plugins.lavasrc.sources`
- [ ] Add `applemusic: true` to `plugins.lavasrc.sources`
- [ ] Add `deezer:` config block with `masterDecryptionKey` env var
- [ ] Add `applemusic:` config block with `mediaAPIToken`, `countryCode` env vars
- [ ] Add `PROVIDER_DEEZER` and `PROVIDER_APPLE_MUSIC` env var references to the `sources:` block
- [ ] Verify Lavalink starts cleanly with the new config (even when credentials are empty)

## Task 2: Update .env.example with new provider credentials
- [ ] Add `PROVIDER_DEEZER=true` toggle
- [ ] Add `PROVIDER_APPLE_MUSIC=true` toggle
- [ ] Add `DEEZER_MASTER_KEY=` (optional, for FLAC quality)
- [ ] Add `APPLE_MUSIC_MEDIA_API_TOKEN=` (required for Apple Music)
- [ ] Add `APPLE_MUSIC_COUNTRY_CODE=US`
- [ ] Add `SPOTIFY_COUNTRY_CODE=US`
- [ ] Document each new variable with a comment explaining what it does and where to get it

## Task 3: Implement unified source prefix mapping in player.py
- [ ] Create `SOURCE_PREFIXES` dict mapping provider names to search prefixes
- [ ] Create `URL_PATTERNS` dict for auto-detecting source from URLs
- [ ] Add `detect_url_source(url)` helper function
- [ ] Update `_resolve_and_play` to use the new prefix mapping
- [ ] Implement fallback chain: primary source → YouTube fallback
- [ ] Log source resolution events via `debug.py` framework
- [ ] Handle the case where lavasrc returns a Playlist object (multiple tracks)

## Task 4: Update /source command with new providers
- [ ] Add `deezer` and `apple_music` to the `/source` command choices
- [ ] Update the source autocomplete to only show sources with valid credentials
- [ ] Add validation: warn if selected source has no configured credentials
- [ ] Update `/info` to show the current source correctly for new providers
- [ ] Persist new provider names correctly in session.py

## Task 5: Implement Deezer source resolution
- [ ] Add `dzsearch:` prefix handling in `_resolve_and_play`
- [ ] Test: Deezer track URL (`deezer.com/track/123`) resolves via lavasrc
- [ ] Test: Deezer playlist URL loads multiple tracks
- [ ] Test: Deezer album URL loads multiple tracks
- [ ] Test: `dzsearch:song name` returns search results
- [ ] Fallback: if Deezer fails, retry with YouTube search

## Task 6: Implement Apple Music source resolution
- [ ] Add `amsearch:` prefix handling in `_resolve_and_play`
- [ ] Test: Apple Music track URL (`music.apple.com/us/album/...`) resolves
- [ ] Test: Apple Music playlist URL loads multiple tracks
- [ ] Test: Apple Music album URL loads multiple tracks
- [ ] Test: `amsearch:song name` returns search results
- [ ] Fallback: if Apple Music fails, retry with YouTube search
- [ ] Handle the APPLE_MUSIC_MEDIA_API_TOKEN requirement (error message if missing)

## Task 7: Harden Spotify integration
- [ ] Verify `spsearch:` works for track search
- [ ] Verify Spotify track URLs resolve (lavasrc ISR → YouTube audio)
- [ ] Verify Spotify playlist URLs load all tracks (chunked for large playlists)
- [ ] Verify Spotify album URLs work
- [ ] Add SPOTIFY_COUNTRY_CODE to env and lavasrc config
- [ ] Add playlistLoadLimit / albumLoadLimit to lavasrc config

## Task 8: Harden Tidal integration (audio + video)
- [ ] Verify `tdsearch:` works for track search via lavasrc
- [ ] Verify Tidal track URLs resolve via lavasrc
- [ ] Verify Tidal playlist/album URLs load tracks
- [ ] Verify `/stream` command fetches Tidal video URL via custom client
- [ ] Verify video download + text channel embed flow
- [ ] Add error handling: video too large, no video available, download timeout
- [ ] Test TIDAL_TOKEN flow (pre-generated token vs client-credentials)

## Task 9: Harden YouTube + YouTube Music integration
- [ ] Verify YouTube OAuth token push works on startup
- [ ] Verify poToken push works for WEB clients
- [ ] Verify YouTube Music search (`ytmsearch:`) returns audio-only results
- [ ] Verify YouTube shorts URLs resolve correctly
- [ ] Verify YouTube livestream detection (no track-end event)
- [ ] Verify remote cipher server connection for signature deciphering
- [ ] Test age-restricted content access via OAuth (TV client)

## Task 10: URL auto-detection and routing
- [ ] Implement regex-based URL source detection
- [ ] YouTube URLs bypass provider preference → use youtube-source directly
- [ ] Spotify URLs bypass provider preference → use lavasrc spsearch
- [ ] Tidal URLs bypass provider preference → use lavasrc tdsearch
- [ ] Deezer URLs bypass provider preference → use lavasrc dzsearch
- [ ] Apple Music URLs bypass provider preference → use lavasrc amsearch
- [ ] SoundCloud URLs bypass provider preference → use built-in source
- [ ] Test: pasting any platform URL "just works" regardless of /source setting

## Task 11: Source-level debug instrumentation
- [ ] Add `get_debug_logger("source")` namespace
- [ ] Log `source_resolve_start` event (provider, query/url, guild_id)
- [ ] Log `source_resolve_success` event (provider, track title, elapsed_ms)
- [ ] Log `source_resolve_fallback` event (from_provider, to_provider, reason)
- [ ] Log `source_resolve_fail` event (provider, error, query)
- [ ] Log `token_refresh` events for Spotify/Tidal/Apple Music
- [ ] Include source resolution timing in `/metrics` output

## Task 12: Integration testing and documentation
- [ ] Test all 7 sources with a live Lavalink instance
- [ ] Verify graceful degradation when credentials are missing
- [ ] Verify fallback chain works (primary → YouTube)
- [ ] Update README.md with supported sources table
- [ ] Update CONTEXT.md with new architecture details
- [ ] Document the lavasrc plugin version requirements
- [ ] Verify session resume works with all new providers

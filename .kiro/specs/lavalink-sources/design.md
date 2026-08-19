# Lavalink Music Source Integration — Design

## Architecture Overview

HelloDJ uses a **two-plugin Lavalink architecture**:

```
┌─────────────────────────────────────────────────────────────────┐
│                         Discord Bot (Python)                      │
│                                                                   │
│  wavelink 3.5 ──→ Playable.search(query, source=...)             │
│                    └─→ Lavalink REST API (loadtracks)             │
│                                                                   │
│  player.py ─────→ _resolve_and_play(player, guild_id, entry)     │
│                    └─→ source_provider → search prefix mapping    │
└─────────────────────┬───────────────────────────────────────────┘
                      │ HTTP (REST) + WebSocket
                      ▼
┌─────────────────────────────────────────────────────────────────┐
│                      Lavalink Server (Java)                       │
│                                                                   │
│  ┌──────────────────────────────────────────────────────────┐    │
│  │  youtube-source plugin (dev.lavalink.youtube:1.18.2)      │    │
│  │  ├─ YouTube video/audio resolution                        │    │
│  │  ├─ YouTube Music search (MUSIC client)                   │    │
│  │  ├─ OAuth (TV client) + poToken (WEB clients)             │    │
│  │  └─ Remote cipher for signature deciphering               │    │
│  └──────────────────────────────────────────────────────────┘    │
│                                                                   │
│  ┌──────────────────────────────────────────────────────────┐    │
│  │  lavasrc plugin (com.github.topi314.lavasrc:4.2.0)        │    │
│  │  ├─ Spotify: metadata → ISR (YouTube audio fallback)      │    │
│  │  ├─ Tidal: metadata + native streams                      │    │
│  │  ├─ Deezer: metadata + native streams (no auth needed)    │    │
│  │  ├─ Apple Music: metadata → ISR (YouTube audio fallback)  │    │
│  │  └─ Yandex Music (not used)                               │    │
│  └──────────────────────────────────────────────────────────┘    │
│                                                                   │
│  Built-in sources: SoundCloud, HTTP, local                       │
└─────────────────────────────────────────────────────────────────┘
```

## Source Resolution Flow

### Search Prefix Mapping

| Provider | Search Prefix | URL Pattern | Plugin |
|----------|--------------|-------------|--------|
| YouTube | `ytsearch:` | `youtube.com/watch?v=`, `youtu.be/` | youtube-source |
| YouTube Music | `ytmsearch:` | `music.youtube.com/watch?v=` | youtube-source |
| SoundCloud | `scsearch:` | `soundcloud.com/` | built-in |
| Spotify | `spsearch:` | `open.spotify.com/` | lavasrc |
| Tidal | `tdsearch:` | `tidal.com/track/` | lavasrc |
| Deezer | `dzsearch:` | `deezer.com/track/` | lavasrc |
| Apple Music | `amsearch:` | `music.apple.com/` | lavasrc |

### Resolution Algorithm (player.py `_resolve_and_play`)

```python
async def _resolve_and_play(player, guild_id, entry):
    url = entry.get("webpage_url") or entry.get("url")
    title = entry.get("title")
    provider = state.get("source_provider", "youtube")

    # 1. Direct URL detection (bypasses provider preference)
    if url and is_valid_url(url):
        detected_source = detect_url_source(url)
        tracks = await Playable.search(url)  # Lavalink auto-routes by URL
        if tracks:
            return await player.play(tracks[0])

    # 2. Provider-prefixed search
    prefix = SOURCE_PREFIXES[provider]  # e.g. "spsearch:", "tdsearch:"
    tracks = await Playable.search(f"{prefix}{title}")

    # 3. Fallback chain
    if not tracks and provider != "youtube":
        dbg.event("source_resolve_fallback", from_provider=provider, to="youtube")
        tracks = await Playable.search(title, source=TrackSource.YouTube)

    if not tracks:
        # All sources exhausted
        dbg.error("source_resolve_fail", title=title, provider=provider)
        return await _play_next_from_queue(guild_id)

    await player.play(tracks[0])
```

## Lavalink Configuration (application.yml)

### Plugin Declarations

```yaml
lavalink:
  plugins:
    - dependency: "dev.lavalink.youtube:youtube-plugin:1.18.2"
      repository: "https://maven.lavalink.dev/releases"
    - dependency: "com.github.topi314.lavasrc:lavasrc-plugin:4.2.0"
      repository: "https://maven.lavalink.dev/releases"
```

### lavasrc Plugin Configuration

```yaml
plugins:
  lavasrc:
    sources:
      spotify: true       # PROVIDER_SPOTIFY env toggle
      tidal: true         # PROVIDER_TIDAL env toggle
      deezer: true        # PROVIDER_DEEZER env toggle (NEW)
      applemusic: true    # PROVIDER_APPLE_MUSIC env toggle (NEW)
      yandexmusic: false  # not used

    spotify:
      clientId: "${SPOTIFY_CLIENT_ID}"
      clientSecret: "${SPOTIFY_CLIENT_SECRET}"
      countryCode: "${SPOTIFY_COUNTRY_CODE:-US}"
      playlistLoadLimit: 6     # pages (6 × 100 = 600 tracks max)
      albumLoadLimit: 6

    tidal:
      clientId: "${TIDAL_CLIENT_ID}"
      clientSecret: "${TIDAL_CLIENT_SECRET}"
      countryCode: "${TIDAL_COUNTRY_CODE:-US}"
      searchLimit: ${TIDAL_SEARCH_LIMIT:-10}
      token: "${TIDAL_TOKEN}"

    deezer:
      masterDecryptionKey: "${DEEZER_MASTER_KEY:-}"  # optional: for FLAC streams
      # Deezer public API needs no auth for search/metadata
      # lavasrc resolves Deezer → YouTube audio (ISR) when no key is provided

    applemusic:
      mediaAPIToken: "${APPLE_MUSIC_MEDIA_API_TOKEN:-}"
      countryCode: "${APPLE_MUSIC_COUNTRY_CODE:-US}"
      playlistLoadLimit: 6
      albumLoadLimit: 6
      # Apple Music resolves metadata → YouTube audio (ISR)
```

## Source Provider Map (Python Side)

### Updated `_resolve_and_play` Source Map

```python
SOURCE_PREFIXES = {
    "youtube": "ytsearch:",
    "youtube_music": "ytmsearch:",
    "soundcloud": "scsearch:",
    "spotify": "spsearch:",
    "tidal": "tdsearch:",
    "deezer": "dzsearch:",
    "apple_music": "amsearch:",
}

SOURCE_WAVELINK_MAP = {
    "youtube": TrackSource.YouTube,
    "youtube_music": TrackSource.YouTubeMusic,
    "soundcloud": TrackSource.SoundCloud,
    # These use string prefixes (not TrackSource enum):
    "spotify": "spsearch",
    "tidal": "tdsearch",
    "deezer": "dzsearch",
    "apple_music": "amsearch",
}
```

### URL Auto-Detection

```python
URL_PATTERNS = {
    r"(youtube\.com|youtu\.be)": "youtube",
    r"music\.youtube\.com": "youtube_music",
    r"soundcloud\.com": "soundcloud",
    r"open\.spotify\.com": "spotify",
    r"tidal\.com": "tidal",
    r"deezer\.com": "deezer",
    r"music\.apple\.com": "apple_music",
}
```

## Tidal Music Video Flow

```
/stream <query>
  │
  ├─ 1. Search Tidal API (tidal.py) for track
  │     └─ OAuth client-credentials → Bearer token
  │
  ├─ 2. Get video URL (tidal.py get_video_url)
  │     ├─ GET /tracks/{id}/video → video_id
  │     └─ GET /videos/{video_id}/stream → stream URL (HLS/MP4)
  │
  ├─ 3. Download video (yt-dlp or aiohttp fallback)
  │     └─ Target: /tmp/hellodj_stream_{guild_id}.mp4
  │
  ├─ 4a. If file ≤ Discord limit: upload as attachment (auto-embeds as video)
  │   4b. If file > limit: post Tidal video URL as link (Discord may embed)
  │
  └─ 5. Queue audio playback in voice channel via Lavalink
        └─ tdsearch:{title} → Lavalink resolves audio stream
```

## Environment Variables (New)

```bash
# Deezer (lavasrc)
PROVIDER_DEEZER=true
DEEZER_MASTER_KEY=           # optional: for FLAC quality streams

# Apple Music (lavasrc)
PROVIDER_APPLE_MUSIC=true
APPLE_MUSIC_MEDIA_API_TOKEN= # required: Apple Music MusicKit JWT or media token
APPLE_MUSIC_COUNTRY_CODE=US

# Spotify (existing, extended)
SPOTIFY_COUNTRY_CODE=US

# Tidal (existing, extended)
TIDAL_COUNTRY_CODE=US
TIDAL_SEARCH_LIMIT=10
```

## Error Handling Strategy

| Scenario | Response |
|----------|----------|
| Source credentials missing | Source disabled, log WARNING, skip in autocomplete |
| Source API timeout (>10s) | Fallback to YouTube, log WARNING |
| Source returns 0 results | Fallback to YouTube search by title |
| Source returns auth error | Log ERROR, disable source temporarily (5min cooldown) |
| YouTube OAuth expired | Auto-refresh via watchdog, retry once |
| Rate limited | Exponential backoff, fallback to alt source |
| Playlist too large (>500) | Load first 500, warn user, offer to load more |

## Files Modified

| File | Changes |
|------|---------|
| `bot/lavalink/application.yml` | Add Deezer + Apple Music to lavasrc config |
| `bot/player.py` | Update source map, add URL detection, fallback chain |
| `bot/cogs/music.py` | Update `/source` choices, add new providers |
| `bot/.env.example` | Add Deezer + Apple Music env vars |
| `bot/bot.py` | Pass new env vars to Lavalink startup logging |
| `bot/session.py` | Ensure new providers persist correctly |
| `bot/debug.py` | Source-resolution debug events (already added) |

## Testing Strategy

1. **Unit**: Mock Playable.search to verify prefix mapping + fallback logic
2. **Integration**: Start Lavalink with all plugins, verify each source loads a known track
3. **E2E**: Bot in a test guild, `/play <spotify_url>`, verify audio plays
4. **Regression**: Existing YouTube/SoundCloud sources still work after changes

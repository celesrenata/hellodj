# spotify-stream

Direct Spotify audio streaming service for HelloDJ. Eliminates YouTube mirroring for Spotify tracks.

## How it works

1. Web-UI OAuth flow stores Spotify tokens in `data/oauth.json`
2. This service uses librespot-python to authenticate with Spotify
3. Bot calls `GET /stream/<track_id>` → gets raw OGG Vorbis audio stream
4. Bot passes `http://spotify-stream:8802/stream/<track_id>` to Lavalink as an HTTP source

## Key difference from tidal-stream

Spotify doesn't provide direct CDN URLs. Instead, this service acts as a **streaming proxy** —
it decrypts the audio in real-time and serves it as an HTTP stream. Lavalink connects to this
service's URL and reads audio bytes directly.

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/stream/<track_id>` | Returns raw audio stream (OGG Vorbis) |
| GET | `/health` | Service health check |

## Authentication flow

1. First run: reads `access_token` from `data/oauth.json` (from web-ui OAuth callback)
2. librespot exchanges this for stored credentials (persistent, survives token expiry)
3. Stored credentials saved to `data/spotify-credentials.json`
4. Subsequent starts: uses stored credentials directly (no token refresh needed)

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `DATA_DIR` | `/app/data` | Path to shared data directory |
| `SPOTIFY_STREAM_PORT` | `8802` | Service port |

## Deployment

Runs as a sidecar or separate service in the hellodj-service namespace.
Shares the same `data/` NFS volume as bot and web-ui.

## Important notes

- Requires **Spotify Premium** — librespot cannot stream from free accounts
- Audio format: OGG Vorbis (320kbps when available)
- The service acts as a single Spotify Connect device — only one stream at a time per session

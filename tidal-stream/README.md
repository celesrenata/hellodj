# tidal-stream

Direct Tidal audio streaming service for HelloDJ. Eliminates YouTube mirroring for Tidal tracks.

## How it works

1. Web-UI OAuth flow stores Tidal tokens in `data/oauth.json`
2. This service reads those tokens and authenticates with Tidal via `tidalapi`
3. Bot calls `GET /stream/<track_id>` → gets a direct CDN audio URL
4. Bot passes that URL to Lavalink as an HTTP audio source (no LavaSrc mirroring)

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/stream/<track_id>` | Returns JSON with direct audio URL |
| GET | `/search?q=<query>` | Search Tidal for tracks |
| GET | `/health` | Service health check |

## Response format (stream)

```json
{
  "url": "https://sp-pr-cf.audio.tidal.com/...",
  "codec": "flac",
  "quality": "LOSSLESS",
  "mime_type": "audio/flac",
  "track_id": "12345678",
  "title": "Track Name",
  "artist": "Artist Name",
  "duration_ms": 240000
}
```

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `DATA_DIR` | `/app/data` | Path to shared data directory |
| `TIDAL_STREAM_PORT` | `8801` | Service port |
| `TIDAL_QUALITY` | `high_lossless` | Preferred quality (hi_res_lossless, high_lossless, low_320k, low_96k) |

## Deployment

Runs as a sidecar or separate service in the hellodj-service namespace.
Shares the same `data/` NFS volume as bot and web-ui.

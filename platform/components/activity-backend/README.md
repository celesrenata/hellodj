# activity-backend

The `activity-backend` component of the HelloDJ AWS platform.

## Responsibility

- Serve the **Discord Activity** HTTP endpoints under the `/activity/` prefix
  (behind ALB/CloudFront — Requirement 18.2).
- Run a **WebSocket hub** (`ws_hub`) for real-time, server-authoritative state
  sync across all connected Activity clients:
  - **video control** — play/pause/seek with an anchor-based, jitter-free sync
    model,
  - **whiteboard** — stroke add/remove/reset with a per-guild stroke registry,
  - **visualizer control** — engine selection + late-joiner state,
  - **synced lyrics** — LRC overlay toggled for everyone.
- **Emit transcode requests** to the `hls-transcode` component (typed HTTP/JSON)
  rather than transcoding locally (Requirement 18.4).
- **Serve/read HLS from S3 via CloudFront** — HLS playlists/segments are written
  by `hls-transcode` to S3 and served to viewers through CloudFront, the managed
  edge cache (Requirements 18.2, 18.4). There are no local-disk assumptions
  beyond ephemeral scratch.

This preserves the existing Activity feature set (video streaming, whiteboard,
audio visualizer, synced lyrics — Requirement 6.2) through the re-platform. It
is an independently deployable, independently versioned component
(Requirement 15.1): its own Nix-built image, its own semantic version, and its
own CI/CD path.

## Package layout

```
activity_backend/
├── __init__.py           # package version + public exports
├── config.py            # environment-driven runtime settings
├── models.py            # PlaybackState / VisualizerState / LyricsState
├── whiteboard.py        # StrokeRegistry + stroke payload validation
├── visualizer.py        # per-guild visualizer control registry
├── lyrics.py            # per-guild synced-lyrics store + LRC parser
├── hls.py               # S3 key + CloudFront URL derivation (HlsCatalog)
├── transcode_client.py  # typed HTTP/JSON client to hls-transcode
├── ws_hub.py            # per-guild WebSocket synchronization hub
├── app.py               # aiohttp app factory + HTTP endpoints
└── server.py            # composition root + entry point
```

## Interfaces

- **Clients (Discord Activity iframe)** — HTTPS + WSS through ALB/CloudFront at
  `/activity/` (HTTP control endpoints + `GET /activity/ws/{guild_id}`).
- **hls-transcode** — typed HTTP/JSON (`activity_backend.transcode_client`); the
  backend emits transcode start/stop requests and never encodes media itself.
- **S3 + CloudFront** — HLS output location and viewer-facing URLs are derived by
  `activity_backend.hls.HlsCatalog`; segments are written by hls-transcode to S3
  and served through CloudFront.

## Configuration (environment)

| Variable | Purpose | Default |
|---|---|---|
| `HELLODJ_TRANSCODE_URL` | hls-transcode base URL | `http://hls-transcode:8080` |
| `HELLODJ_CLOUDFRONT_DOMAIN` | CloudFront domain fronting the HLS S3 bucket | (empty) |
| `HELLODJ_HLS_S3_BUCKET` | S3 bucket for HLS output | (empty) |
| `HELLODJ_HLS_S3_PREFIX` | key prefix for HLS objects | `hls` |
| `HELLODJ_ACTIVITY_ROUTE_PREFIX` | Activity path prefix | `/activity` |
| `HELLODJ_ACTIVITY_HOST` / `HELLODJ_ACTIVITY_PORT` | bind host/port | `0.0.0.0` / `8090` |
| `HELLODJ_ACTIVITY_HEARTBEAT_S` | WebSocket heartbeat interval | `30` |
| `HELLODJ_MAX_STROKES` | per-guild whiteboard stroke cap | `500` |
| `AWS_REGION` | region for AWS SDK clients | boto3 default chain |

## Development

```bash
# From the platform root:
uvx ruff@0.6.9 check components/activity-backend
python3 tools/check_line_count.py components/activity-backend

# aiohttp / boto3 may not be installed in every environment; the modules are
# import-structured (lazy imports) so syntax can be verified without them:
python3 -m py_compile components/activity-backend/activity_backend/*.py

# Unit tests exercise the pure surfaces (config, whiteboard validation, HLS URL
# derivation, transcode request building, WS state transitions, lyrics parsing)
# without requiring aiohttp:
pytest components/activity-backend/tests
```

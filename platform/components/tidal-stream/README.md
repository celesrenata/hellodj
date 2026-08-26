# tidal-stream

Direct Tidal audio streaming sidecar for the HelloDJ AWS platform. It
authenticates Tidal source access through the HelloDJ-owned **first-party
single-app-id** OAuth integration and refreshes tokens via the shared
`hellodj_platform_logic.tidal_refresh` decision logic. The long-lived refresh
token is stored in AWS Secrets Manager.

## What changed from the legacy sidecar

- **Single application id.** All Tidal source auth uses one Tidal application
  identifier (`TIDAL_APP_ID`) — Requirement 9.1.
- **HelloDJ-owned callback.** The OAuth callback (`/auth/callback`) is served by
  this component; the web-ui forwards the authorization code here — R9.2.
- **Legacy key-split removed.** The old two-client-id key-split approach is
  gone. Every refresh routes through
  `hellodj_platform_logic.tidal_refresh.refresh_tidal`, whose guard
  (`reject_legacy_key_split`) rejects the legacy mode outright — R9.3.
- **Secrets Manager refresh token.** The refresh token is loaded from and
  persisted to AWS Secrets Manager, not a local SQLite/credential DB — R9.2/R9.4.
- **Independent of Cognito.** Tidal source auth never touches Cognito — R9.5.

## Modules

| Module | Responsibility |
|---|---|
| `config.py` | Environment-driven `TidalStreamSettings` (single app id, callback, secret id). |
| `secrets.py` | `TidalRefreshTokenStore` — load/persist the refresh token in Secrets Manager. |
| `oauth_client.py` | `FirstPartyTidalOAuthClient` — single-app-id code exchange + refresh (implements the shared `FirstPartyRefreshClient` protocol). |
| `token_manager.py` | `TidalTokenManager` — load → refresh via shared logic → persist; code exchange for the callback. |
| `streaming.py` | `TidalStreamer` — direct search + stream-URL resolution (aiohttp, bearer token, 401 self-heal). |
| `server.py` | aiohttp app: `/healthz`, `/search`, `/tracks/{id}/stream`, `/auth/callback`. |
| `__main__.py` | Container entrypoint (`python -m tidal_stream`). |

## Endpoints

- `GET /healthz` — liveness probe.
- `GET /search?q=<query>&limit=<n>` — Tidal track search.
- `GET /tracks/{track_id}/stream` — resolve the direct audio stream URL.
- `GET /auth/callback?code=<code>` — first-party OAuth code exchange (web-ui
  forwards the code here); persists the refresh token to Secrets Manager.

## Configuration (environment)

| Variable | Required | Description |
|---|---|---|
| `TIDAL_APP_ID` | yes | Single Tidal application id (R9.1). |
| `TIDAL_CALLBACK_URL` | yes | HelloDJ-owned OAuth callback URL (R9.2). |
| `TIDAL_REFRESH_SECRET_ID` | yes | Secrets Manager secret name/ARN for the refresh token. |
| `TIDAL_TOKEN_URL` | no | Tidal OAuth token endpoint (default: Tidal auth URL). |
| `TIDAL_API_BASE` | no | Tidal API base (default: `https://api.tidal.com/v1`). |
| `TIDAL_COUNTRY_CODE` | no | Catalog/stream country code (default `US`). |
| `AWS_REGION` | no | Region for the Secrets Manager client. |
| `TIDAL_STREAM_HOST` | no | Bind host (default `0.0.0.0`). |
| `TIDAL_STREAM_PORT` | no | Bind port (default `8801`). |
| `TIDAL_EXPIRY_SKEW_SECONDS` | no | Expiry skew for refresh decisions (default `60`). |

## Run

```bash
PYTHONPATH=.. python -m tidal_stream
```

## Test

```bash
PYTHONPATH=.. python -m pytest components/tidal-stream/tests
```

_Requirements: 6.1, 9.1, 9.2, 9.3, 9.4, 9.5, 15.1_

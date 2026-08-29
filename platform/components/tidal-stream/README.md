# tidal-stream

Direct Tidal audio streaming sidecar for the HelloDJ AWS platform. It is
**multi-tenant**: every stream/search request carries a `guild_id`, and the
sidecar resolves that guild's owning user's Tidal credential from the unified
per-user credential store (`hellodj-core` DynamoDB, envelope-encrypted, KMS
Decrypt-only) and serves the request from that user's own session — never a
single shared account (multi-tenant-source-streaming R5).

The `TIDAL_APP_ID` / `TIDAL_CALLBACK_URL` remain **global** single-app-id config;
only the per-user token varies. The sidecar is **read-only** on tokens: the
durable token watchdog owns Tidal's refresh (R5.3). The single startup-bound
`TIDAL_REFRESH_SECRET_ID` account is gone from the streaming path (the secret is
now OPTIONAL and backs only the legacy `/auth/callback` code-exchange forward).

## Multi-tenant model (task 3.1)

- Requests carry the `guild_id` in the path (mirroring the Spotify sidecar); the
  owning Cognito `sub` is resolved **server-side** and never appears in a URL or
  log.
- A per-`sub` `TidalSessionRegistry` (the shared bounded-LRU
  `SessionRegistry[str, TidalUserClient]`) holds one live client per user, with
  idle eviction and clean shutdown (R6, R8).
- Each client's access token comes from a read-only
  `ReadOnlyTidalTokenSource` backed by the shared `UserCredentialResolver`
  (guild → owner → decrypt), which handles the expiry re-read (R2.2) and the
  `refresh_status=failed` gate (R2.3).
- A guild with no owner or no Tidal credential fails **observably** (HTTP
  404/502 with a non-secret `reason`) with no cross-user fallback (R5.4, R10.5).

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
| `config.py` | Environment-driven `TidalStreamSettings` (global app id/callback, unified-store table + KMS, session-pool bounds, optional legacy secret id). |
| `resolver_bootstrap.py` | Wire the real `UserCredentialResolver` + guild→owner `OwnerLookup` over a live `CoreTable` + KMS Decrypt-only (lazy boto3). |
| `user_sessions.py` | `TidalSessionRegistry`, `ReadOnlyTidalTokenSource`, `TidalUserClient`, `TidalStreamRouter` — the per-user, read-only streaming path. |
| `streaming.py` | `TidalStreamer` — direct search + stream-URL resolution (aiohttp, bearer token, 401 self-heal); token-source-agnostic. |
| `secrets.py` | `TidalRefreshTokenStore` — load/persist the legacy refresh token in Secrets Manager (optional `/auth/callback` only). |
| `oauth_client.py` | `FirstPartyTidalOAuthClient` — single-app-id code exchange + refresh. |
| `token_manager.py` | `TidalTokenManager` — legacy code exchange for the optional callback. |
| `server.py` | aiohttp app: `/healthz`, `/search/{guild_id}`, `/stream/{guild_id}/{track_id}`, optional `/auth/callback`. |
| `__main__.py` | Container entrypoint (`python -m tidal_stream`). |

## Endpoints

- `GET /healthz` — liveness probe reporting the per-`sub` session pool (live
  session count + per-user states, never a single global status, no token
  material). Reports `not_ready` when the unified store is unavailable (no
  fake-green — R7.5).
- `GET /search/{guild_id}?q=<query>&limit=<n>` — Tidal track search using the
  guild owner's token.
- `GET /stream/{guild_id}/{track_id}` — resolve the direct audio stream URL
  using the guild owner's token.
- `GET /auth/callback?code=<code>` — OPTIONAL legacy first-party OAuth code
  exchange (present only when `TIDAL_REFRESH_SECRET_ID` is set); NOT part of the
  per-user streaming path.

## Configuration (environment)

| Variable | Required | Description |
|---|---|---|
| `TIDAL_APP_ID` | yes | Single Tidal application id — global (R9.1). |
| `TIDAL_CALLBACK_URL` | yes | HelloDJ-owned OAuth callback URL — global (R9.2). |
| `HELLODJ_CORE_TABLE` | no | Unified credential store table (default `hellodj-core`). |
| `HELLODJ_SOURCE_CREDS_KMS_KEY_ID` | no | Source-credentials KMS CMK id (Decrypt-only reader grant). |
| `TIDAL_MAX_SESSIONS` | no | Bounded per-user session-pool size (default `32`, R8.1). |
| `TIDAL_SESSION_IDLE_TIMEOUT` | no | Per-user session idle timeout seconds (default `900`, R8.2). |
| `TIDAL_REFRESH_SECRET_ID` | no | OPTIONAL Secrets Manager secret backing only the legacy `/auth/callback`. |
| `TIDAL_TOKEN_URL` | no | Tidal OAuth token endpoint (default: Tidal auth URL). |
| `TIDAL_API_BASE` | no | Tidal API base (default: `https://api.tidal.com/v1`). |
| `TIDAL_COUNTRY_CODE` | no | Catalog/stream country code (default `US`). |
| `AWS_REGION` | no | Region for the AWS clients. |
| `TIDAL_STREAM_HOST` | no | Bind host (default `0.0.0.0`). |
| `TIDAL_STREAM_PORT` | no | Bind port (default `8801`). |
| `TIDAL_EXPIRY_SKEW_SECONDS` | no | Expiry skew for expiry re-read (default `60`). |

## Run

```bash
PYTHONPATH=.. python -m tidal_stream
```

## Test

```bash
PYTHONPATH=.. python -m pytest components/tidal-stream/tests
```

_Requirements: 5.1, 5.2, 5.3, 5.4, 6.1, 7.3, 9.1, 9.2, 9.3, 9.4, 9.5, 10.5, 15.1_

# HelloDJ — Architecture & Critical Context

This steering file captures the full architecture of the HelloDJ project so agents don't break the carefully constructed integration between services.

## Deployment Targets

HelloDJ runs in TWO environments:

1. **On-prem Kubernetes** (gremlin nodes 10.1.1.12–15) — the current live deployment described below
2. **AWS EKS** (us-east-1, account 874927898283) — the SaaS re-platform, fully deployed infrastructure (9 CDK stacks live), pipeline deploying workloads. See `platform/infra/ARCHITECTURE.md` for the AWS topology.

Source code is hosted in **private AWS CodeCommit** (not public GitHub). The local repos have a `codecommit` remote pointing to `git-codecommit.us-east-1.amazonaws.com/v1/repos/<repo>`.

## Project Overview

HelloDJ is a voice-activated Discord music bot with:
- Custom "Hello DJ" wake word (ONNX model)
- Multi-source playback (YouTube, Spotify, Tidal, SoundCloud)
- Direct streaming sidecars (bypass YouTube mirroring for Spotify/Tidal)
- Discord Activity (video streaming, whiteboard, visualizer, lyrics overlay)
- Voice command pipeline (wake word → STT → LLM intent → action → TTS response)
- Unified playback system (multi-instance orchestrator, content filtering, user bans)
- Web configuration UI at https://hellodj.celestium.life
- Kubernetes deployment on gremlin nodes (10.1.1.12–15)

## Repository Layout

| Repo / Path | Purpose | CodeCommit |
|---|---|---|
| `hellodj/` (this repo) | Bot sources (`bot/`), the 12 workload component sources under `platform/components/*`, and the Kubernetes manifests (`kube/`). Training assets. | `codecommit::us-east-1://hellodj` (branch: main) |
| `hellodj-cdk/` | Standalone CDK application (`infra/`), the repo-wide gates (`tools/`), the encrypted cache secrets (`secrets/`), the closure/pin manifests (`closures.toml`, `pins.toml`, `pins.upstream.toml`), `pyproject.toml`, and the shared pure-logic package `hellodj_platform_logic` (`shared/hellodj_platform_logic/`). Primary synth source for the pipeline. | `codecommit::us-east-1://hellodj-cdk` (branch: main) |
| `celesrenata/Lavalink` | Fork of lavalink-devs/Lavalink (upstream remote: `upstream`), branch `dev` | `codecommit::us-east-1://Lavalink` (branch: dev) |
| `celesrenata/lavaplayer` | Fork of lavalink-devs/lavaplayer | `codecommit::us-east-1://lavaplayer` (branch: main) |
| `celesrenata/LavaSrc` | Fork of topi314/LavaSrc (Tidal/Spotify source plugin) | `codecommit::us-east-1://LavaSrc` (branch: tidal-v2-api) |
| `celesrenata/youtube-source` | Fork of lavalink-devs/youtube-source (SABR support) | `codecommit::us-east-1://youtube-source` (branch: main) |

The Lavalink fork uses a custom Lavalink.jar with lavaplayer fMP4 HLS patches. Plugins baked into the image: `lavasrc-plugin-4.8.3.jar`, `youtube-plugin-sabr.jar`.

**CDK repo split (`cdk-standalone-package` spec).** Post-migration, the CDK application, the repo-wide gates, and the shared `hellodj_platform_logic` package live in the standalone **`hellodj-cdk`** repo, while `platform/components/*` (the 12 workloads), `bot/`, and `kube/` stay in `hellodj`. Concretely, `platform/infra/`, `platform/tools/`, `platform/secrets/`, the closure/pin manifests (`platform/closures.toml`, `platform/pins.toml`, `platform/pins.upstream.toml`), `platform/pyproject.toml`, and `platform/components/hellodj_platform_logic/` have moved OUT of `hellodj` into `hellodj-cdk` (at `infra/`, `tools/`, `secrets/`, the repo root, and `shared/hellodj_platform_logic/` respectively). The pipeline synths from `hellodj-cdk` as its primary source; the 12 per-component Nix image builds take `hellodj` as an additional source input, and vendor `hellodj_platform_logic` from the `hellodj-cdk` `shared/` input. CDK-only changes now go to `hellodj-cdk` without touching the bot repo.

## Pod Architecture (Single Deployment, Multi-Container)

The `hellodj` Deployment in namespace `hellodj-service` runs as a SINGLE POD with these containers:

1. **init: render-lavalink-config** — Runs `render_lavalink_config.py` in the bot image (Python + cryptography). Reads ALL credentials from the encrypted SQLite DB and renders a complete `application.yml` to an emptyDir. No sed, no env var substitution.
2. **bot** — Python 3.11 Discord bot (`bot.py` entry point, wavelink 3.5+). Exposes port 8090 for the Discord Activity backend.
3. **lavalink** — Custom Lavalink image with fMP4 HLS + SABR support (port 2333)
4. **tidal-stream** — Direct Tidal audio streaming sidecar (port 8801)
5. **spotify-stream** — Direct Spotify audio streaming sidecar (port 8802)

Separate deployments run:
6. **yt-cipher** — `ghcr.io/kikkia/yt-cipher:master` (port 8001) — remote YouTube signature deciphering
7. **potoken-server** — `brainicism/bgutil-ytdlp-pot-provider:latest` (port 4416) — generates fresh YouTube Proof-of-Origin tokens on demand
8. **hellodj-web-ui** — Flask/Gunicorn web UI (port 8080) — config, OAuth flows, credential management

## Critical: YouTube Playback Pipeline

YouTube playback goes through the `youtube-source` plugin (custom SABR-capable build). Understanding the client cascade is essential:

### Client Order (unified across kube + docker configs)
1. **TV** — OAuth-capable (the ONLY one). Primary streaming client with sign-in.
2. **TVHTML5_SIMPLY** — Robust unauthenticated playback + search. Works on clean IPs without PoToken.
3. **ANDROID_VR** — Unauthenticated streaming fallback. Playback + search + playlists.
4. **MUSIC** — Search only (`playback: false`, `videoLoading: false`)
5. **WEB** — Metadata/playlist loading only (`playback: false`)

### Authentication Layers (YouTube)

1. **OAuth** (TV client): Refresh token stored in encrypted credential DB (`youtube.oauth_refresh_token`). Pushed to Lavalink's `POST /youtube` endpoint at startup by `bot.py:push_youtube_oauth()`. The `oauth.skipInitialization: true` in config prevents the plugin from starting a broken OAuth poller with an empty token at boot. The bot's runtime push properly initializes OAuth.

2. **PoToken** (WEB-family clients): Proof of Origin token. Defeats "Sign in to confirm you're not a bot". Generated by the in-cluster `potoken-server` (bgutil-ytdlp-pot-provider). The bot's `_potoken_refresh_task()` periodically calls `POST /get_pot` on the server, stores the result in the credential DB (`youtube.pot_token` + `youtube.pot_visitor_data`), and pushes to Lavalink alongside OAuth in a SINGLE `POST /youtube` request.

3. **Remote Cipher** (yt-cipher): Offloads YouTube player-script signature deciphering. URL: `http://yt-cipher.hellodj-service.svc.cluster.local:8001`. Authenticated with `yt-cipher-secret` (`API_TOKEN`). Env `OVERRIDE_PLAYER_VARIANT=IAS` for reliability.

### PoToken Server (bgutil-ytdlp-pot-provider)

- **Image**: `brainicism/bgutil-ytdlp-pot-provider:latest`
- **Port**: 4416 (default)
- **API**: `POST /get_pot` with optional `{ "content_binding": "<visitor_data>" }` body
- **Response**: `{ "poToken": "...", "contentBinding": "...", "expiresAt": "..." }`
- **Health**: `GET /ping`
- **Bot integration**: `fetch_and_push_potoken()` in `bot.py` fetches hourly (configurable via `POTOKEN_REFRESH_INTERVAL`) and pushes to Lavalink
- **Note**: The official youtube-source plugin (1.18.2) does NOT have a `remotePot` config key. PoTokens must be either static in config or pushed via `POST /youtube` at runtime. The bot handles the runtime push.

### YouTube Plugin Config Keys (official 1.18.2 + SABR)

```yaml
plugins:
  youtube:
    enabled: true
    clients: [TV, TVHTML5_SIMPLY, ANDROID_VR, MUSIC, WEB]
    clientOptions:
      MUSIC: { playback: false, videoLoading: false }
    oauth:
      enabled: true/false
      skipInitialization: true  # Prevents broken empty-token poller at boot
      refreshToken: "..."
    pot:
      token: "..."        # static poToken (also pushable at runtime)
      visitorData: "..."  # static visitorData (also pushable at runtime)
    remoteCipher:
      url: "http://..."
      password: "..."
      userAgent: "..."
```

There is NO `remotePot` key in the official plugin. Don't add one.

## Credential Store Architecture

All secrets are stored in an **encrypted SQLite database** (`/app/data/hellodj.db`) on the Longhorn PVC. The encryption key is the ONLY env var secret: `HELLODJ_DB_KEY`.

- `credentials.py` → Fernet-encrypted SQLite store (thread-safe, WAL mode)
- `config.py` → Unified accessor (`cfg("key")`) reads from credential store ONLY
- `oauth_store.py` → Legacy JSON store (`data/oauth.json`) for YouTube/Tidal OAuth tokens (fallback)

The config.py `Config` class does NOT fall back to env vars. It reads exclusively from the SQLite credential store. The `_KEY_TO_ENV` mapping exists only for documentation/migration reference.

### AWS platform: Unified source-credential store (DynamoDB + envelope encryption)

> Applies to the **AWS EKS** deployment only (spec:
> `unified-oauth-and-token-watchdog`). The on-prem encrypted-SQLite store above
> is unchanged.

On AWS, per-user source OAuth credentials (YouTube, YouTube Music, Spotify,
Tidal — Discord is identity-only, SoundCloud is search-only) are **no longer**
stored as one Secrets Manager secret per guild+provider. They live as **one
DynamoDB item per user+provider** on the existing `hellodj-core` table:

- `PK = USER#<sub>`, `SK = SOURCECRED#<provider>`, `entityType = SourceCredential`.
- **Plaintext status fields** the UI and watchdog read without a KMS call:
  `connected`, `connected_at`, `updated_at`, `expires_at`, `scope`,
  `last_refresh_at`, `refresh_status` (`ok`/`failed`), `refresh_error`.
- **Token blob** (`{access_token, refresh_token, expires_at, scope, …}`) stored
  ONLY as `enc_blob` (base64 AES-GCM ciphertext) + `enc_key` (KMS-wrapped data
  key) + `kms_key_id`. Never plaintext tokens; tokens are never logged.

**Double encryption at rest (R3):** the table keeps its KMS at-rest encryption
AND the token blob is **application-layer envelope-encrypted** so a broad table
read or a PITR export never exposes a refresh token. The shared implementation
is `hellodj_platform_logic.token_crypto` (`encrypt_blob`/`decrypt_blob`, an
injectable `KmsClient` Protocol → unit-testable with a fake KMS). Envelope flow:
`kms.generate_data_key(AES_256)` → AES-GCM the blob with the plaintext data key
→ discard the plaintext key → store ciphertext + KMS-wrapped key.

**Source-credentials CMK:** a dedicated customer-managed KMS key
`alias/hellodj-source-creds-<stage>` (key rotation enabled), created in
`data-stack.ts` and exported as `sourceCredsKey`. Its id is wired to the granted
components as `HELLODJ_SOURCE_CREDS_KMS_KEY_ID`. This is the ONLY new persistent
AWS resource the spec adds (no new table).

**Least-privilege KMS matrix** (`SOURCE_CREDENTIAL_KMS_COMPONENTS` in
`workloads-stack.ts`, Property 9 / R9.4) — ONLY these components hold any grant
on the CMK:

| Component | Core table | CMK grant |
|---|---|---|
| `web-ui` (writer) | RW | GenerateDataKey + Encrypt (write path) + Decrypt (flow completion) |
| `playback-orchestrator` (watchdog) | RW | GenerateDataKey + Encrypt + Decrypt (must decrypt to refresh) |
| `discord-bot-core`, `tidal-stream`, `spotify-stream` (readers) | Read | Decrypt only (read-only on tokens) |

No other component (including `lavalink`) gets a CMK grant. The legacy
`hellodj/<stage>/guild/*` Secrets Manager read grant is retained during
migration; new writes go to DynamoDB.

**Unified refresh contract** (`hellodj_platform_logic.source_refresh`):
generalizes the Tidal refresh shape into a provider-agnostic `RefreshClient`
Protocol + pure `needs_refresh`/`apply_refresh` (fast-path if not expired,
preserve prior refresh token when the provider doesn't rotate, treat an
already-expired result as failure). Concrete clients: `GoogleRefreshClient`
(youtube/youtube_music), `SpotifyRefreshClient`, and `TidalRefreshClient` — a
thin adapter that delegates to the EXISTING `tidal_refresh.refresh_tidal`
first-party single-app-id logic UNCHANGED (no regression; its property tests
still pass).

**YouTube auth on AWS uses the plugin's PUBLIC device-code client (no Google
Cloud web app).** There is NO operator-registered Google OAuth app — the
on-prem cred DB never had a `youtube.client_id`/`client_secret`, because the
youtube-source plugin authenticates its TV client with a well-known PUBLIC
"TV / limited-input device" client (`861556708454-…apps.googleusercontent.com`)
baked into the jar. The AWS web-ui Account page drives that SAME device-code
flow (`web-ui/youtube_device_oauth.py`): the user is shown a short code + a
`youtube.com/activate` URL, the web-ui polls `youtube.com/o/oauth2/token` for
the offline refresh token, pairs it with a fresh PoToken, and stores an
encrypted `SourceCredential` — so `source_provider_configured('youtube')` is
ALWAYS true (no `GOOGLE_CLIENT_ID` env needed). The durable watchdog refreshes
device-issued tokens with the SAME public client at `youtube.com/o/oauth2/token`
via `source_refresh.youtube_device_refresh_client`
(`watchdog_bootstrap.build_clients_by_provider` uses it for youtube/youtube_music
whenever no operator `GOOGLE_CLIENT_ID`/secret is set). Spotify/Tidal still use
their own client ids (threaded via `cdk.json` context → the `*_CLIENT_ID` env).
We own `youtube-source` in CodeCommit, so if Google ever rotates that public
client we update the plugin's `YoutubeOauth2Handler` and the mirrored constants
(`youtube_device_oauth` + `source_refresh.YOUTUBE_DEVICE_CLIENT_ID`) together.

**Default playback source is `youtube`** — `player.py` `DEFAULT_SOURCE` +
`resolve_source()` treat an unset/empty source_provider as YouTube; the web-ui
config form preselects it.

## Lavalink Config Rendering (Init Container)

The init container uses `render_lavalink_config.py` running in the **bot image** (which has Python + cryptography installed). It:
1. Reads ALL credentials from the encrypted SQLite DB (credential store)
2. Renders a complete `application.yml` with resolved values
3. Writes to `/out/application.yml` (emptyDir volume `lavalink-config-rendered`)
4. Lavalink mounts that file read-only via `subPath`

This replaced the old busybox sed approach. The configmap (`kube/configmap.yaml`) still exists with `${VAR}` placeholders but is **NOT used at runtime** — it serves as documentation / fallback reference only. The active config comes from the Python renderer.

## Bot Player Architecture

### player.py
- Per-guild state dict (`guild_state`) tracks: queue, current track, player, text_channel, voice_channel, source_provider, repeat_mode, filters, autoplay settings
- `_resolve_and_play()` handles source resolution:
  - For spotify/tidal: tries direct stream sidecars first (port 8801/8802)
  - Falls back to Lavalink search (LavasRC → YouTube)
  - Source map: youtube→YouTube, youtube_music→YouTubeMusic, soundcloud→SoundCloud, spotify→spsearch, tidal→tidal
- Track retry: `MAX_TRACK_RETRIES=3`, `RETRY_BACKOFF_SECONDS=1.5` (env-overridable)
- Uses wavelink 3.5+ (`wavelink.Playable.search`, `wavelink.Pool`)

### Unified Playback System (playback/)
- `session_registry.py` — Tracks active sessions per guild:channel
- `orchestrator.py` — Multi-instance bot orchestrator (health checks, credential loading)
- `router.py` — PlaybackRouter routes play requests through content classification
- `content_filter.py` — Content filtering (per-guild rules)
- `user_bans.py` — Per-guild user ban management
- `classifier.py` — Classifies content type (audio/video/radio)
- `persistence.py` — Unified queue persistence (replaces legacy session.json for multi-instance)
- `unified_controls.py` — Unified control interface
- `queue_display.py` — Queue rendering utilities

### views/unified_remote.py
- `UnifiedControlView` — Persistent Discord view (timeout=None, registered in setup_hook)
- Handles both audio (wavelink) and video (activity streamer) playback
- Buttons: Previous, Pause/Resume, Next, Add to Playlist, Block Track
- Detects current media type and delegates to the correct backend

### session.py
- JSON file persistence (`data/sessions.json`)
- Saves: voice_channel_id, text_channel_id, current track, queue, auto_resume flag, source_provider, repeat_mode, filters, crossfade, tune
- Auto-resume on bot restart when `auto_resume=True`

## Discord Activity System (video/)

The bot runs a Discord Activity (Embedded App) for video streaming, whiteboard, visualizer, and lyrics overlay. The Activity backend runs in the bot container on port 8090.

### Components

| Module | Purpose |
|--------|---------|
| `activity_backend.py` | aiohttp server, serves frontend, Activity API endpoints |
| `activity_launcher.py` | Launches Discord Activity sessions via the API |
| `activity_streamer.py` | HLS video transcoding + streaming orchestration |
| `ws_hub.py` | WebSocket hub for real-time state sync (play/pause/seek/whiteboard) |
| `hls_transcode.py` | FFmpeg HLS transcode with QSV hardware acceleration |
| `hls_cleanup.py` | Cleanup of stale HLS segments |
| `source_router.py` | Routes video requests to source-specific resolvers |
| `music_video_resolver.py` | Resolves music video URLs from multiple sources |
| `tidal_resolver.py` | Tidal-specific video resolution |
| `sources.py` | YouTube video downloading (yt-dlp) |
| `session_registry.py` | Tracks active video sessions per guild |
| `stroke_registry.py` | Whiteboard stroke persistence + sync |
| `sticker_catalog.py` | Whiteboard sticker asset management |
| `lyrics_service.py` | Synced lyrics overlay (LRC + Genius providers) |
| `visualizer_manager.py` | Audio visualizer engine management |
| `visualizer_registry.py` | Available visualizer types |
| `audio_feature_bus.py` | Real-time audio features for visualizer |
| `beat_timing.py` | Beat detection for visualizer sync |
| `gpu_probe.py` | Intel GPU capability detection (QSV/VA-API) |

### Frontend (activity_frontend/)
- Single-page HTML/JS app loaded in Discord's Activity iframe
- HLS.js for video playback
- Canvas-based whiteboard (pen, shapes, text, stickers, eraser)
- WebSocket client for real-time sync
- Discord Embedded App SDK for authentication

### HLS Transcoding
- FFmpeg 9 (built from source in bot Dockerfile) with QSV/VA-API hardware acceleration
- Intel iGPU (device 0300-7d55) via `/dev/dri` hostPath mount
- `supplementalGroups: [26]` (video group) + `privileged: true` for device access
- Segments written to tmpfs emptyDir (`/tmp/hellodj_hls`, 2Gi RAM-backed)
- Real-time transcoding with `-re` flag for live streaming

## Voice Pipeline (voice/)

| Module | Purpose |
|--------|---------|
| `wakeword.py` | ONNX wake word model inference (80ms tick loop) |
| `audio_pipeline.py` | Opus frame receive, buffering, VAD |
| `hybrid_player.py` | wavelink + discord.ext.voice_recv (PipelineSink) |
| `stt.py` | Speech-to-text (local Whisper or cloud APIs) |
| `tts.py` | Text-to-speech (Speaches service or cloud) |
| `intent.py` | Intent classification from transcribed text |
| `llm_intent.py` | LLM-based intent recognition (OpenAI-compatible API) |
| `query_handler.py` | General query routing (music, news, stocks, time, etc.) |
| `voice_commands.py` | Voice command execution |

### External Services
- **Speaches** (TTS): `http://speaches.speaches-service.svc.cluster.local:8000`
- **LLM API**: Configurable (default: `https://api.openai.com/v1`, model: `gpt-4o-mini`)

## Bot Cogs

| Cog | File | Purpose |
|-----|------|---------|
| Music | `cogs/music.py` | /play, /search, /queue, /nowplaying, source selection |
| Playlists | `cogs/playlists.py` | /playlist create/save/load/delete |
| Filters | `cogs/filters.py` | /filter nightcore/vaporwave/8bit/8d/etc |
| Autoplay | `cogs/autoplay.py` | Auto-queue similar tracks when queue empties |
| Admin | `cogs/admin.py` | /admin blacklist/allowlist/mode management |
| AdminPanel | `cogs/admin_panel.py` | /hellodj command group (activation, settings) |
| Lyrics | `cogs/lyrics.py` | /lyrics command |
| Info | `cogs/info.py` | /info, /ping, bot status |
| Help | `cogs/help.py` | /help command |
| Radio | `cogs/radio.py` | /radio genre-based streaming |
| Voice | `cogs/voice.py` | Wake word, voice commands, opus receive |
| Video | `cogs/video.py` | Discord Activity, video streaming, visualizer |
| Playback | `cogs/playback.py` | Unified playback orchestration |
| Visualizer | `cogs/visualizer.py` | Audio visualizer control |

## Bot Background Tasks

| Task | Interval | What it does |
|------|----------|--------------|
| `_token_refresh_watchdog` | 5 min | Refreshes Tidal token + re-pushes YouTube OAuth+PoToken to Lavalink. **On AWS this in-process loop is complemented/superseded by the durable watchdog** (see below) for stored source creds — the in-bot loop dies on a pod bounce, the durable one does not. |
| `_potoken_refresh_task` | 1 hour (configurable) | Fetches fresh poToken from bgutil server, stores in cred DB, pushes to Lavalink |
| `_gateway_health_watchdog` | 30s checks | Detects gateway READY stalls, force-reconnects, escalates to pod restart |
| `_guild_policy_watchdog` | periodic | Re-checks guild authorization as admins join/leave |
| `_orchestrator_health_loop` | 30s | Health checks for multi-instance bot orchestrator |

### AWS platform: durable token-refresh watchdog (playback-orchestrator)

> Spec: `unified-oauth-and-token-watchdog` (R5). AWS EKS only.

Because the bot's in-process `_token_refresh_watchdog` dies whenever the bot pod
bounces, the AWS platform runs a **durable** token-refresh watchdog inside the
standing `playback-orchestrator` container — which already holds a run loop +
DynamoDB access and survives a bot bounce. `__main__.main()` starts it on a
**daemon thread alongside the health server** (`/healthz` on `PORT`, default
8080) via `watchdog_bootstrap.start_watchdog_thread()`.

- `token_watchdog.TokenWatchdog.tick()` — one pass: enumerate `SourceCredential`
  items whose `expires_at` is within the near-expiry threshold (via
  `CoreTable.scan_entity`, a paginated, key-projected scan that never pulls
  `enc_blob`), refresh each via the matching `RefreshClient`, and write back the
  new envelope-encrypted blob + `last_refresh_at` + `refresh_status` with an
  optimistic-lock update (multi-replica safe).
- **Per-item isolation:** one item's refresh failure sets
  `refresh_status=failed` (prior blob intact) and continues; the loop/container
  never crashes.
- **Degraded mode:** when no datastore / KMS / provider clients are configured
  the watchdog logs `degraded: watchdog disabled` and does NOT start — the
  health server still comes up.
- **Env (playback-orchestrator):** `HELLODJ_SOURCE_CREDS_KMS_KEY_ID`,
  `TOKEN_WATCHDOG_INTERVAL` (default 300s), `TOKEN_WATCHDOG_THRESHOLD` (default
  600s), `HELLODJ_GOOGLE_OAUTH_SECRET_ARN` + provider client id/secret envs.

**Playback readers** (`bot/playback/guild_credentials.py`) resolve the
`USER#<sub>`/`SOURCECRED#<provider>` item and decrypt via `token_crypto` with
the reader's KMS decrypt grant, falling back to the legacy per-guild Secrets
Manager secret when the DynamoDB item is absent. The YouTube just-in-time
`POST /youtube` all-fields-together swap (OAuth + poToken + visitorData) and the
per-node `asyncio.Lock` serialization + bounded TTL cache are preserved.

**Migration backfill** (`platform/components/migration`, one-shot Job
`python -m migration_job.backfill_main`): reads existing
`hellodj/<stage>/guild/*` secrets and writes encrypted `SourceCredential` items;
idempotent; logs counts (no token material). Run after the CMK + IAM are
deployed.

## LavasRC Provider Configuration

The `lavasrc` plugin resolves Spotify/Tidal tracks to playable sources:
```yaml
providers:
  - "scsearch:%QUERY%"        # SoundCloud first (most reliable, no auth)
  - "ytsearch:\"%ISRC%\""     # YouTube ISRC lookup (fallback)
  - "ytsearch:%QUERY%"        # YouTube text search (last resort)
```

This means when YouTube is broken, SoundCloud becomes the ONLY working provider for Spotify/Tidal track resolution (unless direct stream sidecars work).

## Volumes & Storage

| PVC | Type | Mount | Purpose |
|-----|------|-------|---------|
| `hellodj-data-pvc` | Longhorn 1Gi | `/app/data` | Sessions, playlists, oauth.json, hellodj.db (encrypted credential store) |
| `hellodj-config-pvc` | NFS (192.168.42.8) | `/app/config` | Bot log, shared config |
| `hellodj-models-pvc` | NFS (192.168.42.8) | `/app/models` | Hello_DJ.onnx wake word model |
| `hellodj-backups-pvc` | NFS (192.168.42.8) | `/app/config-backups` | Configuration backups |

Additional volumes (not PVCs):
| Volume | Type | Mount | Purpose |
|--------|------|-------|---------|
| `lavalink-config-rendered` | emptyDir | `/out` (init), `/opt/Lavalink/application.yml` (lavalink) | Rendered Lavalink config from credential store |
| `hellodj-config-backups` | emptyDir | `/app/config-backups` | Transient backup storage |
| `dev-dri` | hostPath `/dev/dri` | `/dev/dri` (bot) | Intel iGPU device nodes for QSV transcoding |
| `hls-tmp` | emptyDir (Memory, 2Gi) | `/tmp/hellodj_hls` (bot) | RAM-backed tmpfs for HLS segments |

NFS paths on Synology:
- Config: `/volume1/Kubernetes/HelloDJ/data`
- Models: `/volume1/Kubernetes/HelloDJ/models`
- Backups: `/volume1/Kubernetes/HelloDJ/backups`

## Kubernetes Services

| Service | Port | Internal URL |
|---------|------|--------------|
| hellodj (lavalink) | 2333 | `hellodj.hellodj-service.svc.cluster.local:2333` |
| hellodj (activity) | 8090 | `hellodj.hellodj-service.svc.cluster.local:8090` |
| lavalink-pool (headless) | 2333 | `lavalink-pool.hellodj-service.svc.cluster.local:2333` |
| yt-cipher | 8001 | `yt-cipher.hellodj-service.svc.cluster.local:8001` |
| potoken-server | 4416 | `potoken-server.hellodj-service.svc.cluster.local:4416` |
| hellodj-web-ui | 8080 | `hellodj-web-ui.hellodj-service.svc.cluster.local:8080` |
| speaches (TTS) | 8000 | `speaches.speaches-service.svc.cluster.local:8000` |

## Ingress

- Host: `hellodj.celestium.life`
- Path `/activity/` → `hellodj:8090` (Activity frontend + WebSocket hub)
- Path `/` → `hellodj-web-ui:8080` (Web config UI)
- TLS: cert-manager + Let's Encrypt (`celestium-le-production`)
- Ingress class: traefik
- WebSocket: Traefik auto-upgrades on `Upgrade: websocket` header (no special annotation needed)

## Docker Registry

- URL: `registry.celestium.life` (Harbor)
- ImagePullSecret: `harbor-credentials` in namespace
- Images: `registry.celestium.life/hellodj/bot:<tag>`, `registry.celestium.life/hellodj/web-ui:<tag>`, `registry.celestium.life/hellodj/tidal-stream:<tag>`, `registry.celestium.life/hellodj/spotify-stream:<tag>`, `registry.celestium.life/hellodj/lavalink:<tag>`

## DO NOT Break These Things

1. **The init container Python renderer** — `render_lavalink_config.py` reads from the encrypted SQLite credential store and renders a complete `application.yml`. It runs in the bot image (needs Python + cryptography). If you change the credential keys, update the renderer too. The data volume is mounted **read-only** in the init container.

2. **The single POST /youtube request** — `push_youtube_oauth()` sends OAuth token AND poToken together in ONE request. The youtube-source plugin replaces ALL fields on each call. If you send them separately, the second call erases the first.

3. **The yt-cipher-secret API_TOKEN** — Shared between the yt-cipher container (env `API_TOKEN`) and the rendered Lavalink config (`remoteCipher.password`). If these don't match, cipher requests are rejected.

4. **The credential store encryption key** — `HELLODJ_DB_KEY` must be consistent across deployments. Changing it invalidates all stored credentials.

5. **Player retry logic** — `MAX_TRACK_RETRIES=3` with `RETRY_BACKOFF_SECONDS=1.5`. Don't remove this — YouTube has transient failures constantly.

6. **LavasRC provider order** — SoundCloud first is intentional. YouTube is currently unreliable (mid-2026 blocking). Don't reorder.

7. **Direct stream sidecars** — `tidal-stream:8801` and `spotify-stream:8802` share the `/app/data` volume (for `hellodj.db` with tokens). They must mount the SAME `hellodj-data-pvc`.

8. **DNS config** — The deployment has `dnsConfig.nameservers: [192.168.42.1, 192.168.99.42]` and NO custom `search` domains. Adding `search celestium.life` breaks Lavalink's plugin downloads (wildcard DNS hijack).

9. **TCP keepalive sysctl** — `net.ipv4.tcp_keepalive_time: "60"` prevents the Discord gateway websocket from stalling behind NAT. Don't remove.

10. **SecurityContext** — `runAsUser/runAsGroup/fsGroup: 1000`, `supplementalGroups: [26]` (video group), `privileged: true` on bot container for `/dev/dri` access. The bot writes to `/app/data`. Changing UIDs breaks volume permissions.

11. **PoToken refresh graceful degradation** — The `_potoken_refresh_task` must NOT crash the bot if the potoken-server is unavailable. It logs a debug message and skips. TVHTML5_SIMPLY and ANDROID_VR work without PoToken on clean IPs.

12. **Activity port 8090** — The bot container exposes this for the Discord Activity backend. The service + ingress route `/activity/` here. Don't remove or change without updating both.

13. **HLS tmpfs volume** — `hls-tmp` is RAM-backed (`medium: Memory`, 2Gi limit). HLS segments are written here during video streaming. If the bot crashes mid-stream, the tmpfs is automatically cleaned. Don't switch to disk-backed — latency matters for live streaming.

14. **UnifiedControlView persistence** — `timeout=None`, fixed `custom_id` on all buttons, registered via `bot.add_view()` in `setup_hook`. If custom_ids change, existing messages' buttons stop working.

15. **Kustomize image overrides** — `kube/kustomization.yaml` overrides image tags for bot, web-ui, tidal-stream, and spotify-stream. The Lavalink image tag is set directly in `deployment.yaml` (not overridden by kustomize). Always check BOTH files when verifying image tags.

## Config Drift Warning

There are THREE sources of Lavalink config:
1. `kube/configmap.yaml` (lavalink-config ConfigMap) — has `${VAR}` placeholders but is **NOT used at runtime**. Serves as documentation/reference only.
2. `bot/lavalink/application.yml` — used by docker-compose for local dev
3. `bot/render_lavalink_config.py` — **THE ACTIVE PRODUCTION CONFIG**. Reads from credential store, renders complete YAML, written by init container to emptyDir.

The **active production config** is rendered by `render_lavalink_config.py`. The configmap remains in the kustomization for reference but is not mounted into any container.

**Plugins are baked into the custom Lavalink image** (`registry.celestium.life/hellodj/lavalink:<tag>`). The Dockerfile at `kube/lavalink/Dockerfile` uses a custom `Lavalink.jar` (with lavaplayer fMP4 HLS patches) on `eclipse-temurin:21-jre` and copies plugins from `kube/lavalink/plugins/`. This is NOT a layer on the official Lavalink image.

**All three configs share the same client list**: TV, TVHTML5_SIMPLY, ANDROID_VR, MUSIC, WEB. Keep them in sync.

## Current Image Tags (as of 2026-08-22)

Deployment.yaml values (may be overridden by kustomize):
- Bot: `registry.celestium.life/hellodj/bot:rm-video-cmd-2026-08-22`
- Lavalink: `registry.celestium.life/hellodj/lavalink:audio-pipe-2026-08-23` (NOT overridden by kustomize)
- Tidal stream: `registry.celestium.life/hellodj/tidal-stream:whiteboard-2026-08-20`
- Spotify stream: `registry.celestium.life/hellodj/spotify-stream:latest`
- Web UI: `registry.celestium.life/hellodj/web-ui:v2026-08-17`

Kustomize overrides (`kube/kustomization.yaml`):
- Bot: `shader-presets-2026-08-24`
- Web UI: `latest`
- Tidal stream: `latest`
- Spotify stream: `latest`

External images (not in registry):
- yt-cipher: `ghcr.io/kikkia/yt-cipher:master`
- potoken-server: `brainicism/bgutil-ytdlp-pot-provider:latest`

> **AWS re-platform note:** Under the `aws-saas-replatform` spec these two
> external (Debian-based) images are rebuilt from scratch with Nix (no
> Ubuntu/Debian base) as independent components at
> `platform/components/yt-cipher/` (Nix-built Deno base, port 8001, shared
> secret `API_TOKEN` injected at runtime from AWS Secrets Manager) and
> `platform/components/potoken-server/` (Nix-built Node.js base, port 4416).
> The on-prem deployment above still uses the upstream images.

## CRITICAL: YouTube Plugin Choice

The **official** `youtube-plugin:1.18.2` from Maven does NOT support YouTube's SABR (Server Adaptive Bitrate) streaming protocol. As of mid-2026, YouTube serves ONLY SABR streams to the WEB client. The official plugin gets "No supported audio streams available, available types: " (empty).

The **custom** `youtube-plugin-sabr.jar` (baked into the Lavalink image at `kube/lavalink/plugins/`) supports SABR streaming. This is essential for YouTube playback to work.

**DO NOT switch back to the official plugin** (`ghcr.io/lavalink-devs/lavalink:latest` + Maven dependencies) without verifying SABR support has been merged upstream. The custom image is built from `kube/lavalink/Dockerfile` which uses a custom `Lavalink.jar` (with lavaplayer fMP4 HLS patches) on eclipse-temurin:21-jre base, with plugins copied from `kube/lavalink/plugins/`.

Source jars: `celesrenata/Lavalink/plugins/youtube-plugin-sabr.jar` and `lavasrc-plugin-4.8.3.jar`.

## CRITICAL: Custom Lavalink.jar

The `kube/lavalink/Lavalink.jar` is a **custom build** from `celesrenata/Lavalink` (branch `dev`). It includes:
- lavaplayer fMP4 HLS patch (required for Tidal HLS manifest streaming)
- Standard Lavalink v4 server functionality

The Dockerfile does NOT use the official `ghcr.io/lavalink-devs/lavalink` image as a base. It uses `eclipse-temurin:21-jre` directly with the custom JAR. This means Lavalink version upgrades require rebuilding the JAR from the fork.

## Bot Dockerfile Key Details

- Base: `python:3.11-slim`
- FFmpeg 9 built from source with: QSV (libvpl), VA-API, libx264, libx265, libopus, libdav1d, OpenSSL (HTTPS support for Tidal HLS)
- Intel VA-API driver + libmfx-gen for hardware transcoding
- yt-dlp installed for YouTube video downloading
- Requirements split into layers: core → torch → AI (prevents registry push timeouts)
- Stickers directory copied for whiteboard feature
- Wake word model optionally baked in (also mountable via volume)

## AWS platform: fixed-callback source OAuth + multi-bot pool (2026-08-28)

> AWS EKS only. Web-ui changes deploy via the CI/CD pipeline (source push →
> image rebuild → `cdk deploy hellodj-eks -c hellodj:imageTag=<HEAD>` to roll).

### Per-account source OAuth — ONE fixed callback per provider (model B2)

Source OAuth (YouTube/YouTube Music/Spotify/Tidal) uses a SINGLE fixed callback
per provider: `https://<stage>.us-east-1.hellodj.bot/auth/oauth/<provider>/callback`.
Providers require every redirect_uri to be pre-registered, so the connecting
user's identity rides in the OAuth `state` (signed session), NEVER the URL path
— exactly the Discord `/auth/discord/callback` convention. Register 3 paths
(spotify/tidal/youtube — youtube_music shares the Google app) × 3 stage hosts
per provider console.

- Routes: `source_account_routes.register_source_oauth_routes(bp)` adds
  `/auth/oauth/<provider>/connect` (mints state, redirects to provider) and the
  fixed `/auth/oauth/<provider>/callback` (validates state, exchanges, stores).
- Credentials stay PER-USER (`USER#<sub>/SOURCECRED#<provider>`, the unified
  encrypted store). A guild "uses" a source by binding to a managing user's
  connected credential (guild binding), so tokens are never duplicated per guild.
- `source_oauth.redirect_uri_for(provider)` builds the guild-free fixed URI;
  the legacy per-guild `/auth/sources/<gid>/<provider>/*` routes remain for
  migration but their guild-in-path redirect is the deprecated shape.
- Account UI: connect / re-authorize / refresh-PoToken (YouTube) / clear-auth
  (disconnect), plus a read-only "Your entitlements" panel. Account is now a
  sidebar nav item; the Config "Sources" tab was removed (sources live on
  Account).

### Global Discord bot-application pool (multi-bot per guild)

Discord dedupes bot members by application id, so N simultaneous bots in a
guild require N distinct Discord applications. A fixed GLOBAL pool of
pre-registered applications is stored in Secrets Manager
`hellodj/<stage>/bot-app-pool` — a JSON array of
`{label, client_id, client_secret, bot_token}` (created empty by
`auth-stack.ts`, populated out-of-band). Applications are global (one app may
serve many guilds); a guild holds each at most once.

**The Primary_Bot application (`DISCORD_CLIENT_ID`, the `discord-bot-core`
command-owner already in every guild) MUST NOT be a pool member.** Handing out
its invite link or bringing it up as a secondary voice gateway would open a
second gateway identify for the same application id, which Discord rejects and
which collides with the running Primary. The shared parser
`hellodj_platform_logic.bot_app_pool.parse_pool` takes an `exclude_client_ids`
set and drops the Primary regardless of the secret's contents; both the web-ui
`BotAppPool` (via `primary_client_id`, wired from `DISCORD_CLIENT_ID` in
`bootstrap.py`) and the orchestrator `PoolCredentialSource` (via
`primary_client_id`, wired in `instance_bootstrap.py`) pass it. `DISCORD_CLIENT_ID`
is injected into BOTH the web-ui and the `playback-orchestrator`
(`MULTI_BOT_RUNTIME_COMPONENT`) container env by `workloads-stack.ts`. The beta
pool secret was also corrected to remove the Primary entry it had erroneously
contained (8 distinct secondaries `00`–`07`, no `HelloDJ` slot); the guard is
the durable defense so a future mispopulation can never resurface it.

- Web-ui: `bot_app_pool.BotAppPool` (read-only pool reader) +
  `BotAppAssignmentService` (`assign_next` quota-gated, `release`,
  `list_claims`, `pool_size`). Claims: `GUILD#<gid>/BOTAPP#<client_id>`,
  entity `BotAppClaim` (no credential material). Invite URL is
  `discord.com/oauth2/authorize?client_id=<id>&scope=bot applications.commands&permissions=2150714368`
  (View Channel, Send Messages, Embed Links, Read Message History, Connect,
  Speak, Use Application Commands).
- Routes (`guild_bot_routes.register_bot_routes`, ownership-gated):
  `POST /guilds/<gid>/bots` (assign next free app, capped by the guild OWNER's
  `max_bots_per_guild`), `/bots/<client_id>/remove`, `/bots/<client_id>/name`,
  `/bots/<client_id>/avatar`. UI is the guild-detail "Bots" tab.
- Per-bot identity: `bot_identity.BotIdentityService` is now keyed
  `GUILD#<gid>/BOTIDENTITY#<client_id>` (legacy per-guild key when client_id
  is empty). Default names iterate `HelloDJ`, `HelloDJ#1`, … by claim index
  (`default_bot_name`); the OWNER's `custom_name` / `custom_avatar`
  entitlements gate per-bot rename + avatar (enforced server-side).
- CDK: `workloads-stack.ts` grants web-ui + bot-runtime readers READ on the
  pool secret (threaded via `botAppPool` in the workloads secrets bag).

**Runtime (now wired — `aws-multi-bot-runtime` spec):** the AWS multi-instance
bot runtime that connects the assigned pool applications now exists (see the
next section). The `playback-orchestrator` reads the pool + per-guild claims and
runs one voice-only secondary gateway per claimed, token-bearing application.
The only remaining out-of-band step is populating the pool apps' `bot_token`s in
the `hellodj/<stage>/bot-app-pool` secret — a pool entry with an empty
`bot_token` is skipped (logged, never connected), so the extra bots come online
for exactly the applications whose tokens are filled in.

## AWS platform: multi-bot runtime + CPU→GPU offload (aws-multi-bot-runtime spec)

> AWS EKS only. `playback-orchestrator` source changes deploy via the CI/CD
> pipeline (source push → image rebuild → `cdk deploy hellodj-eks -c
> hellodj:imageTag=<HEAD>` to roll); infra (env / IAM / HPA / NodePool) via
> `cdk deploy hellodj-eks`. This is the AWS port of the on-prem
> `Instance_Orchestrator` (unified-playback R6) — it differs ONLY in the
> credential source and host process; the assign/release/health/quota logic is
> inherited unchanged.

### Instance_Runtime (secondary bot gateways in playback-orchestrator)

The standing `playback-orchestrator` process now hosts a third daemon thread
next to its `/healthz` server and the durable token watchdog: the
**Instance_Runtime**, which brings the guild's claimed pool applications online
as voice-only secondary bots.

- **Credential source (pool ∩ claims, not SQLite).** `PoolCredentialSource`
  (`playback_orchestrator/instance_runtime.py`) reads the
  `hellodj/<stage>/bot-app-pool` secret via IRSA and parses it with the SHARED
  `hellodj_platform_logic.bot_app_pool.parse_pool` (the SAME parser the web-ui
  uses — the `PoolApp` frozen dataclass keeps `client_secret`/`bot_token`
  internal and out of `repr`). It reads the guild's `GUILD#<gid>`/`BOTAPP#*`
  `BotAppClaim` items from the `hellodj-core` table, and
  `instances_for_guild(gid)` returns the intersection: pool entries that are
  claimed by the guild AND carry a `bot_token`. The on-prem
  `instance.<index>.token` / `instance.<index>.app_id` SQLite keys are NOT read
  or required on AWS.
- **Voice-only secondary gateways.** `AwsInstanceOrchestrator(InstanceOrchestrator)`
  overrides ONLY `initialize()` to build a `BotInstance` per claimed,
  token-bearing pool app and connect them in parallel with per-instance
  isolation (one gateway's connect/health failure marks that instance
  `unhealthy` and continues — it never crashes the loop, the watchdog, or the
  health server). `discord-bot-core` remains the single Primary_Bot that owns
  slash commands; these instances only join voice channels. Assign / release /
  health-check / quota are inherited from the on-prem orchestrator unchanged.
- **Shared Lavalink node.** All Bot_Instances share ONE Lavalink node/session
  (mirroring unified-playback R6.7), wired via the `HELLODJ_LAVALINK_NODE_URL`
  env.
- **Entitlement quotas.** Assignment resolves the owning user's effective
  entitlements through the SHARED `entitlements_core`
  (`effective_max_bots_per_guild`, `quota_reached`) so the web-ui, the on-prem
  orchestrator, and this runtime agree exactly; a resolution failure applies the
  restrictive `DEFAULT_ENTITLEMENTS` (limits = 1), never a more-permissive
  fallback.
- **Degraded no-op.** When the pool secret is unconfigured, the pool is empty,
  or `discord.py` is unavailable, the runtime logs `degraded: instance runtime
  disabled` and does NOT start — the health server still comes up. SIGTERM/SIGINT
  disconnects the instances cleanly within the shutdown window.
- **Daemon-thread bootstrap.** `instance_bootstrap.start_instance_runtime_thread()`
  (mirrors `watchdog_bootstrap`) builds the source + orchestrator from env and
  runs the asyncio loop on a daemon thread; `__main__.main()` starts it right
  next to `start_watchdog_thread()`. Guild discovery is a `BotAppClaim` scan.
- **Single-replica constraint (hard).** The orchestrator runs at
  `minReplicas=1`/`maxReplicas=1` (pinned in `component-workloads.ts`). A second
  replica would double-connect the same bot tokens and Discord rejects the
  second gateway identify. Do NOT scale it horizontally.
- **CDK least privilege** (`workloads-stack.ts`): `playback-orchestrator` gets
  READ on the pool secret (already granted) + a scoped `BotAppClaimRead`
  statement on the core table's `GUILD#*` items, plus the runtime env (stage,
  region, `HELLODJ_LAVALINK_NODE_URL`). No static Discord credentials live in
  the manifest — bot tokens are read from the pool secret at runtime via IRSA.

### CPU→GPU render/transcode offload (10-minute warm window)

The compute model: **everything runs on a single instance** until CPU
render/transcode load crosses a threshold, at which point a **GPU host is spun
up**, render/transcode work is **drained onto the GPU**, and the GPU host is
kept **warm for 10 minutes** after its last use before it is **spun down**. This
wires the CPU-threshold scale-up trigger and the drain to the existing Karpenter
GPU scale-to-zero primitive.

- **CPU-threshold scale-up trigger.** The `hls-transcode` HPA targets a CPU
  utilization threshold (the shared `DEFAULT_HPA_TARGET_CPU_PERCENT` mirror).
  When CPU render/transcode load exceeds it, the HPA scales up transcode pods
  that request a GPU, so Karpenter provisions a `GPU_Host` from the
  `transcode-gpu` NodePool for the pending GPU pod. While the node is
  provisioning, the CPU transcode path keeps serving in-flight work so the
  interactive latency budget holds during spin-up.
  - **Pod GPU request (wired 2026-08-29).** The transcode pod actually
    REQUESTS `nvidia.com/gpu: 1` — `ResourceSpec.gpuUnits` on
    `TRANSCODE_RESOURCES` (`component-workloads.ts`) is emitted into BOTH the
    container `resources.requests` and `resources.limits` (extended resources
    require request == limit). This is what makes a pending transcode replica
    unschedulable on the CPU app fleet (which advertises zero GPU) and forces
    Karpenter to provision the `transcode-gpu` host from zero — the
    scale-up-on-arrival trigger (asserted by `eks-gpu.test.ts`: "transcode pod
    REQUESTS a time-sliced nvidia.com/gpu"). The `transcode-gpu` toleration +
    `workload=transcode` nodeSelector decide WHICH node; the GPU request is what
    PROVISIONS one. CPU-only components (e.g. `web-ui`) emit no GPU resource.
  - **Runtime control loop (wired 2026-08-29).** The `hls-transcode` component
    now runs the execution half: `hls_transcode/runtime.py`
    (`TranscodeRuntime`) samples live demand (`cpu_pressure()` + active jobs),
    probes NVENC readiness (`probe_gpu_ready()` — needs `nvidia-smi` + ffmpeg
    advertising `h264_nvenc`), advances the shared `hybrid_gpu` controller via
    `TranscodeScheduler.observe(...)`, and publishes CPU/GPU pressure to
    CloudWatch `HelloDJ/Transcode` (R16.4). `FfmpegProcessManager` spawns/kills
    the ffmpeg process per plan (SIGTERM → wait `DEFAULT_PROCESS_DRAIN_SECONDS`
    120s → SIGKILL), and `SegmentUploader` mirrors produced HLS artifacts to
    the S3 CloudFront origin. `server.py:main()` runs the loop + process
    manager next to the aiohttp server. Without this loop the controller stayed
    in `ELECTRIC_ONLY` forever and never drained to GPU.
  - **CDK env + IAM (wired 2026-08-29).** `hls-transcode` now has
    `dependencies: { hlsTranscode: true }`; the workloads stack injects
    `HELLODJ_GPU_AVAILABLE=true`, `HELLODJ_HLS_S3_BUCKET`
    (`hellodj-hls-<stage>-<region>`, EdgeStack), `HELLODJ_CLOUDFRONT_DOMAIN`
    (`https://<stage>.<region>.hellodj.bot`), `HELLODJ_HLS_S3_PREFIX=hls`,
    `HELLODJ_METRICS_NAMESPACE=HelloDJ/Transcode`, and grants the IRSA role
    read/write on the HLS bucket + `cloudwatch:PutMetricData` scoped to the
    `HelloDJ/Transcode` namespace. Deploy via `cdk deploy hellodj-eks`; the
    component source deploys via the pipeline.
- **Drain to GPU.** When a `GPU_Host` is available, transcode/render workloads
  schedule onto it via the `transcode-gpu` taint / toleration, draining new work
  off the CPU path. When the node is reclaimed, in-flight transcode jobs drain
  gracefully within the existing `GPU_DRAIN_TIMEOUT_SECONDS` (120s) window before
  the node is removed — the drain reuses that primitive rather than introducing a
  new one. The `hls-transcode` pod's own `terminationGracePeriodSeconds` is now
  120s to match, so the pod is not SIGKILLed mid-drain.
- **10-minute warm window + scale-to-zero.** The `GpuIdleConfig.idle_window_seconds`
  default is 600 seconds (10 minutes), mirrored by the CDK
  `DEFAULT_GPU_IDLE_WINDOW_SECONDS = 600`; the value is always within the
  enforced [60, 900] range and the CDK mirror equals the shared default. The
  `transcode-gpu` NodePool uses `consolidationPolicy: WhenEmpty` +
  `consolidateAfter: 600s`, so after a continuous 10-minute window with no active
  transcode workload the node scales to zero. While any transcode workload is
  active on the node, `WhenEmpty` does not fire (it only consolidates at zero
  transcode pods), so a busy GPU host is never reclaimed mid-work. GPU
  autoscaling is orthogonal to the orchestrator's single-replica constraint — it
  scales the `transcode-gpu` node, not the orchestrator.

### GPU node image: EKS-optimized AL2023 accelerated AMI (self-join, updated 2026-08-29)

The `GPU_Host` is NOT a custom/pre-baked NixOS AMI. The `transcode-gpu`
Karpenter `EC2NodeClass` (`eks-stack.ts` `addGpuNodePool()`) selects the
**EKS-optimized Amazon Linux 2023 accelerated AMI** via an
`amiSelectorTerms: [{ alias: 'al2023@latest' }]` term. The `alias` implicitly
sets `amiFamily: AL2023`, so Karpenter **auto-generates the NodeConfig
bootstrap userData** (cluster name + `apiServerEndpoint` + cluster CA + kubelet
flags + the `karpenter.sh/unregistered` registration taint). The node runs
`kubelet`/`containerd` and **registers with the EKS control plane on boot** —
the EKS-native self-join path. On a `g5g.xlarge` (T4G) GPU instance the alias
resolves to the **accelerated** variant, which ships the NVIDIA driver; the
NVIDIA k8s device plugin is supplied separately by the time-slicing DaemonSet
(`addNvidiaDevicePlugin()`). The transcode node role is mapped in `aws-auth`
with `system:nodes`, authorizing the joined kubelet.

WHY NOT a pre-baked NixOS AMI (retired 2026-08-29): the old
`amiFamily: Custom` + baked-NixOS-AMI approach had no way to join the cluster —
the NixOS image shipped no kubelet/containerd and `Custom`-family nodes get NO
auto-generated bootstrap userData, so a launched node would boot, never
register, and be reaped as unregistered (the pending `hls-transcode` pod would
never schedule). The `infra/ami/` NixOS AMI flake, the `build-gpu-ami` pipeline
step, the `bakedGpuAmiId`/`PLACEHOLDER_GPU_AMI_ID` wiring, and the
`/hellodj/gpu-ami-id` SSM parameter were all removed. The GPU NodePool is now
always emitted (no baked-AMI id to inject, no placeholder gating). NOTE: this
GPU node is the ONE exception to the otherwise Nix-only, no-Debian image policy
— it runs the AWS-managed AL2023 host so it can self-join EKS; the workloads
that land on it are still the Nix-built OCI container images.

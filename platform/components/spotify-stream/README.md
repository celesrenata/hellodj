# spotify-stream component

Direct **Spotify** audio streaming sidecar, built on the Python **librespot**
client + **aiohttp**, packaged as a **Nix-built OCI image** on a **Nix-built
Python base** — with **no Ubuntu/Debian base layer** (Requirements 9.1, 9.2).

## Multi-tenant (multi-tenant-source-streaming, section 2)

The single global librespot session is **gone**. This sidecar serves **every
guild from that guild's owning user's Spotify account**, concurrently and
isolated:

1. Each request carries the **`guild_id`** in its path (Lavalink builds the URL).
2. The sidecar resolves **`guild → owner_sub`** server-side from the
   `GUILD#<gid>` / `OWNER` item (the `sub` is never in a URL or log).
3. It resolves that user's `USER#<sub>` / `SOURCECRED#spotify` credential from
   the **unified per-user credential store** (`hellodj-core` DynamoDB + the
   source-credentials KMS CMK, **Decrypt-only**), **read-only** — the durable
   watchdog owns refresh.
4. It builds/serves a **per-user librespot session** from a bounded, LRU-evicted,
   idle-swept `SpotifySessionPool` (the shared `SessionRegistry` keyed by `sub`).

There is **no shared-account fallback** (R3.6/R10.5). A guild with no owner, no
Spotify credential, a `refresh_status=failed` credential, no captured librespot
blob, or a non-Premium account fails **observably** with a non-secret reason,
scoped to that user — never affecting anyone else (R3.5/R3.7/R7).

### Non-interactive session factory (R3.3)

librespot builds a per-user `Session` **non-interactively** from a reusable
`{username, credentials, type}` blob via
`Session.Builder(conf).stored(<base64 JSON>).create()` — no OAuth at stream time.
That blob is captured **once** at connect time (see below) and stored **inside**
the envelope-encrypted credential blob under `extra.librespot_credentials`; the
resolver flattens `extra`, so the factory sees `tokens["librespot_credentials"]`.
A non-Premium account authenticates but fails at first track-load → per-`sub`
`failed(not_premium)`; a bad/invalid blob → `failed(session_create_failed)`.

### One-time librespot capture (sidecar side of task 2.2)

librespot is a heavy native dependency that lives here, not in the Flask web-ui.
So the web-ui **orchestrates** the one-time interactive capture over this
sidecar's HTTP contract, and this sidecar performs it:

- `POST /auth/librespot/start` `{"sub", "redirect_uri"}` →
  `{"authorize_url": "..."}` — mints the PKCE verifier + Spotify authorize URL
  for librespot's built-in keymaster client; the verifier stays server-side.
- `POST /auth/librespot/complete` `{"sub", "code"}` →
  `{"credentials": {username, credentials, type}}` — exchanges the code, opens a
  `Session`, and returns the reusable blob (never logged) for the web-ui to
  store envelope-encrypted.

## Endpoints (port 8802)

| Method + path | Purpose |
|---|---|
| `GET /stream/<guild_id>/<track_id>` | Per-user audio stream (OGG→MP3 transcode). |
| `GET /preload/<guild_id>/<track_id>` | Warm the per-`(sub,track)` audio cache. |
| `GET /health` | Liveness + per-`sub` pool counts (never a single global status). |
| `GET /auth/status` | Multi-session auth state: per-`sub` phase/reason (digested sub, no tokens). |
| `POST /auth/librespot/start` | Begin one-time capture. |
| `POST /auth/librespot/complete` | Finish capture, return reusable blob. |

## Modules

| Module | Responsibility |
|---|---|
| `config.py` | Env-driven `SpotifyStreamSettings` (table, KMS, pool bounds, DATA_DIR). |
| `resolver_bootstrap.py` | Wire the shared `UserCredentialResolver` + owner lookup over `CoreTable` + KMS (lazy boto3). |
| `librespot_session.py` | Build a `Session` from the stored blob; load + transcode a track. |
| `session_pool.py` | `SpotifySessionPool`, per-`(sub,track)` cache, guild→owner `SpotifyStreamRouter`. |
| `librespot_capture.py` | Sidecar side of the one-time capture (PKCE start/complete). |
| `server.py` | aiohttp routes + honest per-`sub` health. |
| `__main__.py` | Entrypoint: wire router + capture from env, serve. |

## Base image and CPU architecture

- **Runtime:** `pkgs.python3.withPackages [librespot aiohttp boto3 botocore]` +
  `ffmpeg-headless` (OGG→MP3 transcode) + `cacert`, all Nix closures — **not** a
  Debian/Ubuntu layer (R9.1).
- **Image build:** `pkgs.dockerTools.buildLayeredImage`. The shared
  `hellodj_platform_logic` package is vendored into the source tree by the
  pipeline before the build (same as `tidal-stream`).
- **Default architecture:** `aarch64-linux` (AWS Graviton); `x86_64-linux` is a
  documented fallback.

## Credentials (read-only, unified store)

Tokens are **never** baked into the image and **never** written by this sidecar.
Per-user credentials live in the `hellodj-core` DynamoDB table, envelope-
encrypted; the sidecar's IRSA role holds **table read + KMS Decrypt-only** on the
source-credentials CMK. Per-user librespot caches live under
`DATA_DIR/<sub>/spotify-credentials.json` so a restart never mixes users (R9.3).

## Environment

| Var | Purpose |
|---|---|
| `HELLODJ_CORE_TABLE` | Unified credential store table (default `hellodj-core`). |
| `HELLODJ_SOURCE_CREDS_KMS_KEY_ID` | Source-credentials CMK id (Decrypt-only). |
| `AWS_REGION` | AWS region for the boto3 clients. |
| `SPOTIFY_MAX_SESSIONS` | Bounded per-user session-pool size (default 16). |
| `SPOTIFY_SESSION_IDLE_TIMEOUT` | Per-user session idle timeout, seconds (default 900). |
| `SPOTIFY_STREAM_PORT` / `_HOST` | HTTP bind (default 8802 / all interfaces). |
| `DATA_DIR` | Per-user librespot cache root (default `/opt-app/data`). |

## Build

```bash
nix build .#image --system aarch64-linux   # OCI image (Graviton default)
docker load < result                        # load the docker-archive tarball
nix flake check                             # evaluate/validate the flake
```

## Test / lint

```bash
# The shared hellodj_platform_logic lives in the hellodj-cdk repo.
PYTHONPATH=<hellodj-cdk>/shared python3 -m pytest tests/ -q
ruff check --target-version py314 .
```

## Requirements traceability

- **3.1/3.2/3.6/10.5** — per-user `SpotifySessionPool`; guild→owner routing; no
  shared-account fallback.
- **3.3** — non-interactive session factory from the stored librespot blob.
- **3.5/3.7/7** — per-`sub` `failed(not_premium|session_create_failed)`, isolated
  + honest health.
- **6.2/8.3** — track audio cache keyed by `(sub, track_id)`, bounded.
- **9.1/9.2/9.3** — Nix image, no Debian; read + KMS Decrypt-only; per-user cache dir.

# Lavalink "All clients failed to load the item" — Diagnosis & Fix

Date: 2026-08-16 (UTC)
Symptom: `TrackException ... All clients failed to load the item` (severity: suspicious)
- `Client [TVHTML5] failed: Must find sig function from script: /s/player/b0d2d49a/player_embed.vflset/en_US/base.js`
- `Client [ANDROID_VR] failed: This video requires login`
- `Client [WEB] failed: No supported audio streams available`
- `Client [WEB_EMBEDDED_PLAYER] failed: Video player configuration error`
Guild 1501686893765595296, tracks "Sandstorm Darude (HQ Audio)" and "Wake Pig - 04 Wake Pig - Three 3".

## Root cause (validated, not assumed)

The plugin's **local signature deciphering** broke: YouTube changed its player script
(`b0d2d49a`) in a way the youtube-source 1.18.2 local cipher extractor cannot parse
("Must find sig function"). The OAuth token was valid and reaching the plugin
(`YouTube access token refreshed successfully` on every push), and `TV` (the only
OAuth-capable client) was registered — but it still could not decipher the stream-URL
signature, and the non-OAuth clients were bot-blocked ("requires login" / "No supported
audio streams").

Ruled out (with evidence):
- Invalid/expired token — Lavalink logs show successful refresh on every push.
- Missing OAuth-capable client — `TV`/`TVHTML5` registered; the "OAuth enabled without
  OAuth-compatible clients" warning was already gone.
- Network — token refresh + plugin download both succeed.

## Fix applied

Offload signature deciphering to a **remote cipher server** (the official remedy per the
youtube-source README for "Must find sig function"):

1. **New `yt-cipher` service** (Deno, `ghcr.io/kikkia/yt-cipher:master`, port 8001):
   - `kube/yt-cipher-secret.yaml` — `API_TOKEN` (openssl rand -hex 32).
   - `kube/yt-cipher-deployment.yaml` — `OVERRIDE_PLAYER_VARIANT=IAS` (upstream-recommended),
     `XDG_CACHE_HOME=/cache` + emptyDir (fixes the `/.cache` PermissionDenied CrashLoop).
   - `kube/yt-cipher-service.yaml` — ClusterIP `yt-cipher:8001`.
   - Added to `kube/kustomization.yaml`.
2. **Lavalink config** (`kube/configmap.yaml` + `bot/lavalink/application.yml`):
   - Added `remoteCipher: { url, password: ${YTCIPHER_API_TOKEN}, userAgent: hellodj }`.
   - Added `TVHTML5_SIMPLY` to the `clients` list (more robust playback, added in 1.18.0).
   - `kube/deployment.yaml` init container now renders `YTCIPHER_API_TOKEN` from
     `yt-cipher-secret` into the application.yml.
3. **Bot image-tag regression fix**: `kube/deployment.yaml` pinned the bot to
   `voicedbg-20260816` (a stale image with **no** `push_youtube_oauth`), so the OAuth
   token was not being pushed after redeploy. Changed to `latest` (the image that has the
   push function and was pushing before).

## Verification (live, after fix)

- `yt-cipher` pod `1/1 Running`; logs: `Server listening on http://0.0.0.0:8001`.
- Lavalink startup: `Using remote cipher server with URL
  "http://yt-cipher.hellodj-service.svc.cluster.local:8001"` and
  `YouTube source initialised with clients: WEB_REMIX, TVHTML5, TVHTML5_SIMPLY,
  ANDROID_VR, WEB, WEB_EMBEDDED_PLAYER`.
- Bot: `youtube-oauth: pushed refresh token to Lavalink ... (status=204)`;
  `periodic re-push watchdog started`; Lavalink `GET /youtube` →
  `{"refreshToken":"REDACTED_REFRESH_TOKEN"}`.
- **Definitive**: yt-cipher fetched + cached the exact previously-failing player script
  `b0d2d49a` (`Cache miss for player: .../b0d2d49a/player_ias.vflset/en_US/base.js` →
  `Saved player to cache`).
- **Definitive**: the previously-failing tracks now load via Lavalink REST:
  - `GET /v4/loadtracks?identifier=https://www.youtube.com/watch?v=y6120QOlsfU`
    (Sandstorm Darude) → `loadType: track`, title "Darude - Sandstorm", duration 232000.
  - `GET /v4/loadtracks?identifier=ytsearch:Sandstorm Darude` → `loadType: search` with
    a valid track.
  - `GET /v4/loadtracks?identifier=ytsearch:Wake Pig Three 3` → `loadType: search` with
    a valid track.
  - `GET /v4/loadtracks?identifier=https://www.youtube.com/watch?v=FATTzbm78cc`
    (Aphex Twin) → `loadType: track`, duration 368000.
- All pods healthy: `hellodj 2/2 Running`, `hellodj-web-ui 1/1 Running`,
  `yt-cipher 1/1 Running`.

## Notes / caveats

- The `yt-cipher` cache is an `emptyDir` — it is lost on pod reschedule (re-fetched on
  demand). Acceptable; the service re-caches within seconds.
- `OVERRIDE_PLAYER_VARIANT=IAS` is upstream-recommended for reliability; if a future
  YouTube change breaks IAS, try removing it or setting `ES5`/`TV`.
- The remote cipher is a third-party service dependency. If `yt-cipher` is ever
  unreachable, the plugin falls back to local extraction (which is currently broken), so
  keep the `yt-cipher` pod monitored.
- Use a BURNER Google account for the OAuth token (plugin warning).

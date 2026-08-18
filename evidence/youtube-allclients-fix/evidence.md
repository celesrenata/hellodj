# "All clients failed to load the item" — YouTube OAuth empty-token at startup

Date: 2026-08-17 (UTC)
Symptom: `TrackException ... All clients failed to load the item` for
`Node(...) <HybridPlayer object>` on track **"Zara House vol. 5 - Sunset Mix on the
Balcony"**.
Client failures in the exception:
- `Client [TVHTML5] failed: Something went wrong when decoding the track`
- `Client [TVHTML5_SIMPLY] failed: Sign in to confirm you're not a bot`
- `Client [ANDROID_VR] failed: This video requires login`
- `Client [WEB] failed: No supported audio streams available, available types:`
- `Client [WEB_EMBEDDED_PLAYER] failed: Video player configuration error`

## Root cause (verified, not assumed)

Lavalink's youtube-source plugin **initialized OAuth at startup with an empty
refresh token**, because the deployed `youtube-secret` Kubernetes Secret held an
empty `YOUTUBE_OAUTH_REFRESH_TOKEN` (`""`). The plugin's independent
`ce-token-poller` thread then polled Google with that empty/invalid token
forever — Google returned HTTP 400 (`invalid_grant`) on every poll. With no
valid OAuth access token, TV (the ONLY OAuth-capable client) could not fetch the
stream for the age-restricted / login-required track, and the unauthenticated
clients were bot-blocked / login-blocked → "All clients failed to load the item."

### Verified facts

1. **Stored refresh token is valid** — `data/oauth.json`
   `providers.youtube.refresh_token` = `REDACTED_REFRESH_TOKEN`
   (103 chars). Direct Google exchange
   (`POST https://oauth2.googleapis.com/token`, plugin client
   `861556708454-d6dlm3lh05idd8npek18k6be8ba3oc68.apps.googleusercontent.com`
   + secret `SboVhoG9s0rNafixCSGGKXAT`) returned a NEW access token (len 265).
   So the token itself is NOT the problem.

2. **Live `youtube-secret` was empty** — base64 decode of the Secret's
   `YOUTUBE_OAUTH_REFRESH_TOKEN` returned **nothing** (empty value).

3. **Rendered Lavalink config in the pod had `refreshToken: ""`** —
   `grep -A4 "oauth:" /opt/Lavalink/application.yml` showed:
   ```
   oauth:
     enabled: true
     refreshToken: ""
   ```
   The init container (`kube/deployment.yaml`) rendered the empty secret value
   straight into `application.yml`.

4. **Poller HTTP 400 loop** — Lavalink log, repeated every ~5s:
   ```
   [ce-token-poller] d.l.youtube.http.YoutubeOauth2Handler: Failed to fetch OAuth2 token response
   java.lang.RuntimeException: java.io.IOException: Invalid status code for oauth2 token fetch: 400
   ```
   This is Google's `invalid_grant` signature for an invalid/empty refresh token.

5. **The runtime push CANNOT recover the poller** (decisive) — bot pushed the
   valid token via `POST /youtube` (status=204). Lavalink logged:
   ```
   YouTube access token refreshed successfully
   POST /youtube, payload={"refreshToken": "1//06hza...", "skipInitialization": false}
   ```
   …and **1 second later** the `ce-token-poller` failed HTTP 400 again. The
   poller thread (started at init with the empty config token) never switches to
   the pushed token, so pushing at runtime cannot break the loop.

### Ruled out (with evidence)
- **Invalid/expired token** — direct Google exchange succeeds (len 265).
- **Missing OAuth-capable client** — `TV`/`TVHTML5` registered; the
  "OAuth enabled without OAuth-compatible clients" warning is absent.
- **Cipher/signature extraction** — `Using remote cipher server with URL
  "http://yt-cipher.hellodj-service.svc.cluster.local:8001"` active; not the
  cause here (no "Must find sig function" in logs).
- **Plugin version** — 1.18.2 is the latest released on
  `maven.lavalink.dev` (no newer version exists; verified the maven-metadata).

## Fix applied

Write the valid refresh token into the **Secret at config-render time** so
Lavalink initializes OAuth with a valid token at startup — not rely on the
runtime push (which provably cannot recover the broken poller).

1. **`kube/youtube-secret.yaml`** — filled
   `YOUTUBE_OAUTH_REFRESH_TOKEN` with the valid token `1//06hza...` and updated
   the comments to document why the value MUST be present at render time.
2. **Applied to cluster** — `kubectl apply -f kube/youtube-secret.yaml`; live
   Secret now holds a 103-char token.
3. **Rollout restart** — `kubectl rollout restart deploy/hellodj -n
   hellodj-service` so the init container re-renders `application.yml` with the
   token; `kubectl rollout status` → success.

Tradeoff note: the token is now stored in a k8s Secret (source of truth) AND in
`data/oauth.json` (web-ui device flow). Keeping both in sync is the operator's
responsibility; the bot's runtime watchdog remains as a safety net for future
rotations (a valid pushed token after a correct startup is harmless).

## Verification (live, after fix)

- New pod `hellodj-65d84b7b6c-4kxkv` (2/2 Running, 0 restarts).
- Rendered config now: `refreshToken: "REDACTED_REFRESH_TOKEN"`.
- **Poller 400 loop GONE** — `Failed to fetch OAuth2 token response` count = **0**
  since restart (was continuous before).
- **OAuth refresh works at startup** —
  `YouTube access token refreshed successfully` (21:27:31, main thread) and again
  on a request (21:27:37).
- `YouTube source initialised with clients: WEB_REMIX, TVHTML5, TVHTML5_SIMPLY,
  ANDROID_VR, WEB, WEB_EMBEDDED_PLAYER` — all clients + remote cipher active.
- **The previously-failing track now loads** via Lavalink REST:
  - `ytsearch:Zara House vol. 5 - Sunset Mix on the Balcony` → `loadType: search`,
    20 tracks, first = **"Zara House vol. 5 - Sunset Mix on the Balcony"**.
  - Known-good direct video `https://www.youtube.com/watch?v=FATTzbm78cc` →
    `loadType: track`, "Aphex Twin - Window Licker", sourceName youtube.
- **No TrackException / "All clients failed to load the item"** in Lavalink logs
  since restart (count 0).
- Bot healthy: `HelloDJ connected to Lavalink`, `logged in as HelloDJ#8609`,
  `pushed refresh token to Lavalink (status=204)`, watchdog started.

## Verification limitations (honest)

- Headless REST load-tracks verifies the **resolve** path only. It proves the
  previously-failing track is now resolvable to a playable Lavalink track. It
  does NOT prove end-to-end **audio playback** in a Discord voice channel, which
  requires a live user in a guild voice session. That remains a live-bot check.
- The OAuth poller stability was verified over ~15 min post-restart (0 errors);
  Google token refresh is time-bounded, so if the refresh token is ever revoked
  or rotated the operator must update the Secret AND `data/oauth.json`, then
  restart Lavalink — the runtime push alone cannot recover a broken poller.

## Caveats
- The `yt-cipher` emptyDir cache is lost on pod reschedule (re-fetched on demand).
- If a future YouTube change breaks IAS cipher, revisit `OVERRIDE_PLAYER_VARIANT`.
- Use a BURNER Google account for the OAuth token (plugin warning).

---

# 2026-08-18 — Residual AllClientsFailedException despite valid OAuth → poToken support

## Symptom (live, 2026-08-18 02:56:38)
TrackException for "Wake Pig" (guild 1501686893765595296), `(yts.version: 1.18.2) All clients failed to load the item`:
- `Client [TVHTML5] failed: The page needs to be reloaded.`
- `Client [TVHTML5_SIMPLY] failed: Sign in to confirm you're not a bot`
- `Client [ANDROID_VR] failed: This video requires login.`
- `Client [WEB] failed: No supported audio streams available, available types:`
- `Client [WEB_EMBEDDED_PLAYER] failed: Video player configuration error`

Notably the OAuth watchdog is healthy: `youtube-oauth: pushed refresh token to Lavalink ... (status=204)` every minute.

## Root cause (verified against youtube-source plugin docs)
Prior fixes (cipher + oauth-at-startup) resolved the *signature* and *token-presence* paths. But the residual failures are **YouTube bot-detection**, which the OAuth token alone does not fully defeat:
- **TV / TVHTML5 is the ONLY OAuth-capable client.** Per the plugin's client table, TV is the only one with `OAuth: Yes`. OAuth benefits TV only.
- The failing clients **TVHTML5_SIMPLY, ANDROID_VR, WEB, WEBEMBEDDED are all non-OAuth** — they are bot-blocked ("Sign in to confirm you're not a bot" / "The page needs to be reloaded" / "No supported audio streams available").
- The plugin docs' **documented remedy for bot-detection is a poToken (Proof of Origin)**, applied to the WEB-family clients. It complements OAuth (TV). There is **no `oauth.poToken: true` option** — poToken is a separate `pot:` config / `POST /youtube` body field (`poToken` + `visitorData`), generated externally via `iv-org/youtube-trusted-session-generator`. Per docs: "You do not need to use poToken with OAuth, and vice versa" (mutually exclusive per-request; TV keeps OAuth, WEB-family uses poToken).

## Fix applied (2026-08-18)
Added poToken **support infrastructure** so the deployment can supply a token (blank values are inert):

1. **`bot/lavalink/application.yml`** — added `plugins.youtube.pot` block (lines 55-66):
   ```yaml
   pot:
     token: "${POT_TOKEN:-}"
     visitorData: "${POT_VISITOR_DATA:-}"
   ```
   Documented that poToken complements OAuth and how to generate it.

2. **`bot/bot.py`** —
   - Added env reads `POT_TOKEN` / `POT_VISITOR_DATA` (lines 80-88).
   - Added `push_youtube_pot()` (lines 205-252): POSTs `{"refreshToken": "x", "skipInitialization": false, "poToken": ..., "visitorData": ...}` to `{LAVALINK_URI}/youtube`; `refreshToken: "x"` leaves OAuth untouched (poToken-only update per plugin API). Blank values → no-op.
   - Wired into `connect_lavalink()` after `push_youtube_oauth()` (line 159).
   - Watchdog `_youtube_oauth_watchdog()` now also re-pushes the poToken each tick (line 272).

3. **`kube/bot-configmap.yaml`** — added `POT_TOKEN: ""` and `POT_VISITOR_DATA: ""` (documented, blank-safe).
4. **`docker-compose.yml`** — added `POT_TOKEN`/`POT_VISITOR_DATA` env passthrough to the lavalink service.

## Verification
- `python -m py_compile bot.py` → PY_COMPILE_OK.
- YAML structure validated by inspection: `pot:` block correctly nested under `plugins.youtube` at the same indent as `oauth:`/`remoteCipher:`.

## How to verify in production
1. Generate a fresh poToken+visitorData via `iv-org/youtube-trusted-session-generator`.
2. Set `POT_TOKEN` / `POT_VISITOR_DATA` in `kube/bot-configmap.yaml`, apply, and `kubectl rollout restart deploy/hellodj -n hellodj-service`.
3. Watch bot log for `youtube-pot: pushed poToken to Lavalink ... (status=204)` and confirm the previously-failing track loads (no "All clients failed to load the item").
4. NOTE: poToken is not a silver bullet and is time/account-bound; regenerate periodically. The runtime push + watchdog keep it applied without restarts.

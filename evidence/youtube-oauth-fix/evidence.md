# YouTube OAuth "requires login" — Diagnosis & Fix

Date: 2026-08-16 (UTC)
Symptom: `TrackException ... All clients failed to load the item` —
`Client [ANDROID_VR] failed: This video requires login`,
`Client [WEB] failed: This video requires login` / `No supported audio streams available`,
`Client [WEB_EMBEDDED_PLAYER] failed: Video player configuration error`.
Severity: suspicious. Guild 1501686893765595296, track "Aphex Twin - Windowlicker".

## Root cause (two distinct defects)

### Defect 1 — No OAuth-capable client registered (the actual playback failure)
The OAuth refresh token was **valid** and **reaching** the plugin, but the plugin
never applied it because no registered client could use OAuth.

Evidence (live pod `hellodj-57cc7f9fc9-zj62m`, before fix):
- Token present & valid format: `data/oauth.json` → `providers.youtube.refresh_token`,
  103 chars, prefix `1//06jfq…` (valid Google OAuth refresh-token format).
- Token valid (Google accepts it): Lavalink log `YouTube access token refreshed successfully`.
- Token reaching plugin: Lavalink log `POST /youtube, payload={"refreshToken": "1//06jfq…"}` → 204.
- **Smoking gun**: Lavalink log, repeated on every push:
  `WARN d.l.youtube.YoutubeAudioSourceManager: OAuth has been enabled without registering any OAuth-compatible clients.`
- Official youtube-source client table: only `TV` is `OAuth: Yes`;
  MUSIC/WEB/ANDROID_VR/WEBEMBEDDED are all `OAuth: No`.
- Config registered only MUSIC/ANDROID_VR/WEB/WEBEMBEDDED → no OAuth-capable client.

### Defect 2 — Deployed web-ui image was stale (device-flow button 404)
The "Start YouTube OAuth" button + device-flow endpoints existed in local source
(`web-ui/app.py:788`, `web-ui/templates/config.html:148`) but the running image
(`registry.celestium.life/hellodj/web-ui:latest`, digest `73c975b0…`) predated them:
- Running pod `/app/app.py` had **0** matches for `api/youtube/device`.
- `curl https://hellodj.celestium.life/api/youtube/device` → **404**.

## Fix applied
1. Added `TV` to the `clients` list in `kube/configmap.yaml` and
   `bot/lavalink/application.yml` (TV is the only OAuth-capable client).
2. Rebuilt + pushed the web-ui image from current source
   (`registry.celestium.life/hellodj/web-ui:webui-ytoauth-20260816` and `:latest`,
   digest `ecf2ba24…`), which contains the device-flow endpoints + button.
3. `kubectl apply -f kube/configmap.yaml`; `rollout restart` of `hellodj-web-ui`
   and `hellodj` (bot+lavalink sidecar re-renders config with TV).

## Verification (after fix)
- New web-ui pod digest `ecf2ba24…`; `/app/app.py` has the device route (2 matches).
- `POST https://hellodj.celestium.life/api/youtube/device` → **200** with a real
  Google device code + one-click authorize link
  (`https://www.google.com/device?user_code=JJX-WSW-FYCR`).
- Lavalink startup: `YouTube source initialised with clients: WEB_REMIX, TVHTML5,
  ANDROID_VR, WEB, WEB_EMBEDDED_PLAYER` (TV registered as TVHTML5).
- The `OAuth has been enabled without registering any OAuth-compatible clients`
  warning is **gone** (previously on every push).
- **Definitive**: the previously-failing track now loads via Lavalink REST:
  `GET /v4/loadtracks?identifier=https://www.youtube.com/watch?v=FATTzbm78cc`
  → `loadType: track`, title "Aphex Twin - Window Licker", author "Evz™",
  duration 368000 (was `loadType: error` / "requires login" before).
- Bot healthy: `HelloDJ connected to Lavalink`, `logged in as HelloDJ#8609`,
  `pushed refresh token to Lavalink (status=204)`, watchdog started.

## Notes / caveats
- `TV` is `Metadata Support: None` per the official table. Track loading still
  returns title/author/duration (verified above), so "now playing" metadata is
  unaffected for this track. If a future track shows missing metadata, add a
  `clientOptions` block so a metadata-capable client handles loading while TV
  handles authenticated playback.
- The plugin also logged a startup device-code prompt
  (`go to https://www.google.com/device and enter code ZYQ-RWX-WCGV`) because
  `oauth.enabled: true` with an empty `refreshToken` in the rendered config
  triggers the interactive flow once; the bot's runtime push (204) then supplies
  the real token. Harmless, but can be silenced by setting
  `skipInitialization: true` if the token is always supplied at runtime.
- Use a BURNER Google account for the OAuth token (plugin warning: "DO NOT
  AUTHORISE WITH YOUR MAIN ACCOUNT").

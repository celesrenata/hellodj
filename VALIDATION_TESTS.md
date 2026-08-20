# HelloDJ — Validation Test Checklist

## Purpose

This document is the single source of truth for manually validating that every
implemented HelloDJ feature (Discord bot commands + Admin Web Panel) behaves
correctly in a live environment. Each row records the exact command/action, the
expected observable result, and a pass/fail checkbox. Known limitations and
environment-specific notes are captured inline so validation stays honest.

## Prerequisites

Before starting any validation run, confirm:

- [ ] The Discord bot is online in at least one test guild (`/ping` responds).
- [ ] The bot has **Manage Messages** / **Connect** / **Speak** permissions in the voice channel used for audio tests.
- [ ] A user with Administrator or `Manage Guild` permission is available for permission-gated commands (`/restrict`, `/allow`, `/restrict_mode`, `/revoke`, `/blacklist`).
- [ ] The Admin Panel is reachable at https://hellodj.celestium.life and accepts Discord OAuth login.
- [ ] Lavalink is connected (check `bot/lavalink/application.yml` and the youtube-plugin jar are loaded).
- [ ] A YouTube URL and a direct audio file are available for the `/play` variants.

> **Validation rule:** a test is **PASS** only when the observed result matches
> the expected result. If a known limitation makes the expected result
> impossible in this environment, mark the checkbox `- [ ]` FAILED and record
> the limitation in the Notes column — do not silently mark it pass.

---

## 1. Discord Commands — Playback Core

### `/play` (query / link / playlist)

| # | Command / Action | Expected Result | Pass/Fail | Notes |
|---|------------------|-----------------|-----------|-------|
| 1 | `/play <song name>` | Resolves query → joins voice → queues and starts playing track | - [ ] | Uses cloud STT/query resolution; requires network |
| 2 | `/play <youtube url>` | Plays the linked track; title shown in queue | - [ ] | |
| 3 | `/play <playlist url>` | All tracks queued in order; playback starts | - [ ] | Playlist fetch may be slow for 100+ tracks |

### `/album`

| # | Command / Action | Expected Result | Pass/Fail | Notes |
|---|------------------|-----------------|-----------|-------|
| 4 | `/album <url>` | URL loads directly, queues all tracks from the album | - [ ] | Spotify/Tidal/YouTube album URLs |
| 4a | `/album <search query>` | Shows dropdown with albums (artist, year, track count, duration), selection queues all tracks | - [ ] | Searches across configured provider + fallbacks |

### `/remove`

| # | Command / Action | Expected Result | Pass/Fail | Notes |
|---|------------------|-----------------|-----------|-------|
| 5 | `/remove <index>` | Removes the track at that queue position; queue re-indexed | - [ ] | |
| 6 | `/remove all` | Clears queue (except current track) | - [ ] | |

### `/queue`

| # | Command / Action | Expected Result | Pass/Fail | Notes |
|---|------------------|-----------------|-----------|-------|
| 7 | `/queue` | Shows embed with up to N tracks, position, duration, requester | - [ ] | Long queues paginated |

### `/skip`

| # | Command / Action | Expected Result | Pass/Fail | Notes |
|---|------------------|-----------------|-----------|-------|
| 8 | `/skip` | Skips current track, advances to next in queue | - [ ] | |

### `/pause` / `/resume`

| # | Command / Action | Expected Result | Pass/Fail | Notes |
|---|------------------|-----------------|-----------|-------|
| 9 | `/pause` | Playback paused; button state reflects paused | - [ ] | |
| 10 | `/resume` | Playback resumes from pause | - [ ] | |

### `/stop`

| # | Command / Action | Expected Result | Pass/Fail | Notes |
|---|------------------|-----------------|-----------|-------|
| 11 | `/stop` | Stops playback, clears queue, leaves voice after timeout | - [ ] | |

### `/clear`

| # | Command / Action | Expected Result | Pass/Fail | Notes |
|---|------------------|-----------------|-----------|-------|
| 12 | `/clear` | Removes all queued tracks; current track continues or stops | - [ ] | |

### `/shuffle`

| # | Command / Action | Expected Result | Pass/Fail | Notes |
|---|------------------|-----------------|-----------|-------|
| 13 | `/shuffle` | Queue order randomized; current track unaffected | - [ ] | |

### `/repeat`

| # | Command / Action | Expected Result | Pass/Fail | Notes |
|---|------------------|-----------------|-----------|-------|
| 14 | `/repeat off/one/all` | Loop mode set (off / single track / whole queue) | - [ ] | |

### `/move`

| # | Command / Action | Expected Result | Pass/Fail | Notes |
|---|------------------|-----------------|-----------|-------|
| 15 | `/move <from> <to>` | Track moved between queue positions; order updated | - [ ] | |

---

## 2. Discord Commands — Voice Session Control

### `/fuckoff` + `/l` + `/disconnect`

| # | Command / Action | Expected Result | Pass/Fail | Notes |
|---|------------------|-----------------|-----------|-------|
| 16 | `/fuckoff` | Bot immediately leaves voice + clears session | - [ ] | Alias behavior |
| 17 | `/l` | Same as `/fuckoff` (shorthand alias) | - [ ] | Alias must be registered |
| 18 | `/disconnect` | Bot disconnects from voice channel | - [ ] | |

### `/leave`

| # | Command / Action | Expected Result | Pass/Fail | Notes |
|---|------------------|-----------------|-----------|-------|
| 19 | `/leave` | Leaves voice; keeps or clears queue per config | - [ ] | |

### `/samples`

| # | Command / Action | Expected Result | Pass/Fail | Notes |
|---|------------------|-----------------|-----------|-------|
| 20 | `/samples` | Lists available sample/sound packs (from data/sounds) | - [ ] | |

---

## 3. Discord Commands — Chime Group

### `/chime set|import|list|test|volume|reset`

| # | Command / Action | Expected Result | Pass/Fail | Notes |
|---|------------------|-----------------|-----------|-------|
| 21 | `/chime set <name>` | Sets the active chime sound | - [ ] | |
| 22 | `/chime import <url>` | Imports a chime from URL/upload into sound bank | - [ ] | |
| 23 | `/chime list` | Shows all available chimes | - [ ] | |
| 24 | `/chime test` | Plays the current chime in voice to confirm | - [ ] | |
| 25 | `/chime volume <0-100>` | Adjusts chime volume | - [ ] | |
| 26 | `/chime reset` | Restores default chime | - [ ] | |

---

## 4. Discord Commands — Save / Grab

### `/save` + `/grab`

| # | Command / Action | Expected Result | Pass/Fail | Notes |
|---|------------------|-----------------|-----------|-------|
| 27 | `/save` (or `/grab`) | Saves the current track to the requester's saved songs | - [ ] | Requires per-user save store (storage.py) |

---

## 5. Discord Commands — Attribution

### `/whosat` + `/whosthis`

| # | Command / Action | Expected Result | Pass/Fail | Notes |
|---|------------------|-----------------|-----------|-------|
| 28 | `/whosat` | Reports which user requested the current track | - [ ] | |
| 29 | `/whosthis <track>` | Resolves artist/song info via whosampled | - [ ] | Uses whosampled.py; may be slow |

---

## 6. Discord Commands — Crossfade & Sleep

### `/crossfade`

| # | Command / Action | Expected Result | Pass/Fail | Notes |
|---|------------------|-----------------|-----------|-------|
| 30 | `/crossfade on/off` or `<seconds>` | Enables/disables crossfade between tracks | - [ ] | |

### `/sleep`

| # | Command / Action | Expected Result | Pass/Fail | Notes |
|---|------------------|-----------------|-----------|-------|
| 31 | `/sleep <minutes>` | Schedules auto-stop after N minutes | - [ ] | Uses sleep_settings.py |
| 32 | `/sleep off` | Cancels scheduled sleep timer | - [ ] | |

---

## 7. Discord Commands — Remote Control Panel

### `/remote` (button panel)

| # | Command / Action | Expected Result | Pass/Fail | Notes |
|---|------------------|-----------------|-----------|-------|
| 33 | `/remote` | Posts an interactive message with play/pause/skip/stop/volume buttons | - [ ] | Buttons must invoke matching handlers |
| 34 | Click each button | Button triggers the expected playback action | - [ ] | |

---

## 8. Discord Commands — Filters

### `/filter bassboost|nightcore|8d|vaporwave|8bit|equalizer|stems isolate|808|test`

| # | Command / Action | Expected Result | Pass/Fail | Notes |
|---|------------------|-----------------|-----------|-------|
| 35 | `/filter bassboost on/off` | Bass boost applied via Lavalink filters | - [ ] | |
| 36 | `/filter nightcore on/off` | Pitch/speed shift applied | - [ ] | |
| 37 | `/filter 8d on/off` | 8D panning effect applied | - [ ] | |
| 38 | `/filter vaporwave on/off` | Slowed/pitched-down effect | - [ ] | |
| 39 | `/filter 8bit on/off` | Chip-tune effect | - [ ] | |
| 40 | `/filter equalizer <bands>` | Custom EQ bands applied | - [ ] | |
| 41 | `/filter stems isolate <vocals|drums|...>` | Stems separation (needs requirements-stems.txt models) | - [ ] | Requires ONNX/TFLite stem model + torch |
| 42 | `/filter 808 on/off` | 808 cowbell/sample filter (uses data/sounds/original-808-cowbell.mp3) | - [ ] | |
| 43 | `/filter test` | Applies/cycles test filter to confirm pipeline | - [ ] | |

### `/filter reset`

| # | Command / Action | Expected Result | Pass/Fail | Notes |
|---|------------------|-----------------|-----------|-------|
| 44 | `/filter reset` | Clears all active filters, returns to normal playback | - [ ] | |

### Now-Playing Filter Dropdown

| # | Command / Action | Expected Result | Pass/Fail | Notes |
|---|------------------|-----------------|-----------|-------|
| 44a | Select "Bass Boost" from NP dropdown | Bass boost applied, ephemeral confirmation | - [ ] | |
| 44b | Select "Nightcore" from NP dropdown | Nightcore applied | - [ ] | |
| 44c | Select "8D Audio" from NP dropdown | 8D rotation applied | - [ ] | |
| 44d | Select "Vaporwave" from NP dropdown | Vaporwave effect applied | - [ ] | |
| 44e | Select "Tune (Enhanced)" from NP dropdown | Tune toggled on/off | - [ ] | |
| 44f | Select "Equalizer" from NP dropdown | Opens interactive 10-band EQ (ephemeral) | - [ ] | |
| 44g | Select "Reset Filters" from NP dropdown | All filters cleared | - [ ] | |

### Equalizer View

| # | Command / Action | Expected Result | Pass/Fail | Notes |
|---|------------------|-----------------|-----------|-------|
| 44h | EQ preset dropdown (Bass Boost, V-Shape, etc.) | Preset gains applied, display updates | - [ ] | |
| 44i | ◀/▶ band navigation buttons | Selected band changes, indicator moves | - [ ] | |
| 44j | ▲/▼ gain adjustment buttons | Selected band gain changes, bar updates | - [ ] | |
| 44k | EQ display alignment | Bars, dots, and labels align visually in Discord | - [ ] | |

---

## 9. Discord Commands — Guild Restriction / Permissions

### `/restrict`, `/allow`, `/restrict_mode`, `/revoke`, `/blacklist`

> Naming parity: `/restrict_mode` keeps its underscore name (intentional
> exception). Converting to `/restrict mode` would collide with the existing
> `/restrict` command, so the underscore form is retained as a low-risk,
> non-breaking scheme.

| # | Command / Action | Expected Result | Pass/Fail | Notes |
|---|------------------|-----------------|-----------|-------|
| 45 | `/restrict <channel|role|user>` | Restricts bot usage to the given scope | - [ ] | Requires Manage Guild |
| 46 | `/allow <channel|role|user>` | Allows usage for the given scope | - [ ] | |
| 47 | `/restrict_mode <restrictive|allow_all>` | Sets the guild restriction policy mode | - [ ] | Cross-cutting with allowlist/blacklist |
| 48 | `/revoke <user>` | Removes a granted allow/restrict grant | - [ ] | |
| 49 | `/blacklist add|remove <term>` | Adds/removes a blocked search term | - [ ] | Uses blacklist.py |

---

## 10. Discord Commands — Metrics / Info

### `/metrics`, `/stream`, `/source`, `/voice enable|disable`, `/voice_status`

> Naming parity: `/voice_status` keeps its underscore name (intentional
> exception). Converting to `/voice status` would break the existing
> `/voice enable|disable` toggle command, so the underscore form is retained
> as a low-risk, non-breaking scheme.

| # | Command / Action | Expected Result | Pass/Fail | Notes |
|---|------------------|-----------------|-----------|-------|
| 50 | `/metrics` | Returns bot metrics (tracks played, sessions, uptime) | - [ ] | Uses metrics.py |
| 51 | `/stream <url>` | Queues a live stream URL | - [ ] | Uses cogs/stream.py |
| 52 | `/source` | Reports the active provider for the current track (YouTube/Tidal/Spotify/etc.) | - [ ] | |
| 53 | `/voice enable` | Enables voice activation (wakeword + STT) | - [ ] | Needs wakeword.py + cloud STT |
| 54 | `/voice disable` | Disables voice activation | - [ ] | |
| 55 | `/voice_status` | Shows current voice-activation state and engine | - [ ] | |

---

## 11. Admin Panel (https://hellodj.celestium.life)

### Login via Discord OAuth

| # | Command / Action | Expected Result | Pass/Fail | Notes |
|---|------------------|-----------------|-----------|-------|
| 56 | Open site, click Login | Redirects to Discord OAuth, then back with session cookie | - [ ] | OAuth flow must round-trip |
| 57 | Unauthenticated access to /config etc. | Redirected to login / blocked by auth guard | - [ ] | oauth-verify evidence: unauthenticated state |

### `/config`

| # | Command / Action | Expected Result | Pass/Fail | Notes |
|---|------------------|-----------------|-----------|-------|
| 58 | Load /config | Shows bot + guild configuration form | - [ ] | |
| 59 | Edit + Save /config | Config persisted; reload reflects changes | - [ ] | |

### `/guilds`

| # | Command / Action | Expected Result | Pass/Fail | Notes |
|---|------------------|-----------------|-----------|-------|
| 60 | Load /guilds | Lists guilds with restriction/allow settings | - [ ] | |

### `/playlists`

| # | Command / Action | Expected Result | Pass/Fail | Notes |
|---|------------------|-----------------|-----------|-------|
| 61 | Load /playlists | Shows saved playlists and their tracks | - [ ] | |

### `/backups`

| # | Command / Action | Expected Result | Pass/Fail | Notes |
|---|------------------|-----------------|-----------|-------|
| 62 | Load /backups | Lists backup snapshots (config + playlists + settings) | - [ ] | |
| 63 | Create backup | New backup appears in list with timestamp | - [ ] | |
| 64 | Restore backup | Restore modal opens; confirming restores state | - [ ] | |

### `/blacklist`

| # | Command / Action | Expected Result | Pass/Fail | Notes |
|---|------------------|-----------------|-----------|-------|
| 65 | Load /blacklist | Shows blocked terms list | - [ ] | |
| 66 | Add/remove term | List updates in panel | - [ ] | |

### `/admins`

| # | Command / Action | Expected Result | Pass/Fail | Notes |
|---|------------------|-----------------|-----------|-------|
| 67 | Load /admins | Shows admin/allowlist user list | - [ ] | |

### `/logs`

| # | Command / Action | Expected Result | Pass/Fail | Notes |
|---|------------------|-----------------|-----------|-------|
| 68 | Load /logs | Shows recent bot/panel logs with level filter | - [ ] | Reads config/webui.log |

### `/metrics` (NEW dashboard)

| # | Command / Action | Expected Result | Pass/Fail | Notes |
|---|------------------|-----------------|-----------|-------|
| 69 | Load /metrics | New dashboard renders bot usage metrics (tracks, sessions, top songs) | - [ ] | NEW feature — verify dashboard not blank |
| 70 | Metrics refresh | Numbers update after new playback activity | - [ ] | |

### `/api/status`

| # | Command / Action | Expected Result | Pass/Fail | Notes |
|---|------------------|-----------------|-----------|-------|
| 71 | GET /api/status | Returns JSON: bot online, lavalink status, uptime | - [ ] | Verify 200 + valid JSON |

---

## 12. Cross-Cutting Features

### File Upload (audio/video)

| # | Command / Action | Expected Result | Pass/Fail | Notes |
|---|------------------|-----------------|-----------|-------|
| 72 | Upload an audio file (mp3/wav/ogg) | File accepted, queued, and played | - [ ] | file_handler.py |
| 73 | Upload a video file (mp4) | Video file accepted and audio extracted/played | - [ ] | |
| 74 | Upload invalid/oversized file | Rejected with clear error | - [ ] | |

### Guild Restriction Policy

| # | Command / Action | Expected Result | Pass/Fail | Notes |
|---|------------------|-----------------|-----------|-------|
| 75 | Set restrictive mode | Only allowed users/channels/roles can use bot | - [ ] | guild_policy.py |
| 76 | Set allow_all mode | Everyone allowed unless explicitly blacklisted | - [ ] | |

### Allowlist / Blacklist in restrictive & allow_all modes

| # | Command / Action | Expected Result | Pass/Fail | Notes |
|---|------------------|-----------------|-----------|-------|
| 77 | Allowlist user in restrictive mode | Allowed user can use commands | - [ ] | allowlist.py |
| 78 | Blacklisted term in allow_all mode | Term blocked despite open policy | - [ ] | blacklist.py |
| 79 | Non-allowlisted user in restrictive mode | Command denied with message | - [ ] | |

### YouTube Age-Restricted Playback Fix

| # | Command / Action | Expected Result | Pass/Fail | Notes |
|---|------------------|-----------------|-----------|-------|
| 80 | Play an age-restricted YouTube video | Playback succeeds (oauth/cipher client handles it) | - [ ] | youtube-oauth-fix / youtube-allclients-fix evidence |

### Tidal → YouTube Fallback

| # | Command / Action | Expected Result | Pass/Fail | Notes |
|---|------------------|-----------------|-----------|-------|
| 81 | Request track unavailable on Tidal | Bot falls back to YouTube and plays | - [ ] | tidal.py fallback |

### Tidal v2 Direct Streaming

| # | Command / Action | Expected Result | Pass/Fail | Notes |
|---|------------------|-----------------|-----------|-------|
| 81a | Play Tidal track with source=tidal | Direct stream resolves via tidal-stream sidecar (fMP4 HLS) | - [ ] | stream_resolver.py → localhost:8801 |
| 81b | Now-playing embed shows correct title | Entry metadata used, not "Unknown title" from HLS track | - [ ] | |
| 81c | Now-playing embed shows correct artist | Entry metadata used, not "Unknown artist" | - [ ] | |
| 81d | Now-playing embed shows correct duration | Entry duration (e.g. 3:14), not Long.MAX_VALUE | - [ ] | _DURATION_MAX_MS guard |
| 81e | Progress bar updates during Tidal playback | Position advances, bar fills correctly | - [ ] | Uses entry duration for total |
| 81f | Tidal search returns artist + duration | LavasRC v2 enriches search with individual track fetches | - [ ] | |

### Cloud STT / TTS Engines

| # | Command / Action | Expected Result | Pass/Fail | Notes |
|---|------------------|-----------------|-----------|-------|
| 82 | Speak a command with voice enabled | STT transcribes via cloud engine and executes command | - [ ] | stt.py + tts.py cloud engines |
| 83 | Trigger a TTS reply | Bot replies in voice via cloud TTS | - [ ] | |

---

## Summary Checklist

### Discord Commands

- [ ] `/play` (query / link / playlist)
- [ ] `/album`
- [ ] `/remove`
- [ ] `/queue`
- [ ] `/skip`
- [ ] `/pause` / `/resume`
- [ ] `/stop`
- [ ] `/clear`
- [ ] `/shuffle`
- [ ] `/repeat`
- [ ] `/move`
- [ ] `/fuckoff` + `/l` + `/disconnect`
- [ ] `/leave`
- [ ] `/samples`
- [ ] `/chime` (set / import / list / test / volume / reset)
- [ ] `/save` + `/grab`
- [ ] `/whosat` + `/whosthis`
- [ ] `/crossfade`
- [ ] `/sleep`
- [ ] `/remote` (button panel)
- [ ] `/filter` (bassboost / nightcore / 8d / vaporwave / 8bit / equalizer / stems isolate / 808 / test)
- [ ] `/filter reset`
- [ ] `/restrict`
- [ ] `/allow`
- [ ] `/restrict_mode`
- [ ] `/revoke`
- [ ] `/blacklist`
- [ ] `/metrics`
- [ ] `/stream`
- [ ] `/source`
- [ ] `/voice enable|disable`
- [ ] `/voice_status`

### Admin Panel

- [ ] Discord OAuth login
- [ ] `/config`
- [ ] `/guilds`
- [ ] `/playlists`
- [ ] `/backups`
- [ ] `/blacklist`
- [ ] `/admins`
- [ ] `/logs`
- [ ] `/metrics` (NEW dashboard)
- [ ] `/api/status`

### Cross-Cutting

- [ ] File upload (audio/video)
- [ ] Guild restriction policy
- [ ] YouTube age-restricted playback fix
- [ ] Tidal → YouTube fallback
- [ ] Tidal v2 direct streaming (fMP4 HLS via sidecar)
- [ ] Now-playing display (correct title/artist/duration for HLS streams)
- [ ] Now-playing filter/EQ dropdown
- [ ] Equalizer view (10-band, presets, display alignment)
- [ ] Album selection dropdown (artist, year, track count, duration)
- [ ] Cloud STT/TTS engines
- [ ] Allowlist/blacklist in restrictive & allow_all modes

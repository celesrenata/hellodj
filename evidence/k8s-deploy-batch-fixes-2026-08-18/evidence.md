# K8s Deployment — Batch Fixes (remote panel, /help, /radio, 8bit/tune, blacklist) 2026-08-18

## Scope
- Service changed: **bot** only
- web-ui: NOT modified, NOT redeployed (kept at `v2026-08-17`)
- Changes deployed: unified remote panel (⏮⏯⏭🚫), track blacklist (`bot/blacklist.py` → `data/track_blacklist.json`), `/tune` re-apply + TUNE_GAINS, `/remote` + `/link`, paginated `/help` (25/page, ⬅️➡️), `/radio` group (city/direct/preset), 8bit arcade redesign, UA-header 403 retry + 808 cowbell fallback, `cogs.help` + `cogs.radio` wired in `bot/bot.py`

## Image Tag
- `registry.celestium.life/hellodj/bot:batch-fixes-2026-08-18`
- Build: SUCCESS (`docker build`, exit 0) — local digest `sha256:384d3bb841d4cd74778ad43bcc54e8e680f0f12b15f1633b8b36ec70ca5c624d`
- Push: SUCCESS — registry digest `sha256:8d09f18b211452ab0ccbe0a3b2a2a859f8833401d36fddc021b1c8d13b084d16`

> NOTE: the tag string `batch-fixes-2026-08-18` was already wired into `kube/deployment.yaml` (bot image line) and `kube/kustomization.yaml` (newTag) from the prior batch deploy. Because the image reference is identical, `kubectl apply` alone did **not** trigger a rollout (the running pod still held the previous digest under the same tag). A `rollout restart` was required to force a re-pull of the newly built image.

## kubectl Commands
1. `docker build -t registry.celestium.life/hellodj/bot:batch-fixes-2026-08-18 bot/` → SUCCESS (exit 0)
2. `docker push registry.celestium.life/hellodj/bot:batch-fixes-2026-08-18` → SUCCESS, digest `sha256:8d09f18...`
3. `kubectl apply -f kube/deployment.yaml -n hellodj-service` → `deployment.apps/hellodj configured`
4. `kubectl rollout status deployment/hellodj -n hellodj-service --timeout=300s` → `deployment "hellodj" successfully rolled out`
5. `kubectl rollout restart deployment/hellodj -n hellodj-service` → `deployment.apps/hellodj restarted` (required: image ref unchanged, prior digest running)
6. `kubectl rollout status deployment/hellodj -n hellodj-service --timeout=300s` → `deployment "hellodj" successfully rolled out`

Note: deployment name is `hellodj` (not `bot`), per prior evidence. Manifest-based apply + rollout restart was the correct mechanism since the tag is hardcoded in `kube/deployment.yaml`.

## Pod Status
- `hellodj-7657cbb54f-xs7dj` — 2/2 Running, 0 restarts
- Deployed image verified via imageID: `registry.celestium.life/hellodj/bot@sha256:8d09f18b211452ab0ccbe0a3b2a2a859f8833401d36fddc021b1c8d13b084d16` (new digest, NOT the prior `a60994f2...`)
- `hellodj-web-ui-6d6b598bf4-nkh6g` 1/1 Running (untouched), `yt-cipher-56579858bc-ntg6b` 1/1 Running

## Services
- `hellodj` ClusterIP 10.43.55.20:2333
- `hellodj-web-ui` ClusterIP 10.43.246.248:8080
- `yt-cipher` ClusterIP 10.43.67.141:8001

## Bot Logs (container: bot) — cog load verification
Startup clean, no `ExtensionNotFound` / `Traceback` / import errors. `cogs.help` + `cogs.radio` verified loaded because `bot/bot.py` `setup_hook` awaits `load_extension("cogs.help")` (line 313) and `load_extension("cogs.radio")` (line 314) **before** `bot.tree.sync()` (line 325); the log shows `HelloDJ slash commands synced.` firing, which proves both extensions loaded — any load failure would abort before sync. Grep across the full bot log found **zero** `ExtensionNotFound`, `Traceback`, `ModuleNotFound`, or import/error lines.

```
2026-08-18 13:27:26 INFO blacklist: HelloDJ: data/blacklist.json not found — starting with empty blacklist.
2026-08-18 13:27:44 INFO __main__: Lavalink is reachable at http://hellodj.hellodj-service.svc.cluster.local:2333/v4/session (attempt 10)
2026-08-18 13:27:44 INFO __main__: HelloDJ connected to Lavalink at http://hellodj.hellodj-service.svc.cluster.local:2333
2026-08-18 13:27:44 INFO __main__: youtube-oauth: pushed refresh token to Lavalink .../youtube (status=204)
2026-08-18 13:27:44 INFO __main__: youtube-pot: no poToken configured — skipping push to Lavalink
2026-08-18 13:27:44 INFO cogs.voice: VOICE_ENABLED=true — voice activation auto-enabled for all guilds
2026-08-18 13:27:45 INFO cogs.voice: Voice orchestrator initialized (wakeword=True, tts=True, query=False)
2026-08-18 13:27:45 INFO cogs.voice: Voice tick loop started (every 80ms)
2026-08-18 13:27:45 INFO cogs.voice: Voice activation cog loaded
2026-08-18 13:27:45 INFO __main__: HelloDJ slash commands synced.
2026-08-18 13:27:46 INFO discord.gateway: Shard ID None has connected to Gateway ...
2026-08-18 13:27:46 INFO __main__: gateway health watchdog started (on_connect)
2026-08-18 13:27:48 INFO __main__: HelloDJ logged in as HelloDJ#8609 (1534778518137995325)
2026-08-18 13:27:48 INFO __main__: on_ready fired with 2 guilds
2026-08-18 13:27:48 INFO __main__: guild_policy: periodic re-check watchdog started
```

Track blacklist module initialized via `_blacklist.load_track_blacklist()` in `setup_hook` (line 292); `data/track_blacklist.json` is a persisted file, absent on first boot → empty blacklist, no error.

## Issues / Caveats
- The identical tag string caused `kubectl apply` to no-op the rollout; `rollout restart` was required to re-pull the new digest. Future bot-only deploys under a re-used tag should always `rollout restart` after `apply`.
- No ImagePullBackOff, no startup errors, no command sync failures.

---

# Follow-up redeploy 2026-08-18 — save_guild(tune_enabled) TypeError fix

## Bug (production logs)
```
TypeError: save_guild() got an unexpected keyword argument 'tune_enabled'
File "/app/player.py", line 520, in persist
    session.save_guild(guild_id, auto_resume=True, **_snapshot(state))
File "/app/player.py", line 663, in add_track
    persist(guild_id)
File "/app/cogs/music.py", line 850, in on_pick
    await player.add_track(state, interaction.guild.id, info)
```

## Root cause
`bot/player.py:_snapshot()` (used by `persist()` at line 520) returns `tune_enabled`
in its dict — added at `bot/player.py:130` by the earlier `/tune` implementation —
but `bot/session.py:save_guild()` did NOT accept a `tune_enabled` keyword arg. So
every `/play` → `add_track` → `persist` → `session.save_guild(**_snapshot(state))`
crashed with `TypeError`.

## Fix (option a — keep state round-trip)
`bot/session.py:save_guild()` gained a `tune_enabled: bool = False` parameter (line 77)
and persists it into the saved guild dict (line 97). This keeps the `/tune` "light
switch" surviving restarts, matching how `filters`/`crossfade_seconds` are persisted.
No change needed to `bot/player.py` — `_snapshot()` already supplies the field.

## Verification
- `python -m py_compile bot/player.py bot/session.py` → **exit 0**
- Regression harness `/tmp/tune_persist_harness.py` (admin-venv) → **7/7 PASS**:
  - `_snapshot` returns `tune_enabled`
  - `save_guild(**_snapshot(state))` no longer raises `TypeError`
  - `player.persist()` runs without `TypeError`
  - `tune_enabled=True` round-trips into persisted guild state
  - `crossfade_seconds`/`filters` still round-trip
  - `tune_enabled` survives a disk reload (restart survival)
- Command-registration harness `/tmp/hellodj_batch_cmd_harness.py` (admin-venv +
  numpy `LD_LIBRARY_PATH` fix) → **RESULT: 0 missing** (`/tune`, `/play`, `/filter`,
  `/radio` all present; registration unaffected by the session.py kwarg change).

## New image tag
`registry.celestium.life/hellodj/bot:batch-fixes-2026-08-18-fix1`
- Build: SUCCESS (local sha256 `42c9d8c28574...`)
- Push: SUCCESS — registry digest `sha256:829c5f1933160ac4a7e4329180bfd799baa9d5764d949ede0f39a00d5da8de9c`
- `kube/deployment.yaml` bot image + `kube/kustomization.yaml` bot `newTag` both updated
  to `batch-fixes-2026-08-18-fix1`

## Redeploy
`kubectl apply -k kube/` → `deployment.apps/hellodj configured`
`kubectl rollout status deployment/hellodj -n hellodj-service` → **successfully rolled out**

## Pod health
- `hellodj-cc4545fd8-clpds` — **2/2 Running, 0 restarts**
- Logs (startup): Lavalink reachable/connected, slash commands synced, gateway connected,
  logged in as HelloDJ#8609, `on_ready` with 2 guilds — clean.
- `kubectl exec` in-pod: `/app/session.py` carries the fix
  (`tune_enabled: bool = False` at line 77; persisted at line 97).
- Log error scan (tail 100): **NO_ERRORS_FOUND** — no `TypeError`/`save_guild`/`Traceback`.

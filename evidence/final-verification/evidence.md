# Final End-to-End Verification — HelloDJ

Date: 2026-08-15 · Mode: debug · Scope: VERIFICATION ONLY (no production code modified)

## Known exception (out of scope)
The Discord bot lacking permissions in guilds is EXPECTED / ACCEPTABLE and out of scope. No permission "fix" was attempted.

## 1. Compile checks
Commands (from workspace root `/home/celes/sources/celesrenata/hellodj`):
- `./.admin-venv/bin/python -m py_compile web-ui/app.py` → **PASS**
- `./.admin-venv/bin/python -m py_compile bot/bot.py bot/blacklist.py bot/oauth_store.py bot/permissions.py bot/player.py bot/session.py bot/storage.py bot/cogs/admin.py bot/cogs/music.py bot/cogs/playlists.py bot/cogs/filters.py bot/cogs/autoplay.py bot/cogs/lyrics.py bot/cogs/info.py bot/cogs/voice.py bot/voice/hybrid_player.py bot/voice/voice_commands.py bot/voice/audio_pipeline.py bot/voice/query_handler.py bot/voice/stt.py bot/voice/tts.py bot/voice/wakeword.py bot/voice/intent.py` → **PASS** (syntax-only; discord/wavelink not needed for py_compile)

## 2. Route registration
Command: import `web-ui/app.py` via venv, list `app.app.url_map.iter_rules()`.

| Route | Result |
|---|---|
| /auth/callback | FOUND |
| /auth/login | FOUND |
| /auth/\<provider\>/login | FOUND |
| /auth/\<provider\>/callback | FOUND |
| /api/providers/status | FOUND |
| /api/providers/\<provider\>/refresh | **MISSING (BUG)** |
| /api/guilds | FOUND |
| /api/blacklist | FOUND |
| /api/playlists | FOUND |

`grep -n "refresh" web-ui/app.py` → only `refresh_token` field reads at app.py:478/486/517. **No refresh route/function exists.**

## 3. Behavioral tests (Flask test client, stubbed aiohttp, temp data dirs)
Script: `/tmp/hellodj_final_verify.py` (throwaway; no production code touched). Env: HELLODJ_DATA_DIR/CONFIG_DIR/BACKUP_DIR → /tmp/hellodj-verify-*.

| Check | Result |
|---|---|
| unknown provider callback → 400 | PASS |
| missing code → 400 | PASS |
| invalid state → 400 | PASS |
| code-only (no state) proceeds, no 400 (302) | PASS |
| providers.discord persisted with access_token | PASS |
| bot_invite persisted guild_id/permissions | PASS |
| /api/providers/status → 200 + configured/token_present/token_expired per provider | PASS |
| /api/guilds unauth → 401 | PASS |
| /api/playlists unauth → 401 | PASS |
| /api/blacklist unauth → 401 | PASS |
| web-ui log formatter contains %(asctime)s (2 handlers) | PASS |
| /api/guilds authed includes permissions_ok + missing_permissions | **FAIL (BUG)** — guild0_keys = [channels, icon, id, member_count, name]; fields dropped |

Bot logging timestamps verified statically: bot/bot.py:47-52 sets `%(asctime)s` formatter on every handler (bot compile PASS).

## 4. kube validation
Command: `kubectl apply --dry-run=client -k kube/` (kubectl v1.35.7+k3s1, kustomize v5.7.1) → **PASS**
All 16 resources dry-run clean: namespace, configmaps (hellodj-bot-config, lavalink-config), secret, services, PVs/PVCs, deployments (hellodj, hellodj-web-ui), ingress.
deployment.yaml + web-ui-deployment.yaml confirmed to carry SPOTIFY/TIDAL/GENIUS credential env refs via `valueFrom.secretKeyRef` `optional:true` from hellodj-secret; lavalink creds flow via render-lavalink-config init container from lavalink-config ConfigMap.

## Bugs found (file:line + suggested fix — NOT applied)

### BUG-1: Missing provider token-refresh route/function
- Location: `web-ui/app.py` — no `/api/providers/<provider>/refresh` route; grep found no refresh logic (only `refresh_token` field reads at app.py:478/486/517).
- Impact: spec required token-refresh support from a recent subtask; it does not exist. Tokens can never be refreshed via the API once expired.
- Suggested fix: add a route like:
  ```python
  @app.route("/api/providers/<provider>/refresh", methods=["POST"])
  def api_provider_refresh(provider):
      prov = provider_config(provider)
      if prov is None: return jsonify({"error": f"Unknown provider: {provider}"}), 400
      oauth = load_oauth()
      entry = oauth.get("providers", {}).get(provider)
      if not entry or not entry.get("refresh_token"):
          return jsonify({"error": "No refresh token available"}), 400
      # POST to prov["token_url"] with grant_type=refresh_token + refresh_token,
      # update oauth["providers"][provider] access/refresh/expires_at, save_oauth.
  ```

### BUG-2: /api/guilds drops permissions_ok / missing_permissions
- Location: `web-ui/app.py:813-835` (`api_get_guilds`) builds each guild dict without forwarding `permissions_ok`/`missing_permissions` that the bot writes at `bot/bot.py:237-244`.
- Impact: guilds.html permissions health strip (spec-required) cannot render — the data never reaches the template.
- Suggested fix: in `api_get_guilds`, add to the guild dict:
  ```python
  "permissions_ok": data.get("permissions_ok"),
  "missing_permissions": data.get("missing_permissions", []),
  ```

### BUG-3: guilds.html hardcodes CDN icon URL, ignores app normalization
- Location: `web-ui/templates/guilds.html:67` builds `https://cdn.discordapp.com/icons/${g.id}/${g.icon}.png` unconditionally.
- Impact: contradicts `app.py:826-827` which normalizes a full-URL passthrough vs bare-id. If `icon` is ever a full CDN URL (as api_get_guilds may pass through), the template builds a malformed double-URL; it also never renders permissions badges.
- Suggested fix: use `g.icon` directly as the `<img src>` when it's already a full URL, else splice bare id; add a permissions health strip row consuming `g.permissions_ok` / `g.missing_permissions`.

### BUG-4: config.html has no provider status section
- Location: `web-ui/templates/config.html` — no call to `/api/providers/status` and no provider status rendering (grep for providers/status / token_present: none).
- Impact: spec required a provider status section fed from `/api/providers/status`; the template omits it even though the API works (verified 200).
- Suggested fix: add a card in config.html that fetches `/api/providers/status` and renders configured/token_present/token_expired badges per provider (discord/spotify/tidal/genius).

## What is verified WORKING
- Compile: web-ui/app.py + all 21 bot modules PASS.
- Routes: all spec routes present EXCEPT the refresh route (BUG-1).
- OAuth callback: unknown-provider → 400, missing-code → 400, invalid-state → 400, code-only-no-state proceeds and persists bot_invite + providers.discord — all PASS.
- /api/providers/status: 200 with per-provider configured/token_present/token_expired — PASS.
- Auth guards: /api/guilds, /api/playlists, /api/blacklist unauthenticated → 401 — PASS.
- Logging timestamps: web-ui formatter %(asctime)s on 2 handlers (runtime); bot formatter %(asctime)s on all handlers (static) — PASS.
- kube: `kubectl apply --dry-run=client -k kube/` clean — PASS.
- Icon id storage: bot/bot.py:236 stores `guild.icon.key` not `.url` — confirmed.
- Missing-permission detection: bot/permissions.py REQUIRED_PERMISSIONS/check_permissions/missing_voice_permissions wired into _build_guilds_data (bot.py:237) and player.py:400 voice-connect failure logging — confirmed.
- Blacklist sync: bot/blacklist.py load/reload from data/blacklist.json, called in setup_hook (bot.py:147), /blacklist reload admin cmd (cogs/admin.py:80) — confirmed.
- Dead connect_hybrid removed from hybrid_player.py — confirmed.
- Playlists auth fix at app.py:859 (401 guard) — verified 401.

## What remains the known-permissions exception
The Discord bot lacking required permissions in guilds is EXPECTED / ACCEPTABLE and out of scope (not fixed).

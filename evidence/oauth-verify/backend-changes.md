# Backend Changes — OAuth + Providers + Logging + Guilds/Playlists/Blacklist

Scope: `web-ui/app.py` only (no templates, no bot/*). All changes validated against
the Flask app via the `.admin-venv` interpreter and the test client.

## 1. OAuth callback state tolerance (Task 1)

`/auth/callback` now delegates to `provider_callback("discord")`. State is
optional-tolerant: present → validated against `session["oauth_state_discord"]`;
absent → warning logged, flow proceeds (accepts bot-invite URLs).

Verified with the EXACT reported broken URL (no `state`, has `code` + `guild_id`
+ `permissions`):
- Result: `302 /` (redirect to dashboard)
- `bot_invite` persisted: `{"provider":"discord","guild_id":"1501686893765595296",
  "permissions":"76422566768454","timestamp":...}`
- `providers.discord` token stored + `discord_token` + owner binding intact.

## 2. Generic OAuth provider registry (Task 2)

`PROVIDERS` registry (discord/spotify/tidal/genius) with auth_url/token_url/api_url/
scope/label/client_id_env/client_secret_env/user_path. New generic routes:
- `/auth/<provider>/login` → `provider_login`
- `/auth/<provider>/callback` → `provider_callback`
- Backward compat: `/auth/login` → `provider_login("discord")`,
  `/auth/callback` → `provider_callback("discord")`
- Per-provider tokens under `oauth.json["providers"]`:
  `{access_token, refresh_token, expires_at, updated_at}`
- `/api/providers/status` reports configured / token_present / token_expired /
  expires_at / updated_at per provider.

Verified: Spotify callback with state → `200 {"status":"ok","message":"Spotify
OAuth token stored"}`, `providers.spotify` populated.

## 3. Logging timestamps (Task 3)

`_setup_logging()` now uses `format="%(asctime)s %(levelname)s %(name)s:
%(message)s"`, `datefmt="%Y-%m-%d %H:%M:%S"`. The formatter is set explicitly on
the console (StreamHandler) and the rotating-file handler (RotatingFileHandler),
so both emit timestamps regardless of `basicConfig` handler behavior. Verified:
a sample record renders `2026-08-15 09:50:23 INFO test: hello`; the file handler's
formatter is `%(asctime)s %(levelname)s %(name)s: %(message)s`.

## 4. Guilds icon normalization (Task 4)

`api_get_guilds()` builds the CDN URL for a bare icon id
(`https://cdn.discordapp.com/icons/{gid}/{icon}.webp?size=128`) and passes a full
http(s) URL through untouched.

## 5. Blacklist sync (Task 5)

`data/blacklist.json` is now the single source of truth via `BLACKLIST_FILE`.
GET requires auth; POST (owner/admin) writes the file atomically. Bot-side
reading is handled in the bot subtask.

## 6. Playlists auth fix (Task 6)

`api_get_playlists()` now returns `401 {"error":"Authentication required"}` for
unauthenticated users (previously open). Verified.

## Validation
- `ast.parse` syntax OK
- Module import + full route table registered (incl. `/auth/<provider>/login`,
  `/auth/<provider>/callback`, `/api/providers/status`)
- Test-client checks: invalid state → 400; no-state bot-invite → 302 + persisted;
  unknown provider login → 400; providers/status → 200; playlists & blacklist
  unauthenticated → 401.
- End-to-end mocked-exchange run: real `provider_callback` executed its full
  exchange path and persisted bot_invite + providers + owner.

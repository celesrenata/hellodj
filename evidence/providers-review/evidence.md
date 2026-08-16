# Provider OAuth / API Integration Review — HelloDJ

**Scope:** Verify that each provider (Discord, Spotify, Tidal, Genius) has credentials
that actually work and authenticate. Review-only; no production code was modified.

**Method:** Static code review of the OAuth provider registry + live, non-exfiltrating
validation inside the running Kubernetes cluster (`kubectl` into the web-ui/bot pods).
All token/secret **values redacted** — only lengths, booleans, and HTTP statuses are
recorded. No production code was changed.

---

## 1. Provider Registry (web-ui/app.py)

`PROVIDERS` at [`web-ui/app.py:284`](web-ui/app.py:284) defines discord/spotify/tidal/genius
with `auth_url`/`token_url`/`api_url`/`scope`/`label`/`client_id_env`/`client_secret_env`/`user_path`:

| provider | client_id_env | client_secret_env | scope | user_path |
|----------|---------------|--------------------|-------|-----------|
| discord | `DISCORD_APPID` (:291) | `DISCORD_CLIENT_SECRET` (:292) | `identify` | `/users/@me` |
| spotify | `SPOTIFY_CLIENT_ID` (:302) | `SPOTIFY_CLIENT_SECRET` (:303) | `user-read-private user-read-email` | `/me` |
| tidal | `TIDAL_CLIENT_ID` (:313) | `TIDAL_CLIENT_SECRET` (:314) | `user_read` | `/users/me` |
| genius | `GENIUS_CLIENT_ID` (:324) | `GENIUS_CLIENT_SECRET` (:325) | (empty) | `/account` |

Helpers: `provider_config` (:331), `provider_credentials` (:335, reads `.env` via
`read_env()` at :135), `provider_is_configured` (:345, requires **both** client_id AND
client_secret). Routes: `/auth/<provider>/login` → `provider_login` (:369),
`/auth/<provider>/callback` → `provider_callback` (:404). Status:
`/api/providers/status` → `api_providers_status` (:561).

## 2. Token-Exchange / Refresh Logic

**Exchange** ([`web-ui/app.py:438`](web-ui/app.py:438)): posts `client_id`, `client_secret`,
`grant_type=authorization_code`, `code`, `redirect_uri` to `token_url`, then fetches the
user profile with `Authorization: Bearer <access_token>`.

- **Token formats handled:** generic — reads `access_token`, `refresh_token`, `expires_in`
  from the JSON (:477-479). Spotify returns all three (verified live: `expires_in=3600`).
  Tidal/Genius differ but the code stores whatever fields are present; it does not branch
  per provider. Tidal's real flow additionally needs `countryCode` and a different grant —
  not handled.
- **No token-refresh support.** The exchange is one-time only. `refresh_token` is stored
  (:486) but there is **no code path anywhere** that uses it to re-authenticate an expired
  token. There is no `/auth/<provider>/refresh` route and no background refresher.
  **Gap:** once `expires_at` passes, the token is dead forever; the UI must re-run the
  full login flow.
- **`_token_expires_at`** (:360) parses `expires_in` correctly (validated offline:
  int/str/0/None/negative all handled; `expires_at` computed as `now + expires_in`).

**Legacy async bug (resolved):** `config/webui.log` shows a prior `TypeError: cannot
unpack non-iterable coroutine object` at :473 and `'coroutine' object does not support
the async context manager` at :440 (2026-08-15 09:36-09:37). Later successful Discord
bindings (09:43 "Bound owner 42 (celes)", 09:50 "Bound owner spotuser") prove the
`asyncio.run(exchange())` fix is live. **Historical note only — current code works.**

## 3. /api/providers/status correctness

`api_providers_status` (:561-591) computes `configured` (via `provider_is_configured`),
`token_present` (bool of stored `access_token`), and `token_expired` (`now > expires_at`)
from `oauth.json["providers"]`. Validated offline with simulated oauth.json — correctly
flags expired vs fresh vs absent tokens. **No functional bug** in the route itself.

## 4. Bot Consumption of Provider Tokens

**The bot does NOT consume per-provider tokens from `oauth.json["providers"]`.**

- [`bot/oauth_store.py:28`](bot/oauth_store.py:28) reads `data/oauth.json` **only** to
  enforce owner/admin permissions (`is_bound_admin` at :82). It never reads provider tokens.
- [`bot/player.py:554`](bot/player.py:554) and [`bot/cogs/music.py:169`](bot/cogs/music.py:169)
  use **search-source routing only**: `"spotify": "spsearch"` (:558/:174),
  `"tidal": "tidal"` (:559/:175), which are lavasrc search prefixes. The bot never
  authenticates to Spotify/Tidal APIs directly.
- [`bot/cogs/lyrics.py:52`](bot/cogs/lyrics.py:52) reads Genius creds from **env only**
  (`GENIUS_ACCESS_TOKEN`/`GENIUS_API_KEY`), used as a static Bearer at :112-113. No OAuth
  exchange, no refresh.
- [`bot/bot.py:75`](bot/bot.py:75) defines `SPOTIFY_CLIENT_ID`/`SPOTIFY_CLIENT_SECRET`
  but **no other module references them** — dead code. The bot's Spotify/Tidal playback
  depends entirely on the lavalink sidecar's lavasrc creds, not these.

**Live cluster confirmation:** the bot pod has **zero** provider credentials in its env
(all of SPOTIFY/TIDAL/GENIUS vars empty), and the lavalink sidecar's rendered
`application.yml` has **empty** `clientId`/`clientSecret`/`token` for lavasrc spotify+tidal.
⇒ "tokens work" **cannot be satisfied by playback** — the running deployment authenticates
neither Spotify nor Tidal.

## 5. Live Credential State (kube, values redacted)

**Live `.env` at `/app/data/.env`** (web-ui pod):

| var | len | state |
|-----|-----|-------|
| DISCORD_CLIENT_SECRET | 32 | REAL |
| SPOTIFY_CLIENT_ID | 32 | REAL |
| SPOTIFY_CLIENT_SECRET | 32 | REAL |
| TIDAL_TOKEN | 44 | REAL-looking, but **invalid** (see tests) |
| TIDAL_CLIENT_ID / TIDAL_CLIENT_SECRET | 0 / 0 | **EMPTY** |
| GENIUS_CLIENT_ID / CLIENT_SECRET | 64 / 86 | REAL |
| GENIUS_ACCESS_TOKEN / API_KEY | 64 / 64 | REAL-looking, but **invalid** |

**Live `oauth.json`:** top keys = `owner_user_id, owner_username, admin_user_ids,
discord_token`. **No `providers` key at all** — the per-provider token store is empty.
Only the legacy `discord_token` exists (`access_token`/`refresh_token`/`expires_in=604800`,
no `expires_at`).

**Cluster `hellodj-secret`** contains only `DISCORD_APPID`, `DISCORD_PUBKEY`,
`DISCORD_TOKEN` — **no `DISCORD_CLIENT_SECRET`, no Spotify/Tidal/Genius keys.** The web-ui
deployment wires only Discord creds ([`kube/web-ui-deployment.yaml:59`](kube/web-ui-deployment.yaml:59));
all Spotify/Tidal/Genius secret refs in [`kube/deployment.yaml:184`](kube/deployment.yaml:184)
are `optional: true` and absent.

## 6. Live Token Authentication Tests (read-safe, in-pod)

| provider | test | result | verdict |
|----------|------|--------|---------|
| **Spotify** | client_credentials exchange `accounts.spotify.com/api/token` | **HTTP 200**, access_token issued, expires_in 3600 | **REAL — authenticates** (app-level). `/v1/me` 401 is expected (user scope needs user OAuth); bot doesn't use this token anyway. |
| **Genius** | `/account` with GENIUS_ACCESS_TOKEN | **HTTP 401** "requires a valid user token" | **BROKEN — token does NOT authenticate** |
| **Genius** | client_credentials exchange `api.genius.com/oauth/token` (GENIUS_CLIENT_ID/SECRET) | **HTTP 200**, token granted | REAL at client level, but the bot uses the static access_token/api_key (invalid). |
| **Tidal** | `/v1/tracks/1?countryCode=US` with TIDAL_TOKEN | **HTTP 401** | **BROKEN — token invalid** |
| **Discord** (bot) | `DISCORD_TOKEN` via `/users/@me` Bot and `/oauth2/@me` Bearer | **HTTP 403 code 1010** (invalid token) | **.env copy is STALE/invalid.** The *running bot* uses `hellodj-secret`'s token (bot online 2/2), so this `.env` copy is not what the bot authenticates with. |
| **Discord** (web OAuth) | live callback (webui.log 2026-08-15 09:50) | "Bound owner spotuser" — success | **web-ui Discord OAuth WORKS** with `.env` DISCORD_CLIENT_SECRET. |

## 7. Provider Status Report

| provider | configured? | token present? | token valid? | bot usage | gaps preventing "tokens work" |
|----------|-------------|----------------|--------------|-----------|--------------------------------|
| **Discord** | ✅ yes (.env client secret + appid) | legacy `discord_token` only; **no `providers.discord`** | web OAuth works (live); `.env` bot token stale | owner/admin binding only (`oauth_store.is_bound_admin`); bot's real token from `hellodj-secret` | web-OAuth token not stored under `providers.discord`; no refresh path |
| **Spotify** | ✅ yes (client id+secret) | **no** (`providers` empty) | client_credentials **works (200)** | **none** — bot only search-routes `spsearch`; creds are dead code ([`bot.py:75`](bot/bot.py:75)); lavalink has empty creds | bot/lavalink never receive creds; no user-scope OAuth token ever issued |
| **Tidal** | ❌ **no** (client id+secret empty in `.env`) | **no** | TIDAL_TOKEN **invalid (401)** | **none** — bot search-routes `tidal`/`tdsearch`; lavalink has empty creds | no valid client creds; token invalid; nothing wired to lavalink |
| **Genius** | ✅ yes (client id+secret) | **no** (`providers` empty) | GENIUS_ACCESS_TOKEN **invalid (401)**; client_credentials works | `/lyrics` uses static env token ([`lyrics.py:112`](bot/cogs/lyrics.py:112)) | bot uses the **invalid** static access_token; no OAuth/refresh; client_credentials path unused |

**Overall verdict:** The **generic provider registry, exchange, and status route are
correctly implemented and validated**. But the **"tokens actually work" claim is NOT
satisfied**: (a) `oauth.json["providers"]` is **empty** — no per-provider tokens are ever
stored, so the status UI reports all `token_present=false`; (b) **no token refresh**
exists; (c) the **bot never authenticates** with any provider API — it only search-routes
via lavalink, which itself has **empty** Spotify/Tidal creds in the live deployment; and
(d) the **Genius and Tidal tokens are actually invalid (401)**.

## 8. Recommended Minimal Fixes

1. **Wire provider creds into the cluster.** Add `DISCORD_CLIENT_SECRET`,
   `SPOTIFY_CLIENT_ID/SECRET`, `TIDAL_CLIENT_ID/SECRET`, `TIDAL_TOKEN`,
   `GENIUS_ACCESS_TOKEN` to `hellodj-secret` and reference them (non-optional) in
   [`kube/deployment.yaml:184`](kube/deployment.yaml:184) and
   [`kube/web-ui-deployment.yaml:59`](kube/web-ui-deployment.yaml:59) so the bot and
   lavalink actually receive them.
2. **Store provider tokens under `providers`** — `provider_callback` already writes them;
   the live `oauth.json` simply has none because no provider flow was run except Discord
   (which only wrote the legacy `discord_token`). Run the flows, or backfill.
3. **Add token refresh.** Implement a `/auth/<provider>/refresh` route (or background
   task) that uses the stored `refresh_token` against `token_url` and updates
   `expires_at`; otherwise tokens expire irrecoverably.
4. **Fix invalid Genius token.** Regenerate `GENIUS_ACCESS_TOKEN` (the current one is 401),
   and prefer the verified `client_credentials` exchange instead of a static token.
5. **Fix invalid Tidal token + add client creds.** The `TIDAL_TOKEN` is 401 and
   `TIDAL_CLIENT_ID/SECRET` are empty; obtain a valid Tidal OAuth token and set the client
   creds so lavasrc can authenticate.
6. **Remove dead Spotify creds or use them.** Either delete [`bot/bot.py:75`](bot/bot.py:75)
   or implement real Spotify API usage; the bot currently never uses them.

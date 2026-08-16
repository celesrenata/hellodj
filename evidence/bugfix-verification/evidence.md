# Bugfix Verification — Four Final-Verification Bugs (web-ui)

Date: 2026-08-16
Scope: `web-ui/app.py`, `web-ui/templates/guilds.html`, `web-ui/templates/config.html`
Validator: Flask test-client with mocked aiohttp (`.admin-venv` interpreter)

## Fixes applied

### BUG-1 — Missing provider token-refresh route (`web-ui/app.py`)
- Added `_refresh_provider_token(provider)` helper (after `_token_expires_at`, ~line 369):
  POSTs to `prov["token_url"]` with `grant_type=refresh_token`, `refresh_token`,
  `client_id`, `client_secret` from `provider_credentials()`. On success it persists
  `access_token`, optional `refresh_token`, `expires_at` (now + `expires_in`),
  `updated_at` via the existing atomic `save_oauth`/`write_json`. Never raises.
- Added route `@app.route("/api/providers/<provider>/refresh", methods=["POST"])`
  (~line 619): 400 unknown provider; 404 no stored token/refresh_token; 200 on
  success; 500 on network/exchange error.
- Wired auto-refresh into `/api/providers/status` (~line 628): an expired stored
  token is refreshed first; on success `token_expired` flips false, on failure it
  stays true and `refresh_error` carries the message.

### BUG-2 — `/api/guilds` drops permissions health fields (`web-ui/app.py`)
- `api_get_guilds` (~line 913) now forwards `permissions_ok` and
  `missing_permissions` (default `[]`) into each guild dict. Icon normalization
  (full URL pass-through, bare id → CDN) is preserved.

### BUG-3 — guilds.html hardcoded CDN icon + missing health strip (`web-ui/templates/guilds.html`)
- `iconHtml(g)`: full `http(s)` URL used directly; bare id built to
  `https://cdn.discordapp.com/icons/{g.id}/{g.icon}.webp?size=128`.
  Kept 24px/24px, border-radius:50%, vertical-align:middle, margin-right:6px.
- Added `permissionsHtml(g)` health strip under the guild name in the Name cell:
  green "✅ Permissions OK" when `permissions_ok` true or missing list empty;
  amber "⚠️ Missing: <list>" when missing present; graceful "—" when fields absent.

### BUG-4 — config.html lacks provider status section (`web-ui/templates/config.html`)
- Added a "Provider Status" card (after Bot Settings, ~line 292) and
  `loadProviderStatus()` (called on page load) which fetches `/api/providers/status`
  via the existing `apiFetch` helper and renders per provider (discord, spotify,
  tidal, genius): label, Configured/Not-configured badge, Token present/No token,
  Token valid/Token expired, last-updated time, and refresh-error line when present.
  Layout matches the existing card style.

## Validation results (18/18 checks passed)

| Check | Result |
|-------|--------|
| Route `/api/providers/<provider>/refresh` registered | PASS |
| Route `/api/providers/status` registered | PASS |
| GET `/api/providers/status` → 200 | PASS |
| Status contains all 4 providers | PASS |
| GET `/api/guilds` unauth → 401 | PASS |
| GET `/api/guilds` authed → 200 | PASS |
| Guild has `permissions_ok` | PASS |
| Guild has `missing_permissions` list | PASS |
| Icon normalized (bare id → CDN URL) | PASS |
| Refresh unknown provider → 400 | PASS |
| Refresh unconfigured provider → 404 | PASS |
| Refresh with stored token → 200 | PASS |
| Refresh persisted new `access_token` | PASS |
| Refresh persisted new `refresh_token` | PASS |
| Refresh updated `expires_at` to future | PASS |
| Status reports refreshed spotify as not expired | PASS |
| Status spotify `token_present` true | PASS |
| Refresh network error → 500 (no crash) | PASS |

`py_compile web-ui/app.py` (`.admin-venv`): OK
Template render: `/guilds` → 200, `/config` → 200

Note: Pylance "Import discord could not be resolved" errors in `bot/*` and Flask
route-typing warnings are pre-existing and unrelated to these changes. Only
`web-ui/app.py`, `web-ui/templates/guilds.html`, `web-ui/templates/config.html`
were modified; `bot/*` and `kube/*` were not touched.

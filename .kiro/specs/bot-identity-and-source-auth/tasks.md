# Implementation Plan

## Overview

This is a bugfix spec. Tasks follow the bug-condition methodology: Task 1 writes
exploration tests that demonstrate the defects on the CURRENT (unfixed) code and
are EXPECTED TO FAIL; Task 2 captures preservation baselines that PASS on unfixed
code; Tasks 3–8 implement the fix (change areas A–F) and re-run the exploration +
preservation tests to confirm the fix and guard against regressions.

All tests use fakes (no live AWS / Discord / potoken-server / Lavalink), matching
the existing `bot/playback/test_guild_credentials.py` `FakeSecrets` style and the
web-ui `tests/` style. See the Notes section for gate commands and the two-path
deployment reality (CodeCommit push vs `cdk deploy hellodj-eks`).

## Tasks

- [x] 1. Write bug-condition exploration tests (EXPECTED TO FAIL on unfixed code)
  - **Property 1: Bug Condition** - Per-Guild Source Connect + Bot Identity Defects (C1(X), C2(X))
  - **CRITICAL**: These tests MUST FAIL on the current unfixed code - failure confirms the bugs exist (C1(X) true for spotify/youtube/youtube_music; C2(X) true for all guilds)
  - **DO NOT attempt to fix the tests or the code when they fail** - failure here is the correct, expected outcome
  - **NOTE**: These tests encode the expected post-fix behavior - they will validate the fix when they pass after implementation
  - **GOAL**: Surface counterexamples that demonstrate each defect
  - **Scoped PBT Approach**: These are deterministic defects, so scope each property to the concrete failing cases (empty client-id config; a YouTube callback code; absent identity route/service)
  - web-ui exploration tests in `platform/components/web-ui/tests/` (fake Flask app config, no live calls):
    - Spotify no-op: with `SPOTIFY_CLIENT_ID=""` in config, assert `source_oauth.source_authorize_url("spotify", state, gid)` returns `None`, and that `auth.source_connect` redirects back to the guild page with nothing stored (confirms 1.1, 1.2)
    - YouTube no-op: with `GOOGLE_CLIENT_ID=""`, assert `source_authorize_url("youtube", state, gid)` returns `None` (and same for `youtube_music`) (confirms 1.3)
    - YouTube wrong-token: drive `source_tokens_from_request("youtube")` with a fake callback `?code=...` and assert the returned/stored dict has only `{provider, authorization_code}` and LACKS `oauth_refresh_token`, `pot_token`, `pot_visitor_data` (confirms 1.4)
    - No identity capability: assert no per-guild identity route/service exists - e.g. `url_for('guild.set_bot_nickname', guild_id=...)` (and `guild.set_bot_avatar`) raises `BuildError`, and there is no `GuildIdentityService`/`guild_identity_service` importable (confirms 1.6, 1.7)
  - bot exploration test from `bot/playback/` (bare imports rely on cwd on `sys.path`):
    - No per-guild YouTube resolution: assert `GLOBAL_FALLBACK_LEAVES` has no `youtube`/`youtube_music` key and `GuildCredentialResolver.resolve(gid, "youtube")` returns `None` when no guild secret exists, using `FakeSecrets({})` (documents 1.5; note this specific no-global-leaf behavior must STAY true for guilds without a secret per 3.5/3.7 and is re-used as a preservation baseline in Task 2)
  - infra exploration test in `platform/infra` (jest): assert the synthesized web-ui container env does NOT currently include `SPOTIFY_CLIENT_ID` / `GOOGLE_CLIENT_ID` (confirms root cause 1a on unfixed infra)
  - Run: web-ui `python3 -m pytest tests/ -q`; bot `pytest` from `bot/playback/`; infra `npx jest`
  - **EXPECTED OUTCOME**: The web-ui and infra "should-be-fixed" assertions FAIL (proving the bugs); document counterexamples found (silent `None` authorize URLs; authorization-code-only YouTube secret; absent identity routes; missing env vars)
  - Mark task complete when the tests are written, run, and the failures are documented
  - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7_

- [x] 2. Write preservation property tests (BEFORE implementing the fix)
  - **Property 2: Preservation** - Tidal Connect, Ownership Gating, Secret Isolation, Disconnect, Global YouTube Path, DVD-Visualizer Read, Global Fallback Leaves
  - **IMPORTANT**: Follow observation-first methodology - observe behavior on UNFIXED code, then write property-based tests (hypothesis) capturing it
  - **NOTE**: These tests MUST PASS on unfixed code; after the fix they MUST still pass (no regressions)
  - web-ui preservation tests in `platform/components/web-ui/tests/` (fakes):
    - Tidal connect preserved: with `TIDAL_CLIENT_ID` set, observe connect redirects to the Tidal authorize URL and disconnect removes the secret at `hellodj/<stage>/guild/<gid>/tidal`; capture as a test (3.1)
    - Ownership gating preserved: property test over arbitrary callers (managers and non-managers) - assert every per-guild source connect/disconnect route rejects callers failing `can_manage_guild` / `_can_manage` (3.2)
    - Secret isolation + tokens-out-of-DynamoDB preserved: observe that `GuildSourcesService.store_tokens` writes tokens only to the per-guild secret and the DynamoDB `SOURCE#<provider>` item holds only non-secret metadata; capture as a test (3.3)
    - Disconnect preserved: observe disconnect deletes both the guild secret and the `SOURCE#<provider>` metadata item; capture as a test (3.4)
  - bot preservation tests from `bot/playback/`:
    - Global fallback leaves preserved (property test): for arbitrary guilds without a per-guild secret, assert `resolve(gid, "tidal")` → `tidal-refresh` leaf and `resolve(gid, "spotify")` → `spotify` leaf still resolve, and `GLOBAL_FALLBACK_LEAVES == {"tidal": "tidal-refresh", "spotify": "spotify"}` (3.7)
    - Global YouTube playback preserved: for a guild with no YouTube secret, assert `resolve(gid, "youtube")` returns `None` (so the bot uses the existing global `push_youtube_oauth` single `POST /youtube`) (3.5)
    - DVD-visualizer avatar read preserved: assert `bot/cogs/video.py` still reads `self.bot.user.avatar` (grep/static assertion or a fake-bot read test), unaffected by per-guild identity (3.6)
  - Run: web-ui `python3 -m pytest tests/ -q`; bot `pytest` from `bot/playback/`
  - **EXPECTED OUTCOME**: All preservation tests PASS on UNFIXED code (this is the baseline to preserve)
  - Mark task complete when the tests are written, run, and passing on unfixed code
  - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7_

- [x] 3. Change area A - wire web-ui OAuth client-id/secret env from Secrets Manager (infra)

  - [x] 3.1 Add AuthStack secrets and thread client-ids + client-secrets into web-ui containerEnv
    - `auth-stack.ts`: add empty `secretsmanager.Secret` entries for Google (`hellodj/<stage>/google-oauth`, holding `{client_id, client_secret}`) and Discord OAuth (`hellodj/<stage>/discord-oauth`, holding `{client_id, client_secret}`), created empty and populated out-of-band like the existing secrets; expose them on `WorkloadsSecretRefs`
    - `foundation.ts` / `bin/hellodj.ts`: thread the OAuth client-id values (`spotifyClientId?`, `googleClientId?`, `tidalClientId?`) through `WorkloadsStackProps` (additive, default `""`)
    - `workloads-stack.ts`: inside the `if (spec.name === 'web-ui')` block of `containerEnv`, push `SPOTIFY_CLIENT_ID` / `GOOGLE_CLIENT_ID` / `TIDAL_CLIENT_ID` as plain env values (mirroring `discordClientId`), and inject client secrets (`GOOGLE_CLIENT_SECRET`, `DISCORD_CLIENT_SECRET`) via a per-stage `web-ui-oauth-secret` k8s Secret referenced by `secretKeyRef` (mirroring `web-ui-flask-secret`), so no secret value lands in a CloudFormation env literal
    - `grantDependencies` (or the web-ui grant block): `grantRead` the web-ui IRSA role on the new google-oauth + discord-oauth secrets (and spotify), scoped to their ARNs (least privilege)
    - _Bug_Condition: isBugCondition1(X) - source_authorize_url returns None because client-id env is empty (root cause 1a)_
    - _Expected_Behavior: expectedBehavior - required client id/secret populated so source_authorize_url does not return None for a configured provider_
    - _Preservation: existing containerEnv (Cognito/Discord-client/invite) + web-ui-flask-secret pattern unchanged_
    - _Requirements: 2.6_

  - [x] 3.2 Update CDK jest tests to assert new env vars + grants
    - Assert the synthesized web-ui container env now includes `SPOTIFY_CLIENT_ID`, `GOOGLE_CLIENT_ID`, `TIDAL_CLIENT_ID` (plain) and that `GOOGLE_CLIENT_SECRET` / `DISCORD_CLIENT_SECRET` are wired via `secretKeyRef` to the per-stage `web-ui-oauth-secret`
    - Assert the web-ui role is granted read on the new google-oauth and discord-oauth secrets
    - This converts the Task 1 infra exploration assertion (env absent) into its fixed counterpart (env present)
    - Run: `cd platform/infra && npx tsc --noEmit && npx jest`
    - _Requirements: 2.6_

- [x] 4. Change area B - clear error instead of silent no-op + configured-only connect UI (web-ui)

  - [x] 4.1 Turn the silent no-op into a clear error and expose provider-configured state
    - `source_oauth.py`: add `source_provider_configured(provider) -> bool` returning whether the relevant client id/secret is present in `current_app.config`
    - `auth.py` `source_connect`: when `source_authorize_url` returns `None`, redirect to `guild.guild_detail(guild_id=..., error="provider_not_configured", provider=<p>)` (or flash) so a visible message renders instead of a no-op
    - `guild_routes.py` `guild_detail`: pass a `providers_configured` map into the template alongside `sources`
    - `templates/partials/guild_source_list.html`: for a not-connected provider render an active "Connect" link when configured, or a disabled "Needs setup" button + tooltip when unconfigured; all four `SUPPORTED_PROVIDERS` (youtube, youtube_music, tidal, spotify) always appear
    - Keep `source_oauth.py` under the 500-line ceiling (split helpers if needed)
    - _Bug_Condition: isBugCondition1(X) - Spotify/YouTube connect silently no-ops when client id empty (1.1, 1.2)_
    - _Expected_Behavior: expectedBehavior - all supported providers offered as connectable; unconfigured shows clear "needs setup", never a silent no-op_
    - _Preservation: Tidal renders/connects exactly as before (3.1); can_manage_guild gating unchanged (3.2)_
    - _Requirements: 2.1, 1.2_

- [x] 5. Change area C + D - YouTube full OAuth + PoToken capture; Spotify connectable; Tidal preserved (web-ui)

  - [x] 5.1 Implement YouTube code→refresh-token exchange + PoToken fetch + full stored shape
    - `source_oauth.py`: change youtube / youtube_music scope from `youtube.readonly` to the playback scope (`https://www.googleapis.com/auth/youtube`) with `access_type=offline` + `prompt=consent` so Google returns a refresh token; keep the module a thin dispatcher
    - New `source_token_exchange.py` (keeps 500-line ceiling): `source_exchange_google(code, guild_id) -> dict` POSTs `https://oauth2.googleapis.com/token` (grant_type=authorization_code) resolving `GOOGLE_CLIENT_ID` + `GOOGLE_CLIENT_SECRET` (web-ui holds the secret, resolved lazily from `HELLODJ_GOOGLE_OAUTH_SECRET_ARN`); and `fetch_guild_potoken() -> dict` POSTs the in-cluster potoken-server (`POTOKEN_SERVER_URL`, default in-cluster DNS `POST /get_pot`), reading `poToken` → `pot_token` and `contentBinding` → `pot_visitor_data`; returns `{}` on failure so no partial secret is stored
    - `auth.py` `source_callback`: for youtube/youtube_music, exchange → fetch PoToken → compose `{provider, oauth_refresh_token, pot_token, pot_visitor_data, connected_by, connected_at}` → `GuildSourcesService.store_tokens(...)`; surface a clear error (query param) if compose is empty (potoken-server down or refresh token missing), not a silent no-op
    - `guild_sources.py` `store_tokens`: store the exact JSON shape above in `hellodj/<stage>/guild/<gid>/<provider>`; `SOURCE#<provider>` DynamoDB item stays metadata-only
    - _Bug_Condition: isBugCondition1(X) for p in {youtube, youtube_music} - callback stored only {provider, authorization_code} (1.4)_
    - _Expected_Behavior: expectedBehavior - store oauth_refresh_token + pot_token + pot_visitor_data together in the guild secret (2.3, 2.4)_
    - _Preservation: SOURCE#<provider> metadata-only + secret isolation (3.3)_
    - _Requirements: 2.3, 2.4_

  - [x] 5.2 Make Spotify connectable via web-ui exchange; preserve Tidal exactly
    - `source_oauth.py` / `source_token_exchange.py`: add `source_exchange_spotify(code, guild_id) -> dict` POSTing `https://accounts.spotify.com/api/token` (grant_type=authorization_code), resolving the Spotify client id+secret from `HELLODJ_SPOTIFY_SECRET_ARN`; return `{provider, refresh_token, access_token?, expires_at?, scope, obtained_at}` (refresh-token-centric, mirrors global spotify fallback)
    - `auth.py` `source_callback`: for spotify, call the exchange then `store_tokens(guild_id, "spotify", tokens, connected_by=...)` at `hellodj/<stage>/guild/<gid>/spotify`
    - Tidal: leave `auth.tidal_callback` forward-to-`tidal-stream` path and Tidal secret shape UNCHANGED (sidecar-completes-exchange model)
    - _Bug_Condition: isBugCondition1(X) for p = spotify - connect no-op'd on empty client id (1.1, 1.2)_
    - _Expected_Behavior: expectedBehavior - Spotify connect redirects and stores working per-guild credentials (2.2)_
    - _Preservation: Tidal connect/disconnect + tidal secret shape unchanged (3.1)_
    - _Requirements: 2.2_

- [x] 6. Change area E - wire bot per-guild YouTube credential resolution + just-in-time injection (bot)

  - [x] 6.1 Wire GuildCredentialResolver into the playback path and add per-guild YouTube swap
    - `bot/playback/guild_credentials.py`: document (docstring) that youtube/youtube_music have a per-guild capture path but no global fallback leaf - a guild without a per-guild youtube secret plays via the untouched global credential-store push; make NO structural change to `GLOBAL_FALLBACK_LEAVES`
    - `bot/bot.py`: construct a `GuildCredentialResolver` once at startup (boto3 secretsmanager client + stage), reachable from the play path; parameterize `push_youtube_oauth`'s payload builder to accept explicit `{oauth_refresh_token, pot_token, pot_visitor_data}` instead of always reading the global credential store
    - `bot/player.py`: in the YouTube branch of `_resolve_and_play`, if `resolver.resolve(guild_id, provider)` returns a dict with `oauth_refresh_token`, push THAT guild's creds via the single `POST /youtube` immediately before resolving/playing the track (just-in-time last-writer-wins swap); guilds without a per-guild secret fall through to the existing global push untouched
    - Serialize the swap with an `asyncio.Lock` per Lavalink node (held from push through track-resolved)
    - **SHARED-LAVALINK LIMITATION (document in code + here):** one shared node holds ONE YouTube cred set at a time; the resolve-time swap + per-node lock guarantees each resolution uses the correct guild's creds, but true concurrent per-guild isolation on a single node is out of scope (a node-per-guild pool is the deferred answer, per Design Risks #1)
    - _Bug_Condition: isBugCondition1(X) for p in {youtube, youtube_music} - resolver unwired, no per-guild injection (1.5)_
    - _Expected_Behavior: expectedBehavior - bot loads the guild's oauth_refresh_token + pot_token + pot_visitor_data and uses them for that guild's playback (2.5)_
    - _Preservation: global YouTube push untouched for guilds without a secret (3.5); tidal/spotify fallback leaves unchanged (3.7)_
    - _Requirements: 2.5_

  - [x] 6.2 Unit-test resolver + injector with a fake Lavalink; preserve the global path
    - From `bot/playback/`: given a fake per-guild youtube secret, assert `resolve(gid, "youtube")` returns the three fields; assert the just-in-time injector POSTs exactly that guild's `{oauth_refresh_token, pot_token, pot_visitor_data}` to a FAKE Lavalink `/youtube`
    - Assert a guild with no per-guild secret triggers NO swap and the global push path remains in effect (3.5)
    - Property test: random stored per-guild youtube secrets round-trip through resolver + injector with no cross-guild leakage
    - Run: `pytest` from `bot/playback/`
    - _Requirements: 2.5_

- [x] 7. Change area F - per-guild bot identity (web-ui service/routes/templates + bot applier)

  - [x] 7.1 web-ui BotIdentityService + routes + templates
    - New `bot_identity.py` `BotIdentityService`: persist the `GUILD#<gid>` / `BOTIDENTITY` DynamoDB item (metadata only: `{nickname, avatar_version/avatar_present, requested_by, desired_at, applied_at, apply_status, apply_error}`); upload avatar bytes to S3 at `avatar_key` in the stage-scoped bucket (NOT DynamoDB); enforce format in {PNG, JPG, GIF} + 256 KB max at upload; read back the item for status display
    - `guild_routes.py`: add `POST /guilds/<gid>/identity/nickname` (`guild.set_bot_nickname`) and `POST /guilds/<gid>/identity/avatar` (`guild.set_bot_avatar`), EACH gated by `_can_manage(guild_id)` exactly like the source routes; `guild_detail` reads back `apply_status`/`apply_error`
    - Templates: new `templates/partials/guild_identity_form.html` (nickname text input + avatar file input); add an "Identity" tab to `templates/pages/guild_detail.html` that renders the form and surfaces Pending / Applied / error status (HTMX poll or on next load)
    - `bootstrap.py` / `app.py`: build and register `BotIdentityService`
    - _Bug_Condition: isBugCondition2(X) - no UI/route/service to set the bot's per-guild nickname/avatar (1.6, 1.7)_
    - _Expected_Behavior: expectedBehavior - authorized user persists the desired per-guild identity via gated routes_
    - _Preservation: can_manage_guild gating on all new routes (3.2); tokens/secret isolation model mirrored (3.3)_
    - _Requirements: 2.7, 2.8_

  - [x] 7.2 bot-side applier for nickname + per-guild server avatar
    - New `bot/bot_identity_apply.py`: read pending `BOTIDENTITY` items (periodic poll on the existing watchdog cadence + `on_guild_join`/`on_ready`), diff against applied version, and apply only on change
    - Nickname: `await guild.me.edit(nick=nickname)` (2.7)
    - Per-guild server avatar: read the S3 avatar bytes, build a base64 data URI, and issue raw REST `PATCH /guilds/{guild_id}/members/@me` with `{"avatar": "data:image/png;base64,<...>"}` via `self.bot.http.request(discord.http.Route(...))` (discord.py has no stable public method for the bot's own per-guild member avatar - Design Risk #2) (2.8)
    - On success set `apply_status="applied"` + `applied_at` and clear `apply_error`; on `discord.Forbidden` (missing Manage Nicknames / guild permission) record `apply_status="error"` + a human-readable `apply_error` so the UI surfaces a clear error (2.9)
    - Leave `bot/cogs/video.py`'s global `self.bot.user.avatar` read untouched (3.6)
    - _Bug_Condition: isBugCondition2(X) - identity change cannot be applied (no applier)_
    - _Expected_Behavior: expectedBehavior - nickname/avatar take effect in the guild via Discord API; permission-denied surfaces a clear error (2.7, 2.8, 2.9)_
    - _Preservation: DVD-visualizer global avatar read unchanged (3.6)_
    - _Requirements: 2.7, 2.8, 2.9_

  - [x] 7.3 Unit-test BotIdentityService (fake S3) + applier (fake Discord)
    - web-ui: `BotIdentityService` persists the `BOTIDENTITY` item and uploads avatar bytes to a FAKE S3; enforces PNG/JPG/GIF + 256 KB max (reject oversize/wrong-format); all identity routes reject non-managers
    - bot: applier sets nickname via a FAKE Discord client and avatar via a fake REST route; assert the `discord.Forbidden` → `apply_status="error"` + `apply_error` clear-error path (R2.9); assert `apply_status` flows back to the item the UI reads
    - Run: web-ui `python3 -m pytest tests/ -q`; bot `pytest`
    - _Requirements: 2.7, 2.8, 2.9_

- [x] 8. Verify the fix and preservation, then run all gates

  - [x] 8.1 Verify bug-condition exploration tests now pass
    - **Property 1: Expected Behavior** - Per-Guild Source Connect + Bot Identity Fixed
    - **IMPORTANT**: Re-run the SAME tests from Task 1 - do NOT write new tests; update only the assertions that were written as "expected post-fix" (authorize URL not None for configured providers; YouTube stored shape has the three fields; identity routes resolve; infra env present)
    - When these pass, the expected behavior (C1(X)/C2(X) now false) is confirmed
    - Run: web-ui `python3 -m pytest tests/ -q`; bot `pytest` from `bot/playback/`; infra `npx jest`
    - **EXPECTED OUTCOME**: Tests PASS (confirms both defects are fixed)
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 2.8, 2.9_

  - [x] 8.2 Verify preservation tests still pass
    - **Property 2: Preservation** - No Regressions
    - **IMPORTANT**: Re-run the SAME tests from Task 2 - do NOT write new tests
    - Confirm Tidal connect/disconnect, `can_manage_guild` gating, secret isolation + tokens-out-of-DynamoDB, disconnect deletion, global YouTube path, DVD-visualizer avatar read, and tidal/spotify global fallback leaves all still pass
    - Run: web-ui `python3 -m pytest tests/ -q`; bot `pytest` from `bot/playback/`
    - **EXPECTED OUTCOME**: Tests PASS (confirms no regressions)
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7_

  - [x] 8.3 Checkpoint - run all gate commands
    - web-ui: `cd platform/components/web-ui && ruff check --target-version py314 . && python3 -m pytest tests/ -q`, plus `python3 platform/tools/check_line_count.py platform/components/web-ui` (500-line ceiling)
    - infra: `cd platform/infra && npx tsc --noEmit && npx jest`
    - bot playback: run `pytest` from `bot/playback/`
    - Ensure all pass; ask the user if questions arise
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 2.8, 2.9, 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7_

## Notes

### Deployment reality (two paths) - IMPLEMENTATION ONLY, do NOT deploy here

These tasks produce code only. Deployment is called out for planning; do not run
any deploy as part of task execution.

- **CodeCommit push (pipeline rebuilds image):** web-ui source changes (Tasks 4,
  5, 7.1) and bot source changes (Tasks 6, 7.2) - `*.py` + templates. After the
  pipeline pushes a new `:latest` web-ui image, a rollout restart is required for
  running pods to re-pull:
  `KUBECONFIG=/tmp/hellodj-eks-kubeconfig kubectl rollout restart deploy/web-ui -n hellodj-<stage>`
- **`cdk deploy hellodj-eks` (NOT a push):** infra manifest/IAM changes in
  Task 3 (`auth-stack.ts`, `foundation.ts`, `workloads-stack.ts`,
  `bin/hellodj.ts`) - `containerEnv` + IRSA grants live in the `hellodj-eks`
  stack. A plain CodeCommit push does NOT apply these.
- Out-of-band: populate the new `hellodj/<stage>/google-oauth` and
  `hellodj/<stage>/discord-oauth` secret VALUES (created empty by CDK), matching
  the existing empty-secret convention.

### Gate commands (must pass, per design Testing Strategy)

- web-ui: `cd platform/components/web-ui && ruff check --target-version py314 . && python3 -m pytest tests/ -q`; plus `python3 platform/tools/check_line_count.py platform/components/web-ui`
- infra: `cd platform/infra && npx tsc --noEmit && npx jest`
- bot playback: run `pytest` from `bot/playback/` (bare imports rely on cwd on `sys.path`)

## Task Dependency Graph

Execution waves:

```json
{
  "waves": [
    { "wave": 1, "tasks": ["1"], "rationale": "Bug-condition exploration tests written first; expected to fail on unfixed code." },
    { "wave": 2, "tasks": ["2"], "rationale": "Preservation baselines captured on unfixed code; must pass before any fix." },
    { "wave": 3, "tasks": ["3.1", "4.1", "5.1", "7.1"], "rationale": "Independent fix areas: infra env wiring, web-ui clear-error UI, web-ui YouTube capture, web-ui identity service/routes. No cross-dependencies." },
    { "wave": 4, "tasks": ["3.2", "5.2", "6.1", "7.2"], "rationale": "3.2 asserts 3.1's env; 5.2 (Spotify) reuses 5.1's exchange module; 6.1 consumes 5.1's stored YouTube shape; 7.2 applier consumes 7.1's persisted identity." },
    { "wave": 5, "tasks": ["6.2", "7.3"], "rationale": "Unit tests for the bot injector (6.1) and identity service/applier (7.1/7.2)." },
    { "wave": 6, "tasks": ["8.1", "8.2", "8.3"], "rationale": "Re-run exploration (now passing) + preservation (still passing), then all gate commands." }
  ]
}
```

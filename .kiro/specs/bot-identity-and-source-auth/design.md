# Bot Identity and Source Auth Bugfix Design

## Overview

This spec fixes two related defects in the HelloDJ web-ui + bot, both hanging off
the existing per-guild ownership model (`can_manage_guild`) and the isolated
Secrets Manager layout `hellodj/<stage>/guild/<gid>/<provider>`.

**Defect 1 — per-guild source connect only works for Tidal.** In the deployed
web-ui, `SPOTIFY_CLIENT_ID`, `GOOGLE_CLIENT_ID`, and `DISCORD_CLIENT_SECRET`
default to `""` (they are never injected by the CDK workloads-stack, unlike the
Cognito/Discord/invite env). `source_oauth.source_authorize_url(provider, state,
guild_id)` returns `None` for any provider whose client id is empty, and
`auth.source_connect` treats a `None` URL as a silent redirect back to the guild
page — so the Spotify / YouTube "Connect" buttons no-op. Tidal only works because
`TIDAL_CLIENT_ID` was (or is) wired. Even when a YouTube flow does run,
`source_tokens_from_request` captures only `{provider, authorization_code}` for a
`youtube.readonly` scope — which is **not** the OAuth refresh token + PoToken +
visitor data the playback path (`render_lavalink_config.py`,
`bot.py:push_youtube_oauth`) actually needs.

**Defect 2 — no way to choose the bot's per-guild identity.** No UI, route, or
service exists to set the bot's server nickname or per-guild server avatar. The
bot only *reads* its global avatar (`self.bot.user.avatar` in `cogs/video.py`,
DVD visualizer). The web-ui (Flask) is a separate process from the Discord bot,
so the fix must define a cross-process mechanism: the web-ui persists the desired
per-guild identity, and the bot applies it to Discord.

The fix strategy is: (1) wire the source client-id/secret env into web-ui from
Secrets Manager via the workloads-stack; (2) turn silent no-ops into clear errors
and only offer configured providers as connectable; (3) complete a full
code→refresh-token exchange for YouTube in the web-ui and attach a PoToken from
the in-cluster potoken-server, storing a well-defined JSON shape in the guild
secret; (4) extend the bot's `GuildCredentialResolver` + playback path to resolve
and inject per-guild YouTube credentials; (5) add a per-guild bot-identity
UI/route/service in the web-ui that persists the desired identity to
`hellodj-core`, plus a bot-side applier that calls the Discord API. All of this
is done without regressing Tidal connect, ownership gating, secret isolation, the
global YouTube playback path, or the DVD-visualizer avatar read.

## Glossary

- **Bug_Condition (C)**: The condition that triggers a defect. `C1(X)` — a
  controllable guild + provider whose connect does not yield working stored
  per-guild credentials. `C2(X)` — a controllable guild whose bot
  nickname/avatar cannot be set and applied. Defined formally in requirements.md.
- **Property (P)**: The desired behavior for buggy inputs — connect yields
  working stored per-guild credentials (for YouTube: `oauth_refresh_token` +
  `pot_token` + `pot_visitor_data`); identity change takes effect in the guild.
- **Preservation**: Existing behavior that must remain byte-for-byte unchanged
  for non-buggy inputs (Tidal connect, `can_manage_guild` gating, secret
  isolation, disconnect, global YouTube playback, DVD-visualizer avatar read,
  tidal/spotify global fallback leaves).
- **`source_authorize_url(provider, state, guild_id)`**: The web-ui function in
  `platform/components/web-ui/source_oauth.py` that builds a provider OAuth
  authorize URL, returning `None` when the provider's client id is empty (the
  root cause of the silent no-op).
- **`source_tokens_from_request(provider)`**: The web-ui callback extractor in
  `source_oauth.py` that currently returns only `{provider, authorization_code}`.
- **`GuildSourcesService`**: The web-ui service in `guild_sources.py` that writes
  a guild's tokens into the isolated secret and non-secret metadata into the
  DynamoDB `SOURCE#<provider>` item.
- **`guild_source_secret_name(stage, gid, provider)`**: The secret name
  `hellodj/<stage>/guild/<gid>/<provider>`, defined identically in
  `web-ui/guild_sources.py` and `bot/playback/guild_credentials.py`.
- **`GuildCredentialResolver.resolve(guild_id, provider)`**: The bot playback
  resolver in `bot/playback/guild_credentials.py`. `GLOBAL_FALLBACK_LEAVES =
  {tidal: tidal-refresh, spotify: spotify}` — no youtube leaf.
- **`can_manage_guild`**: The pure per-guild ownership gate in
  `guild_admin_service.py`, applied by `guild_routes._can_manage` and
  `auth._guild_source_authorized`.
- **potoken-server**: The in-cluster bgutil-ytdlp-pot-provider service
  (`POST /get_pot` → `{poToken, contentBinding, expiresAt}`) that mints YouTube
  PoTokens + visitor data.
- **workloads-stack**: `platform/infra/lib/workloads-stack.ts`, deployed via
  `cdk deploy hellodj-eks` (NOT a pipeline push), which builds web-ui
  `containerEnv`.
- **per-guild server avatar**: The bot's avatar within one guild (Discord's
  server-member profile avatar), distinct from the global account avatar.
- **server nickname**: The bot's display name within one guild (`guild.me` nick),
  effectively unrate-limited.

## Bug Details

### Bug Condition

The bug manifests along two independent axes. For **Defect 1**, connecting a
non-Tidal provider for a controllable guild does not produce working, stored,
per-guild credentials: `source_authorize_url` returns `None` for Spotify /
YouTube because the client-id env is empty, so `auth.source_connect` silently
redirects back; and even a completed YouTube flow stores only a
`youtube.readonly` authorization code, not the refresh token + PoToken the
playback path needs. For **Defect 2**, an authorized user cannot set the bot's
per-guild nickname/avatar because no capability exists.

**Formal Specification:**

```
FUNCTION isBugCondition(input)
  INPUT: input = (guild g, op) where can_manage_guild(user, g) is true,
         op is either ("connect", provider p) or ("identity", nickname/avatar)
  OUTPUT: boolean

  IF op is ("connect", p) THEN
    // Defect 1 — C1(X). "Working" for youtube/youtube_music specifically
    // requires the stored secret to include oauth_refresh_token + pot_token
    // (+ pot_visitor_data), NOT merely a youtube.readonly authorization code.
    RETURN NOT connectYieldsWorkingGuildCredentials(F, g, p)
  ELSE  // op is ("identity", ...)
    // Defect 2 — C2(X).
    RETURN NOT canSetAndApplyGuildBotIdentity(F, g)
  END IF
END FUNCTION
```

Under **F** (current system): `C1(X)` is true for every provider except `tidal`;
`C2(X)` is true for every controllable guild. The fix **F'** makes both false.

### Examples

- **Spotify no-op (1.1, 1.2):** Authorized user opens `/guilds/<gid>`, clicks
  "Connect" on Spotify. Expected: redirect to Spotify authorize URL, tokens
  stored at `hellodj/<stage>/guild/<gid>/spotify`. Actual: `SPOTIFY_CLIENT_ID`
  is `""` → `source_authorize_url` returns `None` → `auth.source_connect`
  redirects back to `/guilds/<gid>` with no error, no redirect, nothing stored.
- **YouTube wrong-token (1.3, 1.4):** User clicks "Connect" on YouTube. If
  `GOOGLE_CLIENT_ID` empty → same silent no-op. If it did run, callback stores
  `{provider: "youtube", authorization_code: "4/0A..."}` — a `youtube.readonly`
  code. Expected: `{oauth_refresh_token, pot_token, pot_visitor_data}`.
- **Bot playback can't resolve per-guild YouTube (1.5):**
  `GuildCredentialResolver` has no youtube global fallback leaf and no per-guild
  capture path produced usable youtube tokens, so a guild's YouTube playback
  credentials cannot be resolved.
- **No identity capability (1.6, 1.7):** User wants the bot to appear as
  "DJ Vinyl" with a custom avatar in their guild. Expected: a form + apply.
  Actual: no UI/route/service exists at all.
- **Edge — bot lacks permission (2.9):** User submits a nickname/avatar but the
  bot lacks Manage Nicknames / the guild membership PATCH permission. Expected:
  a clear error surfaced to the user, not a silent failure.
- **Tidal still works (3.1, unchanged):** `TIDAL_CLIENT_ID` set → connect
  redirects and stores at `hellodj/<stage>/guild/<gid>/tidal` exactly as today.

## Expected Behavior

### Preservation Requirements

**Unchanged Behaviors:**

- Tidal per-guild connect/disconnect completes and stores/removes credentials in
  `hellodj/<stage>/guild/<gid>/tidal` exactly as today (3.1).
- Every per-guild source connect/disconnect and every identity route is gated
  through `can_manage_guild` and rejects callers who do not control the guild
  (3.2).
- Guild source tokens stay isolated in `hellodj/<stage>/guild/<gid>/<provider>`;
  DynamoDB `SOURCE#<provider>` items hold only non-secret metadata (3.3).
- Disconnect deletes the guild's isolated secret and its `SOURCE#<provider>`
  metadata (3.4).
- The bot plays YouTube for guilds *without* their own YouTube secret via the
  existing GLOBAL OAuth+PoToken single `POST /youtube` path (3.5).
- The bot reads its own Discord avatar for the DVD visualizer
  (`self.bot.user.avatar` in `cogs/video.py`) unaffected (3.6).
- The resolver's global fallback leaves for `tidal` (`tidal-refresh`) and
  `spotify` (`spotify`) still apply for guilds without a per-guild secret (3.7).

**Scope:**

All inputs where neither `C1(X)` nor `C2(X)` holds must be completely unaffected.
Specifically: any provider that is already Tidal; any guild that already has a
working per-guild secret; the bot's global YouTube playback path; the global
avatar read; and any caller who fails `can_manage_guild` (they were rejected
before and remain rejected).

_The concrete correct behavior for buggy inputs is defined in the Correctness
Properties section (Property 1). This section enumerates what must NOT change._

## Hypothesized Root Cause

Based on reading the actual code, the causes are well understood (not merely
hypothesized) for Defect 1, and structural for Defect 2:

1. **Empty client-id env (Defect 1 primary root cause).** `app.py` reads
   `SPOTIFY_CLIENT_ID` / `GOOGLE_CLIENT_ID` / `TIDAL_CLIENT_ID` /
   `DISCORD_CLIENT_SECRET` from `os.getenv(..., "")`. The workloads-stack
   `containerEnv` for `web-ui` injects Cognito/Discord-client-id/invite env but
   **never** these four. So `source_authorize_url` returns `None` for Spotify and
   YouTube and the connect button no-ops (`auth.source_connect` treats
   `authorize_url is None` as "land back on the guild").

2. **Silent no-op instead of a clear error.** `auth.source_connect` redirects to
   `guild.guild_detail` when `authorize_url` is `None`, with no user-visible
   signal. The UI (`guild_source_list.html`) always renders "Connect" as an
   active link regardless of whether the provider is configured.

3. **Wrong YouTube token kind (Defect 1, YouTube).** The Google authorize URL
   requests `youtube.readonly`; `source_tokens_from_request` stores only the raw
   `authorization_code`. There is no code→token exchange in the web-ui and no
   PoToken acquisition, so the guild secret can never hold the
   `oauth_refresh_token` + `pot_token` + `pot_visitor_data` the playback path
   requires.

4. **No per-guild YouTube resolution / injection path (Defect 1, playback).**
   `GuildCredentialResolver` deliberately has no youtube global fallback leaf,
   and nothing constructs the resolver in the bot playback path today (the class
   exists and is unit-tested but is not wired into `player.py` / `bot.py`). The
   bot's `push_youtube_oauth` reads only the global `youtube.*` credential-store
   keys and pushes a single global `POST /youtube` — there is no per-guild
   injection.

5. **No bot-identity capability at all (Defect 2).** There is no route, service,
   storage item, or bot-side applier for a per-guild nickname/avatar. The web-ui
   is a separate process from the Discord bot, so a cross-process handoff must be
   designed.

## Correctness Properties

Property 1: Bug Condition — per-guild source connect yields working credentials

_For any_ input `X = (g, p)` where the bug condition holds (`isBugCondition`
returns true for a connect op — i.e. connecting provider `p` for controllable
guild `g` does not currently yield working stored per-guild credentials), the
fixed system SHALL offer `p` as connectable, complete the connect flow without a
silent no-op, and store working per-guild credentials at
`hellodj/<stage>/guild/<g>/<p>`; and _for any_ `p in {youtube, youtube_music}`
the stored credentials SHALL include `oauth_refresh_token`, `pot_token`, and
`pot_visitor_data`, which the bot's `GuildCredentialResolver` SHALL resolve and
the playback path SHALL use for that guild.

**Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.5, 2.6**

Property 2: Bug Condition — per-guild bot identity takes effect

_For any_ input `X = (g, identity)` where the bug condition holds
(`isBugCondition` returns true for an identity op — i.e. an authorized user wants
to set the bot's per-guild nickname and/or avatar for controllable guild `g`),
the fixed system SHALL persist the requested identity and the bot SHALL apply it
via the Discord API so the bot's server nickname and/or per-guild server avatar
in `g` reflect the requested value; and when the bot lacks the required guild
permission the system SHALL surface a clear error rather than failing silently.

**Validates: Requirements 2.7, 2.8, 2.9**

Property 3: Preservation — non-buggy inputs unchanged

_For any_ input where the bug condition does NOT hold (`isBugCondition` returns
false), the fixed system SHALL produce the same result as the original system,
preserving Tidal connect/disconnect, `can_manage_guild` gating, per-guild secret
isolation with tokens-out-of-DynamoDB, disconnect deletion, the global YouTube
OAuth+PoToken playback path for guilds without their own YouTube secret, the
DVD-visualizer avatar read, and the tidal/spotify global fallback leaves.

**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7**

## Fix Implementation

### Change area A — web-ui env wiring (Defect 1 primary; R2.6)

**Files:** `platform/infra/lib/auth-stack.ts`,
`platform/infra/lib/foundation.ts`, `platform/infra/lib/pipeline-stack.ts`,
`platform/infra/lib/workloads-stack.ts`, `platform/infra/bin/hellodj.ts`.

1. **Secrets in AuthStack.** `spotifySecret` (`hellodj/<stage>/spotify`) and
   `discordBotTokenSecret` already exist; `tidalRefreshSecret` exists. Add two
   new Secrets Manager entries (created empty, populated out-of-band like the
   others): a **Google/YouTube OAuth client secret** (`hellodj/<stage>/google`
   holding `{client_id, client_secret}`) and, if not already present, a
   **Discord OAuth client secret** value (the existing `discord-bot-token` secret
   is the *bot* token; the OAuth *client secret* for the web-ui callback token
   exchange belongs in its own `hellodj/<stage>/discord-oauth` secret holding
   `{client_id, client_secret}`). Spotify client id + secret live in the existing
   `spotify` secret JSON; Tidal client id lives in the existing `tidal-refresh`
   secret JSON (or a sibling — match whatever the sidecars already read).

2. **Thread the values into web-ui `containerEnv`.** In `workloads-stack.ts`
   `containerEnv`, inside the `if (spec.name === 'web-ui')` block, push:
   `SPOTIFY_CLIENT_ID`, `GOOGLE_CLIENT_ID`, `TIDAL_CLIENT_ID`,
   `DISCORD_CLIENT_SECRET`, and (for the YouTube exchange, see area C)
   `GOOGLE_CLIENT_SECRET`. Two mechanisms are available and both match existing
   patterns in this file:
   - **Plain env from a prop value** (like `cognitoClientId` /
     `discordClientId`): thread the client-id *values* through
     `WorkloadsStackProps` → `FoundationRefs` → `bin/hellodj.ts`.
   - **`valueFrom.secretKeyRef`** (like `FLASK_SECRET_KEY`): if the value is a
     secret (client secrets), surface it into a per-stage Kubernetes `Secret`
     analogous to `web-ui-flask-secret` and reference it via `secretKeyRef`.
     **Chosen approach:** client **ids** are not sensitive → inject as plain env
     values threaded via props; client **secrets** (`GOOGLE_CLIENT_SECRET`,
     `DISCORD_CLIENT_SECRET`) → inject via a per-stage `web-ui-oauth-secret`
     Kubernetes Secret populated from the AuthStack secrets, referenced by
     `secretKeyRef`, so no secret value lands in a CloudFormation env literal.

3. **Grant read.** Extend the web-ui IRSA role grants in `grantDependencies` (or
   the existing `if (spec.name === 'web-ui')` grant block) to `grantRead` the new
   google/discord-oauth secrets, scoped to their ARNs (least privilege).

4. **Deployment note.** These are infra manifest/IAM changes → applied via
   `cd platform/infra && npx cdk deploy hellodj-eks` (NOT a pipeline push), per
   steering. `app.py` already reads the env vars, so no web-ui image rebuild is
   needed for the wiring itself.

### Change area B — clear error + configured-only UI (Defect 1; R2.1, R1.2)

**Files:** `platform/components/web-ui/auth.py`,
`platform/components/web-ui/source_oauth.py`,
`platform/components/web-ui/guild_sources.py`,
`platform/components/web-ui/guild_routes.py`,
`templates/partials/guild_source_list.html`.

1. **Surface a clear error, not a silent no-op.** In `auth.source_connect`, when
   `source_authorize_url` returns `None`, redirect to
   `guild.guild_detail(guild_id=..., error="provider_not_configured&provider=<p>")`
   (or flash an error) so the guild page renders a visible message instead of a
   no-op.

2. **Expose provider-configured state to the template.** Add a small helper
   (e.g. `source_provider_configured(provider)` in `source_oauth.py`) that
   returns whether the relevant client id/secret is present in
   `current_app.config`. `guild_routes.guild_detail` passes a
   `providers_configured` map into the template alongside `sources`.

3. **Offer all supported providers, disable the unconfigured (R2.1).**
   `guild_source_list.html`: for a not-connected provider, render an active
   "Connect" link when configured, or a disabled "Needs setup" button + tooltip
   when not configured. All four supported providers
   (`youtube, youtube_music, tidal, spotify`) always appear (they already do via
   `sources.status`, which iterates `SUPPORTED_PROVIDERS`).

### Change area C — YouTube full OAuth + PoToken capture (Defect 1, YouTube; R2.3, R2.4)

**Files:** `platform/components/web-ui/source_oauth.py`,
`platform/components/web-ui/auth.py`, `platform/components/web-ui/guild_sources.py`.

1. **Request offline access with the right scope.** The Google authorize URL
   already sets `access_type=offline`; add `prompt=consent` so a refresh token is
   reliably returned on re-consent, and keep the scope minimal for the playback
   use case. (Scope choice is orthogonal to obtaining a refresh token; the
   refresh token is what playback needs.)

2. **Complete the code→refresh-token exchange in the web-ui.** For
   `youtube`/`youtube_music`, `source_tokens_from_request` (or a new
   `exchange_google_code(...)` helper) performs a server-side POST to
   `https://oauth2.googleapis.com/token` with `grant_type=authorization_code`,
   the `code`, `redirect_uri`, `GOOGLE_CLIENT_ID`, and `GOOGLE_CLIENT_SECRET`,
   yielding `refresh_token`. **Decision:** the **web-ui holds
   `GOOGLE_CLIENT_SECRET`** and performs the exchange, because — unlike
   Spotify/Tidal which have dedicated streaming sidecars that own their client
   secrets and complete the exchange — there is no per-guild YouTube sidecar, and
   the playback path consumes a refresh token that must be minted at connect
   time. (Injecting `GOOGLE_CLIENT_SECRET` into web-ui is covered by area A.)

3. **Obtain a PoToken from the potoken-server.** After the token exchange, the
   web-ui POSTs to the in-cluster potoken-server `POST /get_pot` (URL from env,
   default `http://potoken-server.<namespace>.svc.cluster.local:4416`, mirroring
   the bot's `POTOKEN_SERVER_URL`), reading `poToken` and `contentBinding`
   (visitor data) from the response — identical shape to the bot's
   `fetch_and_push_potoken`.

4. **Store the well-defined JSON shape.** `GuildSourcesService.store_tokens`
   writes to `hellodj/<stage>/guild/<gid>/<provider>` the exact object the bot
   resolver reads:

   ```json
   {
     "provider": "youtube",
     "oauth_refresh_token": "1//0g...",
     "pot_token": "MnQ...",
     "pot_visitor_data": "Cgs...",
     "connected_by": "<cognito-sub>",
     "connected_at": 1730000000
   }
   ```

   For `spotify`/`tidal` the stored shape is whatever the sidecars complete (see
   area D); the *metadata-only* DynamoDB `SOURCE#<provider>` item is unchanged.

5. **PoToken freshness (design risk, flagged).** PoTokens expire. The connect-time
   PoToken bootstraps the guild secret; a bot-side periodic refresh per connected
   guild is the durable answer (area E, item 4). The connect flow stores what it
   obtains; staleness is handled on the bot side.

### Change area D — Spotify + Tidal per-guild (Defect 1; R2.2, preserve 3.1)

**Confirmation from the code:** `source_oauth.py`'s docstring states the
provider's streaming sidecar completes the code→token exchange (it owns the
client secret) against the guild's isolated secret. For **Tidal**, this is the
already-working path (`auth.tidal_callback` forwards to `tidal-stream`); it must
be preserved exactly (3.1). For **Spotify**, once `SPOTIFY_CLIENT_ID` is wired
(area A), the connect flow redirects and the callback stores the authorization
code; the **spotify-stream** sidecar completes the exchange against the guild
secret, matching the documented model. **Design decision:** keep Spotify/Tidal on
the sidecar-completes-exchange model (no new web-ui exchange for them); only
YouTube needs the web-ui to complete the exchange (area C) because it has no
per-guild sidecar. If verification shows the spotify-stream sidecar does **not**
read the per-guild secret to complete the exchange, that is a follow-up on the
sidecar — flagged, but out of scope for the web-ui change here.

### Change area E — bot per-guild YouTube resolution + injection (Defect 1, playback; R2.5)

**Files:** `bot/playback/guild_credentials.py`, `bot/bot.py`, `bot/player.py`.

1. **Resolver already isolates per-guild secrets.**
   `GuildCredentialResolver.resolve(guild_id, "youtube")` reads
   `hellodj/<stage>/guild/<gid>/youtube` first; there is intentionally no youtube
   global fallback leaf (preserved — 3.5/3.7 rely on this). The stored shape from
   area C is directly consumable. No change to the fallback map.

2. **Wire the resolver into the playback path (currently unwired).** Construct a
   `GuildCredentialResolver` (with a boto3 secretsmanager client + stage) once at
   bot startup and make it reachable from the play path (e.g. on the bot object /
   a module singleton). This is the missing link identified in root cause 4.

3. **Per-guild YouTube injection into Lavalink (DESIGN RISK — see Risks).** The
   current model is a single global `POST /youtube` on one shared Lavalink node;
   the plugin replaces ALL fields each call, so it cannot simultaneously hold
   distinct per-guild OAuth+PoToken sets. The chosen **minimal viable path**:
   before starting playback for a guild that has its own YouTube secret, resolve
   that guild's `{oauth_refresh_token, pot_token, pot_visitor_data}` and
   `POST /youtube` with those values to the node about to serve the track (a
   just-in-time credential swap), then play. Guilds without a per-guild secret
   fall through to the existing global push untouched (3.5). This is
   last-writer-wins on a shared node and does **not** give true concurrent
   per-guild isolation on one Lavalink; the fully-isolated answer (per-guild
   node/session pool) is documented as a design risk and deferred. The design
   does not hand-wave a per-request credential field that the plugin does not
   expose.

4. **Per-guild PoToken refresh (bot side).** Extend the existing
   `_potoken_refresh_task` pattern so that, for each guild with a YouTube secret,
   a fresh PoToken is fetched from the potoken-server and written back to that
   guild's secret (bot has read-only on `hellodj/<stage>/guild/*` today — this
   needs a scoped write grant, or the refresh is done by the web-ui path;
   **flagged** as an IAM decision). Minimal viable path: refresh at
   just-in-time-inject time using the guild's stored visitor data, without
   persisting, so no new write grant is required.

### Change area F — per-guild bot identity (Defect 2; R2.7, R2.8, R2.9)

**Files (web-ui):** new `platform/components/web-ui/bot_identity.py`,
`guild_routes.py`, `templates/pages/guild_detail.html`, new
`templates/partials/guild_identity_form.html`.
**Files (bot):** new `bot/bot_identity_apply.py` (applier) + a background poll or
event hook in `bot.py`; `bot/cogs/video.py` avatar read left untouched.

1. **UI + route (web-ui).** Add an "Identity" tab to `guild_detail.html` with a
   form: text input for nickname, file input for avatar (PNG/JPG/GIF). Add routes
   `POST /guilds/<gid>/identity/nickname` and `POST /guilds/<gid>/identity/avatar`
   in `guild_routes.py`, **each gated by `_can_manage(guild_id)`** exactly like
   the source routes (3.2). On submit the route calls a `BotIdentityService`.

2. **Cross-process handoff via `hellodj-core`.** The web-ui is not the Discord
   bot, so it cannot call Discord directly at request time. `BotIdentityService`
   (new `bot_identity.py`) persists the desired identity as a DynamoDB item under
   the guild PK with sort key `BOTIDENTITY` — metadata only:

   ```json
   {
     "PK": "GUILD#<gid>", "SK": "BOTIDENTITY",
     "entity": "GuildBotIdentity",
     "data": {
       "nickname": "DJ Vinyl",
       "avatar_key": "guild/<gid>/bot-avatar/<hash>.png",
       "desired_at": 1730000000,
       "requested_by": "<cognito-sub>",
       "applied_at": 0,
       "apply_status": "pending",
       "apply_error": ""
     }
   }
   ```

   **Avatar image bytes storage (decision):** avatar bytes do **not** go in
   DynamoDB (item-size limits) or a secret (not a credential). They go in an
   S3 object at `avatar_key` in a stage-scoped bucket the web-ui can write and
   the bot can read (IRSA grants both). Constraints enforced at upload:
   format in {PNG, JPG, GIF}, max 256 KB (Discord's server-avatar limit is
   generous but 256 KB is a safe ceiling), square recommended. The bot converts
   the S3 object to a base64 data URI when calling Discord.

3. **Bot-side applier.** A bot component (`bot_identity_apply.py`) reads pending
   `BOTIDENTITY` items (via a periodic poll on the existing watchdog cadence, or
   an on-demand trigger), and for each guild applies:
   - **Nickname:** `await guild.me.edit(nick=nickname)` (discord.py, supported;
     requires the bot's Change Nickname permission — for its own nick this is
     usually held).
   - **Per-guild server avatar (DESIGN RISK — API availability):** the installed
     stack is **discord.py** (`discord.py[voice]`, wavelink ≥3.5). discord.py does
     **not** expose editing the bot's *own per-guild member (server) avatar* via
     a stable public method — `Member.edit` cannot set the current member's guild
     avatar, and `ClientUser.edit(avatar=...)` changes the **global** account
     avatar (rate-limited, out of scope per requirements). **Chosen approach:**
     issue a direct REST call `PATCH /guilds/{guild_id}/members/@me` with body
     `{"avatar": "data:image/png;base64,<...>"}` using the bot's existing
     authenticated HTTP session (`self.bot.http.request(discord.http.Route(...))`
     with the bot token already on the session). This uses the documented Discord
     endpoint without inventing a discord.py method. If a future discord.py
     exposes it natively, swap to that.
   - On success set `apply_status="applied"`, `applied_at`, clear `apply_error`.

4. **Permission-denied clear error (R2.9).** If `guild.me.edit` or the REST PATCH
   raises `discord.Forbidden` (bot lacks Manage Nicknames / the guild permission),
   the applier records `apply_status="error"` + a human-readable `apply_error`.

5. **Confirm back to the UI.** The web-ui identity tab reads the `BOTIDENTITY`
   item's `apply_status` / `apply_error` and renders "Pending", "Applied", or the
   error message (HTMX poll or on next page load). This closes the loop without a
   synchronous bot call.

6. **Preserve the DVD-visualizer avatar read (3.6).** `cogs/video.py`'s
   `self.bot.user.avatar` read is the **global** account avatar and is entirely
   independent of per-guild identity; it is not modified.

## Testing Strategy

### Validation Approach

Two-phase: first surface counterexamples that demonstrate each defect on the
current (unfixed) code, then verify the fix produces working per-guild
credentials / identity and preserves all non-buggy behavior. All tests use fakes
(no live AWS / Discord / potoken-server / Lavalink), consistent with the existing
`bot/playback/test_guild_credentials.py` and web-ui `tests/` style. Gate commands:
web-ui `ruff check --target-version py314 . && python3 -m pytest tests/ -q` +
`python3 platform/tools/check_line_count.py platform/components/web-ui` (500-line
ceiling); infra `npx tsc --noEmit && npx jest`; bot playback tests run from
`bot/playback/`.

### Exploratory Bug Condition Checking

**Goal:** Surface counterexamples that demonstrate the bugs BEFORE the fix, and
confirm/refute the root cause. If refuted, re-hypothesize.

**Test Plan:** Drive the actual functions with fakes and assert the buggy
outcome on unfixed code.

**Test Cases:**
1. **Spotify no-op:** with `SPOTIFY_CLIENT_ID=""`, assert
   `source_authorize_url("spotify", state, gid)` returns `None` and
   `auth.source_connect` redirects to the guild page with nothing stored (fails
   the fixed expectation). (1.1, 1.2)
2. **YouTube no-op:** with `GOOGLE_CLIENT_ID=""`, same assertion for `youtube`.
   (1.3)
3. **YouTube wrong token:** with a fake callback `?code=...`, assert
   `source_tokens_from_request("youtube")` returns only
   `{provider, authorization_code}` (no `oauth_refresh_token`/`pot_token`). (1.4)
4. **Resolver no per-guild youtube:** assert `GuildCredentialResolver` has no
   youtube fallback leaf and `resolve(gid, "youtube")` is `None` when no guild
   secret exists (edge; documents 1.5, and must stay true for guilds without a
   secret — 3.5/3.7).
5. **No identity capability:** assert no route/service for per-guild identity
   exists (a `url_for('guild.set_nickname', ...)` lookup raises before the fix).
   (1.6, 1.7)

**Expected Counterexamples:** silent `None` authorize URLs; a stored
authorization-code-only YouTube secret; absent identity routes.

### Fix Checking

**Goal:** For all inputs where the bug condition holds, the fixed function
produces the expected behavior.

**Pseudocode:**
```
FOR ALL input WHERE isBugCondition(input) DO
  IF input is ("connect", p) THEN
    ASSERT p is offered as connectable in the guild source UI
    result := connectFlow_fixed(g, p)
    ASSERT result completes without silent no-op
    ASSERT workingCredentialsStored(hellodj/<stage>/guild/<g>/<p>)
    IF p IN {youtube, youtube_music} THEN
      ASSERT stored has oauth_refresh_token AND pot_token AND pot_visitor_data
      ASSERT GuildCredentialResolver.resolve(g, p) returns those tokens
    END IF
  ELSE  // ("identity", ...)
    result := setGuildBotIdentity_fixed(g, identity)
    ASSERT applier set nickname via guild.me.edit AND/OR avatar via REST PATCH
  END IF
END FOR
```

### Preservation Checking

**Goal:** For all inputs where the bug condition does NOT hold, the fixed system
produces the same result as the original.

**Pseudocode:**
```
FOR ALL input WHERE NOT isBugCondition(input) DO
  ASSERT F(input) = F'(input)
END FOR
```

**Testing Approach:** Property-based testing (hypothesis, already used in this
repo per `.hypothesis/`) is recommended for preservation, generating many
non-buggy inputs across the domain (arbitrary providers reduced to Tidal /
already-connected, arbitrary callers including non-managers, arbitrary guilds
without a per-guild YouTube secret) and asserting behavior is unchanged.

**Test Plan:** Observe behavior on UNFIXED code first (Tidal connect, gating,
disconnect, global YouTube resolution `None`), then write tests capturing it.

**Test Cases:**
1. **Tidal connect preserved:** connect/disconnect Tidal stores/removes at
   `hellodj/<stage>/guild/<gid>/tidal` exactly as before. (3.1)
2. **Ownership gating preserved:** a caller failing `can_manage_guild` is
   rejected on every source and identity route. (3.2)
3. **Secret isolation preserved:** tokens only in the per-guild secret;
   `SOURCE#<provider>` DynamoDB item holds only metadata. (3.3)
4. **Disconnect preserved:** deletes the guild secret + `SOURCE#<provider>`. (3.4)
5. **Global YouTube playback preserved:** for a guild with no YouTube secret,
   resolver returns `None` and the bot uses the global `push_youtube_oauth`
   single `POST /youtube`. (3.5)
6. **Global fallback leaves preserved:** tidal→`tidal-refresh`, spotify→`spotify`
   still resolve for guilds without a per-guild secret; youtube still has none.
   (3.7)
7. **DVD-visualizer avatar read preserved:** `cogs/video.py` still reads
   `self.bot.user.avatar`. (3.6)

### Unit Tests

- **web-ui:** `source_authorize_url` returns a URL for each configured provider
  and a clear-error path (not `None`-swallowed) when unconfigured; the YouTube
  callback stores `oauth_refresh_token` + `pot_token` + `pot_visitor_data` using
  a **fake Google token exchange** and a **fake potoken-server**;
  `guild_detail` passes a `providers_configured` map; `BotIdentityService`
  persists the `BOTIDENTITY` item + uploads avatar bytes to a **fake S3**; all
  identity routes reject non-managers.
- **bot:** `GuildCredentialResolver.resolve(gid, "youtube")` returns the stored
  per-guild tokens; the just-in-time injector POSTs the guild's tokens to a
  **fake Lavalink** `/youtube`; the identity applier sets nickname via a **fake
  Discord client** and avatar via a fake REST route, including the
  `discord.Forbidden` → clear-error path (R2.9).

### Property-Based Tests

- Generate random providers and guild states; assert the connect UI offers all
  supported providers and only configured ones are actively connectable.
- Generate random non-buggy inputs (non-managers, guilds without per-guild
  secrets, Tidal) and assert `F(X) = F'(X)` (preservation).
- Generate random stored per-guild YouTube secrets and assert the resolver +
  injector round-trip the three fields without cross-guild leakage.

### Integration Tests

- **CDK jest:** assert the synthesized web-ui container env now includes
  `SPOTIFY_CLIENT_ID`, `GOOGLE_CLIENT_ID`, `TIDAL_CLIENT_ID`, and
  `DISCORD_CLIENT_SECRET` (the latter two/secret ones via `secretKeyRef` to the
  per-stage `web-ui-oauth-secret`), and that the web-ui role is granted read on
  the new google/discord-oauth secrets.
- **web-ui flow:** end-to-end YouTube connect with fakes — authorize redirect →
  callback → Google exchange → potoken-server → secret written with the full
  shape → resolver reads it.
- **bot identity:** end-to-end — web-ui writes `BOTIDENTITY` + S3 avatar → applier
  polls → nickname + avatar applied on fake Discord → `apply_status` flows back.

## Design Risks

1. **Per-guild YouTube credential injection into a shared Lavalink (highest
   risk).** The youtube-source plugin's `POST /youtube` replaces all fields and
   there is one shared node. True concurrent per-guild isolation would require a
   per-guild node/session pool, which is a larger change. **Chosen minimal viable
   path:** just-in-time last-writer-wins credential swap per playback start for
   guilds with their own YouTube secret; global path untouched for the rest. The
   limitation (no simultaneous distinct per-guild YouTube auth on one node) is
   explicit, not hand-waved.
2. **discord.py per-guild server-avatar API.** The installed discord.py does not
   expose setting the bot's own per-guild member avatar via a public method.
   **Chosen approach:** documented raw REST `PATCH /guilds/{guild_id}/members/@me`
   with a base64 data URI via the bot's authenticated HTTP session. Nickname uses
   the supported `guild.me.edit(nick=...)`.
3. **Bot write access for per-guild PoToken refresh.** The bot has read-only IAM
   on `hellodj/<stage>/guild/*`. Persisting a refreshed per-guild PoToken would
   need a scoped write grant. **Chosen minimal viable path:** refresh at
   inject-time without persisting (no new write grant); a scoped write grant is a
   flagged follow-up if persistence is desired.
4. **Who holds `GOOGLE_CLIENT_SECRET`.** Decided: the **web-ui** holds it and
   performs the YouTube code→token exchange (no per-guild YouTube sidecar exists).
   This is the deviation from the Spotify/Tidal sidecar-completes-exchange model
   and is justified by the absence of a YouTube sidecar and the need to mint a
   refresh token at connect time.
5. **Spotify/Tidal sidecar exchange assumption.** The design keeps
   Spotify/Tidal on the documented sidecar-completes-exchange model. If
   verification shows the spotify-stream sidecar does not read the per-guild
   secret to complete the exchange, a sidecar follow-up is needed (flagged).

## Fix Implementation

Changes are grouped by defect and by deployment path (source vs infra), because
they deploy differently (see Deployment). Assuming the root-cause analysis above
is correct (confirmed by the exploratory tests first).

### Defect 1a — web-ui client-id/secret env wiring (infra: `cdk deploy hellodj-eks`)

**File**: `platform/infra/lib/workloads-stack.ts`

Mirror the existing `INVITE_SENDER` / Cognito / Discord-client wiring inside
`containerEnv(spec)` under the `if (spec.name === 'web-ui')` block. The web-ui
needs the OAuth **client ids** in plain env (they are not secret) and the OAuth
**client secrets** resolved from Secrets Manager. Two honest sub-decisions:

1. **Client ids** (`SPOTIFY_CLIENT_ID`, `GOOGLE_CLIENT_ID`, `TIDAL_CLIENT_ID`)
   are public identifiers. Add them as new optional `WorkloadsStackProps`
   (`spotifyClientId?`, `googleClientId?`, `tidalClientId?`) threaded from
   `bin/hellodj.ts`, pushed as plain `{ name, value }` env for the web-ui,
   exactly like `discordClientId`. When unset, they stay `""` (degraded, current
   behavior) — so the change is additive.

2. **Client secrets** (`DISCORD_CLIENT_SECRET`, and the Spotify/Google client
   secrets needed for the code→token exchange — see Defect 1b) live in Secrets
   Manager. The pattern in this repo is to pass the **secret ARN** in env and
   have the pod resolve it via its IRSA role at runtime (e.g.
   `HELLODJ_SPOTIFY_SECRET_ARN`). We follow that pattern rather than injecting
   raw secret values into env (keeps secret material out of the pod spec).

   - **auth-stack inventory (verified in `auth-stack.ts`):**
     - `discord-bot-token` — exists (bot token, NOT the OAuth client secret).
     - `tidal-refresh` — exists (Tidal refresh token; Tidal client id/secret used
       by the sidecar).
     - `spotify` — exists (described "Spotify credentials"; intended to hold
       client id + client secret).
     - `yt-cipher-secret`, `web-ui-flask-session` — exist.
     - **Missing and MUST be added:** a **Discord OAuth client secret** secret
       (`hellodj/<stage>/discord-oauth` — distinct from the bot token) and a
       **Google/YouTube OAuth client secret** secret
       (`hellodj/<stage>/google-oauth`). The Spotify client secret can reuse the
       existing `spotify` secret (store `{client_id, client_secret}` JSON).
   - Add these two secrets to `auth-stack.ts` as new
     `secretsmanager.Secret` properties, created empty (values populated
     out-of-band, matching the existing convention), and expose them on
     `WorkloadsSecretRefs`.
   - In `grantDependencies` for `web-ui`, add `grantRead` for the spotify,
     google-oauth, and discord-oauth secrets (in addition to the existing
     per-guild `hellodj/<stage>/guild/*` RW grant).
   - In `containerEnv` for `web-ui`, push the ARNs:
     `HELLODJ_SPOTIFY_SECRET_ARN`, `HELLODJ_GOOGLE_OAUTH_SECRET_ARN`,
     `HELLODJ_DISCORD_OAUTH_SECRET_ARN`, and the plain client-id values.

**File**: `platform/components/web-ui/app.py` (`_configure`) and
`platform/components/web-ui/bootstrap.py`

- `_configure` already reads `SPOTIFY_CLIENT_ID` / `GOOGLE_CLIENT_ID` /
  `TIDAL_CLIENT_ID` / `DISCORD_CLIENT_SECRET` from env into `app.config`. Once
  the env is injected (above), `source_authorize_url` stops returning `None` for
  configured providers (2.6). No `_configure` change is required for the ids;
  add reads for the new secret-ARN env only if the exchange runs in web-ui (see
  1b decision).
- The **client secrets** for the exchange are resolved lazily from Secrets
  Manager (via the existing `SecretsProvider` / a small helper in
  `secrets_store.py`), keyed by the injected ARN — never placed in `app.config`
  as plaintext at import time.

### Defect 1b — Spotify: where the code→token exchange happens

**Decision: the web-ui performs the Spotify code→token exchange** (option (a)),
not the sidecar (option (b)). Justification, grounded in facts:

- The `spotify-stream` sidecar owns the client secret today for *its own* global
  Spotify auth, but there is **no existing plumbing** for the web-ui to hand a
  per-guild authorization code to the sidecar and have it exchange+store into an
  arbitrary `hellodj/<stage>/guild/<gid>/spotify` secret. The current design
  comment in `source_oauth.py` asserting "the sidecar completes the exchange" is
  aspirational — no such per-guild sidecar endpoint exists (verified: no
  per-guild exchange route in the sidecar path).
- The web-ui already holds AWS Secrets Manager RW on `hellodj/<stage>/guild/*`
  and is the writer of every Per_Guild_Secret. Doing the exchange in the web-ui
  keeps the write in one place and avoids inventing a new sidecar RPC.
- The exchange is a single server-side HTTPS POST to
  `https://accounts.spotify.com/api/token` with `grant_type=authorization_code`;
  the web-ui resolves the Spotify client secret from Secrets Manager
  (`HELLODJ_SPOTIFY_SECRET_ARN`) at callback time.

**Token shape the bot/sidecar must READ.** `guild_credentials.resolve(guild,
"spotify")` returns the parsed secret dict. The global spotify fallback and the
`spotify-stream` sidecar consume Spotify credentials; the per-guild secret SHALL
store the offline **refresh token** plus the client id so the consumer can mint
access tokens:

```json
{ "provider": "spotify", "refresh_token": "<offline refresh token>",
  "access_token": "<short-lived>", "expires_at": <epoch>,
  "scope": "user-read-playback-state streaming", "obtained_at": <epoch> }
```

The key the consumer relies on is `refresh_token` (long-lived); `access_token`/
`expires_at` are a convenience cache. This mirrors the existing global spotify
fallback semantics (refresh-token-centric).

**Changes:**

- `source_oauth.py`: keep `source_authorize_url` for spotify as-is (scopes
  unchanged). Replace `source_tokens_from_request` for spotify with a real
  exchange: add `source_exchange_spotify(code, guild_id) -> dict` that resolves
  the client id + client secret (Secrets Manager), POSTs the token endpoint, and
  returns the token dict above. (Splitting into a new function keeps the module
  under 500 lines and keeps `source_tokens_from_request` a thin dispatcher.)
- `auth.py:source_callback`: for spotify, call the exchange, then
  `guild_sources.store_tokens(guild_id, "spotify", tokens, connected_by=...)` —
  unchanged storage path (2.2, preserves 3.3).

### Defect 1b — YouTube / YouTube Music: refresh token + PoToken + visitor data

This is the important one. Three things must be captured and stored together in
`hellodj/<stage>/guild/<gid>/<provider>` (2.4):

1. **`oauth_refresh_token`** — from a **full offline OAuth grant**, not
   `youtube.readonly`. The youtube-source plugin's OAuth is the **TV-client**
   flow (steering "YouTube Playback Pipeline": TV is the only OAuth-capable
   client; the global flow already stores `youtube.oauth_refresh_token`). The web
   authorization-code + offline flow yields a Google refresh token usable by the
   plugin's TV OAuth. Scope decision: request the YouTube playback scope with
   `access_type=offline` + `prompt=consent` so Google returns a **refresh
   token** (not just an access token, and not the read-only metadata scope). The
   exact scope value is the youtube scope the plugin's TV OAuth accepts
   (`https://www.googleapis.com/auth/youtube`), replacing the current
   `youtube.readonly`. This is verified against `render_lavalink_config.py` +
   `bot.py:push_youtube_oauth`, which push `refreshToken` to the plugin's
   `POST /youtube` — so a refresh token is exactly what the plugin consumes.
2. **`pot_token`** and **3. `pot_visitor_data`** — obtained from the in-cluster
   **potoken-server** (bgutil-ytdlp-pot-provider) via `POST /get_pot`. The
   response shape is `{ "poToken": ..., "contentBinding": ... }` (verified in
   `bot.py:fetch_and_push_potoken`; `contentBinding` is the visitor data).
   Optionally pass `content_binding=<visitorData>` in the request body.

**Where each step happens.**

- The web-ui performs the Google **code→token exchange** (same rationale as
  Spotify: it is the Per_Guild_Secret writer and holds the RW grant). It resolves
  the Google OAuth client secret from `HELLODJ_GOOGLE_OAUTH_SECRET_ARN`.
- The web-ui obtains the **PoToken** by calling the potoken-server. The
  potoken-server is in-cluster (on-prem `potoken-server:4416`; on AWS the
  Nix-built `potoken-server` component). The web-ui reaches it at a configurable
  URL (`POTOKEN_SERVER_URL`, defaulting to the in-cluster service DNS), mirroring
  the bot's `POTOKEN_SERVER_URL` default. The fix obtains the PoToken from that
  server rather than fabricating one (per requirements + steering #11 graceful
  degradation: if the potoken-server is unavailable at connect time, surface a
  clear "try again" error and do NOT store a partial secret — the guild's
  connect is not marked connected until all three values are present).

**Stored secret shape (youtube / youtube_music):**

```json
{ "provider": "youtube",
  "oauth_refresh_token": "<Google offline refresh token>",
  "pot_token": "<poToken from potoken-server>",
  "pot_visitor_data": "<contentBinding / visitorData>",
  "obtained_at": <epoch> }
```

**Changes:**

- `source_oauth.py`:
  - Change the youtube/youtube_music scope from `youtube.readonly` to the
    playback youtube scope with `access_type=offline` + `prompt=consent`.
  - Add `source_exchange_google(code, guild_id) -> dict` (code→refresh token).
  - Add `fetch_guild_potoken() -> dict` that POSTs the potoken-server
    `POST /get_pot` and returns `{pot_token, pot_visitor_data}`; on failure
    returns `{}` so the caller can surface an error without storing a partial.
  - `source_tokens_from_request` (or a new `build_youtube_tokens`) composes the
    three values into the stored shape; returns `{}` if any required value is
    missing (so the callback stores nothing and the button reports failure, not a
    silent no-op).
- `auth.py:source_callback`: for youtube/youtube_music, exchange → fetch PoToken
  → compose → `store_tokens(...)`. Surface a clear error (query param) if the
  compose is empty (potoken-server down or refresh token missing).
- A new small module may be needed to keep `source_oauth.py` under 500 lines
  (e.g. `source_token_exchange.py` holding the HTTPS exchange + potoken fetch);
  the router in `source_oauth.py` delegates to it.

### Defect 1b — bot-side per-guild YouTube consumption

**File**: `bot/playback/guild_credentials.py`

- Add youtube/youtube_music **per-guild** resolution: `resolve(guild, "youtube")`
  already reads `hellodj/<stage>/guild/<gid>/youtube` first (the generic path
  handles any provider). No structural change is needed to read the secret — the
  gap is only the **global fallback** and the **playback injection**.
- Global fallback for youtube/youtube_music: the global YouTube creds do **not**
  live in a `hellodj/<stage>/youtube` secret — they live in the bot's credential
  store (`youtube.oauth_refresh_token`, `youtube.pot_token`,
  `youtube.pot_visitor_data`, read by `render_lavalink_config.py` and
  `bot.py:push_youtube_oauth`). Therefore the resolver's `GLOBAL_FALLBACK_LEAVES`
  **intentionally does NOT** add youtube/youtube_music leaves (there is no global
  secret to point at). Instead, "fallback to global YouTube" for a
  non-connected guild is realized by the **existing global push path being left
  untouched** (3.5). Document this explicitly in the resolver docstring:
  youtube/youtube_music have a per-guild capture path (new) but no global secret
  leaf; a guild with no per-guild youtube secret plays via the global
  credential-store push exactly as today.

**File**: `bot/bot.py` (the YouTube push) — the shared-node injection point.

This is where the honest constraint lives. The plugin holds ONE set of YouTube
creds and is fed by a single global `POST /youtube` (steering #2). Per-guild
YouTube creds cannot be pushed per-track to a shared node without contention.

**Chosen mechanism (option b, made concrete): per-guild secret is the source of
truth; a push-before-play swap keyed by guild, serialized per Lavalink node,
with the shared-node contention accepted as a known constraint.**

- Add `resolve_guild_youtube(guild_id) -> dict | None` usage at the point a
  YouTube track is about to be resolved/played for a guild (in `player.py`'s
  `_resolve_and_play` YouTube branch). If the guild has a per-guild youtube
  secret (`GuildCredentialResolver.resolve(guild_id, provider)` returns a dict
  with `oauth_refresh_token`), push **that guild's** `oauth_refresh_token` +
  `pot_token` + `pot_visitor_data` to Lavalink via the SAME single
  `POST /youtube` (reusing `push_youtube_oauth`'s payload builder, parameterized
  by explicit values instead of always reading the global credential store)
  **immediately before** resolving/playing that track.
- Serialize this swap with an `asyncio.Lock` per Lavalink node so two guilds
  playing YouTube concurrently on the same node cannot interleave pushes
  mid-resolve. The lock is held from "push guild creds" through "track
  resolved/loaded", then released.
- After the track loads, the node continues holding that guild's creds until the
  next YouTube resolution swaps them. For guilds **without** a per-guild secret,
  no swap happens and the global creds pushed at startup/watchdog remain in place
  (3.5 preserved exactly).
- **Accepted known constraint (documented, not hand-waved):** on a **single
  shared Lavalink node**, two guilds streaming YouTube with *different* per-guild
  creds simultaneously contend — the node holds one set at a time. The
  push-before-play swap + per-node lock guarantees each *resolution* uses the
  correct guild's creds, but a long-lived stream started by guild A could, in
  principle, be affected if guild B swaps creds mid-stream. In practice YouTube
  playback resolves the stream URL at load time (the creds matter at
  resolve/load, not for the already-fetched segments), so the swap-at-resolve
  window is the correct injection point and the residual contention is bounded to
  the resolve window. Where stronger isolation is required, the design notes the
  alternative (a) **node-per-guild / again-scoped Lavalink** (a Lavalink node or
  pool keyed by guild so each holds its own YouTube creds) as a future scaling
  option — out of scope for this bugfix, which accepts the shared-node limitation
  and makes the per-guild secret the source of truth with the resolve-time swap
  as the injection point. See "Known Constraints".

This keeps the fix minimal (reuses the single `POST /youtube` and existing
payload builder; no per-track push to a shared node beyond the resolve-time swap)
and honest about the limitation.

### Defect 2 — per-guild bot identity (nickname + server avatar)

**Control path (web-ui in AWS → bot elsewhere).** The web-ui does not hold the
Discord bot token, so it cannot call the Discord API directly. There is **no**
existing web-ui→bot RPC/pubsub the web-ui can use (the bot's heartbeat is
bot→Redis one-way; the orchestrator is bot-internal). The mechanism that fits the
existing architecture is the **shared datastore the bot already reaches**:

- **Web-ui writes** the desired identity to the shared CoreTable as a per-guild
  config item, and writes the **avatar image bytes** to the guild's isolated
  Per_Guild_Secret (base64), NOT to DynamoDB (keeps large/binary + potentially
  sensitive image out of the item, consistent with the tokens-only-in-Secrets
  isolation principle; the web-ui already has RW on `hellodj/<stage>/guild/*`).
- **Bot applies** the identity via the Discord API on (1) a periodic **identity
  watchdog** loop (mirroring `_guild_policy_watchdog`), and (2) `on_guild_join`
  and `on_ready` (apply on join/restart). The bot reads the CoreTable item +
  the avatar secret, diffs against the currently-applied identity (stored
  version/hash in the item), and applies only on change.

Rationale: the bot already reads the shared datastore + Secrets Manager and runs
periodic watchdogs; this reuses that pattern with no new transport. Latency is
bounded by the watchdog interval (and immediate on join/restart), which is
acceptable for an admin identity change.

**Storage.**

- **CoreTable item** (metadata + nickname, non-secret): PK `guild_pk(guild_id)`,
  SK `IDENTITY#bot`, entity `GuildBotIdentity`, data:
  `{ nickname: str|null, avatar_version: int, avatar_present: bool,
     updated_by: sub, updated_at: epoch }`.
- **Per_Guild_Secret** for the avatar bytes: `hellodj/<stage>/guild/<gid>/identity`
  holding `{ avatar_b64: "<data>", content_type: "image/png", version: int }`.
  Using the guild secret path keeps it within the existing IAM prefix
  (`hellodj/<stage>/guild/*`) that the web-ui writes and the bot reads.

**Discord API specifics (verified against Discord's documented endpoints).**

- **Per-guild nickname** — `PATCH /guilds/{guild.id}/members/@me` with
  `{ "nick": "<name>" }`. Requires the bot's **Change Nickname** permission.
  discord.py: `guild.me.edit(nick=...)`.
- **Per-guild server avatar** — `PATCH /guilds/{guild.id}/members/@me` with
  `{ "avatar": "data:image/png;base64,<...>" }` (base64 data URI). Requires the
  guild/bot to support a per-guild member avatar. discord.py exposes this via the
  guild member edit for the bot's own member where supported.
- **Global identity is OUT of scope** — the bot never calls the global
  `PATCH /users/@me` username/avatar. Only per-guild member edits.
- **Permission-missing handling (2.9):** the bot catches `discord.Forbidden`
  (missing permission) when applying, records the failure state
  (`apply_error: "missing_permission"`) back on the CoreTable item, and the
  web-ui surfaces that as a clear error to the user (the guild-detail page reads
  the identity item's `apply_error`/`applied_at` and renders it). This makes the
  failure visible rather than silent.

**Bot apply flow (new module `bot/guild_identity.py` + wiring in `bot.py`):**

```
FOR each guild the bot is in (watchdog tick / on_guild_join / on_ready):
  item := core_table.get(guild_pk(gid), "IDENTITY#bot")
  IF item is None: continue
  IF item.applied_version == item.avatar_version AND nick unchanged: continue
  TRY:
    guild.me.edit(nick=item.nickname)                       # 2.7
    IF item.avatar_present:
      secret := secrets.get(hellodj/<stage>/guild/<gid>/identity)
      guild.me.edit(avatar=<bytes from secret.avatar_b64>)  # 2.8
    core_table.update(... applied_version=avatar_version, applied_at=now,
                          apply_error=null)
  EXCEPT discord.Forbidden:
    core_table.update(... apply_error="missing_permission")  # 2.9
```

**Web-ui side (routes + service, new `guild_identity_service.py`):**

- `GuildIdentityService` writes the CoreTable `IDENTITY#bot` item (nickname +
  version bump) and the `identity` Per_Guild_Secret (avatar bytes), gated by
  `can_manage_guild`. It reads back the item for status/error display. Mirrors
  `GuildSourcesService`'s "metadata in Dynamo, secret in Secrets Manager" split.

## Components and Data Model

### New / changed modules

| Path | Change | Purpose |
|------|--------|---------|
| `platform/components/web-ui/source_oauth.py` | changed | youtube scope → offline playback scope; router delegates exchange |
| `platform/components/web-ui/source_token_exchange.py` | **new** | Spotify + Google code→token exchange; potoken-server fetch (keeps 500-line ceiling) |
| `platform/components/web-ui/auth.py` | changed | `source_callback` performs exchange + potoken fetch + compose before `store_tokens`; surfaces errors |
| `platform/components/web-ui/guild_identity_service.py` | **new** | write/read per-guild bot identity (Dynamo item + avatar secret), gated by `can_manage_guild` |
| `platform/components/web-ui/guild_routes.py` | changed | new identity routes (GET form on guild_detail; POST nickname; POST avatar) |
| `platform/components/web-ui/bootstrap.py` | changed | build `GuildIdentityService`; resolve source client-secret ARNs |
| `platform/components/web-ui/app.py` | changed (minimal) | read new secret-ARN env; register identity service in extensions |
| `platform/components/web-ui/templates/pages/guild_detail.html` | changed | identity form (nickname + avatar upload); apply-error surfacing |
| `bot/playback/guild_credentials.py` | changed | document youtube/youtube_music per-guild path + no-global-leaf; no structural change to reads |
| `bot/bot.py` | changed | parameterized `push_youtube_oauth` payload; per-guild resolve-time swap w/ per-node lock |
| `bot/player.py` | changed | at YouTube resolve, swap in per-guild youtube creds when present |
| `bot/guild_identity.py` | **new** | apply per-guild nickname/avatar via Discord API; watchdog + on_guild_join/on_ready |

### Config keys (web-ui env → `current_app.config`)

| Env var | Source | Config key | Notes |
|---------|--------|-----------|-------|
| `SPOTIFY_CLIENT_ID` | workloads-stack plain env | `SPOTIFY_CLIENT_ID` | public id; already read by `_configure` |
| `GOOGLE_CLIENT_ID` | workloads-stack plain env | `GOOGLE_CLIENT_ID` | public id; already read |
| `TIDAL_CLIENT_ID` | workloads-stack plain env | `TIDAL_CLIENT_ID` | public id; already read (Tidal unchanged) |
| `DISCORD_CLIENT_SECRET` | Secrets Manager ARN → resolved | (lazy) | used for Discord login token exchange (not per-guild source) |
| `HELLODJ_SPOTIFY_SECRET_ARN` | workloads-stack | (lazy resolve) | Spotify client id+secret JSON |
| `HELLODJ_GOOGLE_OAUTH_SECRET_ARN` | workloads-stack | (lazy resolve) | Google OAuth client secret |
| `HELLODJ_DISCORD_OAUTH_SECRET_ARN` | workloads-stack | (lazy resolve) | Discord OAuth client secret |
| `POTOKEN_SERVER_URL` | workloads-stack | (module const) | in-cluster potoken-server; mirrors bot default |

### Bot credential-store keys (unchanged, global YouTube — preserved)

`youtube.oauth_refresh_token`, `youtube.pot_token`, `youtube.pot_visitor_data`
(read by `render_lavalink_config.py` + `bot.py`) — untouched (3.5).

### Secret shapes

- **Per-guild spotify** `hellodj/<stage>/guild/<gid>/spotify`:
  `{provider, refresh_token, access_token?, expires_at?, scope, obtained_at}`
- **Per-guild youtube / youtube_music** `hellodj/<stage>/guild/<gid>/<provider>`:
  `{provider, oauth_refresh_token, pot_token, pot_visitor_data, obtained_at}`
- **Per-guild tidal** `hellodj/<stage>/guild/<gid>/tidal`: **unchanged** (3.1).
- **Per-guild identity avatar** `hellodj/<stage>/guild/<gid>/identity`:
  `{avatar_b64, content_type, version}`

### DynamoDB items (CoreTable `hellodj-core`)

- `SOURCE#<provider>` (existing) — non-secret metadata only (connected flag,
  connected_by, timestamps). **Unchanged** (3.3).
- `IDENTITY#bot` (**new**) — `{nickname, avatar_version, avatar_present,
  updated_by, updated_at, applied_version, applied_at, apply_error}`.

### auth-stack (Secrets Manager) inventory + additions

| Secret name | Status | Purpose |
|-------------|--------|---------|
| `hellodj/<stage>/discord-bot-token` | exists | bot token (NOT OAuth client secret) |
| `hellodj/<stage>/tidal-refresh` | exists | Tidal refresh + client creds (sidecar) |
| `hellodj/<stage>/spotify` | exists | Spotify client id+secret (used by exchange) |
| `hellodj/<stage>/yt-cipher-secret` | exists | yt-cipher shared secret |
| `hellodj/<stage>/web-ui-flask-session` | exists | Flask session key |
| `hellodj/<stage>/discord-oauth` | **add** | Discord OAuth client secret (login) |
| `hellodj/<stage>/google-oauth` | **add** | Google/YouTube OAuth client secret |
| `hellodj/<stage>/guild/<gid>/identity` | **new (runtime)** | per-guild avatar bytes (web-ui-created, like source secrets) |

## Routes

### New / changed web-ui routes (all gated by `can_manage_guild`)

| Method + Path | Handler | Purpose |
|---------------|---------|---------|
| `GET /guilds/<guild_id>` | `guild.guild_detail` (changed) | render sources (all 4 connectable) + identity form + apply-error status |
| `GET /auth/sources/<guild_id>/<provider>/connect` | `auth.source_connect` (unchanged shape) | now redirects for spotify/youtube (client ids wired) |
| `GET /auth/sources/<guild_id>/<provider>/callback` | `auth.source_callback` (changed) | performs real exchange (spotify/google) + potoken fetch (youtube) + store |
| `POST /guilds/<guild_id>/identity/nickname` | `guild.set_bot_nickname` (**new**) | write nickname to `IDENTITY#bot`; returns partial |
| `POST /guilds/<guild_id>/identity/avatar` | `guild.set_bot_avatar` (**new**) | write avatar bytes to `identity` secret + bump version; returns partial |
| `POST /guilds/<guild_id>/sources/<provider>/disconnect` | `guild.disconnect_source` (unchanged) | preserved (3.4) |

Tidal connect/callback (`/auth/tidal/callback` forward to `tidal-stream`) and all
Discord/Cognito auth routes are **unchanged** (preservation).

## Testing Strategy

### Validation Approach

Two phases: first surface counterexamples that demonstrate each defect on
UNFIXED code (confirming/refuting the root-cause analysis), then verify the fix
works and preserves existing behavior. All AWS/Discord/potoken interactions use
fakes (no live calls in tests), consistent with the existing
`test_guild_credentials.py` `FakeSecrets` pattern.

### Exploratory Bug Condition Checking

**Goal**: Surface counterexamples on UNFIXED code, confirm/refute root cause.

**Test Plan & Cases**:
1. **Spotify no-op (1.2)** — with `SPOTIFY_CLIENT_ID=""`, assert
   `source_authorize_url("spotify", ...)` returns `None` (will confirm the
   no-op on unfixed code).
2. **YouTube no-op (1.3)** — with `GOOGLE_CLIENT_ID=""`, assert
   `source_authorize_url("youtube", ...)` returns `None`.
3. **Wrong credential (1.4)** — drive `source_tokens_from_request("youtube")`
   with a code; assert the stored dict has only `authorization_code` and lacks
   `oauth_refresh_token`/`pot_token`/`pot_visitor_data`.
4. **No YouTube resolution (1.5)** — assert `GLOBAL_FALLBACK_LEAVES` has no
   youtube key and `resolve(g, "youtube")` returns `None` when no guild secret
   (existing `test_guild_credentials` already asserts the no-global-lookup path).
5. **No identity capability (1.6/1.7)** — assert no identity route/service
   exists (route table + absence of `GuildIdentityService`).
6. **workloads-stack env (root cause 1a)** — jest: assert the synthesized web-ui
   container env does NOT include `SPOTIFY_CLIENT_ID`/`GOOGLE_CLIENT_ID` on
   unfixed infra.

**Expected Counterexamples**: authorize URL `None` for spotify/youtube; stored
youtube value missing the three playback fields; resolver returns `None` for
youtube; no identity route.

### Fix Checking

**Goal**: For all inputs where the bug condition holds, the fixed system
produces the expected behavior (Properties 1 & 2).

**Pseudocode:**
```
FOR ALL X=(g,p) WHERE isBugCondition1(X) DO
  ASSERT p offered as connectable
  url := source_authorize_url'(p, state, g); ASSERT url is not None (configured)
  tokens := source_callback_exchange'(p, code, g)
  ASSERT guild_sources.store_tokens called with working tokens at .../<g>/<p>
  IF p IN {youtube, youtube_music} THEN
    ASSERT tokens has oauth_refresh_token AND pot_token AND pot_visitor_data
    ASSERT bot resolve(g,p) returns those and push payload uses them
  END IF
END FOR

FOR ALL X=(g,identity) WHERE isBugCondition2(X) DO
  setGuildBotIdentity'(g, identity)   # writes item + avatar secret
  applyIdentity'(g)                   # bot apply
  ASSERT guild.me.edit called with nick and/or avatar
  ASSERT missing-permission path records apply_error and web-ui surfaces it
END FOR
```

**Cases**: spotify authorize URL non-None + exchange stores `refresh_token`;
google exchange stores `oauth_refresh_token`; potoken fetch composes all three;
compose-empty (potoken-server down) stores nothing + surfaces error; bot swap
pushes per-guild youtube creds under the per-node lock; identity apply sets
nick/avatar; `discord.Forbidden` → `apply_error` surfaced (2.9).

### Preservation Checking

**Goal**: For all inputs where NO bug condition holds, `F' == F` (Property 3).

**Pseudocode:**
```
FOR ALL X WHERE NOT isBugCondition1(X) AND NOT isBugCondition2(X) DO
  ASSERT F(X) = F'(X)
END FOR
```

**Approach**: property-based testing (Hypothesis) is recommended for
preservation because it generates many inputs across the domain and catches
edge cases. Observe behavior on UNFIXED code first, then assert it is unchanged.

**Cases**:
1. **Tidal connect/disconnect (3.1)** — authorize URL, callback forward to
   `tidal-stream`, store/remove at `.../tidal` identical before/after.
2. **Ownership gating (3.2)** — property test over random
   (user, guild, owner, admins): `can_manage_guild` decision and route
   accept/reject identical; unauthorized callers rejected on every new route.
3. **Secret isolation (3.3)** — property test: after any connect, tokens appear
   only in the Per_Guild_Secret; the `SOURCE#<provider>` Dynamo item has no
   token fields.
4. **Disconnect (3.4)** — deletes secret + metadata; unchanged.
5. **Global YouTube playback (3.5)** — property test over guilds with NO
   per-guild youtube secret: the resolve-time swap is a no-op and the global
   `POST /youtube` payload is byte-identical to unfixed (`push_youtube_oauth`
   with global creds).
6. **DVD avatar read (3.6)** — `activity_backend._get_guild_icon_url` /
   `visualizer_manager` unaffected: same avatar/icon URL resolution.
7. **tidal/spotify global fallback (3.7)** — `GLOBAL_FALLBACK_LEAVES` still
   `{tidal: tidal-refresh, spotify: spotify}`; resolve falls back for guilds
   without a per-guild secret exactly as before.

### Unit Tests

- web-ui: `source_authorize_url` per provider (configured vs empty); Spotify/
  Google exchange (fake token endpoint); potoken fetch (fake potoken-server);
  compose-empty error path; `GuildIdentityService` write/read; new routes gated
  by `can_manage_guild`.
- bot: parameterized `push_youtube_oauth` payload builder; per-guild swap with
  per-node lock (concurrency test: two guilds do not interleave); resolver
  youtube per-guild read + no-global-leaf; `guild_identity` apply +
  `discord.Forbidden` handling (fake Discord client).
- infra: jest asserting web-ui env includes the new client ids + secret ARNs and
  the web-ui role grants read on the new secrets + RW on the guild prefix.

### Property-Based Tests

- Preservation properties (2, 3, 5, 7 above) generated with Hypothesis over
  random guilds/users/providers/token dicts.
- Isolation: generated tokens never leak cross-guild (extends existing
  `test_guild_credentials` guarantees).

### Integration Tests

- Full connect flow (fakes): spotify connect → callback → secret written →
  bot `resolve` returns usable creds.
- Full youtube connect flow (fakes): authorize → google exchange → potoken fetch
  → three-field secret → bot resolve-time swap pushes the guild's creds.
- Identity flow (fakes): web-ui writes item + avatar secret → bot watchdog/
  on_guild_join applies via fake Discord API → success and Forbidden paths.
- Context: a non-connected guild's YouTube playback path is unchanged end-to-end.

### Gate commands (must pass before push)

- web-ui: `ruff check --target-version py314 .` + `python3 -m pytest tests/ -q`
  + `python3 platform/tools/check_line_count.py platform/components/web-ui`
  (500-line ceiling — the new `source_token_exchange.py` /
  `guild_identity_service.py` splits exist to respect it).
- infra: `npx tsc --noEmit` + `npx jest`.
- bot: `python3 -m pytest` in `bot/playback` (and the new `bot/` identity tests).

## Deployment

Per steering (session-context, website-debug-context), changes deploy by two
distinct paths — do not conflate them:

### Component source (CodeCommit push → pipeline rebuilds image)

- All `platform/components/web-ui/*.py` + templates (source_oauth,
  source_token_exchange, auth, guild_identity_service, guild_routes, bootstrap,
  app, guild_detail.html).
- All `bot/*.py` (guild_credentials, bot.py, player.py, guild_identity).
- After the pipeline pushes `:latest`, restart the web-ui deployment to re-pull
  (`kubectl rollout restart deploy/web-ui -n hellodj-<stage>`); the bot
  redeploys via its own image/tag path.

### Infra (cdk deploy — NOT a pipeline push)

- `platform/infra/lib/auth-stack.ts` (new `discord-oauth`, `google-oauth`
  secrets; expose on props) and `platform/infra/lib/workloads-stack.ts` (new
  web-ui env + IRSA grants) + `bin/hellodj.ts` (thread new props).
- Deploy with `cd platform/infra && npx cdk deploy hellodj-eks` (workloads
  manifests live in the `hellodj-eks` stack via `cluster.addManifest`), and
  `npx cdk deploy hellodj-auth` (or the auth stack id) for the new secrets. The
  secret **values** (Discord/Google/Spotify client id+secret) are populated
  out-of-band (console/CLI), matching the empty-secret convention in auth-stack.

### Keep architecture docs in sync

Per steering, update `website-debug-context.md` "KNOWN GAPS" (#1, #3, #4 are
resolved by this fix) and note the new env vars/secrets and the per-guild
YouTube resolve-time swap + shared-node constraint in the same change.

## Known Constraints

- **Shared Lavalink node holds one YouTube credential set.** Per-guild YouTube
  auth is stored per guild (source of truth) and injected via a resolve-time
  push-before-play swap serialized by a per-node lock. Concurrent YouTube
  streams from different guilds on one shared node contend at the resolve window;
  this is accepted for this bugfix. Stronger isolation (node-per-guild /
  again-scoped Lavalink pool) is a future scaling option, out of scope here.
- **potoken-server availability at connect time.** If the potoken-server is down
  when a guild connects YouTube, the connect reports a clear "try again" error
  and stores no partial secret (all three values are required together, 2.4).
- **Discord per-guild avatar support.** Setting a per-guild server avatar
  requires the guild/bot to support it and the bot to hold the permission; the
  missing-permission case is surfaced as a clear error (2.9), never silent.
- **SES-style out-of-band secret population.** The new OAuth client-secret
  secrets are created empty by CDK; their values must be populated out-of-band
  before the connect flows work in a given stage.

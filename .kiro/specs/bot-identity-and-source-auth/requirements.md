# Requirements Document

## Introduction

This spec fixes two related defects the user reported in the HelloDJ web-ui +
bot:

1. **Per-guild source connect only works for Tidal.** The per-guild "Connect"
   buttons for Spotify and YouTube / YouTube Music silently no-op, and even when
   a YouTube connect flow does run it captures the wrong thing (a
   `youtube.readonly` authorization code) rather than the PoToken + OAuth
   refresh token the playback path actually needs. Tidal appears to work only
   because `TIDAL_CLIENT_ID` is (or was) wired into the web-ui env.

2. **There is no way to choose the bot's name or avatar.** No UI or service
   exists to set the bot's display name or avatar for a guild. The bot only
   *reads* its own Discord avatar (for the DVD visualizer). The user wants to
   *choose* the bot's per-guild identity (server nickname + per-guild server
   avatar).

Both defects share the same per-guild ownership model (`can_manage_guild`) and
the same isolated Secrets Manager layout (`hellodj/<stage>/guild/<gid>/...`),
so they are fixed together in one spec.

### Scope decisions (confirmed with the user)

- **Bot identity is per-guild only** — the fix sets the bot's *server nickname*
  and *per-guild server avatar* for a guild the user controls. It does **not**
  change the bot's global Discord account username/avatar (that path is heavily
  rate-limited by Discord and affects every server). Discord's per-guild
  nickname is effectively unlimited; the per-guild server avatar requires the
  bot to hold the relevant guild permissions.
- **Per-guild YouTube auth captures a distinct per-guild YouTube account** —
  the fix completes a full OAuth **refresh-token** grant for the guild's own
  Google/YouTube account and generates a **PoToken** (via the in-cluster
  potoken-server) so the guild's isolated secret contains the OAuth refresh
  token + PoToken + visitor data the playback path needs. There is no fallback
  to the bot's global YouTube auth for a connected guild.

### Bug condition framing

Using the bug-condition methodology, let **F** be the current (unfixed) system
and **F'** be the fixed system.

**Defect 1 bug condition** `C1(X)` — for input `X = (guild g the user controls,
provider p ∈ {youtube, youtube_music, tidal, spotify})`:

```pascal
FUNCTION isBugCondition1(X)
  INPUT: X = (guild g, provider p) where can_manage_guild(user, g) is true
  OUTPUT: boolean

  // A provider is "broken" for a controllable guild when connecting it does not
  // yield working, stored, per-guild credentials the playback path can use.
  // For youtube / youtube_music, "working" specifically requires the stored
  // credentials to include the OAuth refresh token + PoToken (+ visitor data),
  // NOT merely a youtube.readonly authorization code.
  RETURN NOT connectYieldsWorkingGuildCredentials(F, g, p)
END FUNCTION
```

Under **F**, `C1(X)` is true for every provider except `tidal` (Spotify/YouTube
connect no-op on empty client-id config, and YouTube captures the wrong token
kind). The fix **F'** removes the Tidal-only restriction so `C1(X)` becomes
false for all four providers.

**Defect 2 bug condition** `C2(X)` — for input `X = (guild g the user controls,
desired nickname and/or avatar)`:

```pascal
FUNCTION isBugCondition2(X)
  INPUT: X = (guild g, identity change) where can_manage_guild(user, g) is true
  OUTPUT: boolean

  // The bug condition is met whenever an authorized user wants to set the bot's
  // per-guild name/avatar and cannot make it take effect (no capability exists).
  RETURN NOT canSetAndApplyGuildBotIdentity(F, g)
END FUNCTION
```

Under **F**, `C2(X)` is true for every controllable guild (no capability
exists). The fix **F'** makes `C2(X)` false.

**Property (fix checking)** — for all buggy inputs, `F'` produces working,
persisted, per-guild results:

```pascal
// Property: Fix Checking — per-guild source connect (Defect 1)
FOR ALL X = (g, p) WHERE isBugCondition1(X) DO
  result ← connectFlow'(g, p)
  ASSERT p IS offered as connectable in the guild source UI
  ASSERT result completes without silent no-op
  ASSERT working per-guild credentials are stored at
         hellodj/<stage>/guild/<g>/<p>
  IF p IN {youtube, youtube_music} THEN
    ASSERT stored credentials include oauth_refresh_token AND pot_token
           AND pot_visitor_data
  END IF
END FOR

// Property: Fix Checking — per-guild bot identity (Defect 2)
FOR ALL X = (g, identity) WHERE isBugCondition2(X) DO
  result ← setGuildBotIdentity'(g, identity)
  ASSERT the bot's server nickname and/or per-guild server avatar in g
         reflect(s) the requested value
END FOR
```

**Preservation (regression prevention)** — for all non-buggy inputs, `F'`
behaves identically to `F`:

```pascal
// Property: Preservation Checking
FOR ALL X WHERE NOT isBugCondition1(X) AND NOT isBugCondition2(X) DO
  ASSERT F(X) = F'(X)
END FOR
```

Concretely: Tidal per-guild connect, guild-ownership gating
(`can_manage_guild`), secret isolation (`hellodj/<stage>/guild/<gid>/<provider>`),
disconnect behavior, and the bot's global YouTube OAuth+PoToken playback path
must all continue to behave exactly as before.

### Deployment reality (part of the fix, per steering)

- The web-ui client-id env vars (`SPOTIFY_CLIENT_ID`, `TIDAL_CLIENT_ID`,
  `GOOGLE_CLIENT_ID`, `DISCORD_CLIENT_SECRET`) currently default to `""` and are
  injected via the CDK **workloads-stack** (`cdk deploy hellodj-eks`, NOT a
  pipeline push) sourced from Secrets Manager. Wiring these env vars — and
  ensuring the underlying Spotify/Google/Discord client secrets exist in Secrets
  Manager — is part of this fix.
- Per-guild YouTube PoToken generation depends on the in-cluster
  **potoken-server** (bgutil-ytdlp-pot-provider). The fix must obtain the
  PoToken from that server rather than fabricating one.

## Glossary

- **Per_Guild_Secret** — A Secrets Manager secret scoped to a single guild and
  provider, at path `hellodj/<stage>/guild/<gid>/<provider>`. Holds the working
  per-guild credentials (e.g. OAuth refresh token, PoToken, visitor data) and is
  isolated per guild so one guild's tokens never leak to another.
- **PoToken** — A YouTube Proof-of-Origin token used to defeat "Sign in to
  confirm you're not a bot". Generated on demand by the in-cluster
  potoken-server and required (alongside the OAuth refresh token) for reliable
  per-guild YouTube playback.
- **visitor data** — The YouTube `visitorData` value that binds a PoToken to a
  session/content context. Stored alongside the PoToken (`pot_visitor_data`) in
  the per-guild secret and pushed to Lavalink for playback.
- **OAuth refresh token** — A long-lived offline token obtained from a full
  OAuth grant (Google/YouTube, Spotify, Tidal) that lets the system mint access
  tokens without re-prompting the user. For YouTube this is the credential the
  playback path needs — not a short-lived `youtube.readonly` authorization code.
- **can_manage_guild** — The pure per-guild ownership/authorization gate. Returns
  true only when the calling user controls the guild (OWNER/admin edge). Every
  per-guild source connect/disconnect and identity change is gated through it.
- **per-guild server avatar** — The bot's avatar as displayed within a single
  guild (Discord's server-profile avatar), distinct from the bot's global
  account avatar. Setting it requires the bot to hold the relevant guild
  permission.
- **server nickname** — The bot's display name within a single guild (Discord's
  per-guild nickname), distinct from the bot's global account username. Discord
  imposes effectively no rate limit on per-guild nicknames.
- **potoken-server** — The in-cluster bgutil-ytdlp-pot-provider service that
  issues fresh YouTube PoTokens (and visitor data) via `POST /get_pot`. The fix
  obtains PoTokens from this server rather than fabricating them.
- **workloads-stack** — The CDK stack (`platform/infra/lib/workloads-stack.ts`,
  deployed via `cdk deploy hellodj-eks`, not a pipeline push) that injects the
  web-ui container env, including source client-id/secret env vars sourced from
  Secrets Manager.
- **supported providers** — The set of music sources offered for per-guild
  connect: `youtube`, `youtube_music`, `tidal`, and `spotify`.

## Requirements

The requirements below restructure the bug-condition analysis (defect →
expected → preserved behavior) into discrete, testable requirements. Acceptance
criteria are written in EARS format. Clause numbering preserves the original
current-behavior (1.x), expected-behavior (2.x), and regression-prevention (3.x)
identifiers so the bug-condition mapping stays intact.

### Requirement 1: Per-guild Spotify source connect

**User Story:** As an authorized guild manager, I want to connect a Spotify
account to a guild I control, so that the guild has working, isolated per-guild
Spotify credentials for playback instead of a silent no-op.

#### Acceptance Criteria

**Defect — current behavior**

1.1 WHEN an authorized user opens the per-guild source list for a guild they
control THEN the system offers connect flows that only complete for Tidal;
Spotify and YouTube / YouTube Music appear but do not connect.

1.2 WHEN an authorized user clicks "Connect" for Spotify on a guild they control
AND `SPOTIFY_CLIENT_ID` is empty in the web-ui env THEN `source_authorize_url`
returns `None` and the button silently no-ops (no redirect, no error, no stored
credentials).

**Expected — correct behavior**

2.1 WHEN an authorized user opens the per-guild source list for a guild they
control THEN the system SHALL offer all supported providers (youtube,
youtube_music, tidal, spotify) as connectable.

2.2 WHEN an authorized user clicks "Connect" for Spotify on a guild they control
THEN the system SHALL redirect to the Spotify authorize URL, and on callback
SHALL store working per-guild Spotify credentials in the guild's isolated secret
`hellodj/<stage>/guild/<gid>/spotify`.

### Requirement 2: Per-guild YouTube / YouTube Music connect with PoToken + OAuth refresh

**User Story:** As an authorized guild manager, I want to connect a distinct
YouTube / YouTube Music account to a guild I control, so that the guild's
isolated secret holds the OAuth refresh token + PoToken + visitor data the
playback path actually needs (not a `youtube.readonly` authorization code).

#### Acceptance Criteria

**Defect — current behavior**

1.3 WHEN an authorized user clicks "Connect" for YouTube or YouTube Music on a
guild they control AND `GOOGLE_CLIENT_ID` is empty in the web-ui env THEN
`source_authorize_url` returns `None` and the button silently no-ops.

1.4 WHEN a YouTube per-guild connect flow does run to callback THEN the system
stores only a `youtube.readonly` authorization code, which is NOT the PoToken +
OAuth refresh token the playback path needs, so the guild still cannot play
YouTube.

1.5 WHEN the bot resolves per-guild credentials for playback THEN youtube and
youtube_music have no per-guild capture path and no global fallback leaf, so a
guild's YouTube playback credentials cannot be resolved.

**Expected — correct behavior**

2.3 WHEN an authorized user clicks "Connect" for YouTube or YouTube Music on a
guild they control THEN the system SHALL complete an OAuth flow that yields an
offline **refresh token** for the guild's YouTube account (not merely a
`youtube.readonly` authorization code).

2.4 WHEN a per-guild YouTube or YouTube Music connect flow completes THEN the
system SHALL obtain a PoToken (and visitor data) from the in-cluster
potoken-server and SHALL store `oauth_refresh_token`, `pot_token`, and
`pot_visitor_data` together in the guild's isolated secret
`hellodj/<stage>/guild/<gid>/<provider>`.

2.5 WHEN the bot resolves per-guild credentials for a connected guild's YouTube
or YouTube Music playback THEN the system SHALL load that guild's
`oauth_refresh_token` + `pot_token` + `pot_visitor_data` from the guild's
isolated secret and use them for that guild's playback.

### Requirement 3: web-ui client-id/secret env wiring

**User Story:** As the platform operator, I want the source client-id/secret env
vars populated into the web-ui deployment from Secrets Manager, so that connect
flows do not no-op because `source_authorize_url` returned `None`.

#### Acceptance Criteria

**Expected — correct behavior**

2.6 WHEN a required source client id/secret (`SPOTIFY_CLIENT_ID`,
`GOOGLE_CLIENT_ID`, `TIDAL_CLIENT_ID`, `DISCORD_CLIENT_SECRET`) is needed for a
connect flow THEN the system SHALL have that value populated into the web-ui
deployment env from Secrets Manager via the workloads-stack, so
`source_authorize_url` does not return `None` for a configured provider.

### Requirement 4: Per-guild bot nickname

**User Story:** As an authorized guild manager, I want to set the bot's server
nickname for a guild I control, so that the bot presents the identity I choose
in that guild.

#### Acceptance Criteria

**Defect — current behavior**

1.6 WHEN an authorized user wants to set the bot's name for a guild they control
THEN the system provides no UI or service to do so.

**Expected — correct behavior**

2.7 WHEN an authorized user submits a new bot nickname for a guild they control
THEN the system SHALL set the bot's server nickname in that guild via the
Discord API and the change SHALL take effect in that guild.

2.9 WHEN a bot identity change (nickname or avatar) cannot be applied because
the bot lacks the required guild permission THEN the system SHALL surface a
clear error to the user rather than failing silently.

### Requirement 5: Per-guild bot avatar

**User Story:** As an authorized guild manager, I want to set the bot's per-guild
server avatar for a guild I control, so that the bot shows the avatar I choose in
that guild without altering its global account avatar.

#### Acceptance Criteria

**Defect — current behavior**

1.7 WHEN an authorized user wants to set the bot's avatar for a guild they
control THEN the system provides no UI or service to do so.

**Expected — correct behavior**

2.8 WHEN an authorized user submits a new bot avatar for a guild they control
AND the bot holds the required guild permission THEN the system SHALL set the
bot's per-guild server avatar via the Discord API and the change SHALL take
effect in that guild.

2.9 WHEN a bot identity change (nickname or avatar) cannot be applied because
the bot lacks the required guild permission THEN the system SHALL surface a
clear error to the user rather than failing silently.

### Requirement 6: Regression prevention / preservation

**User Story:** As an existing HelloDJ operator, I want all currently-working
behavior (Tidal connect, ownership gating, secret isolation, disconnect, global
YouTube playback, DVD-visualizer avatar read, tidal/spotify global fallback) to
keep working, so that fixing the two defects introduces no regressions.

#### Acceptance Criteria

**Unchanged — regression prevention**

3.1 WHEN an authorized user connects or disconnects Tidal for a guild they
control THEN the system SHALL CONTINUE TO complete the flow and store/remove
credentials in `hellodj/<stage>/guild/<gid>/tidal` exactly as it does today.

3.2 WHEN any per-guild source connect or disconnect is requested THEN the system
SHALL CONTINUE TO gate the action through `can_manage_guild` and reject callers
who do not control the guild.

3.3 WHEN a guild's source tokens are stored THEN the system SHALL CONTINUE TO
isolate them in the per-guild secret `hellodj/<stage>/guild/<gid>/<provider>`
and SHALL CONTINUE TO keep tokens out of DynamoDB (only non-secret metadata in
the `SOURCE#<provider>` item).

3.4 WHEN an authorized user disconnects a provider for a guild THEN the system
SHALL CONTINUE TO delete that guild's isolated secret and its `SOURCE#<provider>`
metadata.

3.5 WHEN the bot plays YouTube using the existing GLOBAL YouTube auth (OAuth
refresh token + PoToken + visitor data pushed to Lavalink in a single
`POST /youtube`) for guilds that have NOT connected their own YouTube account
THEN the system SHALL CONTINUE TO play YouTube exactly as it does today.

3.6 WHEN the bot reads its own Discord avatar for the DVD visualizer THEN the
system SHALL CONTINUE TO do so unaffected by the new per-guild identity feature.

3.7 WHEN the bot's per-guild credential resolver falls back to the global leaf
for tidal (`tidal-refresh`) and spotify (`spotify`) for guilds without a
per-guild secret THEN the system SHALL CONTINUE TO use those global fallbacks
for tidal and spotify.

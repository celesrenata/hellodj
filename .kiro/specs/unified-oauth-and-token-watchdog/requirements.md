# Requirements Document

## Introduction

Today the HelloDJ AWS platform authenticates music sources through a patchwork:
per-provider OAuth flows scattered across `auth.py` / `source_oauth.py` /
`source_token_exchange.py`, per-guild refresh tokens stored as **one AWS Secrets
Manager secret per guild+provider** (`hellodj/<stage>/guild/<gid>/<provider>`),
Tidal-only refresh logic, and token-refresh loops that live **in-process in the
bot** and therefore die whenever the bot pod bounces. There is no single place a
user's source connections live, no unified refresh, and no durable watchdog.

This spec unifies that into one system with three goals the user stated:

1. **One OAuth system for all providers** — Discord (per-user account link),
   Spotify, Tidal, YouTube, and YouTube Music. SoundCloud is search-only
   (LavasRC `scsearch`) and needs no OAuth. The **default playback source is
   YouTube**.
2. **A durable token-refresh watchdog** that keeps every stored credential alive
   independently of the bot. It reuses the existing standing
   `playback-orchestrator` container (already deployed, already holds a run loop
   + DynamoDB access, already survives a bot bounce) rather than a new component.
3. **All credential data persisted in DynamoDB** keyed by the user, so bouncing
   any service loses nothing. Tokens are stored **encrypted at rest twice**: the
   `hellodj-core` table's KMS at-rest encryption plus **application-layer
   envelope encryption (KMS data key)** on the token fields themselves, so a
   broad table read or a PITR export never exposes plaintext refresh tokens.

### Why DynamoDB, not Secrets Manager (scale decision, confirmed with the user)

At ~1000 users × up to 5 providers each, the per-secret model is ~5000 Secrets
Manager secrets: ~$0.40/secret/month (~$2000/mo), plus create/delete churn on
every connect/disconnect and per-secret API rate limits. DynamoDB is one table
we already run, one item per user+provider, no per-secret cost. Tokens move to
DynamoDB with app-layer envelope encryption so we keep "encrypted at rest"
without the Secrets Manager sprawl.

### Bug-condition framing

Let **F** be the current system and **F'** the system after this spec.

`C_lost(X)` — for input `X = (user u, provider p, event = "service bounce")`:

```pascal
FUNCTION isBugCondition_lost(X)
  INPUT: X = (user u, provider p) with a completed OAuth connection
  OUTPUT: boolean
  // A connection is "lost on bounce" when, after any single pod restart
  // (bot, sidecar, watchdog, or web-ui), the user's stored credential is no
  // longer usable for playback without the user re-authorizing.
  RETURN NOT credentialSurvivesRestart(F, u, p)
END FUNCTION
```

`C_stale(X)` — for input `X = (user u, provider p)`:

```pascal
FUNCTION isBugCondition_stale(X)
  INPUT: X = (user u, provider p) with a stored refresh token
  OUTPUT: boolean
  // A credential is "allowed to die" when nothing refreshes it before its
  // access token expires while the bot is down (no in-bot loop running).
  RETURN NOT refreshedIndependentlyOfBot(F, u, p)
END FUNCTION
```

F' must make `isBugCondition_lost` and `isBugCondition_stale` false for every
provider that has a refresh grant.

## Glossary

- **Credential item** — the DynamoDB item holding a user's connection for one
  provider: status metadata (plaintext) + an envelope-encrypted token blob.
- **Token blob** — the JSON `{access_token?, refresh_token, expires_at, scope?,
  provider-specific fields}` that is envelope-encrypted before storage.
- **Envelope encryption** — encrypt the token blob with a per-item data key;
  the data key is itself encrypted by a KMS CMK and stored beside the
  ciphertext. Decrypt = KMS-decrypt the data key, then decrypt the blob.
- **Watchdog** — the refresh loop hosted in `playback-orchestrator` that walks
  credential items and refreshes any nearing expiry.
- **Provider** — one of `discord` (link only), `youtube`, `youtube_music`,
  `spotify`, `tidal`. `soundcloud` is search-only (no OAuth).

## Requirements

### Requirement 1: Unified OAuth connect for every provider

**User Story:** As a user managing my bot, I want one consistent "Connect" flow
for every source provider, so I can authorize YouTube, YouTube Music, Spotify,
and Tidal the same way and link my Discord account.

#### Acceptance Criteria

1. WHEN a user opens the config/account page THEN the system SHALL present a
   Connect control for each OAuth provider (`youtube`, `youtube_music`,
   `spotify`, `tidal`) and a Discord link control.
2. WHEN a provider's OAuth client id is not configured THEN the system SHALL
   render a disabled "Needs setup" control (never an active link that silently
   no-ops).
3. WHEN a user starts a Connect flow THEN the system SHALL build the provider
   authorize URL with a CSRF `state` bound to the session and redirect the user
   to the provider.
4. WHEN the provider redirects back with a valid `state` and `code` THEN the
   system SHALL complete the authorization-code→token exchange and persist a
   credential item for that user+provider.
5. IF the `state` is missing or mismatched THEN the system SHALL reject the
   callback and surface a clear error (no token stored).
6. WHEN a provider callback fails the exchange (no refresh token, provider
   error) THEN the system SHALL store nothing partial and surface a clear
   `<provider>_connect_failed` error.
7. WHERE a provider is SoundCloud THE system SHALL NOT present an OAuth control
   (it is search-only).

### Requirement 2: Credentials persisted in DynamoDB, per user

**User Story:** As an operator, I want every user's source credentials stored in
DynamoDB keyed by the user, so nothing is lost when a service restarts and one
store holds all connections.

#### Acceptance Criteria

1. WHEN a credential is persisted THEN the system SHALL write a DynamoDB item on
   `hellodj-core` at `PK=USER#<sub>`, `SK=SOURCECRED#<provider>`, entityType
   `SourceCredential`.
2. THE credential item SHALL store non-secret status fields in plaintext
   (`connected`, `connected_at`, `updated_at`, `expires_at`, `scope`,
   `last_refresh_at`, `refresh_status`) so the UI and watchdog can read status
   without decrypting.
3. THE credential item SHALL store the token blob only as an envelope-encrypted
   ciphertext field (`enc_blob`) plus the wrapped data key (`enc_key`) and the
   KMS key id (`kms_key_id`) — never plaintext tokens.
4. WHEN any pod restarts THEN a previously connected credential SHALL remain
   usable without re-authorization (durable in DynamoDB).
5. WHEN a user disconnects a provider THEN the system SHALL delete that
   credential item (and only that one).
6. WHERE a legacy per-guild Secrets Manager secret exists for a
   guild+provider THE system SHALL continue to read it as a fallback during
   migration, but SHALL write all new credentials to DynamoDB.

### Requirement 3: Encryption at rest (double)

**User Story:** As a security-conscious operator, I want tokens encrypted at
rest beyond the table's default, so a broad read or backup export never leaks a
refresh token.

#### Acceptance Criteria

1. THE `hellodj-core` table SHALL retain KMS at-rest encryption.
2. WHEN a token blob is written THEN it SHALL be envelope-encrypted with a KMS
   data key before it is placed in the item.
3. WHEN a token blob is read THEN it SHALL be decrypted only by a principal
   holding the KMS decrypt grant (the watchdog and the specific
   sidecar/bot/web-ui readers), and the plaintext SHALL never be logged.
4. IF decryption fails THEN the caller SHALL treat the credential as unusable
   and surface/refresh rather than crash.
5. THE KMS key SHALL be a dedicated customer-managed key (CMK) for source
   credentials, with grants scoped to the components that read/write tokens.

### Requirement 4: Unified per-provider refresh contract

**User Story:** As a developer, I want one refresh interface all providers
implement, so the watchdog can refresh any provider uniformly.

#### Acceptance Criteria

1. THE system SHALL define one pure, side-effect-free refresh contract taking a
   current token state + `now` and returning a non-expired token state (mirrors
   the existing `hellodj_platform_logic.tidal_refresh` shape).
2. THE system SHALL provide a refresh client per OAuth provider
   (`youtube`/`google`, `spotify`, `tidal`) that implements the contract using
   that provider's token endpoint and `grant_type=refresh_token`.
3. WHEN a provider does not rotate its refresh token THEN the refreshed state
   SHALL preserve the prior refresh token.
4. WHEN a refresh returns an already-expired token THEN it SHALL be treated as a
   failed refresh (not stored as success).
5. THE Tidal refresh SHALL continue to route through the existing
   `tidal_refresh` first-party single-app-id logic unchanged (no regression).
6. WHERE a provider is `discord` THE system SHALL treat the link as identity
   only (no playback token to refresh).

### Requirement 5: Durable token-refresh watchdog (reuses playback-orchestrator)

**User Story:** As an operator, I want refresh to keep happening even if the bot
is down, so tokens never silently expire.

#### Acceptance Criteria

1. THE watchdog SHALL run inside the standing `playback-orchestrator` container
   as a background loop alongside its health server (not as an in-bot task).
2. WHEN the loop ticks (default every 5 minutes, configurable) THEN it SHALL
   enumerate credential items whose `expires_at` is within a refresh threshold
   and refresh each via the matching provider refresh client.
3. WHEN a refresh succeeds THEN the watchdog SHALL write the new
   envelope-encrypted blob + updated `expires_at` / `last_refresh_at` /
   `refresh_status="ok"` back to the credential item.
4. WHEN a refresh fails THEN the watchdog SHALL set `refresh_status="failed"`
   with a reason, leave the prior blob intact, and retry on the next tick
   (bounded), never crashing the loop or the container.
5. THE watchdog SHALL be idempotent and safe to run in multiple replicas
   (optimistic-lock writes; a concurrent refresh does not corrupt the item).
6. IF the bot bounces mid-session THEN the watchdog SHALL have kept the
   credential fresh so the bot resumes without user re-auth.
7. THE watchdog loop SHALL pause/skip cleanly when no datastore or KMS is
   configured (degraded mode) rather than failing the container.

### Requirement 6: Playback path reads unified credentials

**User Story:** As a developer, I want the bot/sidecars to read the same
credential store, so playback uses the user's live tokens.

#### Acceptance Criteria

1. WHEN the playback path needs a provider token for a user/guild THEN it SHALL
   resolve the DynamoDB credential item, decrypt the blob, and use the access
   token.
2. WHEN the resolved access token is expired THEN the reader SHALL trigger a
   refresh (or read the watchdog-refreshed value) rather than use a dead token.
3. THE existing per-guild YouTube just-in-time `POST /youtube` swap behavior
   SHALL be preserved (OAuth refresh + poToken + visitorData sent together).
4. THE resolution SHALL be cached with a bounded TTL and never return one
   user's credential for another.
5. WHERE a legacy per-guild secret is the only source THE reader SHALL fall back
   to it (migration compatibility).

### Requirement 7: Default source is YouTube

**User Story:** As a user, I want YouTube to be the default source, so playback
works out of the box.

#### Acceptance Criteria

1. WHEN a new guild/user has no explicit default source configured THEN the
   effective default source SHALL be `youtube`.
2. WHEN the config page renders the default-source control with no stored value
   THEN it SHALL preselect `youtube`.
3. THE bot's source resolution SHALL treat an unset default as `youtube`.

### Requirement 8: Config/account UI for connections + status

**User Story:** As a user, I want to see each provider's connection status and
connect/disconnect from the UI.

#### Acceptance Criteria

1. THE config/account page SHALL show, per provider: connected/not-connected,
   last-refresh time, and refresh status (ok/failed).
2. WHEN a user clicks Disconnect THEN the system SHALL delete the credential
   item and reflect the change without a full page reload (HTMX partial).
3. THE UI SHALL never render a token value.
4. THE Discord link control SHALL show linked/not-linked and allow enable and
   reset (unlink) of the Discord auth for the account.

### Requirement 9: Least-privilege access to tokens

**User Story:** As an operator, I want only the components that need tokens to
be able to decrypt them.

#### Acceptance Criteria

1. THE web-ui role SHALL be able to write credential items and encrypt token
   blobs (KMS encrypt + generate-data-key), and decrypt only where it completes
   a flow that needs the token.
2. THE watchdog (`playback-orchestrator`) role SHALL be able to read/write
   credential items and KMS encrypt+decrypt (it must decrypt to refresh).
3. THE playback readers (bot/sidecars) SHALL be able to read credential items
   and KMS decrypt (read-only on tokens).
4. NO other component SHALL hold the KMS decrypt grant for the source-credential
   CMK.

### Requirement 10: No regressions

**User Story:** As an operator, I want the existing entitlement, guild-admin,
invite, registration-mode, and bot-identity behavior preserved, so unifying
OAuth and adding the watchdog introduces no regressions.

#### Acceptance Criteria

1. THE existing entitlement, guild-admin, invite, registration-mode, and
   bot-identity DynamoDB items and flows SHALL be unaffected.
2. THE Tidal first-party single-app-id refresh behavior and its property tests
   SHALL continue to pass.
3. THE gate commands (CDK `tsc`/`jest`, web-ui `ruff`/`pytest`, 500-line check)
   SHALL pass before any push.

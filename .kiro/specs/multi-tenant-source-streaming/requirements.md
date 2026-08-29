# Requirements Document

Feature: Multi-Tenant Source Streaming

## Introduction

The HelloDJ AWS platform already has a fully multi-tenant **credential control
plane**: the unified per-user credential store (`hellodj-core`,
`USER#<sub>/SOURCECRED#<provider>`, envelope-encrypted) and the durable
**token-refresh watchdog** in `playback-orchestrator` keep *every* user's OAuth
token fresh for YouTube, YouTube Music, Spotify, and Tidal, independently and
with per-item isolation.

The **data plane (the streaming sidecars)** does not yet match that model:

- **spotify-stream** holds ONE global librespot session from ONE cached
  credential file. It cannot serve more than one user's Spotify account, does
  not read the per-user `SOURCECRED#spotify` items, and is currently a
  single-account bridge.
- **YouTube / YouTube Music** resolves per-guild credentials but the
  youtube-source plugin's `POST /youtube` replaces ALL credential fields on one
  shared Lavalink node, so only ONE YouTube credential set is live at a time
  (a documented last-writer-wins serialization, not true concurrency).
- **Tidal** resolves per-user/guild via the first-party single-app-id flow and
  is the closest to correct, but its per-request isolation and freshness
  contract should be unified with the others.

This feature makes the streaming/consumption side **truly multi-tenant** across
YouTube, YouTube Music, Spotify, and Tidal: every playback request is served
using the requesting guild's owning user's credential, resolved from the
unified store, isolated from every other user, and kept fresh by the existing
watchdog. Sidecars remain **read-only** on tokens (the watchdog and web-ui are
the only writers). No fake-green: a provider/user that cannot be authenticated
fails that request cleanly and observably rather than silently serving the
wrong account or a stale token.

This is an **AWS EKS** feature. The on-prem single-tenant deployment is out of
scope and unchanged.

## Glossary

- **Source provider**: one of `youtube`, `youtube_music`, `spotify`, `tidal`.
  `discord` is identity-only and never streams.
- **Unified credential store**: the `hellodj-core` DynamoDB table item
  `PK=USER#<sub>` / `SK=SOURCECRED#<provider>`, `entityType=SourceCredential`,
  with plaintext status fields + an envelope-encrypted token blob.
- **Owning user / owner sub**: the Cognito `sub` recorded on the guild's
  `GUILD#<gid>` / `OWNER` item (`data.owner_sub`) — the user whose connected
  source credential a guild's playback uses.
- **Watchdog**: the durable `TokenWatchdog` in `playback-orchestrator` that
  refreshes near-expiry `SourceCredential` items for all users/providers.
- **Sidecar**: a streaming data-plane service — `spotify-stream` (librespot
  proxy, 8802), `tidal-stream` (8801), and the YouTube path via `lavalink` +
  the youtube-source plugin.
- **librespot session**: a `librespot.core.Session` bound to one Spotify user's
  cached credentials; one session streams one account.
- **Session pool**: an in-process, bounded, per-user collection of live
  provider sessions (e.g. librespot sessions keyed by user sub) with eviction.
- **Read-only token access**: a component decrypts and uses a stored token but
  never writes/refreshes it (the watchdog owns refresh).
- **Cross-user leakage**: any path where user A's playback request is served
  using user B's credential or session. This must be impossible.

## Requirements

### Requirement 1: Per-request user/guild credential resolution

**User Story:** As a guild owner, I want my guild's Spotify/YouTube/Tidal
playback to use MY connected account, so that my listening history, library,
and entitlements are mine and never mixed with another guild's.

#### Acceptance Criteria

1. WHEN a playback/stream request is made for a track from provider P in guild
   G THEN the system SHALL resolve the credential of G's owning user's
   `USER#<owner_sub>` / `SOURCECRED#P` item from the unified credential store
   before streaming.
2. WHERE guild G has no recorded owner (`GUILD#<G>`/`OWNER` absent) OR the owner
   has no `SOURCECRED#P` item THEN the system SHALL fail that request for
   provider P with an observable "no credential for this guild/provider" outcome
   and SHALL NOT fall back to any other user's credential.
3. WHEN two different guilds request tracks from the same provider P
   concurrently THEN each request SHALL be served with its OWN owning user's
   credential, and the system SHALL NOT serve one guild's request with another
   guild's credential (no cross-user leakage).
4. WHERE the legacy per-guild Secrets Manager secret
   (`hellodj/<stage>/guild/<gid>/<provider>`) exists AND no unified DynamoDB
   item exists THEN the system SHALL resolve the legacy secret (migration
   fallback), addressing exactly that guild's secret and no other's.
5. THE credential resolution SHALL be cached per `(guild_id, provider)` with a
   bounded TTL, and the cache key SHALL include the guild id so a cached
   resolution is never returned for a different guild.

### Requirement 2: Read-only token use with watchdog-driven freshness

**User Story:** As the platform operator, I want the streaming sidecars to only
READ tokens and rely on the watchdog for refresh, so that token refresh has a
single owner and a bot/sidecar bounce never loses or corrupts credentials.

#### Acceptance Criteria

1. THE streaming sidecars and the bot playback path SHALL be read-only on
   `SourceCredential` token material: they decrypt and use tokens but SHALL NOT
   write, re-encrypt, or refresh the stored credential item.
2. WHEN a resolved access token is already expired (within a configured skew)
   THEN the consumer SHALL re-read the credential item once, bypassing its TTL
   cache, to pick up the value the watchdog refreshed out-of-band, and SHALL use
   the freshest available token rather than the dead one.
3. WHERE the watchdog has marked a user+provider credential `refresh_status =
   failed` THEN a stream request for that user+provider SHALL fail that request
   observably rather than attempt to stream with a known-bad token.
4. THE watchdog SHALL continue to refresh near-expiry credentials for ALL users
   and ALL configured providers (youtube, youtube_music, spotify, tidal) with
   per-item isolation, unchanged by this feature.

### Requirement 3: Multi-account Spotify streaming (librespot session pool)

**User Story:** As a guild owner with Spotify Premium, I want my guild to stream
from MY Spotify account even when other guilds are streaming from theirs, so
that Spotify works for everyone, not just the first account connected.

#### Acceptance Criteria

1. THE spotify-stream sidecar SHALL maintain a POOL of librespot sessions keyed
   by owning user (Cognito sub), NOT a single global session.
2. WHEN a stream request for guild G arrives THEN the sidecar SHALL select or
   create the librespot session for G's owning user and serve the track from
   that user's session.
3. WHERE a user's librespot session does not exist THEN the sidecar SHALL create
   it from that user's stored Spotify credential (resolved per R1), and SHALL
   NOT require an interactive OAuth flow at stream time when a stored credential
   is present.
4. THE session pool SHALL be bounded (a configurable maximum number of live
   sessions) and SHALL evict the least-recently-used session when the bound is
   exceeded, closing the evicted session cleanly.
5. WHERE a user's Spotify account is not Premium OR the credential cannot
   establish a valid session THEN that user's stream requests SHALL fail
   observably and SHALL NOT affect any other user's session.
6. THE spotify-stream sidecar SHALL NOT hold a single ambient/default Spotify
   session that any guild would fall back to (no shared-account fallback).
7. THE session pool SHALL be isolated per user: one user's session, cache, and
   audio data SHALL never be served for another user's request.

### Requirement 4: Multi-tenant YouTube / YouTube Music playback

**User Story:** As a guild owner, I want my guild's YouTube playback to use my
connected YouTube credential even when another guild is playing YouTube at the
same time, so that per-guild YouTube auth is real and not first-writer-wins.

#### Acceptance Criteria

1. WHEN a YouTube/YouTube Music track is resolved for guild G THEN the system
   SHALL use G's owning user's YouTube credential (OAuth refresh token + PoToken
   + visitorData), resolved per R1 and sent together in a single credential
   application (never split across calls).
2. WHERE the current architecture serializes YouTube credentials on one shared
   Lavalink node THEN the system SHALL guarantee that each track RESOLUTION uses
   the correct guild's credential (a per-resolution correctness guarantee), and
   the design SHALL state explicitly whether true concurrent per-guild
   isolation is achieved or deferred.
3. IF the design chooses a node-per-user/guild Lavalink pool for true
   concurrency THEN it SHALL define pool sizing, assignment, and teardown, and
   SHALL preserve the single `POST /youtube` all-fields-together contract per
   node.
4. WHERE a guild has no connected YouTube credential THEN the system SHALL
   preserve the existing untouched global credential-store push path (no
   per-guild swap), exactly as today, and SHALL NOT force that guild through a
   per-user path it has not opted into.
5. WHEN a guild's YouTube access token is expired THEN resolution SHALL use the
   watchdog-refreshed value (per R2.2), never a dead token.

### Requirement 5: Multi-tenant Tidal streaming

**User Story:** As a guild owner, I want my guild's Tidal playback to use my
connected Tidal account concurrently with other guilds, so that Tidal is as
multi-tenant as the credential store already is.

#### Acceptance Criteria

1. WHEN a Tidal track is streamed for guild G THEN the tidal-stream sidecar
   SHALL resolve and use G's owning user's Tidal credential (per R1) for that
   request.
2. THE tidal-stream sidecar SHALL support concurrent requests from different
   guilds using different users' Tidal credentials without one request's
   credential affecting another's.
3. THE Tidal first-party single-app-id refresh behavior SHALL remain unchanged
   (the watchdog's existing Tidal adapter continues to own refresh); this
   feature only changes how the sidecar SELECTS which user's token to use per
   request.
4. WHERE a guild has no Tidal credential (no unified item and no legacy secret)
   THEN the Tidal request SHALL fail observably without falling back to another
   user's token.

### Requirement 6: Isolation and no cross-user leakage (correctness property)

**User Story:** As the platform operator, I want a hard guarantee that no user
is ever served another user's audio or credential, so that the platform is safe
to run multi-tenant.

#### Acceptance Criteria

1. FOR ANY two distinct guilds G1 and G2 with distinct owning users, no stream
   request for G1 SHALL be served using G2's credential, session, or cached
   audio, under any interleaving of concurrent requests.
2. THE per-user session pool AND every credential/audio cache SHALL be keyed by
   an identity that uniquely determines the user (owner sub), so a cache hit can
   never cross users.
3. WHEN a session or credential is evicted or invalidated for user A THEN no
   residual state SHALL allow a later request to serve user A's data to user B
   (or vice versa).
4. Token values SHALL never be written to logs by any component in this feature.

### Requirement 7: Observability and honest failure

**User Story:** As an operator debugging playback, I want each per-user/provider
failure to be visible and attributable, so that a broken account is diagnosable
without leaking secrets.

#### Acceptance Criteria

1. WHEN a stream request fails to resolve or authenticate a user's credential
   THEN the system SHALL emit a log line naming the provider and a non-secret
   reason (e.g. `no_credential`, `refresh_failed`, `not_premium`,
   `session_create_failed`) WITHOUT any token material.
2. WHEN a credential authentication failure is detected for a user+provider
   independently of a stream request (e.g. during a background session-health
   check or a proactive session build) THEN the system SHALL act on that
   failure by setting that user+provider's session/health state to a SPECIFIC
   failure state (e.g. `unhealthy` / `failed`), not merely preventing a success
   status.
3. THE spotify-stream `/health` and `/auth/status` (or provider equivalents)
   SHALL report multi-session state (e.g. number of live sessions, and per-user
   session states including any specific failure states) rather than a single
   global session status.
4. WHERE a provider/user is unavailable THEN the failure SHALL be scoped to that
   request/user and SHALL NOT crash the sidecar or degrade other users' streams.
5. THE system SHALL NOT present a healthy/green status for a provider path that
   cannot actually stream (no fake-green): an unimplemented or unauthenticated
   path SHALL be observably not-ready for that user without masking it.

### Requirement 8: Resource bounds and lifecycle

**User Story:** As the platform operator, I want per-user sessions and caches
bounded and cleaned up, so that a large number of connected users cannot
exhaust sidecar memory or leak sessions.

#### Acceptance Criteria

1. THE per-user session pool SHALL enforce a configurable maximum size and
   evict the least-recently-used session when exceeded.
2. WHEN a session is idle beyond a configurable timeout THEN the sidecar SHALL
   close and remove it, releasing its resources.
3. THE per-track audio cache SHALL remain bounded (max entries + TTL) and SHALL
   be keyed such that entries cannot be shared across users (per R6.2).
4. WHEN the sidecar receives a shutdown signal THEN it SHALL close all live
   sessions cleanly within the shutdown window.

### Requirement 9: Deployment, packaging, and least privilege

**User Story:** As the platform operator, I want these sidecars deployed the
Nix-native way with least-privilege IAM, so that they fit the existing platform
guarantees.

#### Acceptance Criteria

1. THE spotify-stream and tidal-stream sidecars SHALL be packaged as Nix-built
   OCI images with NO Debian/Ubuntu base layers.
2. THE sidecars' IAM (IRSA) SHALL grant READ on the `hellodj-core` table's
   credential items and KMS **Decrypt only** on the source-credentials CMK — no
   write/encrypt on token material.
3. WHERE a sidecar needs a persistent per-user credential cache (e.g.
   librespot's stored-credential files) THEN the storage SHALL be scoped and
   sized so it survives a restart without cross-user mixing, OR the design SHALL
   justify deriving sessions from the DynamoDB store on each start instead.
4. THE watchdog's existing least-privilege model (RW + full CMK for
   web-ui/orchestrator, Decrypt-only for readers) SHALL remain unchanged.

### Requirement 10: Correct-by-construction rollout (no live migration to preserve)

**User Story:** As the platform operator with ZERO existing customers and no
data to migrate, I want the per-user streaming model built PROPERLY as the one
true path, so that we are not carrying compatibility shims for users who do not
exist.

#### Acceptance Criteria

1. THE unified per-user DynamoDB credential store SHALL be treated as the single
   source of truth for streaming credential resolution; the legacy per-guild
   Secrets Manager path is NOT a required long-term fallback and MAY be removed
   in this feature's implementation since there is no live guild depending on
   it.
2. WHERE a legacy per-guild Secrets Manager secret still exists AND a unified
   DynamoDB credential also exists for the same guild/provider THEN the unified
   DynamoDB credential SHALL take precedence.
3. THE watchdog's storage schema and the web-ui's write path MAY be changed as
   needed to implement the multi-tenant consumption model correctly (there is
   no active migration and no customer data to preserve); WHERE such a change is
   made THEN the writer (web-ui), the refresher (watchdog), and the reader
   (sidecars/bot) SHALL be updated together so the schema stays internally
   consistent across all three.
4. WHERE a provider path is not yet converted to multi-tenant THEN it SHALL be
   explicitly marked not-ready (per R7.5) rather than silently serving a single
   account.
5. THE implementation SHALL NOT retain a single-global-account code path for any
   provider as a "temporary" measure; each provider is either fully per-user or
   explicitly not-ready (no hidden single-tenant fallback).

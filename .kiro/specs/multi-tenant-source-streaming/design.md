# Design Document

Feature: Multi-Tenant Source Streaming

## Overview

The credential **control plane** is already multi-tenant: the unified store
(`hellodj-core`, `USER#<sub>/SOURCECRED#<provider>`, envelope-encrypted) plus
the durable `TokenWatchdog` keep every user's YouTube/YouTube Music/Spotify/
Tidal token fresh with per-item isolation. The **data plane** (the streaming
sidecars) is not: `spotify-stream` and `tidal-stream` each bind ONE account at
startup, and YouTube serializes one credential on a shared Lavalink node.

This design makes the data plane per-user across all four providers by
introducing one shared consumption contract:

> Every stream/resolution request carries the **owning user's sub** (derived
> from the guild), resolves THAT user's `SOURCECRED#<provider>` credential from
> the unified store (read-only, decrypt), and serves the request from a
> **per-user session/credential** — never a single ambient account.

The mechanism differs per provider because their streaming engines differ, but
they share: (1) the same per-user credential resolver, (2) a per-user
keyed-by-`sub` session/credential registry with bounded LRU + idle eviction,
(3) read-only token use with watchdog-driven freshness, and (4) honest,
per-user failure states (no fake-green, no cross-user leakage).

Because there are **zero customers and no live migration** (R10), this is built
as the single true path: no lingering single-global-account fallback survives.
The legacy per-guild Secrets Manager path is kept only as an optional,
precedence-losing lookup and is not load-bearing.

### Requirements coverage map

| Requirement | Where addressed |
|---|---|
| R1 per-request resolution | `UserCredentialResolver` + guild→owner lookup (shared) |
| R2 read-only + freshness | `UserCredentialResolver` expiry re-read; `refresh_status` gate |
| R3 Spotify session pool | `SpotifySessionPool` (spotify-stream) |
| R4 YouTube multi-tenant | `YouTubeCredentialInjector` node-pool + per-resolution lock |
| R5 Tidal multi-tenant | `TidalSessionRegistry` per-request token selection |
| R6 isolation / no leakage | `sub`-keyed registries + caches; leakage property tests |
| R7 observability / honest failure | per-user `SessionState`; multi-session health |
| R8 resource bounds | LRU + idle-timeout in the shared `SessionRegistry` |
| R9 packaging / least privilege | Nix images; IRSA read + KMS Decrypt-only |
| R10 correct-by-construction | unified store is source of truth; no single-tenant path |

## Architecture

```
                    Discord bot / lavalink (resolution + playback)
                                    │  request carries guild_id
                                    ▼
        ┌───────────────────────────────────────────────────────────┐
        │  Shared consumption contract (per-provider sidecar/path)    │
        │                                                             │
        │   guild_id ──► OwnerLookup ──► owner_sub                    │
        │   owner_sub + provider ──► UserCredentialResolver           │
        │        │  (read hellodj-core, KMS Decrypt-only)             │
        │        ▼                                                    │
        │   TokenState (access/refresh/expires/extra)  ── read-only   │
        │        │                                                    │
        │        ▼                                                    │
        │   SessionRegistry[sub]  (bounded LRU + idle evict)          │
        │        │           │            │                           │
        │   Spotify pool   Tidal reg   YouTube node-pool              │
        └───────────────────────────────────────────────────────────┘
                                    │
                                    ▼
              hellodj-core (DynamoDB)          KMS CMK (Decrypt only)
              USER#<sub>/SOURCECRED#<p>        alias/hellodj-source-creds-<stage>
                       ▲
                       │ refresh (RW + Encrypt/Decrypt)
                 TokenWatchdog (playback-orchestrator)  ── unchanged
```

Key point: the **watchdog is unchanged** — it already refreshes all users/
providers. This feature only changes how the sidecars SELECT and USE the
per-user credential the watchdog keeps fresh.

## Components and Interfaces

### `UserCredentialResolver` (shared logic, per sidecar + bot)

A small, dependency-light resolver that generalizes the existing bot
`DynamoCredentialResolver`. It resolves a provider credential for a given
`(guild_id, provider)`:

1. `OwnerLookup.owner_of(guild_id)` → `owner_sub` (reads
   `GUILD#<gid>`/`OWNER`.`data.owner_sub`, the item the web-ui writes).
2. `CredentialItemStore.get(USER#<owner_sub>, SOURCECRED#<provider>)`.
3. Decrypt the envelope blob via `token_crypto.decrypt_blob` (KMS Decrypt-only),
   yielding a `TokenState`.
4. Return the `TokenState` (never logs token values).

Contract details (R1, R2, R6):

- **Keyed by `sub`, cached per `(guild_id, provider)`** with a bounded TTL; the
  cache key includes the guild id so a hit can never cross guilds/users.
- **Expiry re-read (R2.2):** if the decrypted `expires_at` is at/within skew of
  now, re-read the item ONCE bypassing the cache to pick up the watchdog's
  fresh value; never return the dead token.
- **`refresh_status=failed` gate (R2.3):** if the item's plaintext
  `refresh_status` is `failed`, the resolver returns a typed
  `CredentialUnavailable(reason="refresh_failed")` instead of a token — the
  request fails observably rather than streaming with a known-bad token.
- **No cross-user fallback (R1.2):** no owner / no item / decrypt failure →
  `CredentialUnavailable`; NEVER another user's credential.
- **Legacy fallback (R1.4, R10.1):** optional; only when no unified item exists;
  precedence-losing; addressed strictly by the requesting guild's secret name.

The existing `bot/playback/guild_credentials.py::DynamoCredentialResolver`
already implements most of this; it is promoted to the shared
`hellodj_platform_logic` package (or mirrored verbatim, matching the repo's
existing "shared copy, change together" convention) so all three sidecars +
the bot use ONE implementation. The `refresh_status=failed` gate and a typed
`CredentialUnavailable` result are added.

### `SessionRegistry[K, S]` (shared, generic, per sidecar)

A bounded, thread/async-safe registry mapping a user key (`sub`) to a live
provider session `S`, satisfying R6 (isolation) and R8 (bounds):

- `get_or_create(sub, factory)` — returns the live session for `sub`, creating
  it via `factory(sub)` on miss; updates LRU recency.
- **Bounded LRU (R8.1):** `max_sessions` cap; on overflow, evict the
  least-recently-used session and call `session.close()`.
- **Idle eviction (R8.2):** a sweeper closes sessions idle beyond
  `idle_timeout`.
- **Per-key isolation (R6.2/R6.3):** the map key IS the `sub`; eviction removes
  all state for that key; a new request for an evicted key rebuilds cleanly with
  no residue.
- **`SessionState` per key (R7):** `building | ready | failed(reason) | closed`
  — surfaced by health endpoints; a `failed` state is set on a background/health
  detected auth failure (R7.2), not merely "not ready".
- **Failure isolation (R7.4):** a `factory` raise sets that key's state to
  `failed` and propagates a per-request error; it never crashes the sidecar or
  other keys.
- **Clean shutdown (R8.4):** `close_all()` on SIGTERM/SIGINT within the window.

## Provider designs

### Spotify — per-user librespot session pool (R3)

`spotify-stream/app.py` is rewritten from a single global `_session` to a
`SpotifySessionPool = SessionRegistry[str, librespot.Session]`.

- **Request shape:** the stream/preload routes take the owning identity. Two
  options; design chooses **path-embedded guild id** to match how Lavalink
  addresses the sidecar today: `GET /stream/<guild_id>/<track_id>` (and
  `/preload/<guild_id>/<track_id>`). The sidecar resolves `owner_sub` from
  `guild_id` via `UserCredentialResolver` and selects the pool session.
  (Rationale: Lavalink builds the HTTP source URL, so the guild id must ride in
  the URL; a header is equally acceptable if the caller can set it.)
- **Session factory (R3.3):** build a librespot `Session` from the user's stored
  Spotify credential. librespot needs a *user session*, not the app
  `client_id/secret`. Two supported inputs:
  - the stored `TokenState` carries a librespot-usable credential blob
    (preferred: the web-ui Spotify connect flow stores the librespot
    `credentials.json` material as the token blob), OR
  - librespot's stored-credential cache file per user under
    `DATA_DIR/<sub>/spotify-credentials.json` (R9.3), rebuilt from the store on
    miss.
  The factory does NOT run an interactive OAuth at stream time when a stored
  credential exists.
- **No shared/default session (R3.6):** the global `_session` is deleted; there
  is no ambient account any guild falls back to.
- **Premium / invalid handling (R3.5):** a non-Premium or invalid credential
  raises in the factory → that `sub`'s `SessionState=failed(not_premium|
  session_create_failed)`, scoped to that user.
- **Per-user cache (R6.2, R8.3):** the track audio cache key becomes
  `(sub, track_id)` so cached audio can never be served across users.
- **Health (R7.3):** `/health` and `/auth/status` report the pool: live session
  count and per-`sub` states (including `failed`), not one global status.
- **Web-ui connect change (R10.3):** because there is no migration, the web-ui
  Spotify connect flow is updated (together with the store schema if needed) to
  capture and store a librespot-usable credential per user, so the sidecar can
  build a session non-interactively. Writer + watchdog + reader move together.

### YouTube / YouTube Music — node-pool + per-resolution lock (R4)

The youtube-source plugin's `POST /youtube` replaces ALL credential fields on a
node, so one node = one live YouTube credential. The existing
`YouTubeCredentialInjector` already keys a per-node `asyncio.Lock` and its
`swap_lock(node_key)` seam anticipates a node pool. This design realizes the
node pool:

- **`LavalinkNodePool`** maps `owner_sub` → a Lavalink node (session) from a
  configured pool of nodes. A YouTube resolution for guild G:
  1. resolves G's `owner_sub` + YouTube credential (per R1/R2, single
     all-fields payload — R4.1);
  2. picks the node assigned to `owner_sub` (or the least-loaded free node),
     acquires that node's `swap_lock`;
  3. pushes the credential via the single `POST /youtube` and resolves the
     track under the held lock (R4.1).
- **Concurrency (R4.2/R4.3):** with N nodes, up to N distinct users resolve
  YouTube concurrently, each on its own node — true per-guild isolation up to
  the pool size. Within a node, the per-node lock preserves per-resolution
  correctness. The design states this explicitly: **concurrency is bounded by
  pool size**; beyond N concurrent distinct users, requests serialize on a
  shared node (still correct per resolution, R4.2).
- **No-credential guilds unchanged (R4.4):** a guild with no connected YouTube
  credential resolves to `None` and triggers NO swap — it uses the untouched
  global credential-store push exactly as today.
- **Freshness (R4.5):** the resolved credential comes through the expiry
  re-read path (R2.2), never a dead token.
- **Sizing/teardown (R4.3):** node pool size, `sub`→node assignment (sticky with
  LRU reassignment), and idle node reclamation are config-driven; the shared
  Lavalink session contract per node is preserved.

The single-node deployment remains valid: pool size 1 == today's behavior
(per-resolution correctness, no concurrency), so this is strictly additive.

### Tidal — per-request user token selection (R5)

`tidal-stream` currently binds ONE `refresh_secret_id` at startup. This design
replaces the single-account `TidalRefreshTokenStore` with a
`TidalSessionRegistry = SessionRegistry[str, TidalClient]`:

- **Request shape:** the stream route takes the owning identity (path-embedded
  guild id, mirroring Spotify), resolves `owner_sub`, and selects that user's
  Tidal client.
- **Per-user token (R5.1/R5.2):** each `TidalClient` uses the owning user's
  Tidal `TokenState` (resolved per R1/R2). Concurrent requests from different
  guilds use different users' tokens with no shared mutable token state.
- **Refresh unchanged (R5.3):** the first-party single-app-id refresh stays
  owned by the watchdog's existing Tidal adapter; the sidecar is read-only and
  only SELECTS which user's token to use. The `app_id`/`callback_url`
  (single-app-id) remain global config; only the per-user token varies.
- **No-credential guilds (R5.4):** no unified item and no legacy secret →
  request fails observably, no cross-user fallback.

## Data Models

No new table. The `hellodj-core` `SourceCredential` item schema is reused. If
the Spotify librespot-credential capture (R3/R10.3) requires storing a
librespot-specific blob, it is stored inside the SAME envelope-encrypted token
blob (an `extra`/typed field of `TokenState`), NOT as a new plaintext field, so
the KMS-Decrypt-only reader contract and the "tokens never in plaintext"
guarantee hold. Writer (web-ui), watchdog, and readers are updated together
(R10.3).

## Correctness Properties

Property 1: No cross-user leakage (R6.1, R6.2, R6.3). For any two distinct subs
A≠B and ANY interleaving of concurrent requests, a request keyed to A never
returns B's session, token, or cached audio. The `SessionRegistry` and every
credential/audio cache are keyed by `sub` (audio cache by `(sub, track_id)`), so
a hit cannot cross users; eviction/invalidation of A leaves no residue reachable
by B.
**Validates: Requirements 6.1, 6.2, 6.3**

Property 2: Read-only use with watchdog-driven freshness (R2.1, R2.2, R2.3). A
consumer NEVER writes/encrypts/refreshes the credential item. A resolved access
token that is expired within skew triggers exactly ONE uncached re-read to pick
up the watchdog-refreshed value; an item with `refresh_status=failed` yields
`CredentialUnavailable(refresh_failed)` and never a stream with a dead token.
**Validates: Requirements 2.1, 2.2, 2.3**

Property 3: Bounded resources and lifecycle (R8.1, R8.2, R8.4). The registry
never exceeds `max_sessions`; exceeding it evicts and closes the LRU session;
idle-beyond-timeout sessions are swept and closed; `close_all()` closes every
live session on shutdown.
**Validates: Requirements 8.1, 8.2, 8.4**

Property 4: Honest, isolated failure (R7.1, R7.2, R7.4, R7.5). A factory/auth
failure sets a SPECIFIC per-`sub` failure state (`failed(<reason>)`), is
reported by the health surface, never surfaces as green, and never crashes the
sidecar or affects another `sub`'s session.
**Validates: Requirements 7.1, 7.2, 7.4, 7.5**

Property 5: No single-tenant path (R10.5). No code path selects or uses a
credential without a resolved owning `sub`; there is no ambient/default account
any request can fall back to.
**Validates: Requirements 10.5**

## Testing Strategy

- **Unit + fakes (no live AWS):** `UserCredentialResolver`, `SessionRegistry`,
  and each provider's factory are exercised with in-memory fakes for the
  DynamoDB store, `OwnerLookup`, KMS decrypt, and the provider session, so tests
  run without boto3/AWS or a real Spotify/Tidal/Lavalink backend (mirrors the
  existing `guild_credentials` / `user_entitlements` fake-friendly pattern).
- **Property-based tests** for the correctness properties P1–P5 above
  (Hypothesis): randomized concurrent `(sub, provider, track)` access asserting
  no cross-user leakage (P1), read-only + single expiry re-read + failed-status
  gate (P2), registry bounds/eviction/close (P3), specific per-`sub` failure
  states (P4), and no credential selection without a resolved `sub` (P5).
- **Per-provider factory contract tests:** Spotify session builds from a stored
  (non-interactive) credential and rejects non-Premium; Tidal client uses the
  per-request user token and never shares mutable token state; YouTube node
  pool applies one all-fields payload per resolution under the node lock and
  isolates up to pool size.
- **Health/observability tests:** `/health` + `/auth/status` (and provider
  equivalents) report per-`sub` session states including specific failure
  states, never a single global status, and never leak token material.
- **CDK/IAM tests:** the sidecar IRSA roles have DynamoDB read (scoped to
  `SourceCredential` + `GUILD#*/OWNER`) + KMS Decrypt-only and NO
  write/encrypt on token material.
- **Gate compliance:** ruff + the 500-line ceiling on the touched component
  trees; the shared resolver/registry live under the shared package with their
  own tests.

## Error Handling

| Condition | Behavior |
|---|---|
| No owner for guild | `CredentialUnavailable(no_owner)` → request fails, logged, no fallback |
| No `SOURCECRED#<p>` item | `CredentialUnavailable(no_credential)` |
| `refresh_status=failed` | `CredentialUnavailable(refresh_failed)` (R2.3) |
| Decrypt failure (tamper/KMS) | `CredentialUnavailable(decrypt_failed)`; item treated unusable |
| Expired access token | one uncached re-read (R2.2); if still expired → `refresh_failed` |
| Spotify non-Premium | per-`sub` `SessionState=failed(not_premium)` (R3.5) |
| Session factory raise | per-`sub` `failed(session_create_failed)`; isolated (R7.4) |
| Pool overflow | LRU evict + close; new session built (R8.1) |
| Idle session | swept + closed (R8.2) |
| SIGTERM/SIGINT | `close_all()` within shutdown window (R8.4) |

All failures log a provider + non-secret reason; token values are never logged
(R6.4, R7.1).

## Deployment & least privilege (R9)

- `spotify-stream` and `tidal-stream` stay Nix-built OCI images (no Debian).
  Spotify is the ported Python `librespot`+`aiohttp` app; Tidal keeps its Nix
  build. Both gain the `UserCredentialResolver` (DynamoDB read + KMS Decrypt).
- **IRSA (R9.2):** both sidecars' service-account roles get READ on the
  `hellodj-core` table (scoped to `SourceCredential` + `GUILD#*/OWNER` items)
  and KMS **Decrypt only** on `alias/hellodj-source-creds-<stage>`. No
  write/encrypt on token material. This matches the existing reader grants in
  `workloads-stack.ts` `SOURCE_CREDENTIAL_KMS_COMPONENTS` (spotify-stream and
  tidal-stream are already Decrypt-only readers there — verify the DynamoDB
  read scope covers `GUILD#*/OWNER`).
- **Per-user credential cache storage (R9.3):** if librespot per-user cache
  files are used they live under a `DATA_DIR/<sub>/` path (per-user
  subdirectory) on the sidecar's volume; the design prefers rebuilding sessions
  from the DynamoDB store on start over durable local caches to avoid
  cross-user file mixing, and justifies whichever is chosen.
- **Watchdog IAM unchanged (R9.4).**
- New env: sidecars gain `HELLODJ_CORE_TABLE`, `HELLODJ_SOURCE_CREDS_KMS_KEY_ID`,
  `AWS_REGION`, and session-pool tunables (`*_MAX_SESSIONS`,
  `*_SESSION_IDLE_TIMEOUT`). YouTube node pool gains
  `HELLODJ_LAVALINK_NODE_POOL` (list/count).

## Design decisions & tradeoffs

1. **Guild id in the request path, not the user sub.** Lavalink builds the
   sidecar URL and knows the guild, not the Cognito sub. The sidecar resolves
   `guild→owner_sub` server-side. This keeps the sub server-side only (never in
   a URL/log) and reuses the existing ownership item.
2. **One shared `SessionRegistry` abstraction across providers.** Spotify's
   librespot sessions, Tidal clients, and YouTube node assignments are all
   "bounded per-user resources with lifecycle" — one tested abstraction reduces
   the surface for isolation bugs (the highest-risk area, R6).
3. **YouTube concurrency bounded by node-pool size, not unbounded.** True
   per-guild concurrency needs N Lavalink nodes; we make it configurable and
   correct-per-resolution at any size (size 1 == today). Unbounded per-guild
   nodes are rejected as cost-prohibitive.
4. **No single-tenant fallback retained (R10.5).** With zero customers, keeping
   a "temporary" single-account path would be the exact fake-green trap we are
   removing. Each provider is per-user or explicitly not-ready.
5. **librespot credential capture moves to the web-ui connect flow.** The app
   `client_id/secret` cannot open a stream session; the user session credential
   must be captured at connect time and stored (encrypted). Since there is no
   migration, the connect flow + store + reader change together.

## Risks

- **R-1 librespot credential model:** ~~confirming librespot-python can build a
  `Session` from a stored (non-interactive) credential per user is the critical
  unknown; the design's session factory depends on it.~~ **RESOLVED (task 2.1
  spike, verified against `kokarare1212/librespot-python` `librespot/core.py`,
  the `pkgs.librespot` this component builds on):**

  **Conclusion: YES — a per-user `Session` CAN be built fully non-interactively
  from a stored credential blob, but that blob is librespot-specific and can
  ONLY be produced by a one-time interactive capture. So the session *factory*
  is non-interactive (the stream-time requirement of R3.3), while the *connect*
  flow is one-time interactive (task 2.2).**

  Facts (librespot API surface):
  - `Session.Builder(conf).stored(stored_credentials_str).create()` builds a
    `Session` in-memory from a **base64-encoded JSON string** — no file, no
    interaction, no `client_id`/`client_secret`. `Session.Builder.stored_file(path)`
    is the equivalent file-based path (reads the same JSON). `create()` accepts
    any `login_credentials` the builder set, so `.stored(...)`/`.stored_file(...)`
    is a complete, non-interactive login. This is exactly what the
    `SpotifySessionPool` factory needs (task 2.3): resolve the per-user blob from
    the unified store, `Session.Builder(...).stored(blob).create()`, no OAuth at
    stream time.
  - The **reusable credential material** the web-ui must capture is this exact
    JSON object (as librespot writes it on a successful auth from the
    `APWelcome.reusable_auth_credentials`):
    ```json
    {
      "username":    "<canonical_username>",
      "credentials": "<base64 reusable_auth_credentials>",
      "type":        "AUTHENTICATION_STORED_SPOTIFY_CREDENTIALS"
    }
    ```
    (librespot also accepts the alias keys `auth_type` for `type` and `auth_data`
    for `credentials`.) The `credentials` field is a long-lived, reusable Spotify
    auth blob — NOT a standard OAuth access/refresh token, and NOT derivable from
    the `hellodj/<stage>/spotify` app `{client_id, client_secret}` secret. It is
    the ONLY material `librespot` streams with.
  - **This blob does not exist until a one-time interactive login produces it.**
    librespot has two capture paths, both interactive exactly once:
    1. **OAuth** — `Session.Builder(conf).oauth(url_callback).create()` opens a
       Spotify OAuth authorize URL (librespot's own built-in `keymaster` client,
       loopback redirect `http://127.0.0.1:5588/login`); on authorize, the
       `APWelcome` yields `reusable_auth_credentials`, which librespot serializes
       to the JSON above. This is what today's single-account `app.py`
       `_run_oauth_flow()` does.
    2. **Zeroconf / Spotify-Connect transfer** — `ZeroconfServer` receives the
       auth blob when a user transfers playback from an official Spotify client.
    Either way, the reusable blob is the *output* of a one-time interactive step.
  - **Premium constraint unchanged:** librespot can only STREAM audio for Spotify
    Premium accounts; a Free account can authenticate but a track load fails.
    This maps to the design's per-`sub` `failed(not_premium)` state (R3.5) — the
    Premium check is at first stream/track-load, not at session build.

  **Web-ui one-time-capture contract (feeds task 2.2):**
  - The web-ui Spotify **connect** flow runs the librespot OAuth capture ONCE per
    user (server-side, using librespot's built-in client — no operator Spotify
    app is consumed for this). It obtains the reusable-credentials JSON object
    above.
  - It stores that JSON object **inside the SAME envelope-encrypted `TokenState`
    blob** as a typed `extra` field (e.g. `extra.librespot_credentials`),
    per the Data Models section — never a new plaintext column, so the
    KMS-Decrypt-only reader contract and "tokens never in plaintext" hold. Writer
    (web-ui), watchdog, and reader (`spotify-stream`) change together (R10.3).
  - The loopback redirect (`127.0.0.1:5588`) used by librespot's console flow is
    NOT web-compatible as-is; task 2.2 must drive librespot's OAuth so the
    authorize URL is surfaced to the browser and the code/callback is completed
    server-side (the `oauth_url_callback` seam already exposes the URL; the
    connect route captures the resulting blob rather than relying on a local
    browser). If a clean server-side OAuth capture proves impractical, the
    Zeroconf/Spotify-Connect transfer capture is the documented fallback — either
    way the *output* is the same reusable JSON blob the factory consumes.
  - **Refresh:** the reusable blob is long-lived and is NOT refreshed by the
    standard OAuth `RefreshClient`; the sidecar remains read-only and the watchdog
    does not need to (and must not attempt to) OAuth-refresh it. If the blob is
    ever rejected by Spotify, that user re-runs the one-time connect capture —
    surfaced as per-`sub` `failed(session_create_failed)` (R7).

  Net effect on the plan: task 2.3's factory is confirmed non-interactive and
  unblocked; task 2.2 is the one-time interactive capture + store of the
  reusable JSON blob described above. No design change to the pool is required.
- **R-2 Lavalink node pool cost:** N nodes for N concurrent YouTube users.
  Mitigation: configurable pool, sticky assignment, size 1 default (== today).
- **R-3 per-user DATA_DIR growth:** bounded by LRU/idle eviction (R8) and
  preferring store-derived sessions over durable local caches.

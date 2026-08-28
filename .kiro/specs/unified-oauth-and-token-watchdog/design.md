# Design Document

## Overview

This design unifies source OAuth for all providers, moves credential storage
from per-guild Secrets Manager secrets into the `hellodj-core` DynamoDB table
(one item per user+provider, with application-layer envelope encryption on the
token blob), and adds a durable token-refresh **watchdog loop hosted inside the
existing `playback-orchestrator` container** so refresh survives bot bounces.
The default playback source becomes YouTube.

The design deliberately reuses primitives that already exist:

- `hellodj_platform_logic.data_access.CoreTable` — item get/put/query/delete +
  optimistic-lock read-modify-write. New credential items use the same patterns
  as `entitlement_service` / `guild_sources`.
- `hellodj_platform_logic.tidal_refresh` — the pure refresh contract shape.
  We generalize its idea into a provider-agnostic `RefreshClient` protocol and
  keep Tidal routing through the existing first-party logic unchanged.
- `playback-orchestrator` — a standing container with a run loop and DynamoDB
  access. We add the watchdog loop next to its health server.
- `source_oauth` / `source_token_exchange` — the authorize-URL builders and
  code→token exchanges. We keep the builders and route their output through the
  new credential store.

## Architecture

```
┌──────────────┐   authorize    ┌──────────────┐
│   Browser    │ ─────────────▶ │  Provider    │
│ (config page)│ ◀───────────── │ (YT/Spotify/ │
└──────┬───────┘   code+state   │  Tidal/…)    │
       │                        └──────────────┘
       │ /auth/sources/<provider>/connect|callback
       ▼
┌──────────────────────────────────────────────┐
│                 web-ui (Flask)                │
│  source_oauth (authorize URL builders)        │
│  source_token_exchange (code→token per prov.) │
│  SourceCredentialService ──────────┐          │
└───────────────┬────────────────────┼──────────┘
                │ put encrypted blob  │ encrypt (KMS GenerateDataKey)
                ▼                     ▼
        ┌───────────────┐     ┌──────────────┐
        │ hellodj-core  │     │  KMS CMK      │
        │ USER#<sub>    │     │ source-creds  │
        │ SOURCECRED#p  │◀────│ (envelope)    │
        └───────┬───────┘     └──────────────┘
                │ read/write (optimistic lock)
       ┌────────┴─────────────────────────┐
       ▼                                   ▼
┌────────────────────┐          ┌────────────────────────┐
│ playback-orchestr. │          │ bot / tidal-stream /    │
│  WATCHDOG LOOP     │          │ spotify-stream (readers)│
│  every 5m:         │          │  resolve + decrypt +    │
│  find near-expiry  │          │  use access token       │
│  refresh via       │          │  (read-only on tokens)  │
│  RefreshClient     │          └────────────────────────┘
│  write back (enc)  │
└────────────────────┘
```

### Storage model

New item on `hellodj-core`:

| Field | Type | Notes |
|---|---|---|
| `PK` | S | `USER#<sub>` |
| `SK` | S | `SOURCECRED#<provider>` |
| `entityType` | S | `SourceCredential` |
| `data.connected` | BOOL | plaintext status |
| `data.connected_at` | N | epoch s |
| `data.updated_at` | N | epoch s |
| `data.expires_at` | N | plaintext access-token expiry (watchdog reads this) |
| `data.scope` | S | plaintext |
| `data.last_refresh_at` | N | plaintext |
| `data.refresh_status` | S | `ok` / `failed` |
| `data.refresh_error` | S | short reason on failure (never the token) |
| `data.enc_blob` | S | base64 ciphertext of the token JSON |
| `data.enc_key` | S | base64 KMS-wrapped data key |
| `data.kms_key_id` | S | CMK id used (for decrypt + rotation) |

Rationale for splitting status vs blob: the watchdog and UI enumerate/read
status (`expires_at`, `refresh_status`) **without** a KMS call; only refresh and
playback decrypt the blob. This keeps KMS traffic proportional to refreshes, not
to every list/render.

Per-guild items remain: the existing `GUILD#<gid>` / `SOURCE#<provider>`
metadata item is unchanged for guild-scoped connections; this spec adds the
per-user `USER#<sub>` / `SOURCECRED#<provider>` credential item as the unified,
encrypted, watchdog-managed store. Guild-owned source connections write a
credential item keyed by the connecting user's sub (the owner), and the guild
metadata item points at it — so a single user identity spans web-ui, watchdog,
and bot (mirrors how entitlements key on `sub`).

### Envelope encryption (`token_crypto`)

A new pure-ish module `token_crypto` in `hellodj_platform_logic` (so web-ui,
watchdog, and readers share ONE implementation):

- `encrypt_blob(plaintext: bytes, kms) -> EncryptedBlob` — calls
  `kms.generate_data_key(KeyId, KeySpec="AES_256")`, uses the plaintext data key
  with AES-GCM (via `cryptography`) to encrypt the blob, discards the plaintext
  data key, returns `{ciphertext, wrapped_key, key_id, nonce}`.
- `decrypt_blob(enc: EncryptedBlob, kms) -> bytes` — `kms.decrypt(wrapped_key)`
  to recover the data key, AES-GCM-decrypt.
- The KMS client is injected (a Protocol with `generate_data_key`/`decrypt`) so
  the module is unit-testable with a fake KMS (no AWS).
- Tokens are never logged; errors carry no plaintext.

`cryptography` is already a web-ui dependency (used by `cognito_jwt`), so no new
dep in web-ui; the watchdog (playback-orchestrator) and any reader that decrypts
gains `cryptography` + `boto3` (kms) in its flake/requirements.

### Unified refresh contract (`source_refresh`)

Generalize the Tidal shape into a provider-agnostic contract in
`hellodj_platform_logic.source_refresh`:

```python
@dataclass(frozen=True)
class TokenState:
    access_token: str
    refresh_token: str
    expires_at: float
    scope: str = ""
    extra: Mapping[str, Any] = field(default_factory=dict)  # provider fields

class RefreshClient(Protocol):
    provider: str
    def refresh(self, refresh_token: str, now: float) -> TokenState: ...

def needs_refresh(state: TokenState, now: float, skew: float) -> bool: ...
def apply_refresh(state, client, now, *, skew, force=False) -> TokenState: ...
```

Concrete clients:
- `GoogleRefreshClient` (youtube / youtube_music) — POST
  `https://oauth2.googleapis.com/token`, `grant_type=refresh_token`.
- `SpotifyRefreshClient` — POST `https://accounts.spotify.com/api/token`.
- `TidalRefreshClient` — a thin adapter delegating to the EXISTING
  `tidal_refresh.refresh_tidal` + `FirstPartyTidalOAuthClient` so Tidal's
  behavior and property tests are untouched (R4.5, R10.2).

`apply_refresh` mirrors `refresh_tidal`: fast-path if not expired, preserve
prior refresh token when the provider doesn't rotate, treat an already-expired
result as failure.

## Components and Interfaces

This section names each component and the interface it exposes/consumes.

### `hellodj_platform_logic.token_crypto`
- `EncryptedBlob(ciphertext, wrapped_key, key_id, nonce)` dataclass.
- `KmsClient` Protocol: `generate_data_key(**kwargs)`, `decrypt(**kwargs)`.
- `encrypt_blob(plaintext: bytes, kms: KmsClient, key_id: str) -> EncryptedBlob`.
- `decrypt_blob(enc: EncryptedBlob, kms: KmsClient) -> bytes`.

### `hellodj_platform_logic.source_refresh`
- `TokenState(access_token, refresh_token, expires_at, scope, extra)`.
- `RefreshClient` Protocol: `provider: str`, `refresh(refresh_token, now) -> TokenState`.
- `needs_refresh(state, now, skew) -> bool`; `apply_refresh(state, client, now, *, skew, force) -> TokenState`.
- Concrete: `GoogleRefreshClient`, `SpotifyRefreshClient`, `TidalRefreshClient` (adapter over existing `tidal_refresh`).

### `hellodj_platform_logic.data_access.CoreTable` (extended)
- New: `scan_entity(entity_type) -> Iterator[dict]` — paginated, key-projected
  (keys + `expires_at` + `refresh_status`, never `enc_blob`).

### `SourceCredentialService` (web-ui + watchdog shared)
- Consumes `CoreTable` + `token_crypto` + `KmsClient`.
- `store`, `status`/`status_for`, `load_token`, `disconnect`,
  `iter_near_expiry`, `record_refresh` (signatures below).

### `playback_orchestrator.token_watchdog.TokenWatchdog`
- Consumes `SourceCredentialService` + `{provider: RefreshClient}`.
- `tick()`, `run_forever()`; started on a daemon thread by `__main__.main`.

### web-ui routes (`auth.py` / `guild_routes.py` / `pages.py`)
- `/auth/sources/<provider>/connect|callback`, config/account status partials,
  Discord enable/reset — consume `SourceCredentialService` + existing
  `source_oauth` builders.

### playback readers (`bot/playback/guild_credentials.py`)
- Consume `CoreTable` (read) + `token_crypto` (decrypt) with the reader KMS
  decrypt grant; legacy Secrets Manager fallback.

### SourceCredentialService (method detail)

`SourceCredentialService` (web-ui module, importable by the watchdog) wraps
`CoreTable` + `token_crypto`:

- `store(sub, provider, token_state, *, connected_by)` — encrypt blob, put/merge
  the credential item (optimistic lock).
- `status(sub) -> list[...]` / `status_for(sub, provider)` — plaintext status
  only, no decrypt.
- `load_token(sub, provider) -> TokenState | None` — decrypt blob.
- `disconnect(sub, provider)` — delete the item.
- `iter_near_expiry(now, threshold)` — used by the watchdog; queries credential
  items (see access pattern below) and yields those needing refresh.
- `record_refresh(sub, provider, new_state | error)` — write-back with
  optimistic lock; sets `refresh_status`.

Watchdog enumeration access pattern: the watchdog must find near-expiry items
across all users. Options considered:
- Scan the table filtered by `entityType=SourceCredential` — simple, but a full
  scan. Acceptable at 1000 users (few thousand items) on a periodic 5-min loop.
- Add a GSI keyed by a coarse expiry bucket for targeted queries — more work,
  deferred. **Chosen: a filtered paginated scan** for v1 (documented; GSI is a
  future optimization if item count grows). The scan is added as
  `CoreTable.scan_entity(entity_type)` (paginated, projection to just the keys +
  `expires_at` + `refresh_status`, so it never pulls `enc_blob` during
  enumeration).

### Watchdog loop (in playback-orchestrator)

`playback_orchestrator/token_watchdog.py`:

- `TokenWatchdog(service, clients_by_provider, *, interval, threshold, clock)`.
- `tick()` — one pass: `service.iter_near_expiry(now, threshold)` → for each,
  pick `clients_by_provider[provider]`, `apply_refresh(force=…)`, then
  `service.record_refresh(...)`. Each item is independent; one failure logs +
  sets `refresh_status=failed` and continues (R5.4).
- `run_forever()` — sleep `interval` between ticks; catches and logs any
  loop-level exception so the container never dies (R5.4/R5.7).
- Started from `__main__.main()` on a daemon thread next to the health server,
  guarded by config: if no core table / KMS / clients are configured, the loop
  logs "degraded: watchdog disabled" and does not start (R5.7). Health server is
  unchanged.
- Idempotent + multi-replica safe via `record_refresh` optimistic lock (R5.5):
  a losing writer re-reads and either sees the already-refreshed token (skips)
  or retries.

### Playback reader integration

`bot/playback/guild_credentials.py` gains a DynamoDB-backed resolution branch:
resolve the `USER#<sub>`/`SOURCECRED#<provider>` item, decrypt via `token_crypto`
with the reader's KMS decrypt grant, and use it; fall back to the legacy
per-guild Secrets Manager secret when the DynamoDB item is absent (R6.5,
migration). The YouTube just-in-time `POST /youtube` swap and its all-fields-
together invariant are preserved unchanged (R6.3). Cache TTL + guild-scoped key
unchanged (R6.4).

### Default source = YouTube

- `entitlements_core` / config default: `default_source` resolves to `youtube`
  when unset. Concretely, a shared constant `DEFAULT_SOURCE = "youtube"` in the
  config layer; `ConfigStore.get_global`/`get_guild` callers use it, the config
  form preselects it, and the bot's source map treats unset as `youtube`
  (R7.1–R7.3).

## Infrastructure (CDK)

New/changed in `platform/infra/lib`:

- **`data-stack.ts`**: add a dedicated **source-credentials KMS CMK**
  (`hellodj-source-creds-<stage>`), key rotation enabled, exported for grants.
  (Table already has AWS_MANAGED at-rest encryption — unchanged, R3.1.)
- **`workloads-stack.ts`**:
  - web-ui role: `kms:GenerateDataKey` + `kms:Encrypt` on the CMK (write path),
    `kms:Decrypt` only if it completes a flow needing plaintext; already has
    `grantReadWriteData` on core table. Wire `HELLODJ_SOURCE_CREDS_KMS_KEY_ID`
    env. Wire `HELLODJ_GOOGLE_OAUTH_SECRET_ARN`, `HELLODJ_DISCORD_OAUTH_SECRET_ARN`,
    and `POTOKEN_SERVER_URL` (env gaps the context map flagged). NOTE: the
    Discord OAuth client-id + lazy-secret-ARN wiring already landed in a prior
    session (`workloads-stack.ts` pushes `DISCORD_CLIENT_ID` from
    `discordClientId` and `HELLODJ_DISCORD_OAUTH_SECRET_ARN` from the
    `discord-oauth` secret; `bin/hellodj.ts` threads `discordClientId`). This
    task keeps that consistent with the new unified store rather than
    re-introducing it — verify it's present, don't duplicate.
  - playback-orchestrator role (watchdog): `grantReadWriteData` on core table
    (add if missing) + `kms:Decrypt`/`kms:GenerateDataKey`/`kms:Encrypt` on the
    CMK; env `HELLODJ_SOURCE_CREDS_KMS_KEY_ID`, `TOKEN_WATCHDOG_INTERVAL`,
    provider client id/secret envs it needs to build refresh clients.
  - playback readers (bot/sidecars): `kms:Decrypt` on the CMK (read-only on
    tokens), plus core-table read. Retain the legacy `hellodj/<stage>/guild/*`
    read grant during migration.
  - A new `SOURCE_CREDENTIAL_KMS_COMPONENTS` set documents exactly which
    components get the decrypt grant (R9.4).
- The per-guild Secrets Manager write grant on web-ui stays during migration but
  new writes go to DynamoDB; a follow-up removes it once migration completes.

## Data Models

Reuses `hellodj-core` (PK/SK + GSI1, optimistic-lock `version`). New item type
`SourceCredential` documented above. No new tables. KMS CMK is the only new
persistent resource.

## Error Handling

- **Exchange failure** (R1.6): store nothing; surface `<provider>_connect_failed`.
- **Decrypt failure** (R3.4): treat credential as unusable; watchdog marks
  `refresh_status=failed` and re-refreshes from `refresh_token` if the blob is
  recoverable, else flags for user re-auth; readers skip and fall back.
- **Refresh failure** (R5.4): per-item, logged, `refresh_status=failed`, prior
  blob intact, retried next tick; loop and container never crash.
- **Optimistic-lock conflict** (R5.5): `update_with_lock` retries; watchdog
  re-reads (may see peer's refresh and skip).
- **Degraded mode** (R5.7): no table/KMS/clients → watchdog does not start;
  web-ui renders "Needs setup" controls; nothing crashes.

## Correctness Properties

### Property 1: Crypto round-trip

For any token blob `b`, `decrypt_blob(encrypt_blob(b)) == b`; any tamper of
ciphertext/nonce/wrapped key makes decrypt fail (never returns wrong plaintext).
**Validates: Requirements 3.2, 3.3**

### Property 2: No plaintext leak

No code path logs or `repr`s a token; the stored item contains no plaintext
token field. **Validates: Requirements 2.3, 3.3**

### Property 3: Durability

A stored credential is readable after any single pod restart with no re-auth
(state lives only in DynamoDB + KMS, both external). **Validates: Requirements 2.4, 5.6**

### Property 4: Refresh soundness

`apply_refresh` returns a non-expired token or raises; a provider that doesn't
rotate keeps the prior refresh token; an already-expired result is a failure,
not a stored success. **Validates: Requirements 4.3, 4.4**

### Property 5: Watchdog isolation

One item's refresh failure never stops the pass and never crashes the
loop/container; the failed item's prior blob is intact. **Validates: Requirements 5.4**

### Property 6: Multi-replica safety

Concurrent watchdog writes to the same item never corrupt it (optimistic lock);
a loser re-reads and either skips or retries. **Validates: Requirements 5.5**

### Property 7: Tidal no-regression

Tidal refresh routes through the existing first-party single-app-id logic; its
property tests still pass. **Validates: Requirements 4.5, 10.2**

### Property 8: Default source

An unset default source resolves to `youtube` in the config layer and the bot
source map. **Validates: Requirements 7.1, 7.3**

### Property 9: Least privilege

Only components in `SOURCE_CREDENTIAL_KMS_COMPONENTS` hold the CMK decrypt grant.
**Validates: Requirements 9.4**

## Testing Strategy

- **`token_crypto`**: round-trip encrypt→decrypt with a fake KMS; tamper →
  decrypt fails; plaintext never in `repr`/logs.
- **`source_refresh`**: property test mirroring Tidal Property 14 for each
  client (fast-path, rotate/no-rotate, expired-result-is-failure). Tidal adapter
  delegates to existing logic (existing tests still pass, R10.2).
- **`SourceCredentialService`**: store→status(no decrypt)→load(decrypt)→
  disconnect; near-expiry enumeration; record_refresh write-back + lock.
- **`TokenWatchdog`**: tick refreshes only near-expiry items; one failure
  doesn't stop the pass; multi-replica lock safety; degraded no-op.
- **web-ui routes**: connect builds URL + state; callback stores encrypted item;
  disconnect deletes; status partial renders no token; Discord enable/reset.
- **Default source**: unset resolves to `youtube` in config + bot map.
- **CDK**: CMK created; grants scoped to the documented component set; existing
  226 tests unaffected.

## Migration & Rollout

1. Ship storage + crypto + service + refresh clients (web-ui writes DynamoDB;
   readers try DynamoDB then legacy secret).
2. Ship watchdog in playback-orchestrator.
3. Backfill: a one-shot (migration component or ops script) reads existing
   `hellodj/<stage>/guild/*` secrets and writes encrypted DynamoDB credential
   items; verify; then a later change drops the Secrets Manager write grant.
4. Deploy order (reconciled with the ACTUAL deploy machinery — see
   `.kiro/steering/website-debug-context.md`):
   - **Infra** (the new CMK in `data-stack.ts`, IAM grants + container env in
     `workloads-stack.ts`): `cdk deploy hellodj-data` for the CMK, then
     `cdk deploy hellodj-eks` for the workloads manifests/IAM (the workloads'
     Kubernetes manifests live in the `hellodj-eks` stack via
     `cluster.addManifest`, NOT the per-stage `WorkloadsStack`, and
     `selfMutation` is OFF by design — so `cdk deploy hellodj-eks` is what
     applies env/IAM changes, not a plain push).
   - **Component source** (web-ui, playback-orchestrator, bot `*.py`): CodeCommit
     push → pipeline rebuilds the images (ECR). The push does NOT roll the pods.
     Roll them by re-applying the manifests with an immutable commit-hash tag:
     `platform/tools/deploy_workloads.sh` (verifies the ECR image for HEAD
     exists, pins `-c hellodj:imageTag=<HEAD>`, clean env + private cdk.out —
     avoids the stale-`CODEBUILD_RESOLVED_SOURCE_VERSION` footgun). This is the
     precise form of the old "push → rollout restart" shorthand.

### Scope note — what this spec does and does NOT fix

This spec solves credential **durability + unified refresh** (the on-prem→AWS
gap where in-bot refresh loops die on a pod bounce and per-guild Secrets Manager
secrets sprawl). It is NOT the fix for:
- Cognito admin/registration **login** (chosen-name-as-username) — a separate,
  already-shipped fix; out of scope here.
- The **deploy two-step** itself (`cdk deploy hellodj-eks` to roll pods). That
  is a deliberate architecture (`selfMutation` off; manifests on `hellodj-eks`);
  this spec RIDES that machinery and documents it, but does not change it. A
  fully push-to-roll pipeline would be its own spec.
What it DOES clean up from recent pain: the "silent broken authorize URL"
class (R1.2 renders a disabled "Needs setup" control instead of an active link
that no-ops when a client id is missing), and the Discord/Google/Spotify OAuth
env wiring gaps (Task 10).

## Non-goals

- SoundCloud OAuth (search-only).
- Removing the legacy Secrets Manager path in the same change (kept for
  migration; removed in a follow-up).
- A GSI for expiry-bucketed watchdog queries (filtered scan for v1; GSI is a
  future optimization).

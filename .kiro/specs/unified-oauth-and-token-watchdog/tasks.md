# Implementation Plan

## Overview

Build the shared primitives first (crypto, refresh contract, core-table scan),
then the credential service, then wire web-ui + UI, the watchdog, the readers,
and finally CDK, migration, and gates. Tasks 1–3 are independent and unblock
everything else.

## Tasks

- [x] 1. Shared envelope encryption module (`token_crypto`)
  - Add `hellodj_platform_logic/token_crypto.py`: `EncryptedBlob` dataclass,
    `encrypt_blob(plaintext, kms)` (GenerateDataKey + AES-GCM), `decrypt_blob`,
    injectable `KmsClient` Protocol (`generate_data_key`/`decrypt`).
  - Never log plaintext; errors carry no token material.
  - Unit tests: round-trip, tamper-fails, no plaintext in repr.
  - _Requirements: 3.2, 3.3, 3.4_

- [x] 2. Unified refresh contract + provider clients (`source_refresh`)
  - Add `hellodj_platform_logic/source_refresh.py`: `TokenState`,
    `RefreshClient` Protocol, `needs_refresh`, `apply_refresh` (fast-path,
    preserve-refresh-token, expired-result-is-failure).
  - `GoogleRefreshClient`, `SpotifyRefreshClient` (urllib form posters,
    injectable), `TidalRefreshClient` adapter delegating to existing
    `tidal_refresh.refresh_tidal`.
  - Property tests per client (mirror Tidal Property 14); confirm existing
    `tidal_refresh` tests still pass.
  - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 10.2_

- [x] 3. Core-table access for credential items
  - Add `CoreTable.scan_entity(entity_type)` (paginated, projection to keys +
    `expires_at` + `refresh_status`, excludes `enc_blob`).
  - Unit tests against a fake table (pagination, projection).
  - _Requirements: 2.1, 5.2_

- [x] 4. `SourceCredentialService` (web-ui, importable by watchdog)
  - `store`, `status`/`status_for` (no decrypt), `load_token` (decrypt),
    `disconnect`, `iter_near_expiry`, `record_refresh`.
  - Item shape `USER#<sub>` / `SOURCECRED#<provider>`, entityType
    `SourceCredential`; status plaintext, blob encrypted via `token_crypto`.
  - Unit tests: store→status→load→disconnect; near-expiry; write-back lock.
  - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 3.2, 3.3_

- [x] 5. Wire web-ui connect/callback through the new store
  - Route provider callbacks (youtube/youtube_music/spotify) into
    `SourceCredentialService.store` (encrypted DynamoDB), keeping the existing
    authorize-URL builders in `source_oauth`.
  - Preserve Tidal sidecar-forward path; add DynamoDB write of Tidal status.
  - Legacy per-guild secret read remains as fallback (migration).
  - Clear-error-not-partial on failure.
  - Tests: callback stores encrypted item; state mismatch rejected; failure path.
  - _Requirements: 1.3, 1.4, 1.5, 1.6, 2.6_

- [x] 6. Config/account UI: all providers, status, disconnect, Discord enable/reset
  - Config page shows Connect for youtube/youtube_music/spotify/tidal +
    disabled "Needs setup" when client id absent; per-provider status
    (connected, last-refresh, refresh_status); Disconnect (HTMX partial).
  - Discord link control: linked/not-linked + enable + reset (unlink).
  - Never render a token value.
  - Tests: partial renders no token; disconnect deletes; Discord enable/reset.
  - _Requirements: 1.1, 1.2, 1.7, 8.1, 8.2, 8.3, 8.4_

- [x] 7. Default source = YouTube
  - `DEFAULT_SOURCE = "youtube"` constant; config `get_global`/`get_guild`
    callers + config form preselect; bot source map treats unset as youtube.
  - Tests: unset resolves to youtube in config + bot map; form preselects.
  - _Requirements: 7.1, 7.2, 7.3_

- [x] 8. Token-refresh watchdog in playback-orchestrator
  - `playback_orchestrator/token_watchdog.py`: `TokenWatchdog(tick/run_forever)`.
  - Start on a daemon thread in `__main__.main()` next to the health server,
    guarded by config (degraded → don't start).
  - Per-item isolation, loop never crashes; optimistic-lock multi-replica safe.
  - Tests: tick refreshes only near-expiry; failure isolation; degraded no-op;
    lock safety.
  - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 5.7_

- [x] 9. Playback reader integration (bot/sidecars)
  - `bot/playback/guild_credentials.py`: add DynamoDB credential resolution +
    `token_crypto` decrypt; fall back to legacy per-guild secret; preserve the
    YouTube `POST /youtube` all-fields-together swap + TTL cache.
  - Tests: resolve+decrypt path; expired triggers refresh/read; fallback;
    no cross-user leakage.
  - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5_

- [x] 10. CDK: KMS CMK + grants + env
  - `data-stack.ts`: source-credentials CMK with rotation; export.
  - `workloads-stack.ts`: web-ui (encrypt + generate-data-key, core RW, env
    `HELLODJ_SOURCE_CREDS_KMS_KEY_ID` + `HELLODJ_GOOGLE_OAUTH_SECRET_ARN` +
    `POTOKEN_SERVER_URL`); playback-orchestrator (core RW + KMS enc/dec, watchdog
    env + provider client id/secret); readers (KMS decrypt + core read);
    `SOURCE_CREDENTIAL_KMS_COMPONENTS` set (R9.4).
  - VERIFY (do not duplicate) the Discord OAuth wiring landed in a prior session
    is intact and consistent with the unified store: `workloads-stack.ts` pushes
    `DISCORD_CLIENT_ID` (from `discordClientId`) + `HELLODJ_DISCORD_OAUTH_SECRET_ARN`
    (from the `discord-oauth` secret) for web-ui, and `bin/hellodj.ts` threads
    `discordClientId`. If any piece regressed, restore it here.
  - CDK tests: CMK created; grants scoped to the documented set; existing tests
    unaffected (do not hardcode a stale test count — assert the current suite
    still passes).
  - _Requirements: 3.1, 3.5, 9.1, 9.2, 9.3, 9.4_

- [x] 11. Migration backfill (Secrets Manager → encrypted DynamoDB)
  - One-shot (migration component or ops script): read existing
    `hellodj/<stage>/guild/*` secrets, write encrypted `SourceCredential` items;
    idempotent; verify; log counts (no token material).
  - Tests: fake secrets → items written encrypted; re-run idempotent.
  - _Requirements: 2.6, 6.5_

- [x] 12. Gates + deploy
  - Run `cd platform/infra && npx tsc --noEmit && npx jest`; web-ui
    `ruff check --target-version py314 . && python3 -m pytest tests/ -q`;
    `python3 platform/tools/check_line_count.py` on changed components.
  - Deploy (reconciled with the ACTUAL machinery — `selfMutation` OFF, manifests
    on `hellodj-eks`):
    - Infra: `cd platform/infra && npx cdk deploy hellodj-data` (the new CMK),
      then `npx cdk deploy hellodj-eks` (workloads env/IAM/manifests). NOT a push.
    - Component source (web-ui, playback-orchestrator, bot `*.py`): CodeCommit
      push → pipeline rebuilds images → roll the pods by re-applying the
      manifests at HEAD with `platform/tools/deploy_workloads.sh` (immutable
      commit-hash tag; NOT a plain `kubectl rollout restart`, which only re-pulls
      `:latest` and is the last-resort fallback).
  - Update `hellodj-architecture.md` / `website-debug-context.md` steering with
    the new credential store, CMK, and watchdog (docs-in-sync rule).
  - _Requirements: 10.1, 10.3_

## Task Dependency Graph

```
1 (token_crypto) ─┐
2 (source_refresh)─┼─▶ 4 (SourceCredentialService) ─┬─▶ 5 (web-ui wiring) ─▶ 6 (UI)
3 (scan_entity) ──┘                                 ├─▶ 8 (watchdog)
                                                    └─▶ 9 (readers)
7 (default source) ── independent
10 (CDK) ── depends on 1,4,8,9 (grants/env for those components)
11 (migration) ── depends on 1,4
12 (gates + deploy) ── depends on all
```

- 1, 2, 3, 7 have no dependencies (start in parallel).
- 4 depends on 1, 2, 3.
- 5 and 6 depend on 4 (6 after 5).
- 8 and 9 depend on 4 (and 1 for decrypt).
- 10 depends on the components it grants (1, 4, 8, 9).
- 11 depends on 1, 4.
- 12 depends on everything.

```json
{
  "waves": [
    { "wave": 1, "tasks": [1, 2, 3, 7] },
    { "wave": 2, "tasks": [4] },
    { "wave": 3, "tasks": [5, 8, 9] },
    { "wave": 4, "tasks": [6, 10, 11] },
    { "wave": 5, "tasks": [12] }
  ]
}
```

## Notes

- Storage decision (confirmed): tokens in DynamoDB (Option B) with app-layer
  envelope encryption, NOT per-secret Secrets Manager (cost/scale at ~1000
  users). Legacy per-guild secrets kept read-only for migration only.
- Watchdog reuses the existing `playback-orchestrator` container (already
  standing, already has a run loop + DynamoDB access) rather than a new
  component.
- Default source is YouTube.
- Follow the CI rules: infra via `cdk deploy hellodj-data` / `hellodj-eks`
  (manifests live on `hellodj-eks`; `selfMutation` is OFF), component source via
  CodeCommit push → pipeline → roll pods with `platform/tools/deploy_workloads.sh`
  (immutable commit tag). Do NOT build/push images locally.
- Scope (reconciled): this spec fixes credential durability + unified refresh
  and the "silent broken authorize URL" / OAuth env-wiring class. It does NOT
  change Cognito login (already fixed) or the deploy two-step (deliberate;
  its own spec if ever fully automated). See the design's "Scope note".
```

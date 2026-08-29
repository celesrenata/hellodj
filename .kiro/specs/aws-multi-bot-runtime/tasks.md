# Implementation Plan

## Overview

Extract the pool parser to shared, build the AWS credential source + orchestrator
subclass, host it on the orchestrator daemon thread, then wire the GPU-offload
CDK bits (idle window already done). Reuse the on-prem `InstanceOrchestrator`
and `entitlements_core` unchanged wherever possible.

## Tasks

- [x] 1. Extract pool parsing into the shared package
  - Add `hellodj_platform_logic/bot_app_pool.py`: `PoolApp` dataclass +
    `parse_pool(raw_json) -> list[PoolApp]` (skips entries with no client_id;
    keeps client_secret/bot_token internal, never logged).
  - Refactor `web-ui/bot_app_pool.py` `BotAppPool` to delegate parsing to
    `parse_pool` while keeping its public label+client_id-only surface.
  - Unit tests: parse shape, tokenless entries flagged, no secret in repr.
  - _Requirements: 1.1, 1.3, 1.5_

- [x] 2. `PoolCredentialSource` (playback-orchestrator)
  - Add `playback_orchestrator/instance_runtime.py` `PoolCredentialSource`:
    `pool()` (read `bot-app-pool` secret via injected secrets client + stage),
    `claimed_client_ids(guild_id)` (read `GUILD#<gid>/BOTAPP#*` via CoreTable),
    `instances_for_guild(guild_id)` (pool ∩ claims ∩ has-token).
  - Unit tests against fake secrets + fake CoreTable: claim intersection,
    tokenless skip, empty/absent pool → empty.
  - _Requirements: 1.1, 1.2, 1.3_

- [x] 3. `AwsInstanceOrchestrator` (subclass, override initialize only)
  - Add `AwsInstanceOrchestrator(InstanceOrchestrator)` in `instance_runtime.py`:
    override `initialize()` to build `BotInstance`s from
    `PoolCredentialSource.instances_for_guild` (per claimed+token app) and
    connect them in parallel with per-instance isolation (reuse the on-prem
    `_connect_instance` semantics). Inherit assign/release/health/quota.
  - Unit tests (fake discord.Client): initialize builds the right instances;
    a connect failure marks that instance unhealthy without affecting others.
  - _Requirements: 2.1, 2.2, 2.5, 3.1, 3.2, 3.3, 3.4, 3.5, 5.1, 5.2, 5.3_

- [x] 4. Entitlement quota enforcement on the AWS path
  - Ensure `AwsInstanceOrchestrator` resolves the owning user's effective
    entitlements via `entitlements_core` and enforces
    `effective_max_bots_per_guild` + `max_guilds` at assignment (restrictive
    default on resolution failure). Wire the resolver the orchestrator reads.
  - Property tests: quota safety (never exceed; failure → restrictive default),
    mirroring the on-prem orchestrator's quota tests.
  - _Requirements: 4.1, 4.2, 4.3, 4.4_

- [x] 5. Instance runtime bootstrap on the daemon thread
  - Add `playback_orchestrator/instance_bootstrap.py`
    `start_instance_runtime_thread()` (mirrors `watchdog_bootstrap`): build
    source + orchestrator from env, run the asyncio loop on a daemon thread,
    self-degrade to a logged no-op when pool empty/unconfigured/discord.py
    absent. Call it from `__main__.main()` next to `start_watchdog_thread()`.
    Disconnect instances cleanly on shutdown.
  - Unit tests: degraded no-op path (health server unaffected); startup wiring.
  - _Requirements: 2.1, 2.2, 2.3, 2.4_

- [x] 6. CDK: orchestrator claim-read grant + runtime env + single replica
  - `workloads-stack.ts`: grant `playback-orchestrator` READ on the core-table
    `GUILD#*`/`BOTAPP#*` items (claims) [pool secret READ already granted];
    wire runtime env (stage/region/Lavalink node URL); pin the orchestrator to
    a single replica (maxReplicas=1) so bot tokens are never double-connected.
  - CDK tests: grant scoped to the claim keys; orchestrator maxReplicas=1;
    existing suite still passes.
  - _Requirements: 9.1, 9.2, 9.3_

- [x] 7. GPU offload wiring (idle window done; trigger + drain assertions)
  - Confirm the 600s idle window in `GpuIdleConfig` + CDK mirror (done); ensure
    the `hls-transcode` HPA CPU target drives GPU-pod scale-up and the
    `transcode-gpu` NodePool uses `WhenEmpty` + `consolidateAfter: 600s` +
    the existing `GPU_DRAIN_TIMEOUT_SECONDS` drain.
  - CDK tests (extend `eks-gpu.test.ts`): idle window 600 (done); transcode HPA
    CPU-target present; NodePool consolidation + drain-timeout assertions.
  - _Requirements: 6.1, 6.2, 6.3, 7.1, 7.2, 7.3, 8.1, 8.2, 8.3, 8.4, 9.4_

- [x] 8. Gates, deploy, docs
  - Run `ruff check --target-version py314`, orchestrator + shared pytest,
    `cd infra && npx tsc --noEmit && npx jest`, and the 500-line ceiling check
    on changed components.
  - Deploy: push orchestrator/shared source → pipeline rebuild →
    `cdk deploy hellodj-eks -c hellodj:imageTag=<HEAD>` (roll); infra via
    `cdk deploy hellodj-eks`.
  - Update `hellodj-architecture.md` with the AWS multi-bot runtime +
    CPU→GPU offload / 10-min warm window model.
  - _Requirements: 10.1, 10.2, 10.3_

## Task Dependency Graph

```
1 (shared parse) ──▶ 2 (PoolCredentialSource) ──▶ 3 (AwsInstanceOrchestrator) ──▶ 4 (quota)
                                                        └──▶ 5 (bootstrap)
6 (CDK grant/env/replica) ── depends on 2,3,5 (grants for what they read)
7 (GPU offload) ── independent (idle window already landed)
8 (gates + deploy + docs) ── depends on all
```

- 1 unblocks everything on the runtime path.
- 2 → 3 → {4, 5}.
- 6 depends on the runtime existing (2/3/5) to grant/env correctly.
- 7 is independent of the bot runtime (GPU compute path).
- 8 last.

```json
{
  "waves": [
    { "wave": 1, "tasks": [1, 7] },
    { "wave": 2, "tasks": [2] },
    { "wave": 3, "tasks": [3] },
    { "wave": 4, "tasks": [4, 5] },
    { "wave": 5, "tasks": [6] },
    { "wave": 6, "tasks": [8] }
  ]
}
```

## Notes

- The on-prem `bot/playback/orchestrator.py` `InstanceOrchestrator` stays the
  source of truth for assign/release/health/quota; the AWS runtime subclasses it
  and overrides ONLY `initialize()` (credential source). Do not fork the
  assignment logic.
- Single-replica orchestrator is a hard constraint: two replicas would connect
  the same bot tokens twice and Discord rejects the second identify.
- GPU idle window default is already 600s (`GpuIdleConfig` + CDK mirror + tests
  updated); task 7 only adds/asserts the trigger + drain wiring.
- Follow CI rules: component source via push→pipeline→`cdk deploy hellodj-eks`
  roll; infra via `cdk deploy`. Do NOT build/push images locally.

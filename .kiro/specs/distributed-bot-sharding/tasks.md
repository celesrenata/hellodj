# Implementation Plan — Distributed Bot Sharding (one bot per node)

## Overview

Add pure shard math, make the runtime shard-aware (served guilds + app-owner
tiebreak), add cross-replica play forwarding, and switch the orchestrator to a
StatefulSet + headless Service with per-node anti-affinity. Reuse
`AwsInstanceOrchestrator` unchanged below `initialize()`.

## Tasks

- [x] 1. Pure shard math (`playback_orchestrator/sharding.py`)
  - `shard(guild_id, replica_count)` (blake2b hash % count, hash-randomization
    independent), `parse_ordinal(hostname)`, `resolve_topology(hostname,
    replicas_env)` with the R1.3 degrade to `(0, 1)`.
  - Unit/property tests: partition property (disjoint + total over a guild set),
    determinism across processes, ordinal parse + degrade, `replica_count>=1`.
  - _Requirements: R1.2, R1.3, R1.4, R2.2_

- [x] 2. Shard-aware served-guild discovery (`instance_bootstrap.py`)
  - Thread `(ordinal, replica_count)` from `resolve_topology(os.uname/HOSTNAME,
    HELLODJ_ORCHESTRATOR_REPLICAS)` into `build_instance_runtime`; filter
    `discover_claimed_guild_ids` to guilds this replica owns.
  - Tests: `replica_count==1` serves all (R7.1); N>1 partitions.
  - _Requirements: R2.1, R2.2, R7.1_

- [x] 3. App-owner tiebreak in the credential source (`instance_pool_source.py`)
  - Add `app_owner_ordinal(client_id, replica_count)` = `shard(min(claiming
    guild ids for app), N)`. Reverse-claim read via a `BOTAPP#<client_id>` GSI1
    (preferred) or a bounded `scan_entity('BotAppClaim')` fallback.
  - `AwsInstanceOrchestrator.initialize` guard: connect an app locally ONLY when
    `app_owner_ordinal == ordinal`; else record remote-owned + skip (R3.1/R3.2).
  - Property test: each app owned by exactly one ordinal across a claim set.
  - _Requirements: R3.1, R3.2_

- [x] 4. Cross-replica play forwarding (`playback_forwarding.py` / `__main__.py`)
  - Forwarding shim on `POST /v1/playback`: owner via `shard(guild_id, N)`;
    handle locally when owner or already-forwarded; else forward once (header
    `X-HelloDJ-Forwarded: 1`) to `playback-orchestrator-<owner>.<headless>:PORT`
    and relay; truthful "unavailable" on connect error (R4.3); never connect
    locally.
  - Tests: local-when-owner, forward-when-not, forward-once hop guard, error →
    unavailable (no local connect).
  - _Requirements: R4.1, R4.2, R4.3, R4.4_

- [x] 5. CDK: StatefulSet + headless Service + anti-affinity + env
  - `component-workloads.ts`: mark `playback-orchestrator` as a sharded
    StatefulSet with a replica count (documented default sized to the app fleet;
    e.g. 3 on the resized `m7g.xlarge` floor).
  - `workloads-stack.ts`: synthesize StatefulSet + headless Service (clusterIP
    None) when the trait is set; inject `HELLODJ_ORCHESTRATOR_REPLICAS`; add
    hostname-topology pod anti-affinity; keep `workload=app` nodeSelector, NO
    GPU request, NO transcode toleration. Remove the old min1/max1 Deployment
    HPA for this component.
  - CDK tests: StatefulSet not Deployment; headless Service; anti-affinity
    topologyKey hostname on the app label; env present == replicas; no GPU
    request/toleration; replicas ≤ documented cap.
  - _Requirements: R1.1, R3.3(dns), R5.1, R5.2, R5.3, R8.2_

- [x] 6. Watchdog multi-replica note/optional shard (`token_watchdog`)
  - CONFIRMED correct under N replicas: `TokenWatchdog` already writes back with
    an optimistic-lock (version) update, so N replicas racing the same
    near-expiry credential cannot double-refresh or corrupt it — the loser's
    conditional write fails and it re-reads. No code change required for R6.1.
  - The optional scan-shard (R6.2, cut N× scan cost by having each replica scan
    only credentials whose owner `shard(...) == ordinal`) is INTENTIONALLY
    deferred: at pre-prod credential volume the N× scan is negligible, and
    keeping every replica scanning all items means a credential is still
    refreshed even if its "owning" replica is momentarily down (more robust).
    Left as a documented future optimization, not built.
  - _Requirements: R6.1, R6.2_

- [ ] 7. Gates, deploy, docs
  - `ruff --target-version py314` + `pytest` (orchestrator + shared);
    `cd infra && npx tsc --noEmit && npx jest`; 500-line ceiling on changed
    components.
  - Deploy: orchestrator/shared source → pipeline rebuild → per-stage
    WorkloadsStack roll; the StatefulSet/Service/anti-affinity are per-stage
    WorkloadsStack manifests (ride the pipeline), NOT a foundation `cdk deploy`.
    The `m7g.xlarge` fleet resize is the separate foundation `cdk deploy
    hellodj-eks` already prepared.
  - Update `hellodj-architecture.md`: the orchestrator is now a per-node-sharded
    StatefulSet (was single-replica), the guild→shard partition, the app-owner
    tiebreak, and the play-forwarding path. Update the multi-bot-runtime section
    to note the single-replica constraint is lifted **via disjoint sharding**
    (not removed — two replicas still never connect the same app).
  - _Requirements: R7.2, R7.3, R8.1, R8.2, R8.3_

## Task Dependency Graph

```
1 (shard math) ──▶ 2 (served guilds) ──▶ 3 (app-owner) ──▶ 4 (forwarding)
                                    └──▶ 5 (CDK StatefulSet) depends on 2/4 env+dns
6 (watchdog) — independent (optional)
7 (gates/deploy/docs) — depends on all
```

```json
{
  "waves": [
    { "wave": 1, "tasks": [1, 6] },
    { "wave": 2, "tasks": [2] },
    { "wave": 3, "tasks": [3] },
    { "wave": 4, "tasks": [4] },
    { "wave": 5, "tasks": [5] },
    { "wave": 6, "tasks": [7] }
  ]
}
```

## Notes

- Do NOT fork `AwsInstanceOrchestrator` assign/release/health/quota; only add
  the `initialize()` app-owner guard + the discovery filter.
- `replicaCount == 1` MUST be byte-for-byte today's behavior (guard the shard
  math so ordinal 0 of 1 owns everything and forwarding is a no-op).
- StatefulSet rolls one pod at a time (default `podManagementPolicy`), so a
  rescale never briefly double-connects an app beyond the forward-once guard's
  tolerance.
- Component source rides the pipeline; the StatefulSet/Service manifests are
  per-stage WorkloadsStack (pipeline-deployed), not a foundation stack.

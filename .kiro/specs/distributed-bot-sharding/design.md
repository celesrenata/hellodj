# Design — Distributed Bot Sharding (one bot per node)

## Overview

Convert `playback-orchestrator` from a single-replica Deployment into a
**sharded StatefulSet**. Each replica has a stable ordinal, owns a disjoint set
of guilds by `shard(guild_id) == ordinal`, connects only the pool apps owned by
its guilds (with a deterministic tiebreak for apps shared across shards), and
forwards a play request to the owning replica when it isn't the owner. Pod
anti-affinity (`topologyKey: kubernetes.io/hostname`) puts at most one replica
per node; the GPU node is excluded because orchestrator pods carry no GPU
request and don't tolerate the transcode taint.

This reuses `AwsInstanceOrchestrator` (assign/release/health/quota) unchanged
and adds three small, pure, testable seams: shard math, an app-owner tiebreak in
the credential source, and a forwarding shim in the play endpoint.

## Why sharding by guild (not by app-index)

A guild's session/queue state and its bot assignments must live in ONE place
(the single-writer guarantee on `hellodj-session`, R2.3). Sharding by guild
keeps all of a guild's state on its owning replica — no cross-pod session
coordination. The only cross-shard concern is a pool app claimed by guilds that
land on different shards; R3.2 resolves it with a deterministic single-owner
tiebreak so the app is connected exactly once, and R4 routes a non-owner's play
request to the app's owner.

## Architecture

```
/play in guild G ──▶ any orchestrator replica R (StatefulSet, 1 per node)
                        owner = shard(G, N)
            ┌───────────┴────────────┐
       R == owner                 R != owner
   handle locally             forward once (X-HelloDJ-Forwarded: 1)
   (bot on a locally-          to playback-orchestrator-<owner>.<headless>
    owned pool app)            relay response, or truthful "unavailable"
```

- **StatefulSet** (was single-replica Deployment) → stable ordinal per pod.
- **Headless Service** (`clusterIP: None`) → stable per-pod DNS for forwarding.
- **Pod anti-affinity** `topologyKey: kubernetes.io/hostname` → ≤1 replica/node.
- GPU node excluded: no `nvidia.com/gpu` request, no `transcode-gpu` toleration.
- Guild → shard by `shard(guild_id, N)`; pool app → single owner by
  `shard(min(claiming guild ids), N)`.

## Components and Interfaces

### 1. Shard math (new, pure) — `playback_orchestrator/sharding.py`

```python
def shard(guild_id: str, replica_count: int) -> int:
    """Deterministic, replica-count-stable owner ordinal for a guild.
    Stable hash (blake2b of the guild id) modulo replica_count. Pure."""

def parse_ordinal(hostname: str) -> int | None:
    """Extract the StatefulSet ordinal from 'name-<n>' hostname, else None."""

def resolve_topology(hostname: str, replicas_env: str) -> tuple[int, int]:
    """(ordinal, replica_count) with the R1.3 degrade to (0, 1) on any parse
    failure. replica_count >= 1 always."""
```

- `shard` MUST be stable across pod restarts and independent of Python's
  hash-randomization → use `hashlib.blake2b(guild_id.encode()).digest()` folded
  to an int, `% replica_count`.
- Owner of a guild `G` = `shard(G, N)`. Owner of an **app** = `shard(min(claiming
  guild ids for that app), N)` — the lexicographically-smallest claiming guild
  id gives a deterministic, replica-count-stable single owner (R3.2).

### 2. Shard-aware served guilds — `instance_bootstrap.discover_claimed_guild_ids`

`discover_claimed_guild_ids` already enumerates all claimed guilds. Add a filter
so a replica keeps only guilds it owns:

```python
served = [g for g in discover_claimed_guild_ids(core)
          if shard(g, replica_count) == ordinal]
```

At `replica_count == 1` this is the full set (R7.1 — identical to today).

### 3. App-owner tiebreak — `PoolCredentialSource`

`instances_for_guild(guild_id)` stays as-is (pool ∩ that guild's claims ∩
token). The runtime's `initialize()` already dedups an app across the guilds a
SINGLE replica serves (`seen_client_ids`). Add the CROSS-replica guard: when
building instances, skip an app this replica would otherwise connect if the app
is **owned by a different replica**:

```python
def app_owner_ordinal(self, app_client_id, replica_count) -> int:
    """min(claiming guild ids for app) -> shard(...). Reads the app's reverse
    claim set (GSI or a scan of BOTAPP#<client_id> claims)."""
```

`AwsInstanceOrchestrator.initialize()` gains a guard: for each candidate app,
connect locally only if `app_owner_ordinal(app, N) == ordinal`; otherwise record
it as "remote-owned" (so routing knows where it lives) and skip connecting. This
preserves R3.1/R3.2 (exactly one replica connects each app).

> Implementation note: reading "which guilds claim app X" is a reverse lookup.
> The claim item is `PK=GUILD#<gid>`/`SK=BOTAPP#<client_id>`. A GSI1 keyed
> `BOTAPP#<client_id>` → `GUILD#<gid>` (mirroring the existing reverse-index
> pattern used elsewhere in `hellodj-core`) gives an O(claims-per-app) read; a
> bounded `scan_entity('BotAppClaim')` fallback is acceptable at pre-prod scale
> and is what discovery already does. Design picks the GSI; task list allows the
> scan fallback if the GSI is deferred.

### 4. Cross-replica play forwarding — `playback_api` / `__main__`

The health server already serves `POST /v1/playback` → `handle_playback`. Wrap
it with a forwarding shim:

```
receive POST /v1/playback {guild_id, ...}
  owner = shard(guild_id, N)
  if owner == ordinal or request has X-HelloDJ-Forwarded: 1:
      handle locally (existing handle_playback)
  else:
      forward to http://playback-orchestrator-<owner>.<headless>:PORT/v1/playback
        with header X-HelloDJ-Forwarded: 1  (forward-once hop guard, R4.4)
      relay the owner's response body verbatim (R4.2)
      on connect error → truthful "temporarily unavailable" body (R4.3),
        NEVER connect the app locally
```

- Stable pod DNS requires a **headless Service** (`clusterIP: None`) for the
  StatefulSet so `playback-orchestrator-<ordinal>.<svc>` resolves (R4.1).
- The hop guard header makes forwarding at-most-once (R4.4) — an owner that
  receives an already-forwarded request handles it locally even if its own view
  of `N`/ownership momentarily disagrees (rescale race), avoiding relay loops.

### 5. CDK — `component-workloads.ts` + `workloads-stack.ts`

- Add a component trait `statefulSet?: boolean` + `orchestratorReplicas?:
  number` (or a dedicated `ShardedStatefulSet` placement flag) for
  `playback-orchestrator`.
- `workloads-stack.ts`: when `statefulSet`, synthesize a **StatefulSet** +
  **headless Service** instead of Deployment+ClusterIP; inject
  `HELLODJ_ORCHESTRATOR_REPLICAS = <replicas>`; add pod anti-affinity
  (`requiredDuringScheduling`, `topologyKey: kubernetes.io/hostname`,
  `matchLabels` the orchestrator app label); keep `nodeSelector workload=app`,
  NO GPU request, NO transcode toleration (R5.2).
- Replica count default: size to the on-demand app floor + burst (documented);
  e.g. `min(poolSize, appNodeBurst)`. At pre-prod, a small N (e.g. 3) matching
  the resized `m7g.xlarge` fleet. The old `hpa: {min:1,max:1}` Deployment is
  replaced by fixed StatefulSet replicas (HPA on a sharded StatefulSet is out of
  scope — rescaling changes the shard map and is an operator action).

### 6. IAM

No new grant beyond what the orchestrator already holds (pool secret READ +
core-table claim READ + session RW + CMK). The reverse-claim GSI read (if added)
is covered by the existing core-table read grant. Cross-replica HTTP is
in-cluster (headless Service), no IAM.

## Data Models

- **Pool app** (`hellodj/<stage>/bot-app-pool` secret): `{label, client_id,
  client_secret, bot_token}` — unchanged; parsed by shared `parse_pool`.
- **BotAppClaim** (`hellodj-core`): `PK=GUILD#<gid>`, `SK=BOTAPP#<client_id>`,
  `entityType=BotAppClaim` — unchanged. New **reverse read** by
  `BOTAPP#<client_id>` (GSI1 `BOTAPP#<client_id> → GUILD#<gid>`, or a bounded
  `scan_entity('BotAppClaim')` fallback) to compute an app's claiming guilds.
- **Topology** (runtime, not persisted): `(ordinal, replica_count)` derived from
  pod hostname + `HELLODJ_ORCHESTRATOR_REPLICAS`; degrades to `(0, 1)`.
- **Session/queue** (`hellodj-session`): unchanged; written only by a guild's
  owning replica (single-writer preserved per shard).

## Correctness Properties

Property 1: Guild partition. For any claimed-guild set,
`⋃ served(ordinal) = all` and `served(i) ∩ served(j) = ∅` for `i ≠ j`.
**Validates: Requirements 2.2**

Property 2: Single app owner. Every pool app maps to exactly one ordinal via
`shard(min(claiming guild ids), N)`; no app is connected by two replicas.
**Validates: Requirements 3.1, 3.2**

Property 3: Forward-once. A request carries `X-HelloDJ-Forwarded` after one hop;
an owner never re-forwards, so relay terminates in ≤1 hop.
**Validates: Requirements 4.4**

Property 4: Identity at N=1. `shard(g, 1) == 0` for all g → ordinal 0 owns
everything and forwarding is a no-op → byte-for-byte today's behavior.
**Validates: Requirements 7.1**

## Error Handling

- Ordinal/replica parse failure → degrade to `(0, 1)`, log, never crash (R1.3).
- Reverse-claim read failure → treat app as owned-here-only is UNSAFE; instead
  fall back to the deterministic `scan_entity` path; on total read failure the
  app is skipped (not connected) rather than risk a double-connect.
- Owner replica unreachable on forward → truthful "temporarily unavailable" body,
  logged; never connect the app locally (R3.1/R4.3).
- All existing degraded no-op paths (no pool/claims/discord.py) preserved (R7.2).

## Testing Strategy

- **Pure/property tests:** `shard()` partition + determinism; `app_owner_ordinal`
  single-owner; `resolve_topology` parse + degrade; forward-once hop guard.
- **Runtime tests (fakes):** `initialize()` connects only owned apps; a
  remote-owned app is skipped; `replica_count==1` connects everything.
- **CDK tests:** StatefulSet not Deployment; headless Service; hostname-topology
  anti-affinity on the app label; `HELLODJ_ORCHESTRATOR_REPLICAS` == replicas;
  no GPU request/toleration; replica cap.

## Invariants preserved

- **Single gateway identify per app** (R3): each app connected by exactly one
  replica via the `app_owner_ordinal` tiebreak.
- **Single writer per guild** (R2.3): a guild's session writes only on its owner
  replica.
- **Primary_Bot untouched** (R3.4/R7.3).
- **`replicaCount == 1` ≡ today** (R7.1): shard owns everything, no forwarding.

## Risks / mitigations

- **Rescale reshuffles ownership.** Changing `N` remaps guilds→shards and
  apps→owners. During a rolling rescale two replicas could briefly both consider
  themselves an app's owner. Mitigation: the forward-once hop guard prevents
  relay loops; brief double-connect windows are avoided by the StatefulSet
  rolling one pod at a time (a replica disconnects its apps on SIGTERM before the
  new topology settles). Rescale is an operator action, done at low traffic
  (pre-prod).
- **Uneven guild distribution.** blake2b modulo gives good spread at scale; at
  tiny guild counts one shard may hold more — acceptable, load is voice-bound
  not guild-count-bound.

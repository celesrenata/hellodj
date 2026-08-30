# Requirements Document

Distributed Bot Sharding (one bot per node)

## Introduction

Today the AWS multi-bot runtime runs **every** secondary Discord bot gateway as
`discord.Client` instances inside a **single** `playback-orchestrator` pod
(`AwsInstanceOrchestrator._connect_all`). That pod is hard-pinned to
`minReplicas=1/maxReplicas=1` because a second replica of the same Deployment
would connect the **same** bot tokens twice, and Discord rejects a duplicate
gateway `identify` for one application id. The result is load-**concentrated**:
all voice gateways, opus/audio work, and the token watchdog live on one node.

This feature distributes that load across the app fleet — **at most one
orchestrator replica per node** — by converting the orchestrator into a
**sharded StatefulSet**. Each replica owns a **disjoint** slice of the work, so
no bot application is ever connected by two replicas and the single-`identify`
invariant is preserved while running N replicas. The GPU node is excluded
automatically (orchestrator pods carry no GPU request and do not tolerate the
`transcode-gpu` taint).

This is the "Option A" chosen over splitting gateways into a separate
`bot-instance` component (Option B): it reuses the existing
`AwsInstanceOrchestrator` assign/release/health/quota logic almost verbatim and
only changes (a) which slice of the pool a replica connects and (b) how a play
request reaches the replica that owns the target guild.

Non-goals: changing the Primary_Bot (`discord-bot-core`, single gateway, owns
slash commands — unchanged); changing the on-prem orchestrator; changing the
GPU transcode model.

## Glossary

- **Replica / shard**: one `playback-orchestrator` StatefulSet pod with a stable
  ordinal (`playback-orchestrator-<ordinal>`), `0 ≤ ordinal < replicaCount`.
- **Pool app**: a distinct Discord application in `hellodj/<stage>/bot-app-pool`
  (distinct `client_id` = distinct gateway identity).
- **Owning replica of a guild**: `shard(guild_id) == ordinal`, the single
  replica responsible for that guild's secondary bots and session/queue writes.

## Requirements

### R1 — Stable ordinal identity

**User Story:** As the platform, I want each orchestrator replica to know its
own stable shard ordinal and the total replica count, so it can compute a
deterministic, collision-free partition of work.

#### Acceptance Criteria
1. THE orchestrator SHALL run as a Kubernetes **StatefulSet** so each pod has a
   stable hostname `playback-orchestrator-<ordinal>` and a stable ordinal.
2. THE runtime SHALL derive its `ordinal` from the pod hostname suffix (the
   StatefulSet ordinal) and its `replicaCount` from an injected env
   (`HELLODJ_ORCHESTRATOR_REPLICAS`), both as non-negative integers with
   `0 ≤ ordinal < replicaCount`.
3. WHEN the ordinal cannot be parsed from the hostname THE runtime SHALL degrade
   to `ordinal=0, replicaCount=1` (single-shard behavior — the current model),
   never crash, and log the degradation.
4. THE shard function SHALL be a pure, unit-tested function
   `shard(guild_id, replicaCount) -> ordinal` (stable hash modulo count).

### R2 — Disjoint guild ownership (collision-free)

**User Story:** As the platform, I want each guild served by exactly one
replica, so a guild's secondary bots and session writes never split-brain.

#### Acceptance Criteria
1. A replica SHALL serve a guild **iff** `shard(guild_id, replicaCount) ==
   ordinal`; every other replica SHALL ignore that guild entirely.
2. THE union of all replicas' served guilds SHALL equal the full set of claimed
   guilds, and the intersection of any two replicas' served-guild sets SHALL be
   empty (a partition).
3. THE single-writer guarantee on the `hellodj-session` hot table SHALL be
   preserved: for any guild, only its owning replica writes that guild's
   session/queue state.

### R3 — No duplicate gateway identify (the hard invariant)

**User Story:** As the platform, I must never open two gateway connections for
the same Discord application id across replicas, because Discord rejects the
second `identify`.

#### Acceptance Criteria
1. A pool app SHALL be connected by **at most one** replica at any time.
2. WHEN a pool app is claimed by guilds that map to **different** shards, the
   runtime SHALL connect that app on exactly one replica — its **owner replica**
   `shard_owner(app) = shard(min(claiming_guild_ids_for_app), replicaCount)` (a
   deterministic, replica-count-stable tiebreak) — and every other replica SHALL
   NOT connect it, even for a guild it otherwise serves.
3. WHEN a replica serves guild G but G's play request needs a bot on an app
   owned by a different replica (per R3.2), the runtime SHALL route the
   join/play action to that owner replica (see R4) rather than connecting the
   app locally.
4. THE Primary_Bot (`DISCORD_CLIENT_ID`) SHALL remain excluded from every
   replica's connectable pool (unchanged from the existing `parse_pool`
   exclusion).

### R4 — Cross-replica play routing

**User Story:** As a user issuing `/play` in a guild, I want it to work
regardless of which replica the request first lands on, so sharding is invisible
to me.

#### Acceptance Criteria
1. THE orchestrator's play endpoint (`POST /v1/playback`) SHALL, on receiving a
   request for guild G, forward it to G's **owning replica** when the receiving
   replica is not the owner, using the StatefulSet's stable per-pod DNS
   (`playback-orchestrator-<ordinal>.<headless-svc>`).
2. THE owning replica SHALL execute assign/release/play against its locally
   connected bot(s) and return the result; the receiving replica SHALL relay
   that result to the caller unchanged.
3. WHEN the owning replica is unreachable THE receiving replica SHALL return a
   truthful "temporarily unavailable" body (never a false success) and log it;
   it SHALL NOT attempt to connect the app locally (R3.1).
4. Forwarding SHALL carry a hop guard (a header/flag) so a request is forwarded
   **at most once**, preventing infinite relay loops.

### R5 — Per-node distribution

**User Story:** As the platform owner, I want at most one orchestrator replica
per node so bot load spreads across the fleet and no node is the single
bottleneck.

#### Acceptance Criteria
1. THE StatefulSet SHALL declare `requiredDuringSchedulingIgnoredDuringExecution`
   pod anti-affinity on its own app label with `topologyKey:
   kubernetes.io/hostname`, so no two replicas schedule on the same node.
2. THE replicas SHALL schedule ONLY on the `workload=app` fleet (existing
   `nodeSelector`); they SHALL carry no `nvidia.com/gpu` request and SHALL NOT
   tolerate the `transcode-gpu` taint, so they never land on the GPU node.
3. THE replica count SHALL be bounded so it never exceeds the schedulable app
   nodes; unschedulable surplus replicas are a misconfiguration the design must
   avoid (default replica count sized to the on-demand app floor + expected
   burst, documented).

### R6 — Safe multi-replica daemons

**User Story:** As the platform, I want the co-hosted token watchdog to remain
correct when N replicas run it.

#### Acceptance Criteria
1. THE token-refresh watchdog SHALL remain multi-replica safe (it already uses
   optimistic-locked writes); running it on every replica SHALL NOT corrupt or
   double-refresh credentials.
2. Optionally, to avoid N× scan cost, the watchdog MAY shard its scan by the
   same `shard()` on the credential's owning user/guild; this is a performance
   optimization, not a correctness requirement.

### R7 — Degradation & backward compatibility

**User Story:** As an operator, I want the sharded runtime to behave exactly
like today's single-pod runtime when `replicaCount == 1`.

#### Acceptance Criteria
1. WHEN `replicaCount == 1` THE behavior SHALL be identical to the current
   single-replica orchestrator (shard owns all guilds; no forwarding).
2. All existing degraded no-op paths (no pool / no claims / discord.py absent)
   SHALL be preserved unchanged.
3. THE change SHALL NOT alter the Primary_Bot, the on-prem orchestrator, or the
   GPU transcode model.

### R8 — Tests & gates

#### Acceptance Criteria
1. Unit/property tests SHALL cover: `shard()` partition property (disjoint +
   total), `shard_owner(app)` single-owner property, ordinal parsing +
   degradation, and forward-once hop-guard.
2. CDK tests SHALL assert: StatefulSet (not Deployment) for the orchestrator,
   hostname-topology pod anti-affinity, headless Service for stable pod DNS,
   `HELLODJ_ORCHESTRATOR_REPLICAS` env present and equal to replica count, no
   GPU request/toleration on the orchestrator pod.
3. The repo gates SHALL pass: orchestrator `ruff` + `pytest`, shared `pytest`,
   `cd infra && npx tsc --noEmit && npx jest`, and the 500-line ceiling on
   changed components.

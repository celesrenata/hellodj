# Design Document

## Overview

This feature makes the AWS platform actually **run** the multiple Discord bots a
guild has been assigned from the global pool, and formalizes the CPU→GPU
render/transcode offload with a 10-minute warm window.

Two independent workstreams:

1. **Instance_Runtime** — a new module in the standing `playback-orchestrator`
   component that reads the `bot-app-pool` secret + per-guild `BotAppClaim`s and
   runs one voice-only `discord.Client` per claimed application, assigning /
   releasing / health-checking them under the per-user entitlement quotas. This
   is the AWS port of the on-prem `bot/playback/orchestrator.py`
   `InstanceOrchestrator` (unified-playback R6), differing ONLY in the
   credential source (pool secret + claims instead of the SQLite
   `instance.<index>.` keys) and the host process (playback-orchestrator daemon
   thread instead of the on-prem bot process).

2. **GPU offload wiring** — connect the CPU-render-load scale-up trigger to the
   existing Karpenter `transcode-gpu` NodePool primitive (already does
   `WhenEmpty` + `consolidateAfter`), reuse the drain-timeout, and pin the idle
   window at 600s (done: `GpuIdleConfig` default + CDK mirror).

Both reuse existing primitives; almost nothing is invented.

## Reuse of existing components

| Concern | Existing artifact reused |
|---|---|
| Pool read | `web-ui/bot_app_pool.py` `BotAppPool` (pool reader) — extract the pool-parsing shape into the shared package so the orchestrator reuses it |
| Claim resolution | `BotAppClaim` items (`GUILD#<gid>/BOTAPP#<client_id>`), read via `CoreTable.query_pk_prefix` |
| Instance lifecycle | `bot/playback/orchestrator.py` `InstanceOrchestrator` (assign/release/health/quota) — the class is credential-source-agnostic below `initialize()`; only `initialize()` changes |
| Quota logic | `entitlements_core.effective_max_bots_per_guild` / `quota_reached` |
| Daemon-thread host | `playback_orchestrator/watchdog_bootstrap.start_watchdog_thread()` pattern (health server + background loop) |
| GPU scale-to-zero | Karpenter `transcode-gpu` NodePool, `GpuIdleConfig` / `gpu_idle_decision`, `GPU_DRAIN_TIMEOUT_SECONDS` |

## Architecture

```
playback-orchestrator process
├── health server (:PORT)                     [existing]
├── token-refresh watchdog (daemon thread)    [existing]
└── Instance_Runtime (daemon thread)           [NEW]
     ├── PoolCredentialSource
     │     ├── read hellodj/<stage>/bot-app-pool  (Secrets Manager, IRSA)
     │     └── read GUILD#<gid>/BOTAPP#* claims     (CoreTable)
     ├── AwsInstanceOrchestrator(InstanceOrchestrator)
     │     └── initialize(): build BotInstance list from pool∩claims
     │           connect N discord.Client gateways (voice-only)
     ├── assign_instance / release_instance / health_check   [inherited]
     └── entitlement quota enforcement                        [inherited]
```

### Why playback-orchestrator (not discord-bot-core)

The orchestrator already runs a standing process with a run loop, DynamoDB
access, and the daemon-thread bootstrap pattern; it survives a
`discord-bot-core` bounce (the whole point of putting the durable watchdog
there). Hosting the secondary gateways here means the extra bots stay online
across a primary-bot restart. `discord-bot-core` remains the single Primary_Bot
that owns slash commands; the runtime's Bot_Instances are voice-only.

### Single-instance compute model

All of the above runs on the one standing instance (single replica of
`playback-orchestrator`). The multi-bot runtime is NOT horizontally scaled — a
second replica would double-connect the same bot tokens (Discord rejects two
gateway identifies for one token). The runtime therefore runs at **replica
count 1** (HPA maxReplicas=1 for the orchestrator, or a leader guard). GPU
autoscaling is orthogonal: it scales the `transcode-gpu` node, not the
orchestrator.

## Components and Interfaces

### `hellodj_platform_logic.bot_app_pool` (shared, extracted)

Move the pure pool-parsing out of `web-ui/bot_app_pool.py` into the shared
package so both the web-ui and the orchestrator parse the pool identically:

```python
@dataclass(frozen=True)
class PoolApp:
    label: str
    client_id: str
    client_secret: str  # never logged
    bot_token: str      # never logged

def parse_pool(raw_json: str) -> list[PoolApp]: ...
```

The web-ui's `BotAppPool` (which exposes only label + client_id publicly) keeps
its public surface; internally it delegates parsing to `parse_pool`.

### `playback_orchestrator.instance_runtime`

```python
class PoolCredentialSource:
    def __init__(self, secrets_client, core_table, *, stage): ...
    def pool(self) -> list[PoolApp]: ...            # from the secret
    def claimed_client_ids(self, guild_id) -> set[str]: ...  # from claims
    def instances_for_guild(self, guild_id) -> list[PoolApp]:
        """pool entries claimed by the guild AND holding a bot_token."""

class AwsInstanceOrchestrator(InstanceOrchestrator):
    """Override ONLY initialize() to build BotInstances from the pool+claims.

    Everything else (assign_instance/release_instance/health_check/_enforce_quotas)
    is inherited unchanged from the on-prem orchestrator — the assignment and
    quota logic is credential-source-agnostic.
    """
    def __init__(self, primary, registry, source: PoolCredentialSource): ...
    async def initialize(self): ...   # replaces cfg("instance.<N>.*") reads
```

`initialize()` builds `BotInstance(index, client, token, application_id, ...)`
per pool app that is claimed and token-bearing, then connects them in parallel
exactly as the on-prem `_connect_instance` does (per-instance isolation:
failures mark `unhealthy`, never crash).

### `playback_orchestrator.instance_bootstrap`

Mirrors `watchdog_bootstrap`: `start_instance_runtime_thread()` builds the
source + orchestrator from env and starts the asyncio loop on a daemon thread,
self-degrading to a logged no-op when the pool is empty / unconfigured /
discord.py absent. Called from `__main__.main()` next to
`start_watchdog_thread()`.

### GPU offload (CDK + shared, mostly done)

- `GpuIdleConfig.idle_window_seconds` default = 600 (done) + CDK
  `DEFAULT_GPU_IDLE_WINDOW_SECONDS = 600` mirror (done).
- Scale-up trigger: the `hls-transcode` HPA targets a CPU threshold; when CPU
  render/transcode load exceeds it, the HPA scales up transcode pods that
  request the GPU, so Karpenter provisions a GPU_Host for the pending GPU pod
  (R6). The threshold is the existing `DEFAULT_HPA_TARGET_CPU_PERCENT` mirror.
- Drain: reuse `GPU_DRAIN_TIMEOUT_SECONDS` (120s) on the NodePool
  disruption/termination — already wired.

## Data Models

No new persistent items. Reads:

- `bot-app-pool` secret (JSON array; adds `bot_token` usage — already stored).
- `BotAppClaim` items (`GUILD#<gid>/BOTAPP#<client_id>`) — already written by
  the web-ui.

In-memory: `BotInstance` (existing dataclass) + `PoolApp` (new shared dataclass).

## Correctness Properties

### Property 1: No token leakage
No bot token or client secret ever appears in a log line or a rendered
response (assert on `repr` + captured logs).
**Validates: Requirements 1.5**

### Property 2: Claim-gated connection
A Bot_Instance is connected/assigned for a guild ONLY if its application id is
in that guild's claim set (property test over arbitrary pool ∩ claim subsets).
**Validates: Requirements 1.2, 3.5**

### Property 3: Per-instance isolation
One instance's connect/health failure never removes another instance nor stops
the loop (fault-injection test).
**Validates: Requirements 2.2**

### Property 4: Quota safety
Assignment never exceeds `effective_max_bots_per_guild` or `max_guilds`; a
resolution failure yields the restrictive default (property test mirrors the
on-prem orchestrator's quota tests).
**Validates: Requirements 4.1, 4.2, 4.3**

### Property 5: Degraded no-op
An empty/absent pool leaves the runtime disabled and the health server up.
**Validates: Requirements 2.3**

### Property 6: GPU idle bound
The idle window is always within [60, 900] and the CDK mirror equals the shared
`GpuIdleConfig` default (existing mirror test).
**Validates: Requirements 8.1, 8.4**

## Error Handling

- Missing/denied pool secret, empty pool, or missing discord.py → degraded
  no-op (log `degraded: instance runtime disabled`), health server unaffected.
- A single instance's gateway connect failure → mark `unhealthy`, continue.
- Entitlement resolution failure → restrictive defaults (never permissive).
- SIGTERM → disconnect instances within the shutdown window; the health server's
  existing signal handler drives shutdown.

## Testing Strategy

- Unit: `parse_pool` (shape, skips tokenless), `PoolCredentialSource`
  (pool∩claims), `AwsInstanceOrchestrator.initialize` builds the right
  BotInstances from a fake pool+claims + fake discord.Client.
- Property: claim-gated connection, quota safety, per-instance isolation, no
  token in repr/logs.
- Reuse: the on-prem orchestrator's assign/release/health/quota tests already
  cover the inherited behavior; add AWS-init tests only for the overridden path.
- CDK: idle window = 600 mirror (done); transcode HPA CPU-target + GPU NodePool
  consolidation assertions (extend existing `eks-gpu.test.ts`).
- Gates: ruff py314, pytest (orchestrator + shared), tsc + jest, 500-line ceiling.

## Deployment

- `playback_orchestrator/*` + shared `bot_app_pool` extraction: CodeCommit push
  → pipeline rebuilds the orchestrator image → `cdk deploy hellodj-eks -c
  hellodj:imageTag=<HEAD>` rolls the pod (immutable tag).
- CDK env/HPA/idle-window: `cdk deploy hellodj-eks` (already the manifest home).
- Orchestrator replica count pinned to 1 (single-instance model) so bot tokens
  are never double-connected.
- Update `hellodj-architecture.md` with the runtime + GPU-offload model.

## Scope note

This feature is the AWS RUNTIME + GPU-offload wiring only. It does NOT change:
the pool provisioning / claim / invite-link UI (already shipped), the on-prem
`InstanceOrchestrator` (unchanged; this subclasses/mirrors it), or the Discord
ToS-compliance model (unified-playback R6, unchanged — distinct applications).

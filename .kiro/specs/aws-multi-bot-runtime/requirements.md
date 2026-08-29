# Requirements Document

## Introduction

The AWS platform can already **provision** multiple Discord bots per guild (the
global `hellodj/<stage>/bot-app-pool` Secrets Manager pool of pre-registered
applications, per-guild `BotAppClaim` items, quota-gated assignment, and invite
links on the guild detail page). What is missing is the **runtime** that
actually connects those claimed bot applications to Discord so they appear
online and can play audio in additional voice channels — the AWS equivalent of
the on-prem `Instance_Orchestrator` (unified-playback Requirement 6), whose
credentials live in the encrypted SQLite store under `instance.<index>.` keys
that do not exist on AWS.

This feature ports the multi-instance runtime to the AWS `playback-orchestrator`
component: it reads the bot-application pool + per-guild claims, runs one
secondary Discord gateway connection per claimed application on the standing
orchestrator process (alongside the existing durable token watchdog and health
server), assigns/releases those instances to voice channels under the per-user
entitlement quotas, and health-checks them.

It also formalizes the compute model the operator described: **everything runs
on a single instance** until CPU load from rendering/transcoding crosses a
threshold, at which point a **GPU host is spun up**, render/transcode work is
**drained onto the GPU**, and the GPU host is kept **warm for 10 minutes** after
its last use before it is **spun down**. The GPU scale-to-zero primitives
already exist (Karpenter `transcode-gpu` NodePool with `WhenEmpty` +
`consolidateAfter`, the shared `GpuIdleConfig`/`gpu_idle_decision` logic, now
defaulted to a 600-second window); this feature wires the CPU-threshold
scale-up trigger and the drain behavior to that primitive and pins the warm
window at 10 minutes.

## Glossary

- **Bot_App_Pool**: The global set of pre-registered Discord applications stored
  in the `hellodj/<stage>/bot-app-pool` Secrets Manager secret as a JSON array
  of `{label, client_id, client_secret, bot_token}`. Each entry is a distinct
  Discord application (a distinct bot identity).
- **Bot_App_Claim**: A per-guild DynamoDB item (`PK=GUILD#<gid>`,
  `SK=BOTAPP#<client_id>`, entityType `BotAppClaim`) recording that a guild has
  been assigned a pool application. Written by the web-ui assignment flow.
- **Bot_Instance**: A running secondary Discord gateway connection for one
  claimed pool application — its own `discord.Client`, bot token, and
  application id — capable of joining one voice channel per guild.
- **Instance_Runtime**: The AWS-hosted coordination layer (in
  `playback-orchestrator`) that loads the pool + claims, connects Bot_Instances,
  and assigns/releases them to voice channels. The AWS port of the on-prem
  `Instance_Orchestrator` (unified-playback R6).
- **Primary_Bot**: The single main `discord-bot-core` gateway connection that
  owns slash commands. Bot_Instances are voice-only secondaries.
- **Render_Transcode_Load**: CPU utilization attributable to HLS transcoding and
  visualizer rendering on the CPU (non-GPU) path.
- **GPU_Host**: A Karpenter-provisioned node from the `transcode-gpu` NodePool
  that serves GPU-accelerated transcode/render workloads.
- **GPU_Idle_Window**: The continuous no-active-transcode duration after which
  the GPU_Host scales to zero — a 10-minute (600s) warm window
  (`GpuIdleConfig.idle_window_seconds`, valid range [60, 900]).
- **Entitlement_Quota**: The per-user `max_bots_per_guild` and `max_guilds`
  entitlements (admin-entitlements-panel) that bound how many Bot_Instances a
  user may run in a guild and across guilds.

## Requirements

### Requirement 1: Bot instance credential source (AWS pool, not SQLite)

**User Story:** As a platform operator, I want the AWS multi-bot runtime to
source its bot applications from the Secrets Manager pool and per-guild claims,
so that the on-prem SQLite `instance.<index>.` credential model is not required
on AWS.

#### Acceptance Criteria

1. THE Instance_Runtime SHALL read the Bot_App_Pool from the
   `hellodj/<stage>/bot-app-pool` secret (resolved by stage) as the source of
   Bot_Instance credentials.
2. THE Instance_Runtime SHALL connect a Bot_Instance only for a pool application
   that has an active Bot_App_Claim for the guild it is serving.
3. WHERE a pool entry has an empty `bot_token`, THE Instance_Runtime SHALL skip
   that entry and log it (no token → cannot connect), without failing other
   instances.
4. THE Instance_Runtime SHALL NOT read or require the on-prem
   `instance.<index>.token` / `instance.<index>.app_id` SQLite keys.
5. THE Instance_Runtime SHALL never log a bot token or client secret.

### Requirement 2: Bot instance gateway lifecycle on the standing orchestrator

**User Story:** As a platform operator, I want the secondary bot gateways to run
inside the standing `playback-orchestrator` process, so that they survive a
`discord-bot-core` restart and reuse the component that already holds a run loop
and datastore access.

#### Acceptance Criteria

1. THE Instance_Runtime SHALL start inside the `playback-orchestrator`
   component's process, alongside the existing health server and token watchdog.
2. WHEN the Instance_Runtime starts, THE Instance_Runtime SHALL run on a
   background thread/loop such that a Bot_Instance connection failure never
   crashes the health server or the token watchdog (per-instance isolation).
3. WHEN no Bot_App_Pool secret is configured OR the pool is empty OR discord.py
   is unavailable, THE Instance_Runtime SHALL self-degrade to a no-op and log
   `degraded: instance runtime disabled`, and the health server SHALL still
   come up.
4. WHEN the process receives SIGTERM/SIGINT, THE Instance_Runtime SHALL
   disconnect its Bot_Instances cleanly within the shutdown window.
5. THE Instance_Runtime SHALL share one Lavalink node/session across all its
   Bot_Instances (mirroring unified-playback R6.7).

### Requirement 3: Instance assignment and release

**User Story:** As a server member, I want a claimed extra bot to join my voice
channel when the primary bot is busy elsewhere, so that music can play in more
than one channel at once.

#### Acceptance Criteria

1. WHEN audio playback is requested for a voice channel that has no Bot_Instance
   connected and the Primary_Bot is connected to a different voice channel in
   the same guild, THE Instance_Runtime SHALL assign the first available
   Bot_Instance (status `available`) to that channel.
2. WHEN a Bot_Instance is already connected to the requesting voice channel, THE
   Instance_Runtime SHALL reuse it without reassignment.
3. IF no Bot_Instance is available (all connected elsewhere), THEN THE
   Instance_Runtime SHALL report that all music slots are in use, listing each
   occupied channel and the Bot_Instance display name.
4. WHEN a Bot_Instance disconnects (explicit stop OR 5 minutes with no listeners
   in the channel), THE Instance_Runtime SHALL set its status to `available` and
   clear its channel assignment within 5 seconds.
5. THE Instance_Runtime SHALL only assign Bot_Instances whose application is
   claimed by the guild being served (R1.2).

### Requirement 4: Entitlement quota enforcement

**User Story:** As a platform operator, I want per-guild and per-user bot limits
enforced at assignment time, so that a user cannot exceed the bots they are
entitled to.

#### Acceptance Criteria

1. WHEN assigning a Bot_Instance on behalf of an owning user, THE
   Instance_Runtime SHALL resolve that user's effective entitlements and reject
   an assignment that would exceed `effective_max_bots_per_guild` for the guild
   (`quota_reached`), with a clear limit message.
2. WHEN assigning a Bot_Instance for a guild the user has no active instance in,
   THE Instance_Runtime SHALL reject the assignment if it would exceed the
   user's `max_guilds` limit.
3. WHERE entitlement resolution fails, THE Instance_Runtime SHALL apply the
   secure default entitlements (limits = 1), never a more-permissive fallback.
4. THE Instance_Runtime SHALL reuse the shared entitlement decision helpers
   (`entitlements_core.effective_max_bots_per_guild`, `quota_reached`) so the
   web-ui, the on-prem orchestrator, and this runtime agree exactly.

### Requirement 5: Bot instance health checking

**User Story:** As a platform operator, I want unhealthy bot instances detected
and skipped, so that a dead gateway connection does not silently swallow
playback requests.

#### Acceptance Criteria

1. IF a Bot_Instance fails a health check within 10 seconds, THEN THE
   Instance_Runtime SHALL mark it `unhealthy`, skip it during assignment, and
   clear any channel assignment.
2. WHEN a previously unhealthy Bot_Instance recovers (gateway ready, finite
   latency) and holds no assignment, THE Instance_Runtime SHALL mark it
   `available` again.
3. THE Instance_Runtime SHALL run health checks periodically without blocking
   assignment or playback.

### Requirement 6: CPU-threshold GPU scale-up trigger

**User Story:** As a platform operator, I want the GPU host to spin up when CPU
render/transcode load is high, so that heavy visual workloads move off the CPU
before they degrade interactive latency.

#### Acceptance Criteria

1. WHEN Render_Transcode_Load on the CPU path crosses the configured scale-up
   threshold, THE platform SHALL cause a GPU-requiring transcode/render workload
   to be scheduled, so Karpenter provisions a GPU_Host for the pending GPU pod.
2. WHILE the GPU_Host is provisioning, THE CPU transcode path SHALL continue
   serving in-flight work so the interactive latency budget holds during
   spin-up.
3. THE scale-up trigger threshold SHALL be a single configured value mirrored
   between the CDK (HPA/autoscale target) and the shared autoscale logic.

### Requirement 7: Drain render/transcode to the GPU

**User Story:** As a platform operator, I want render/transcode work to move to
the GPU once it is available, so that the GPU host does the heavy lifting while
it is warm.

#### Acceptance Criteria

1. WHEN a GPU_Host is available, THE platform SHALL schedule transcode/render
   workloads onto it (the `transcode-gpu` NodePool via the transcode taint /
   toleration), draining new work off the CPU path.
2. WHEN a GPU_Host is being reclaimed/terminated, THE platform SHALL gracefully
   drain in-flight transcode jobs within the drain-timeout window before the
   node is removed.
3. THE drain behavior SHALL reuse the existing drain-timeout primitive
   (`GPU_DRAIN_TIMEOUT_SECONDS`) rather than introducing a new one.

### Requirement 8: GPU warm window and scale-to-zero

**User Story:** As a platform operator, I want the GPU host kept warm for 10
minutes after its last use and then spun down, so that bursty workloads avoid
repeated cold starts without paying for an idle GPU indefinitely.

#### Acceptance Criteria

1. THE GPU_Idle_Window SHALL default to 600 seconds (10 minutes).
2. WHEN the GPU_Host has had no active transcode workload for a continuous
   GPU_Idle_Window, THE platform SHALL scale the `transcode-gpu` NodePool to
   zero (`consolidationPolicy: WhenEmpty` + `consolidateAfter: <window>`).
3. WHILE any transcode workload is active on the GPU_Host, THE platform SHALL
   NOT scale it down (WhenEmpty fires only at zero transcode pods).
4. THE GPU_Idle_Window SHALL remain within the enforced [60, 900] second range,
   and the CDK mirror SHALL equal the shared `GpuIdleConfig` default.

### Requirement 9: CDK wiring and least privilege

**User Story:** As a platform operator, I want the orchestrator granted exactly
the access the multi-bot runtime needs, so that the runtime works under IRSA
with no static keys and no over-broad grants.

#### Acceptance Criteria

1. THE `playback-orchestrator` service account SHALL have READ on the
   `hellodj/<stage>/bot-app-pool` secret (already granted) and READ on the
   `hellodj-core` table's `GUILD#*` / `BOTAPP#*` items to resolve claims.
2. THE CDK SHALL wire the `playback-orchestrator` container env needed by the
   runtime (stage, region, pool secret name resolution, Lavalink node URL).
3. THE runtime SHALL require NO static Discord credentials in the manifest — bot
   tokens are read from the pool secret at runtime via IRSA.
4. THE GPU scale-up threshold and the 600s idle window SHALL be expressed in the
   CDK so a deploy realizes both the trigger and the warm window.

### Requirement 10: Gates and deployment

**User Story:** As a platform operator, I want the change to pass the repo gates
and deploy through the established path, so that it ships safely.

#### Acceptance Criteria

1. THE change SHALL pass `ruff` (py314), the web-ui/orchestrator pytest suites,
   the CDK `tsc --noEmit` + `jest` suites, and the 500-line-per-file ceiling.
2. Component source changes (playback-orchestrator `*.py`) SHALL deploy via the
   CodeCommit push → pipeline image rebuild → `cdk deploy hellodj-eks` roll
   path; infra changes SHALL deploy via `cdk deploy` (not a plain push).
3. THE architecture steering docs SHALL be updated with the AWS multi-bot
   runtime + GPU-offload model (docs-in-sync rule).

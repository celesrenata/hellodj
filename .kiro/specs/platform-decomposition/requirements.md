# Requirements Document

## Introduction

The HelloDJ Platform Decomposition transforms the monolithic single-pod deployment into a distributed AWS-native architecture with two layers: a stateless API/Orchestrator (ECS Fargate) that handles HTTP traffic, Discord interactions, auth, tenant management, and worker coordination; and a pool of stateless Audio/Video Worker Tasks (ECS EC2 with GPU capacity provider) that handle actual audio playback, video transcoding, and visualizer rendering. The platform leverages AWS managed services — DynamoDB (data), ElastiCache Redis (ephemeral state), Cognito (auth with Discord federation), ECR (images), CloudFront (HLS delivery), and Secrets Manager (credentials) — to minimize operational overhead and idle costs. Infrastructure is defined in AWS CDK (TypeScript). The dev/test environment remains the 4-node K3s cluster (gremlins); production is AWS.

**Key architectural decisions:**
- **ECS over EKS**: No $73/mo control plane fee. Fargate for API (pay-per-invocation-second), EC2 capacity providers with g4dn spot for GPU workers.
- **DynamoDB over PostgreSQL (prod)**: Pay-per-request, zero idle cost, auto-scales reads/writes, no connection pool management. Dev cluster keeps PostgreSQL for compatibility.
- **Cognito with Discord federation**: Discord OAuth2 is the primary identity flow for all users. Cognito wraps it — issuing JWTs, managing sessions, providing account recovery + optional MFA for admin/operator accounts if Discord gets compromised.
- **CDK (TypeScript)**: Full infrastructure pipeline — VPC, ECS clusters, task definitions, DynamoDB tables, Cognito pools, CloudFront, CodePipeline for CI/CD.
- **Nix-based container images**: All container images built with Nix (nixpkgs) rather than Ubuntu/Debian. Produces minimal, reproducible OCI images with exact dependency closures. Enables pinning to latest upstream versions (FFmpeg, Python, Java, etc.) without waiting for distro packaging. Dev cluster already runs NixOS — production images use the same Nix derivations for parity.

## Glossary

- **Orchestrator**: The stateless API/coordination layer running as ECS Fargate tasks behind an Application Load Balancer. Handles HTTP traffic, Discord interactions (via Cognito), tenant management, credential distribution, and worker assignment. Zero idle cost when scaled to minimum (1 Fargate task).
- **Worker**: A stateless ECS task running on EC2 capacity provider (g4dn.xlarge spot for GPU, t3.medium for audio-only) containing a bot container and Lavalink sidecar. Boots blank, receives credentials from Orchestrator at assignment time.
- **Worker_Pool**: The ECS Service managing Worker tasks, scaled by Application Auto Scaling based on custom CloudWatch metrics from Redis/DynamoDB.
- **Assignment**: The binding of a Worker to a specific tenant, guild, and set of credentials for the duration of an active playback session.
- **DynamoDB_Store**: The primary data store for all persistent platform data in production: credentials (encrypted), guild settings, playlists, content filters, user bans, tenant configuration. Pay-per-request billing.
- **Session_Cache**: ElastiCache Redis Serverless instance storing ephemeral session state: active queue, current track position, worker assignments, heartbeats, pub/sub channels, and Cognito session tokens.
- **Cognito_Pool**: AWS Cognito User Pool with Discord configured as a federated identity provider. Issues JWTs for API access. Provides account recovery and optional MFA for operator/admin accounts.
- **CDK_Pipeline**: AWS CDK (TypeScript) application defining all infrastructure as code, deployed via CodePipeline with source from GitHub.
- **Capacity_Provider**: ECS EC2 Capacity Provider with Auto Scaling Group that provisions g4dn.xlarge spot instances for GPU workers and t3.medium instances for audio-only workers. Scales to zero when no tasks need placement.
- **Audio_Pipe**: A POSIX FIFO shared between the bot container and Lavalink sidecar within a single Worker task, used to stream PCM audio data from Lavalink to the visualizer engine.
- **HLS_Delivery**: CloudFront distribution serving HLS segments from Worker tasks (origin: ALB path-based routing to assigned worker) with sub-second segment caching.
- **Credential_Envelope**: A signed payload retrieved from Secrets Manager + DynamoDB containing all credentials needed by a Worker at assignment time.
- **Dev_Cluster**: The 4-node K3s cluster (gremlin-1 through gremlin-4) with Intel iGPUs, running PostgreSQL + Redis locally. Used for development and testing with the same application code but local infrastructure.
- **Prod_Cluster**: AWS ECS cluster with Fargate (API) + EC2 capacity providers (workers), DynamoDB, ElastiCache Redis Serverless, Cognito, CloudFront, and CDK-managed infrastructure.

## Requirements

### Requirement 1: Data Migration from Longhorn PVC to PostgreSQL

**User Story:** As a platform operator, I want all persistent data migrated from the Longhorn PVC to PostgreSQL, so that workers can be stateless and the platform eliminates its single-point-of-failure storage dependency.

#### Acceptance Criteria

1. WHEN the migration is executed, THE Orchestrator_Store SHALL contain all records from `hellodj.db` (credential store) with encrypted values preserved byte-for-byte in the PostgreSQL `credentials` table.
2. WHEN the migration is executed, THE Orchestrator_Store SHALL contain all guild settings from `guild_settings.json` in a `guild_settings` table with columns for guild_id, tenant_id, mode, visualizer_engine, and preset configuration.
3. WHEN the migration is executed, THE Orchestrator_Store SHALL contain all content filter rules from `content_filters.json` in a `content_filters` table keyed by guild_id and tenant_id.
4. WHEN the migration is executed, THE Orchestrator_Store SHALL contain all user ban records from `user_bans.json` in a `user_bans` table keyed by guild_id and tenant_id.
5. WHEN the migration is executed, THE Session_Cache SHALL contain all active session data from `sessions.json` with queue state, current track, and channel identifiers.
6. WHEN the migration is executed, THE Orchestrator_Store SHALL contain all metrics data from `metrics.json` in a `usage_metrics` table or forwarded to Prometheus exposition format.
7. IF any record fails to migrate, THEN THE migration script SHALL log the failed record key, error details, and continue processing remaining records without aborting.
8. THE migration script SHALL provide a summary report indicating the count of migrated records, skipped records, and failed records per data source.

### Requirement 2: Orchestrator Layer Deployment

**User Story:** As a platform operator, I want a stateless, horizontally-replicated API/Orchestrator layer, so that the platform can handle HTTP traffic and coordinate workers without single-point-of-failure constraints.

#### Acceptance Criteria

1. THE Orchestrator SHALL be deployed as a Kubernetes Deployment with at least 2 replicas behind Traefik ingress at `hellodj.celestium.life`.
2. THE Orchestrator SHALL handle all HTTP traffic including Discord OAuth2 flows, tenant management API, admin panel, web player API, and worker coordination endpoints.
3. THE Orchestrator SHALL store no local state — all persistent data SHALL be read from the Orchestrator_Store (PostgreSQL) and Session_Cache (Redis).
4. THE Orchestrator SHALL expose an internal API endpoint for Workers to register, report heartbeats, and receive assignments.
5. THE Orchestrator SHALL expose an internal API endpoint for delivering Credential_Envelopes to Workers upon assignment.
6. IF one Orchestrator replica becomes unavailable, THEN the remaining replicas SHALL continue serving all requests without data loss or session interruption.
7. THE Orchestrator SHALL serve the web-ui (Flask application), Activity frontend static assets, and WebSocket proxy endpoints.

### Requirement 3: Worker Pod Specification

**User Story:** As a platform operator, I want workers to be fully stateless pods that boot blank and receive all configuration at assignment time, so that they can be scheduled on any cluster node and scaled dynamically.

#### Acceptance Criteria

1. THE Worker SHALL contain exactly two containers: a bot container (Python, Discord gateway + wavelink + Activity backend) and a Lavalink sidecar container (Java, audio streaming).
2. THE Worker SHALL mount no persistent volumes — all storage SHALL be limited to an emptyDir tmpfs for HLS segments and an emptyDir for the rendered Lavalink configuration.
3. THE Worker SHALL access GPU hardware via a hostPath mount of `/dev/dri` for Intel iGPU (QSV/VA-API) transcoding, with no SR-IOV resource claims required.
4. THE Worker SHALL share an emptyDir volume (Memory-backed, 2Gi) between the bot container and Lavalink sidecar at `/tmp/hellodj_hls` for Audio_Pipe FIFO and HLS segment storage.
5. WHEN a Worker starts, THE Worker SHALL register with the Orchestrator via HTTP POST to the Worker_Registry endpoint within 10 seconds of container readiness.
6. THE Worker SHALL report heartbeats to the Session_Cache (Redis key `heartbeat:{worker_id}` with 30-second TTL) every 15 seconds.
7. IF the Worker does not receive an assignment within 5 minutes of registration, THEN THE Worker SHALL remain idle and available in the Worker_Pool, eligible for scale-down by KEDA.

### Requirement 4: Worker Assignment Lifecycle

**User Story:** As the Orchestrator, I want to assign tenants and guilds to workers dynamically, so that workers can serve any tenant without pre-configuration.

#### Acceptance Criteria

1. WHEN a tenant initiates playback and no Worker currently holds an assignment for that guild, THE Orchestrator SHALL select an available Worker from the Worker_Registry and deliver a Credential_Envelope containing the bot token, guild ID, voice channel ID, and all required service credentials.
2. WHEN a Worker receives an assignment, THE Worker SHALL connect to the Discord gateway with the provided bot token within 15 seconds of receiving the Credential_Envelope.
3. WHEN a Worker receives an assignment, THE Worker SHALL render the Lavalink configuration from the provided credentials and start the Lavalink sidecar connection within 15 seconds.
4. WHEN a Worker receives an assignment, THE Worker SHALL fetch the current queue state and playback position from the Session_Cache and resume playback from the last known position.
5. WHEN a voice channel becomes empty or an inactivity timeout (configurable, default 5 minutes) expires, THE Worker SHALL release its assignment by notifying the Orchestrator, persisting final queue state to the Session_Cache, and disconnecting from the Discord gateway.
6. WHEN a Worker releases an assignment, THE Orchestrator SHALL mark the Worker as available in the Worker_Registry for future assignments.
7. IF a Worker crashes during an active assignment, THEN THE Orchestrator SHALL detect the missing heartbeat within 45 seconds and reassign the guild to another available Worker using the last persisted queue state from the Session_Cache.

### Requirement 5: Worker Credential Delivery

**User Story:** As a worker, I want to receive all required credentials securely from the Orchestrator at assignment time, so that I never store credentials locally and can serve any tenant.

#### Acceptance Criteria

1. WHEN the Orchestrator delivers a Credential_Envelope to a Worker, THE Orchestrator SHALL transmit the envelope over the cluster-internal network (pod-to-pod) using HTTPS or mutual TLS.
2. THE Credential_Envelope SHALL contain: Discord bot token, Tidal OAuth token, Spotify OAuth token, YouTube OAuth refresh token, YouTube PoToken and visitor data, the Fernet encryption key, and the PostgreSQL connection URI.
3. THE Worker SHALL hold credentials only in memory for the duration of the assignment and SHALL NOT write credentials to any persistent or shared filesystem.
4. WHEN a Worker releases an assignment, THE Worker SHALL discard all credentials from memory.
5. IF credential delivery fails (network timeout exceeding 10 seconds or HTTP error), THEN THE Orchestrator SHALL retry delivery up to 3 times with exponential backoff (1s, 2s, 4s) before marking the assignment as failed and selecting a different Worker.

### Requirement 6: Lavalink Sidecar Co-Location

**User Story:** As the system architect, I want Lavalink to run as a sidecar within each Worker pod, so that audio pipe locality is maintained and player session state stays coupled to the worker.

#### Acceptance Criteria

1. THE Worker pod SHALL run Lavalink as a sidecar container sharing the pod's network namespace, accessible from the bot container at `localhost:2333`.
2. THE Lavalink sidecar SHALL receive its `application.yml` configuration from a shared emptyDir volume, rendered by the bot container's init process using credentials from the Credential_Envelope.
3. THE Audio_Pipe FIFO SHALL be created in the shared tmpfs volume (`/tmp/hellodj_hls/{guild_id}/viz/audio.pipe`) accessible to both the bot container and Lavalink sidecar.
4. WHEN the Worker receives an assignment, THE bot container SHALL render the Lavalink configuration and signal readiness to the Lavalink sidecar before initiating the Discord gateway connection.
5. IF the Lavalink sidecar becomes unresponsive (health check fails for 30 seconds), THEN THE Worker SHALL report degraded status to the Orchestrator and attempt a local restart of the Lavalink process.

### Requirement 7: HLS Segment Delivery in Distributed Architecture

**User Story:** As a user watching a Discord Activity, I want HLS video segments delivered with low latency regardless of which worker is handling my guild, so that video playback is seamless.

#### Acceptance Criteria

1. WHILE a Worker is transcoding video, THE Worker SHALL write HLS segments to the local tmpfs volume and serve them directly via the Activity backend on port 8090.
2. THE Orchestrator SHALL proxy Activity HTTP requests (path `/activity/*`) to the correct Worker pod based on the guild-to-worker assignment in the Worker_Registry.
3. WHEN a client connects to the Activity WebSocket endpoint, THE Orchestrator SHALL proxy the WebSocket connection to the Worker currently assigned to that guild.
4. IF the assigned Worker becomes unavailable during an active Activity session, THEN THE Orchestrator SHALL return HTTP 503 to Activity clients until a replacement Worker is assigned and ready.
5. THE Orchestrator SHALL maintain a routing table (guild_id → worker_pod_ip) in the Session_Cache, updated whenever assignments change.

### Requirement 8: WebSocket Hub Externalization

**User Story:** As the system architect, I want WebSocket state broadcasting externalized via Redis pub/sub, so that multiple Orchestrator replicas can relay real-time updates to connected clients.

#### Acceptance Criteria

1. WHEN a Worker publishes a state update (track change, playback position, whiteboard stroke, visualizer data), THE Worker SHALL publish the update to a Redis pub/sub channel named `ws:{guild_id}`.
2. THE Orchestrator SHALL subscribe to all `ws:*` channels for guilds with connected Activity clients and forward messages to the appropriate WebSocket connections.
3. WHEN a client sends a WebSocket command (play, pause, seek, whiteboard action), THE Orchestrator SHALL publish the command to the Redis pub/sub channel `cmd:{guild_id}` for the assigned Worker to consume.
4. THE Worker SHALL subscribe to the `cmd:{guild_id}` channel for its assigned guild and execute received commands within 200 milliseconds of receipt.
5. IF Redis pub/sub becomes unavailable, THEN THE Orchestrator SHALL buffer WebSocket messages for up to 5 seconds and retry delivery, returning an error to clients if the outage exceeds 5 seconds.

### Requirement 9: KEDA Event-Driven Scaling

**User Story:** As a platform operator, I want workers to scale automatically based on demand metrics, including scaling to zero when no tenants are actively playing, so that infrastructure costs are minimized.

#### Acceptance Criteria

1. THE platform SHALL deploy a KEDA ScaledObject targeting the Worker_Pool Deployment with scaling metrics derived from the Session_Cache.
2. WHEN the number of active guild voice sessions (Redis key `hellodj:active_sessions` or equivalent metric) increases, THE KEDA_Scaler SHALL increase Worker_Pool replicas proportionally, with a minimum of 1 Worker per active session.
3. WHEN no tenants have active playback sessions for a configurable cooldown period (default 5 minutes), THE KEDA_Scaler SHALL scale the Worker_Pool to zero replicas.
4. WHEN a new playback request arrives while the Worker_Pool is at zero replicas, THE Orchestrator SHALL queue the request and THE KEDA_Scaler SHALL scale up at least one Worker, with the first Worker ready to accept an assignment within 30 seconds on the Dev_Cluster.
5. THE KEDA_Scaler SHALL support separate scaling triggers for audio-only Workers and GPU video Workers based on the `hellodj:video_transcode_active` metric.
6. THE KEDA_Scaler SHALL enforce a maximum replica count (configurable, default 20 for audio, 8 for video) to prevent runaway scaling.

### Requirement 10: AWS Production Deployment with Karpenter

**User Story:** As a platform operator deploying to AWS EKS, I want Karpenter to auto-provision appropriately-sized nodes for pending worker pods, so that the cluster scales infrastructure on demand without pre-provisioning.

#### Acceptance Criteria

1. THE Prod_Cluster SHALL define separate Karpenter NodePool resources for audio-only Workers (c7g.medium ARM Graviton instances) and GPU video Workers (g4dn.xlarge or g5.xlarge instances).
2. WHEN a Worker pod is pending due to insufficient cluster capacity, THE Karpenter_Provisioner SHALL provision a new node of the appropriate instance type within 2 minutes.
3. THE Karpenter_Provisioner SHALL prefer Spot instances for audio-only Workers, falling back to On-Demand instances if Spot capacity is unavailable.
4. THE Karpenter_Provisioner SHALL use On-Demand instances for GPU video Workers due to limited Spot availability for GPU instance types.
5. WHEN a node has no scheduled Worker pods for a configurable TTL (default 5 minutes), THE Karpenter_Provisioner SHALL terminate the node to reduce costs.
6. THE Orchestrator SHALL be deployable on t3.small instances or AWS Fargate with minimal compute requirements (250m CPU, 512Mi RAM per replica).
7. WHEN the first cold-start Worker is requested in the Prod_Cluster (including node provisioning), THE Worker SHALL be ready to accept an assignment within 2 minutes.

### Requirement 11: Backward Compatibility During Migration

**User Story:** As a platform operator, I want the migration from monolith to distributed architecture to be incremental and non-disruptive, so that the platform continues serving users throughout the transition.

#### Acceptance Criteria

1. WHILE the monolith deployment is still active, THE Orchestrator SHALL coexist alongside it in the same namespace without port or resource conflicts.
2. THE Orchestrator SHALL read from the same PostgreSQL and Redis instances as the existing web-ui and SaaS auth system.
3. WHEN a feature is migrated from the monolith to the distributed architecture, THE Orchestrator SHALL expose the same API endpoints with identical request/response formats as the monolith's web-ui.
4. THE platform SHALL support a hybrid mode where the monolith handles some guilds while Workers handle others, determined by a per-tenant feature flag (`distributed_playback: true/false`).
5. WHEN the hybrid mode feature flag is enabled for a tenant, THE Orchestrator SHALL route that tenant's playback requests to the Worker_Pool instead of the monolith.
6. IF a rollback is required, THEN disabling the hybrid mode feature flag SHALL immediately route affected tenants back to the monolith without data loss, using the shared Session_Cache as the source of truth.
7. THE migration SHALL proceed in phases: data migration first, then Orchestrator deployment, then Worker pool deployment, then per-tenant cutover, then monolith decommissioning.

### Requirement 12: Worker Health Monitoring and Self-Healing

**User Story:** As the Orchestrator, I want continuous visibility into worker health and automatic recovery from failures, so that playback disruptions are minimized.

#### Acceptance Criteria

1. THE Orchestrator SHALL check the Worker_Registry for missing heartbeats every 30 seconds and mark Workers with heartbeats older than 45 seconds as unhealthy.
2. WHEN a Worker is marked unhealthy, THE Orchestrator SHALL reassign its active guilds to healthy Workers within 60 seconds of detection.
3. WHEN a Worker is reassigned due to health failure, THE replacement Worker SHALL resume playback from the last persisted queue state and track position in the Session_Cache, with no more than 5 seconds of audible interruption.
4. THE Orchestrator SHALL track restart counts per Worker and escalate to permanent removal (pod deletion) after 5 health failures within a 10-minute window.
5. WHEN a Worker is permanently removed, THE Orchestrator SHALL request KEDA to evaluate scaling (which may provision a replacement Worker).
6. THE Worker SHALL expose a `/health` HTTP endpoint returning status 200 when healthy and status 503 when degraded, used by Kubernetes liveness and readiness probes.

### Requirement 13: Queue State Persistence and Recovery

**User Story:** As a user, I want my playback queue and current track position preserved even if the worker handling my guild crashes, so that I do not lose my listening session.

#### Acceptance Criteria

1. WHILE a Worker is playing audio, THE Worker SHALL persist the current queue state (track list, current index, position in seconds) to the Session_Cache every 10 seconds.
2. WHEN a track change occurs, THE Worker SHALL immediately persist the updated queue state to the Session_Cache before beginning playback of the next track.
3. WHEN a Worker receives a reassignment for a guild, THE Worker SHALL read the last persisted queue state from the Session_Cache and resume playback from the recorded track and position.
4. IF the Session_Cache is unavailable during a persistence write, THEN THE Worker SHALL buffer the state update locally and retry within 5 seconds, logging a warning.
5. THE Session_Cache SHALL retain queue state for a guild for 24 hours after the last update, after which the state SHALL expire automatically.
6. WHEN a Worker shuts down gracefully (SIGTERM), THE Worker SHALL persist final queue state to the Session_Cache before exiting, within the pod's termination grace period (30 seconds).

### Requirement 14: Orchestrator-to-Worker Internal API

**User Story:** As a platform architect, I want a well-defined internal API between the Orchestrator and Workers, so that the two layers are loosely coupled and independently deployable.

#### Acceptance Criteria

1. THE Orchestrator SHALL expose the following internal endpoints: `POST /internal/workers/register` (Worker registration), `POST /internal/workers/{id}/heartbeat` (heartbeat), `POST /internal/assignments` (create assignment), `DELETE /internal/assignments/{guild_id}` (release assignment), `GET /internal/assignments/{guild_id}` (query assignment).
2. THE Worker SHALL expose the following internal endpoints: `POST /internal/assign` (receive Credential_Envelope and guild assignment), `DELETE /internal/assign` (release current assignment), `GET /health` (health check).
3. THE internal API SHALL be accessible only within the Kubernetes cluster network (ClusterIP services, no ingress exposure).
4. WHEN the Orchestrator sends an assignment to a Worker, THE Worker SHALL respond with HTTP 200 and a readiness timestamp within 15 seconds, or HTTP 503 if it cannot accept the assignment.
5. THE internal API SHALL use structured JSON payloads with versioned schemas (header `X-API-Version: 1`), enabling future protocol evolution without breaking existing deployments.

### Requirement 15: GPU Access for Video Workers

**User Story:** As a video worker, I want access to the host GPU via a simple hostPath mount, so that I can perform hardware-accelerated video transcoding and visualizer rendering without complex device plugin configuration.

#### Acceptance Criteria

1. THE Worker pod specification SHALL mount `/dev/dri` from the host as a hostPath volume, granting access to all DRM render nodes on the host.
2. THE Worker pod specification SHALL include `supplementalGroups: [26]` (video group) and `privileged: true` on the bot container for direct GPU device access.
3. WHILE running on the Dev_Cluster (gremlin nodes with Intel Meteor Lake iGPUs), THE Worker SHALL use QSV/VA-API hardware acceleration for H.264 encoding without SR-IOV resource claims.
4. WHILE running on the Prod_Cluster with NVIDIA GPU instances (g4dn/g5), THE Worker SHALL use NVENC hardware acceleration and request `nvidia.com/gpu: 1` resource.
5. THE Orchestrator SHALL track which Workers have GPU access and route video/visualizer assignments only to GPU-capable Workers.
6. IF no GPU-capable Worker is available for a video assignment, THEN THE Orchestrator SHALL queue the request and signal KEDA to scale up a GPU Worker.

### Requirement 16: Rolling Updates with Zero Downtime

**User Story:** As a platform operator, I want to deploy new Worker and Orchestrator versions with zero playback interruption, so that users are not affected by maintenance operations.

#### Acceptance Criteria

1. WHEN a new Worker image is deployed via rolling update, THE Kubernetes Deployment SHALL use `maxSurge: 1` and `maxUnavailable: 0` to ensure no active Workers are terminated before replacements are ready.
2. WHEN a Worker receives SIGTERM during a rolling update, THE Worker SHALL drain its active assignment (persist state, notify Orchestrator of release) within the 30-second termination grace period before exiting.
3. THE Orchestrator SHALL not assign new guilds to Workers that have received SIGTERM (detected via readiness probe failure).
4. WHEN an Orchestrator replica receives SIGTERM during a rolling update, THE Orchestrator SHALL complete in-flight HTTP requests and gracefully close WebSocket connections within 15 seconds.
5. THE platform SHALL support canary deployments where a percentage of new assignments are routed to Workers running the new image version, controlled by a Deployment label selector.

### Requirement 17: Observability and Distributed Tracing

**User Story:** As a platform operator, I want comprehensive observability across the distributed system, so that I can diagnose issues and monitor performance across Orchestrator and Workers.

#### Acceptance Criteria

1. THE Orchestrator SHALL expose Prometheus metrics at `/metrics` including: active assignments count, Worker pool size, assignment latency histogram, credential delivery latency, and WebSocket connection count.
2. THE Worker SHALL expose Prometheus metrics at `/metrics` including: playback duration counter, track skip count, audio pipe health, Lavalink connection status, GPU utilization percentage, and HLS segment generation rate.
3. THE Orchestrator and Workers SHALL emit structured JSON logs with fields: timestamp, level, component, guild_id, tenant_id, worker_id, and trace_id.
4. THE Orchestrator SHALL propagate trace context headers (W3C Trace Context format) to Workers on all internal API calls, enabling end-to-end distributed tracing.
5. WHEN a Worker fails a health check, THE Orchestrator SHALL emit a structured log event and increment a `worker_failures_total` Prometheus counter with labels for failure_reason and worker_id.

### Requirement 18: Dev Cluster Deployment Configuration

**User Story:** As a developer, I want a deployment configuration for the 4-node K3s dev cluster that exercises the full distributed architecture, so that I can develop and test the decomposed platform locally.

#### Acceptance Criteria

1. THE Dev_Cluster deployment SHALL run the Orchestrator as a 2-replica Deployment on any available node.
2. THE Dev_Cluster deployment SHALL run the Worker_Pool as a Deployment with KEDA scaling (min 0, max 4 replicas, one per gremlin node maximum).
3. THE Dev_Cluster deployment SHALL use the existing CNPG PostgreSQL cluster and Redis deployment already running in the cluster.
4. THE Dev_Cluster deployment SHALL configure Workers with Intel iGPU access via hostPath `/dev/dri` on all gremlin nodes.
5. THE Dev_Cluster deployment SHALL include a KEDA ScaledObject configured with Redis-based triggers for the `hellodj:active_sessions` metric.
6. THE Dev_Cluster deployment SHALL support running the monolith and distributed deployments simultaneously for hybrid-mode testing.

### Requirement 19: Discord Gateway Multi-Bot Pattern

**User Story:** As the platform architect, I want each Worker to connect to Discord with its own bot token (multi-bot pattern), so that multiple Workers can operate independently without gateway conflicts.

#### Acceptance Criteria

1. WHEN a Worker receives an assignment, THE Worker SHALL connect to the Discord gateway using the tenant's bot token from the Credential_Envelope, not a shared application token.
2. THE Orchestrator SHALL ensure that no two Workers are simultaneously connected to the Discord gateway with the same bot token.
3. IF a Worker needs to join a voice channel in a guild that another Worker already serves, THEN THE Orchestrator SHALL route the request to the existing Worker rather than creating a conflicting connection.
4. WHEN a tenant has multiple bot tokens configured (multi-instance pattern), THE Orchestrator SHALL assign different tokens to different Workers, enabling the tenant to be in multiple voice channels simultaneously.
5. THE Worker SHALL handle Discord gateway rate limits (identify, connection) by implementing a backoff strategy: 1 identify per 5 seconds, reconnect after disconnect with jitter (1-5 second random delay).

### Requirement 20: Scale-to-Zero Cold Start Optimization

**User Story:** As a user requesting playback when no workers are running, I want the cold-start latency to be acceptable, so that scale-to-zero does not create a poor user experience.

#### Acceptance Criteria

1. WHEN a playback request arrives while the Worker_Pool is at zero replicas, THE Orchestrator SHALL immediately acknowledge the request and send a "Starting up..." status message to the user's Discord channel.
2. THE Orchestrator SHALL pre-warm a Worker assignment (fetch credentials, prepare Credential_Envelope) while waiting for KEDA to scale up the Worker pod.
3. WHEN the first Worker becomes ready after a scale-from-zero event on the Dev_Cluster, THE Worker SHALL be fully operational (Discord gateway connected, Lavalink ready, playback started) within 30 seconds of pod scheduling.
4. WHEN the first Worker becomes ready after a scale-from-zero event on the Prod_Cluster (including node provisioning by Karpenter), THE Worker SHALL be fully operational within 2 minutes of the initial playback request.
5. THE Orchestrator SHALL maintain a pool of pre-rendered Lavalink configurations in the Session_Cache (keyed by tenant_id) to avoid re-computation on cold start.
6. IF cold-start latency exceeds 30 seconds on the Dev_Cluster or 2 minutes on the Prod_Cluster, THEN THE Orchestrator SHALL log a warning with timing breakdown (scheduling_time, pull_time, startup_time, assignment_time).

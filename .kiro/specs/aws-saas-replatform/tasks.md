# Implementation Plan: AWS SaaS Re-Platform

## Overview

This plan converts the AWS re-platform design into an incremental, coding-only sequence. It builds bottom-up: first the pure decision/derivation logic (which the 15 correctness properties test and which the CDK constructs consume), then the CDK infrastructure foundation, the DynamoDB data layer, the auth model, the per-component application refactor, the hybrid GPU/pre-baked-NixOS-AMI transcode pipeline, the observability stack, and the Beta→Gamma→Prod deployment pipeline with its build-stage gates, finishing with the three-tier cost model and end-to-end wiring.

Two runtimes are used, both dictated by the design (no pseudocode to resolve): **AWS CDK in TypeScript** (`aws-cdk-lib`) for all infrastructure, and **Python 3.11** for all application components and the pure decision-logic modules. Property-based tests use **Hypothesis** (Python), each with ≥100 iterations and tagged `Feature: aws-saas-replatform, Property N`.

The pure decision functions (DNS naming, auth routing, GPU strategy/placement, dependency gate, base-image gate, autoscaling, draining, promotion, migration filter, Hive keys, cost model, Tidal refresh, hybrid GPU controller) are implemented as standalone Python so both the CDK layer and the runtime components import a single source of truth — and so each property test runs with no live AWS calls.

## Tasks

- [x] 1. Set up monorepo project structure and shared pure-logic package
  - [x] 1.1 Create repository skeleton and tooling config
    - Create `platform/` root with `infra/` (CDK TypeScript app: `cdk.json`, `package.json`, `tsconfig.json`, `bin/`, `lib/`) and `components/` (per-component Python packages) directories
    - Add `pyproject.toml` with `ruff` config (PEP 8) and a `max-line-count = 500` check hook; add `hypothesis` and `pytest` as test deps
    - Add a shared Python package `components/hellodj_platform_logic/` for the pure decision functions consumed across components and mirrored by CDK
    - _Requirements: 1.1, 13.1, 13.2, 13.3, 15.1_

  - [x] 1.2 Define shared typed contracts and enums for decision logic
    - Create `hellodj_platform_logic/types.py` with enums/dataclasses: `DeploymentStage` (Beta/Gamma/Prod), `AuthPurpose`, `UserType`, `GpuStrategy`, `CpuArch`, `AutoscaleDecision`, `DrainState`, `CostTier`, `HybridGpuState`
    - _Requirements: 2.2, 8.x, 3.10, 4.1, 16.x, 17.x, 20.1_

- [x] 2. Implement DNS naming and pipeline-promotion decision logic
  - [x] 2.1 Implement DNS environment-name derivation function
    - Write `hellodj_platform_logic/dns_naming.py`: `derive_env_name(stage, region)` → `<stage>.<region>.hellodj.bot` for non-prod, `prod.<region>.hellodj.bot` for prod; expose `apex_alias_target()` returning `hellodj.bot`; assert every name is a subdomain of the zone
    - _Requirements: 12.1, 12.2, 12.3, 12.4, 18.3_

  - [x] 2.2 Write property test for DNS environment-naming invariant
    - **Property 1: DNS environment-naming invariant**
    - Generate arbitrary stages and regions; assert naming shape, subdomain-of-zone, prod apex alias existence, and no cross-region name collisions
    - Tag: `Feature: aws-saas-replatform, Property 1`; ≥100 iterations
    - **Validates: Requirements 12.2, 12.3, 12.4, 18.3**

  - [x] 2.3 Implement pipeline promotion controller
    - Write `hellodj_platform_logic/promotion.py`: `promote(stage_results)` deploys in fixed order Beta→Gamma→Prod, never deploys a stage unless its predecessor succeeded, halts at first failure
    - _Requirements: 11.1, 11.2, 11.3, 11.4_

  - [x] 2.4 Write property test for pipeline promotion ordering and halt-on-failure
    - **Property 9: Pipeline promotion ordering and halt-on-failure**
    - Generate arbitrary per-stage result sequences; assert fixed order, no deploy without predecessor success, halt at first failure
    - Tag: `Feature: aws-saas-replatform, Property 9`; ≥100 iterations
    - **Validates: Requirements 11.2, 11.3, 11.4**

- [x] 3. Implement auth-routing and Tidal-refresh decision logic
  - [x] 3.1 Implement auth-routing-by-purpose function
    - Write `hellodj_platform_logic/auth_routing.py`: `route_auth(purpose, user_type)` → Cognito for admin/registration/recovery, Discord OAuth for day-to-day registered/appointed login, never route Tidal source auth to Cognito
    - _Requirements: 8.1, 8.2, 8.3, 8.4, 8.5, 8.6, 9.5_

  - [x] 3.2 Write property test for auth-routing by purpose
    - **Property 2: Auth-routing by purpose**
    - Generate arbitrary (purpose, user_type); assert routing rules and that Tidal source auth never routes to Cognito
    - Tag: `Feature: aws-saas-replatform, Property 2`; ≥100 iterations
    - **Validates: Requirements 8.2, 8.3, 8.4, 8.5, 8.6, 9.5**

  - [x] 3.3 Implement first-party Tidal token refresh logic
    - Write `hellodj_platform_logic/tidal_refresh.py`: `refresh_tidal(token_state, first_party_client)` returns a non-expired token via the single-app-id first-party path; expose a guard that rejects the legacy two-client-id key-split path
    - _Requirements: 9.1, 9.2, 9.3, 9.4, 9.5_

  - [x] 3.4 Write property test for Tidal token refresh via first-party path
    - **Property 14: Tidal token refresh via first-party path**
    - Generate arbitrary token states; when expired assert refreshed token is non-expired, obtained via first-party single-app-id path, never via legacy key-split
    - Tag: `Feature: aws-saas-replatform, Property 14`; ≥100 iterations
    - **Validates: Requirements 9.4**

- [x] 4. Implement GPU strategy/placement and dependency/base-image gate logic
  - [x] 4.1 Implement GPU acquisition strategy selection
    - Write `hellodj_platform_logic/gpu_strategy.py`: `select_strategy(candidates, latency_budget=5)` returns lowest-cost strategy with latency ≤ budget; never returns an over-budget strategy when a feasible one exists
    - _Requirements: 3.9, 3.10, 3.12, 3.13_

  - [x] 4.2 Write property test for GPU acquisition strategy selection
    - **Property 3: GPU acquisition strategy selection**
    - Generate arbitrary (cost, latency) candidate sets; assert lowest feasible cost chosen and budget never violated when feasible exists
    - Tag: `Feature: aws-saas-replatform, Property 3`; ≥100 iterations
    - **Validates: Requirements 3.9, 3.10, 3.12, 3.13**

  - [x] 4.3 Implement GPU placement decision
    - Add `place_gpu(inter_host_cost, egress_cost)` to `gpu_strategy.py`: select co-located whenever inter-host streaming cost > egress cost
    - _Requirements: 3.4, 3.5, 3.6_

  - [x] 4.4 Write property test for GPU placement decision
    - **Property 4: GPU placement decision**
    - Generate arbitrary cost pairs; assert co-located chosen whenever inter-host > egress
    - Tag: `Feature: aws-saas-replatform, Property 4`; ≥100 iterations
    - **Validates: Requirements 3.6**

  - [x] 4.5 Implement Graviton/x86 dependency-compatibility gate
    - Write `hellodj_platform_logic/dependency_gate.py`: `gate(dep_arm64_map)` → ARM64-only iff all deps ARM64-compatible, else x86-64/substitute; cover wakeword ONNX runtime, STT engine, audio libs, transcode toolchain, JVM audio services, streaming source clients
    - _Requirements: 4.1, 4.2, 4.3, 4.5_

  - [x] 4.6 Write property test for Graviton/x86 dependency-gate decision
    - **Property 5: Graviton/x86 dependency-gate decision**
    - Generate arbitrary dependency-compatibility maps; assert ARM64-only iff all compatible, never ARM64-only when any incompatible
    - Tag: `Feature: aws-saas-replatform, Property 5`; ≥100 iterations
    - **Validates: Requirements 4.1, 4.2, 4.3**

  - [x] 4.7 Implement non-Nix base-image gate
    - Write `hellodj_platform_logic/base_image_gate.py`: `check_base(image_descriptor)` rejects iff base not Nix-produced (e.g. ubuntu/debian), accepts only Nix-produced bases
    - _Requirements: 5.1, 5.2, 5.3, 5.4_

  - [x] 4.8 Write property test for non-Nix base-image gate
    - **Property 6: Non-Nix base-image gate**
    - Generate arbitrary base descriptors; assert reject iff non-Nix, accept iff Nix-produced
    - Tag: `Feature: aws-saas-replatform, Property 6`; ≥100 iterations
    - **Validates: Requirements 5.4**

- [x] 5. Implement autoscaling, draining, and hybrid-GPU state-machine logic
  - [x] 5.1 Implement autoscaling decision function
    - Write `hellodj_platform_logic/autoscale.py`: `decide(cpu, ram, gpu, scale_out_thresholds, scale_in_thresholds)` → scale-out if any signal > its scale-out threshold, scale-in only if all < scale-in thresholds, else hold; guarantee monotonicity (defaults: out at 70%, in at 40%)
    - _Requirements: 3.2, 3.3, 16.1, 16.2, 16.3, 16.4, 16.5_

  - [x] 5.2 Write property test for autoscaling decision
    - **Property 7: Autoscaling decision over CPU, RAM, and GPU pressure**
    - Generate arbitrary utilization triples and thresholds; assert scale-out/scale-in/hold rules and monotonicity (raising a signal never downgrades a scale-out)
    - Tag: `Feature: aws-saas-replatform, Property 7`; ≥100 iterations
    - **Validates: Requirements 3.2, 3.3, 16.2, 16.3, 16.4, 16.5**

  - [x] 5.3 Implement connection-draining state machine
    - Write `hellodj_platform_logic/draining.py`: on drain start accept no new connections, let tasks finishing within drain timeout (default 120s) complete, terminate tasks still running at timeout and record exactly one termination event each
    - _Requirements: 17.1, 17.2, 17.3, 17.4, 17.5_

  - [x] 5.4 Write property test for connection-draining state machine
    - **Property 8: Connection-draining state machine**
    - Generate arbitrary task sets with remaining durations and a drain timeout; assert no new connections, in-window completion, and exactly-one termination event per timed-out task
    - Tag: `Feature: aws-saas-replatform, Property 8`; ≥100 iterations
    - **Validates: Requirements 17.1, 17.2, 17.3, 17.5**

  - [x] 5.5 Implement hybrid GPU spin-up / scale-to-zero controller
    - Write `hellodj_platform_logic/hybrid_gpu.py`: gas/electric state machine (ElectricOnly→EngineStarting→HybridGpu→Coasting) with `spin_up_threshold` > `spin_down_threshold`, sustained-duration windows, hysteresis; CPU path always serving, GPU requested only after sustained above spin-up, GPU preferred only while Ready, scale-to-zero only after sustained below spin-down, no interactive request unserved during spin-up
    - _Requirements: 3.1, 3.2, 3.3, 3.9, 3.10, 3.11, 3.12, 3.13_

  - [x] 5.6 Write property test for hybrid GPU spin-up / scale-to-zero state machine
    - **Property 15: Hybrid GPU spin-up / scale-to-zero state machine**
    - Generate arbitrary demand sequences with thresholds and sustained windows; assert CPU always serves, spin-up/scale-to-zero timing, GPU-preferred-while-Ready, and no unserved interactive request during spin-up
    - Tag: `Feature: aws-saas-replatform, Property 15`; ≥100 iterations
    - **Validates: Requirements 3.2, 3.3, 3.9, 3.10, 3.12, 3.13**

- [x] 6. Checkpoint - Ensure all decision-logic tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 7. Implement data-layer, Hive-partition, migration, and cost-model logic
  - [x] 7.1 Implement DynamoDB data-access layer (single-table + hot tables)
    - Write `hellodj_platform_logic/data_access.py`: single-table `hellodj-core` (PK/SK/entityType/data/version/updatedAt, GSI1), hot tables `hellodj-search-cache` (queryKey/results/ttl) and `hellodj-session` (PK/SK/state/version); DAX-fronted read path with fall-through to DynamoDB; optimistic-lock read-modify-write; typed errors with backoff on throttle; test against DynamoDB Local / moto
    - _Requirements: 7.1, 7.2, 7.3, 7.4, 7.5, 7.6_

  - [x] 7.2 Write property test for data-layer persistence round-trip and idempotence
    - **Property 10: Data-layer persistence round-trip and idempotence**
    - Generate valid session/queue and search-cache records; assert write-then-read equivalence and search-cache write idempotence (against moto/DynamoDB Local)
    - Tag: `Feature: aws-saas-replatform, Property 10`; ≥100 iterations
    - **Validates: Requirements 7.4, 7.5**

  - [x] 7.3 Implement Hive log-partition key derivation
    - Write `hellodj_platform_logic/hive_partition.py`: `to_key(event)` → `.../year=YYYY/month=MM/day=DD/hour=HH/...`; `from_key(key)` recovers exact partition values
    - _Requirements: 10.1_

  - [x] 7.4 Write property test for Hive log-partition key derivation round-trip
    - **Property 11: Hive log-partition key derivation round-trip**
    - Generate arbitrary timestamps/partition fields; assert to_key/from_key round-trip recovers original values
    - Tag: `Feature: aws-saas-replatform, Property 11`; ≥100 iterations
    - **Validates: Requirements 10.1**

  - [x] 7.5 Implement clean-slate migration filter
    - Write `hellodj_platform_logic/migration.py`: `filter_legacy(records)` outputs only the admin bootstrap credential, excludes all playback/session/playlist/configuration records
    - _Requirements: 19.1, 19.2, 19.4_

  - [x] 7.6 Write property test for clean-slate migration filter
    - **Property 12: Clean-slate migration filter**
    - Generate arbitrary legacy datasets mixing all record types; assert only admin bootstrap credential survives
    - Tag: `Feature: aws-saas-replatform, Property 12`; ≥100 iterations
    - **Validates: Requirements 19.1, 19.2, 19.4**

  - [x] 7.7 Implement three-tier cost model
    - Write `hellodj_platform_logic/cost_model.py`: itemize six categories (compute, GPU, Data_Layer, Edge_Cache, Log_Store, Observability) per tier; Recommended-with-Headroom = Recommended + non-negative reserve; encode verified us-east-1 2026-08-24 unit prices and the ~$168/~$578/~$1,313 tier totals; enforce total(Min) ≤ total(Rec) ≤ total(Rec+Headroom)
    - _Requirements: 20.1, 20.2, 20.3, 20.4, 20.5, 20.6_

  - [x] 7.8 Write property test for cost-tier monotonicity and itemization
    - **Property 13: Cost-tier monotonicity and itemization**
    - Generate arbitrary non-negative itemized line items with headroom reserve; assert all six categories itemized per tier and monotonic totals
    - Tag: `Feature: aws-saas-replatform, Property 13`; ≥100 iterations
    - **Validates: Requirements 20.1, 20.2, 20.5, 20.6**

- [x] 8. Checkpoint - Ensure all data/cost logic tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 9. Implement CDK infrastructure foundation (VPC, EKS, Route 53, edge)
  - [x] 9.1 Bootstrap CDK app and shared config constructs
    - Create `infra/bin/hellodj.ts` and `infra/lib/config.ts`; parameterize region/stage; wire the CDK app to import DNS names from the Python `dns_naming` logic (via a generated JSON or a TS port kept in sync) so IaC and runtime agree
    - _Requirements: 1.1, 1.2, 1.4, 18.1, 18.3_

  - [x] 9.2 Implement VPC + multi-AZ networking construct
    - Write `infra/lib/network-stack.ts`: multi-AZ VPC, subnets, ALB + NLB (gateway sockets), security groups
    - _Requirements: 1.1, 2.1, 18.1_

  - [x] 9.3 Implement EKS cluster with Graviton node groups
    - Write `infra/lib/eks-stack.ts`: EKS control plane, Graviton (ARM64) app managed node group (on-demand + spot), and a taint/label-isolated transcode node group; configure cluster autoscaling on CPU/RAM/GPU pressure using thresholds from `autoscale.py` (D1)
    - _Requirements: 2.1, 2.2, 3.7, 3.8, 3.11, 4.1, 16.1, 16.2, 16.3, 16.4, 16.5_

  - [x] 9.4 Write CDK assertion/snapshot tests for network + EKS stacks
    - Assert EKS control plane, Graviton app node group, taint-isolated transcode node group, ALB/NLB present and shaped correctly
    - _Requirements: 2.1, 3.7, 3.8, 3.11_

  - [x] 9.5 Implement Route 53 + ACM + CloudFront edge stack
    - Write `infra/lib/edge-stack.ts`: `hellodj.bot` hosted zone, per-env records from `derive_env_name`, prod apex CNAME alias, ACM certs, CloudFront distribution (web static + HLS segments) as managed edge cache
    - _Requirements: 12.1, 12.2, 12.3, 12.4, 18.2, 18.4_

  - [x] 9.6 Write CDK assertion/snapshot tests for edge stack
    - Assert hosted zone, `<stage>.<region>.hellodj.bot` records, prod apex alias, CloudFront behaviors present
    - _Requirements: 12.2, 12.3, 12.4, 18.2_

- [x] 10. Implement CDK data, auth, and secrets stacks
  - [x] 10.1 Implement DynamoDB + DAX data stack
    - Write `infra/lib/data-stack.ts`: `hellodj-core` single table + GSI1, `hellodj-search-cache` (TTL) and `hellodj-session` hot tables, DAX cluster fronting hot tables; no RDS/PostgreSQL resources
    - _Requirements: 7.1, 7.2, 7.3, 7.4, 7.5, 7.6_

  - [x] 10.2 Implement Cognito + OAuth + Secrets Manager stack
    - Write `infra/lib/auth-stack.ts`: Cognito user pool (admin + registration + recovery), Secrets Manager entries for Discord bot token / Tidal refresh / Spotify / yt-cipher secret; IAM task roles for Bedrock/Transcribe/Polly (no static keys)
    - _Requirements: 8.2, 8.3, 8.5, 8.6, 9.2, 19.1, 19.3_

  - [x] 10.3 Write CDK assertion/snapshot tests for data + auth stacks
    - Assert DynamoDB tables + DAX present, no PostgreSQL/SQLite resource, Cognito user pool present, Secrets Manager + IAM roles present
    - _Requirements: 7.1, 7.2, 7.3, 8.2, 8.6_

- [x] 11. Checkpoint - Ensure IaC tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 12. Refactor app components (part 1): bot, orchestrator, config-renderer, web-ui
  - [x] 12.1 Implement discord-bot-core component
    - Create `components/discord-bot-core/` (Python 3.11 discord.py/wavelink): gateway, cog/command registration, guild policy, watchdogs; reads Discord token from Secrets Manager; delegates playback to orchestrator; each file ≤500 lines, PEP 8
    - _Requirements: 6.1, 6.3, 13.1, 13.2, 13.3, 15.1, 15.3_

  - [x] 12.2 Implement playback-orchestrator component
    - Create `components/playback-orchestrator/`: router, classifier, content filter, user bans, unified queue persistence as single writer to `hellodj-session` via `data_access.py` (DAX hot path)
    - _Requirements: 6.1, 6.4, 7.4, 7.5, 15.1, 15.3_

  - [x] 12.3 Implement config-renderer component
    - Create `components/config-renderer/`: render complete lavalink `application.yml` from Secrets Manager + DynamoDB (replace legacy SQLite renderer); run as init container/Job
    - _Requirements: 6.1, 7.3, 15.1_

  - [x] 12.4 Implement web-ui component (Flask + HTMX + Alpine + Tailwind v4)
    - Create `components/web-ui/`: dark-glassmorphism sidebar UI per modern-web-ui standard, WCAG AA contrast; host Cognito + Discord OAuth flows and Tidal OAuth callback wired to `auth_routing.py`; config via DynamoDB, secrets via Secrets Manager; Tailwind v4 build step
    - _Requirements: 6.5, 8.1, 8.2, 8.3, 8.4, 8.5, 9.2, 14.1, 14.2, 14.3, 14.4_

  - [x] 12.5 Write unit tests + WCAG AA contrast checks for web-ui
    - Snapshot templates; run axe contrast checks for text/UI (note: full WCAG AA needs manual AT review)
    - _Requirements: 14.4_

- [x] 13. Refactor app components (part 2): lavalink, stream sidecars, cipher/token servers
  - [x] 13.1 Implement lavalink component (Nix-built custom JAR)
    - Create `components/lavalink/`: custom fMP4 HLS + SABR + LavaSrc JAR on Nix-built JVM 21 aarch64 image; config injected by config-renderer
    - _Requirements: 5.1, 5.2, 5.3, 6.1, 15.1, 15.3_

  - [x] 13.2 Implement tidal-stream component with first-party OAuth
    - Create `components/tidal-stream/`: direct Tidal streaming; integrate `tidal_refresh.py`; single-app-id first-party OAuth; remove legacy two-client-id key-split; refresh token from Secrets Manager
    - _Requirements: 6.1, 9.1, 9.2, 9.3, 9.4, 9.5, 15.1_

  - [x] 13.3 Implement spotify-stream component
    - Create `components/spotify-stream/` (Rust librespot, Nix-built): direct Spotify streaming; secrets from Secrets Manager
    - _Requirements: 5.1, 6.1, 15.1_

  - [x] 13.4 Implement yt-cipher and potoken-server components (Nix-rebuilt)
    - Create `components/yt-cipher/` and `components/potoken-server/`: rebuild external images with Nix (no Ubuntu/Debian base); wire shared secret from Secrets Manager
    - _Requirements: 5.1, 5.2, 5.3, 6.1, 15.1_

- [x] 14. Refactor app components (part 3): activity-backend and voice-pipeline
  - [x] 14.1 Implement activity-backend component
    - Create `components/activity-backend/`: aiohttp Activity server (video control, whiteboard, visualizer control, lyrics), WebSocket hub over ALB/CloudFront `/activity/`; emits transcode requests to hls-transcode; serves/reads HLS from S3 via CloudFront
    - _Requirements: 6.2, 15.1, 18.2, 18.4_

  - [x] 14.2 Implement voice-pipeline component (Bedrock-backed)
    - Create `components/voice-pipeline/`: local wakeword ONNX only; STT/intent/TTS via Amazon Bedrock (+ Transcribe/Polly) over IAM task role; remove Kokoro/faster-whisper/self-hosted LLM/Speaches; consume opus via bot-core, dispatch actions to orchestrator
    - _Requirements: 4.5, 6.3, 15.1, 18.4_

  - [x] 14.3 Write unit tests for voice-pipeline Bedrock integration (mocked)
    - Mock Bedrock/Transcribe/Polly; assert STT→intent→action→TTS flow and graceful degradation
    - _Requirements: 6.3_

- [x] 15. Checkpoint - Ensure component tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 16. Implement hls-transcode component and pre-baked NixOS GPU AMI pipeline
  - [x] 16.1 Implement hls-transcode component with hybrid CPU/GPU path
    - Create `components/hls-transcode/`: libx264 (Graviton) default path + NVENC path when GPU Ready; visualizer rendering; consume media over loopback/intra-node (co-located per D2); write HLS to S3 (CloudFront origin); wire the transcode scheduler to `hybrid_gpu.py` and publish CPU/GPU pressure metrics to CloudWatch for the Autoscaler
    - _Requirements: 3.1, 3.9, 3.11, 6.2, 15.1, 16.4_

  - [x] 16.2 Build pre-baked minimal NixOS GPU AMI
    - Add `infra/ami/gpu-node.nix` + `nixos-generators` `amazon-image` (aarch64) build: no SSH/getty/users, NVIDIA/NVENC userspace+modules, CloudWatch agent, tmpfs HLS scratch, ~8–16 GiB gp3 root, IAM instance role; produce AMI artifact for the transcode node group
    - _Requirements: 3.11, 5.1, 5.2, 5.3, 10.1, 10.2, 17.1_

  - [x] 16.3 Wire transcode node group to Karpenter scale-to-zero from the baked AMI
    - Update `infra/lib/eks-stack.ts`: Karpenter/node-group provisions `g5g.xlarge` Spot from the baked AMI on spin-up, time-sliced NVIDIA device plugin, scale-to-zero on coast-down, connection draining on Spot reclaim (downshift to CPU)
    - _Requirements: 3.2, 3.3, 3.11, 16.4, 17.1, 17.2, 17.3, 17.4_

  - [x] 16.4 Write CDK assertion tests for transcode node group + GPU device plugin
    - Assert taint-isolated g5g Spot node group, time-slicing device plugin config, scale-to-zero and drain settings present
    - _Requirements: 3.11, 17.1_

- [x] 17. Implement observability and analytics stack (CDK)
  - [x] 17.1 Implement CloudWatch logs/metrics/dashboards/alarms/SNS stack
    - Write `infra/lib/observability-stack.ts`: CloudWatch Logs, metrics, dashboards, alarms → SNS notifications on threshold breach; no Prometheus resources
    - _Requirements: 10.2, 10.3, 10.4, 10.5, 10.9_

  - [x] 17.2 Implement S3 Hive Log_Store + Glue + Athena + QuickSight stack
    - Write `infra/lib/analytics-stack.ts`: S3 log bucket written with Hive partition keys from `hive_partition.py`, Glue crawler + catalog, Athena workgroup/queries, QuickSight dashboards
    - _Requirements: 10.1, 10.6, 10.7, 10.8_

  - [x] 17.3 Write CDK assertion/snapshot tests for observability + analytics stacks
    - Assert CloudWatch dashboards/alarms/SNS, S3 log bucket, Glue crawler, Athena, QuickSight present and Prometheus absent
    - _Requirements: 10.4, 10.5, 10.6, 10.7, 10.8, 10.9_

- [x] 18. Implement deployment pipeline with build-stage gates
  - [x] 18.1 Implement CDK Pipelines Beta→Gamma→Prod construct
    - Write `infra/lib/pipeline-stack.ts`: CDK Pipelines (CodePipeline/CodeBuild) with per-component paths for independent promotion; deploy order Beta→Gamma→Prod driven by `promotion.py`; halt promotion on stage failure
    - _Requirements: 11.1, 11.2, 11.3, 11.4, 15.2_

  - [x] 18.2 Implement build-stage Nix base-image gate
    - Add a build-stage CodeBuild step invoking `base_image_gate.py` to reject any non-Nix (ubuntu/debian) base image and fail the build
    - _Requirements: 5.1, 5.4_

  - [x] 18.3 Implement build-stage PEP 8 / line-count gate
    - Add a build-stage step running `ruff` + the 500-line-max check; fail the build on style/line-count violations
    - _Requirements: 13.2, 13.3, 13.4_

  - [x] 18.4 Wire dependency-compatibility gate into per-component build
    - Add a build step invoking `dependency_gate.py` per component to decide ARM64-only vs x86-64 fallback and document any x86 dependency
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5_

  - [x] 18.5 Write CDK assertion tests for pipeline stages and gates
    - Assert three stages in order, per-component paths, and presence of base-image/PEP8/dependency gate steps
    - _Requirements: 11.1, 11.2, 11.3, 5.4, 13.4_

- [x] 19. Implement admin bootstrap migration and clean-slate initialization
  - [x] 19.1 Implement admin bootstrap migration job
    - Create `components/migration/`: run `migration.filter_legacy` over legacy export, seed only the Admin_Bootstrap_Credential into Cognito, initialize all other data fresh in DynamoDB
    - _Requirements: 19.1, 19.2, 19.3, 19.4_

  - [x] 19.2 Write integration test for first admin login (mocked Cognito)
    - Assert bootstrap credential authenticates first admin login via Cognito and no legacy playback/session/playlist/config data present
    - _Requirements: 19.3_

- [x] 20. Final integration and end-to-end wiring
  - [x] 20.1 Wire all component workloads into the EKS/CDK deployment
    - Compose per-component Nix OCI images, EKS workloads (Deployments/Services/HPA), ALB/CloudFront routing, and DAX/DynamoDB/Secrets/Bedrock IAM wiring into the pipeline so a single `cdk deploy` provisions the platform with no manual console steps
    - _Requirements: 1.2, 1.3, 1.4, 6.1, 6.2, 6.3, 6.4, 6.5, 15.1, 15.2, 18.4_

  - [x] 20.2 Write integration/smoke tests against Beta
    - Assert CDK deploys with no manual step, per-component feature preservation, alarm→SNS on breach, and independent single-component promotion
    - _Requirements: 1.2, 6.1, 6.2, 6.3, 6.4, 6.5, 10.5, 15.2_

- [x] 21. Final checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional test tasks and can be skipped for a faster MVP; core implementation tasks are never optional.
- All 15 correctness properties from the design map to a dedicated Hypothesis property test (Properties 1–15), each ≥100 iterations and tagged `Feature: aws-saas-replatform, Property N`.
- Pure decision logic (tasks 2–7) is implemented once in `hellodj_platform_logic/` and imported by both the CDK layer and runtime components, giving IaC and runtime a single source of truth.
- CDK infrastructure is verified with assertion/snapshot tests; managed-service wiring and UI with integration/smoke and contrast tests, per the design's Testing Strategy.
- Checkpoints (tasks 6, 8, 11, 15, 21) provide incremental validation points.
- Every task is coding-only: no manual AWS console steps, no user acceptance testing, no production deployment actions.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1"] },
    { "id": 1, "tasks": ["1.2"] },
    { "id": 2, "tasks": ["2.1", "2.3", "3.1", "3.3", "4.1", "4.5", "4.7", "5.1", "5.3", "5.5"] },
    { "id": 3, "tasks": ["2.2", "2.4", "3.2", "3.4", "4.2", "4.6", "4.8", "5.2", "5.4", "5.6", "4.3"] },
    { "id": 4, "tasks": ["4.4", "7.1", "7.3", "7.5", "7.7"] },
    { "id": 5, "tasks": ["7.2", "7.4", "7.6", "7.8"] },
    { "id": 6, "tasks": ["9.1", "9.2"] },
    { "id": 7, "tasks": ["9.3", "9.5", "10.1", "10.2"] },
    { "id": 8, "tasks": ["9.4", "9.6", "10.3"] },
    { "id": 9, "tasks": ["12.1", "12.2", "12.3", "12.4", "13.1", "13.2", "13.3", "13.4", "14.1", "14.2"] },
    { "id": 10, "tasks": ["12.5", "14.3", "16.1", "16.2"] },
    { "id": 11, "tasks": ["16.3", "17.1", "17.2"] },
    { "id": 12, "tasks": ["16.4", "17.3", "18.1", "18.2", "18.3", "18.4"] },
    { "id": 13, "tasks": ["18.5", "19.1"] },
    { "id": 14, "tasks": ["19.2", "20.1"] },
    { "id": 15, "tasks": ["20.2"] }
  ]
}
```

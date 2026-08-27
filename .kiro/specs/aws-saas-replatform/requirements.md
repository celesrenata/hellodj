# Requirements Document

## Introduction

This document specifies requirements for the ground-up re-platforming of HelloDJ from an on-premises Kubernetes cluster (NixOS "gremlin" nodes with Intel iGPU QSV transcoding) to AWS. HelloDJ is a voice-activated Discord music bot providing multi-source playback (YouTube, Spotify, Tidal, SoundCloud), a Discord Activity (video streaming, whiteboard, audio visualizer, synced lyrics), a voice command pipeline (wake word → speech-to-text → LLM intent → action → text-to-speech), a unified playback system, and a web configuration and administration UI.

The re-platform provisions the AWS production environment entirely through infrastructure as code, replaces the credential and data layer with AWS-native services, replaces custom authentication with a Cognito plus Discord OAuth model, rebuilds the Tidal source OAuth integration, adds a full observability and analytics stack, introduces a multi-stage deployment pipeline, and refactors the application into modular, independently deployable components. All existing user-facing bot, Activity, and voice features are preserved through the refactor. The on-premises gremlin environment is retained only as a test layer; production runs on AWS.

The architecture is not multi-region at launch but MUST be designed so that a future multi-region deployment can be enabled without a redesign. The only data migrated from the legacy platform is the minimum required to allow the platform owner to log in as the administrator for the first time; all other data starts fresh.

Two architectural decisions are deliberately deferred to the design phase and are captured here as decision requirements that the design MUST resolve with documented justification: (1) the container orchestrator choice between Amazon ECS and Amazon EKS, and (2) the placement of GPU workloads (co-located within the cluster versus a dedicated GPU host).

The CPU architecture is set to AWS Graviton (ARM64) by default to minimize compute cost, with x86-64 retained only as a per-component fallback where a dependency is not available on ARM64. A dependency-compatibility verification is required before any component formally drops x86-64. The platform must also produce a three-tier cost model (Minimum, Recommended, Recommended-with-Headroom) based on live AWS pricing verified during design. A supporting research note capturing the verified architecture-compatibility findings and the pricing baseline gathered during the requirements phase accompanies this document.

## Glossary

- **HelloDJ_Platform**: The complete re-platformed system running on AWS, comprising all bot, Activity, voice, web, data, and infrastructure components.
- **CDK_Application**: The AWS Cloud Development Kit codebase that defines and provisions all AWS infrastructure for the HelloDJ_Platform.
- **Orchestrator**: The AWS container orchestration service (Amazon ECS or Amazon EKS) selected in the design phase to run HelloDJ_Platform containers.
- **GPU_Workload**: Transcode and visualizer processing that requires GPU acceleration.
- **GPU_Node**: A compute host providing GPU resources to GPU_Workloads.
- **GPU_Sharing**: A mechanism that lets multiple concurrent GPU_Workloads share a single physical GPU on a warm GPU_Node (for example NVIDIA time-slicing or MPS), instead of provisioning a dedicated GPU per job.
- **Interactive_Latency_Budget**: The maximum acceptable delay, no greater than 5 seconds, from an interactive playback request to first served media.
- **Transcode_Host**: The compute host running transcode and video GPU_Workloads.
- **Container_Image**: A deployable container image for any HelloDJ_Platform component.
- **Nix_Build_System**: The Nix toolchain used to build all Container_Images.
- **Data_Layer**: The AWS-native persistent data services for the HelloDJ_Platform, based on Amazon DynamoDB.
- **Hot_Path_Store**: The low-latency data store serving search cache and session and queue state, implemented with DynamoDB and DynamoDB Accelerator (DAX) and/or global tables.
- **Cognito_Service**: The Amazon Cognito user pool providing administrator and initial-registration authentication and account recovery.
- **Discord_OAuth_Service**: The Discord OAuth 2.0 integration providing default day-to-day login for registered users and appointed users.
- **Tidal_OAuth_Service**: The HelloDJ-owned Tidal OAuth 2.0 integration used by the Tidal playback source.
- **Platform_Owner**: The individual who owns and administers the HelloDJ_Platform.
- **Registered_User**: A person who has completed registration through Cognito_Service.
- **Appointed_User**: A person granted access by a Registered_User or the Platform_Owner.
- **Observability_Stack**: The combined logging, metrics, dashboards, alarms, and analytics services for the HelloDJ_Platform.
- **Log_Store**: The Amazon S3 location storing logs in Hive-partitioned format.
- **Metrics_Service**: Amazon CloudWatch metrics, dashboards, and alarms for the HelloDJ_Platform.
- **Log_Service**: Amazon CloudWatch Logs for the HelloDJ_Platform.
- **Glue_Crawler**: An AWS Glue crawler that catalogs the Log_Store.
- **Analytics_Query_Service**: Amazon Athena queries and jobs over the cataloged Log_Store.
- **Analytics_Dashboard_Service**: Amazon QuickSight dashboards and visualizations over analytics data.
- **Deployment_Pipeline**: The multi-stage continuous delivery pipeline with Beta, Gamma, and Prod stages.
- **Deployment_Stage**: One of the pipeline stages: Beta, Gamma, or Prod.
- **DNS_Service**: The Amazon Route 53 hosted zone for the domain hellodj.bot.
- **Edge_Cache_Service**: Amazon CloudFront distributions and edge caching for the HelloDJ_Platform.
- **Autoscaler**: The AWS autoscaling mechanism that adjusts HelloDJ_Platform capacity based on measured pressure.
- **Connection_Draining**: The graceful draining of in-flight tasks and connections from a host, container, or GPU_Node before termination.
- **Admin_Bootstrap_Credential**: The minimal credential data migrated from the legacy platform to enable the Platform_Owner's first administrator login.
- **Component**: An independently buildable, deployable, and upgradable unit of the HelloDJ_Platform.
- **Web_UI**: The HelloDJ_Platform web configuration and administration interface.
- **CPU_Architecture**: The processor instruction set architecture (ARM64 or x86-64) on which a Component runs.
- **Graviton_Architecture**: The ARM64 CPU architecture provided by AWS Graviton processors, the default CPU_Architecture for the HelloDJ_Platform.
- **Dependency_Compatibility_Gate**: A verification step that confirms every runtime dependency of a Component is available and functional on Graviton_Architecture before that Component drops x86-64 support.
- **Cost_Model**: A documented estimate of total AWS running cost for the HelloDJ_Platform, expressed in three tiers.
- **Cost_Tier**: One of the three Cost_Model tiers: Minimum, Recommended, or Recommended-with-Headroom.

## Requirements

### Requirement 1: AWS Infrastructure as Code with CDK

**User Story:** As the Platform_Owner, I want all AWS infrastructure provisioned through code, so that the production environment is reproducible, reviewable, and version controlled.

#### Acceptance Criteria

1. THE CDK_Application SHALL define all AWS infrastructure resources for the HelloDJ_Platform.
2. WHEN the CDK_Application is deployed, THE CDK_Application SHALL provision the HelloDJ_Platform infrastructure in AWS without manual console configuration steps.
3. THE HelloDJ_Platform SHALL host all production workloads in AWS.
4. IF a required AWS resource is absent during deployment, THEN THE CDK_Application SHALL create the resource as part of the deployment.
5. WHERE the on-premises gremlin environment is used, THE HelloDJ_Platform SHALL treat the gremlin environment as a test layer only.

### Requirement 2: Container Orchestrator Selection (ECS vs EKS)

**User Story:** As the Platform_Owner, I want the container orchestrator chosen on measurable cost and latency grounds, so that the platform runs efficiently and the decision is justified rather than assumed.

#### Acceptance Criteria

1. THE HelloDJ_Platform SHALL run all containerized workloads on the Orchestrator.
2. THE design phase SHALL select the Orchestrator as either Amazon ECS or Amazon EKS.
3. THE design phase SHALL justify the Orchestrator selection using inter-host and inter-Availability-Zone data transfer cost analysis.
4. THE design phase SHALL justify the Orchestrator selection using GPU and distance latency analysis.
5. THE design phase SHALL document the Orchestrator selection decision with the supporting cost and latency analysis.

### Requirement 3: GPU Workload Sizing and Placement

**User Story:** As the Platform_Owner, I want GPU capacity sized to actual usage and placed cost-effectively, so that I do not overpay for idle GPU capacity or incur unnecessary data transfer costs.

#### Acceptance Criteria

1. THE HelloDJ_Platform SHALL provision GPU capacity sized to measured GPU utilization rather than to peak demand.
2. WHEN GPU pressure on the Transcode_Host increases, THE Autoscaler SHALL add GPU capacity in response to the measured pressure.
3. WHEN GPU pressure on the Transcode_Host decreases, THE Autoscaler SHALL reduce GPU capacity in response to the measured pressure.
4. THE design phase SHALL select GPU_Workload placement as either co-located within the Orchestrator cluster or on a dedicated GPU_Node.
5. THE design phase SHALL justify the GPU_Workload placement decision using AWS data transfer cost analysis that compares inter-host streaming cost against egress cost.
6. IF inter-host streaming of audio or video costs more than egress, THEN THE design phase SHALL specify co-located GPU_Workload placement within the Orchestrator cluster.
7. WHERE a GPU_Node is provisioned, THE HelloDJ_Platform SHALL prefer a Graviton_Architecture GPU instance family so that the GPU_Node shares the CPU_Architecture of the rest of the fleet.
8. THE HelloDJ_Platform SHALL size the GPU_Node to the smallest GPU instance that satisfies measured transcode and visualizer load.
9. WHERE measured GPU load is low enough to be served without dedicated GPU hardware, THE HelloDJ_Platform SHALL support software transcode as a lower-cost fallback.
10. THE design phase SHALL evaluate GPU acquisition strategy across per-job GPU provisioning, a warm shared GPU with GPU_Sharing, and software transcode, and SHALL select the lowest-cost strategy that satisfies the Interactive_Latency_Budget.
11. WHERE a GPU_Node is provisioned, THE HelloDJ_Platform SHALL keep the GPU_Node warm and share it across concurrent GPU_Workloads using GPU_Sharing rather than provisioning a separate GPU per job.
12. IF a GPU acquisition strategy introduces cold-start latency that exceeds the Interactive_Latency_Budget for an interactive playback request, THEN THE design phase SHALL reject that strategy for interactive GPU_Workloads.
13. THE Interactive_Latency_Budget SHALL be no greater than 5 seconds from an interactive playback request to first served media.

### Requirement 4: CPU Architecture (Graviton Default with Dependency Verification)

**User Story:** As the Platform_Owner, I want the platform to run on the cheapest suitable CPU architecture, defaulting to Graviton, so that I minimize compute cost without breaking any dependency.

#### Acceptance Criteria

1. THE HelloDJ_Platform SHALL use Graviton_Architecture as the default CPU_Architecture for every Component.
2. BEFORE a Component drops x86-64 support, THE Dependency_Compatibility_Gate SHALL verify that every runtime dependency of that Component is available and functional on Graviton_Architecture.
3. IF a runtime dependency of a Component is not available or not functional on Graviton_Architecture, THEN THE HelloDJ_Platform SHALL either provide a Graviton_Architecture-compatible substitute or run that specific Component on x86-64.
4. WHERE a Component runs on x86-64 due to a dependency limitation, THE HelloDJ_Platform SHALL document the specific dependency that requires x86-64.
5. THE Dependency_Compatibility_Gate SHALL cover the wake word ONNX runtime, the speech-to-text engine, the audio processing libraries, the media transcode toolchain, the JVM audio services, and the streaming source clients.

### Requirement 5: Nix-Only Container Images

**User Story:** As the Platform_Owner, I want every container image built with Nix and free of Debian or Ubuntu base images, so that the platform stays reproducible and consistent with the Nix-native toolchain.

#### Acceptance Criteria

1. THE Nix_Build_System SHALL build every Container_Image for the HelloDJ_Platform.
2. THE HelloDJ_Platform SHALL exclude Ubuntu base images from every Container_Image.
3. THE HelloDJ_Platform SHALL exclude Debian base images from every Container_Image.
4. IF a Container_Image is defined with a non-Nix base image, THEN THE Deployment_Pipeline SHALL reject the Container_Image during the build stage.

### Requirement 6: Preservation of Existing User-Facing Features

**User Story:** As a Registered_User, I want all existing bot, Activity, and voice features to keep working after the re-platform, so that the migration does not remove capabilities I rely on.

#### Acceptance Criteria

1. THE HelloDJ_Platform SHALL provide multi-source playback for YouTube, Spotify, Tidal, and SoundCloud sources.
2. THE HelloDJ_Platform SHALL provide the Discord Activity features for video streaming, whiteboard, audio visualizer, and synced lyrics.
3. THE HelloDJ_Platform SHALL provide the voice command pipeline covering wake word detection, speech-to-text, LLM intent recognition, action execution, and text-to-speech response.
4. THE HelloDJ_Platform SHALL provide the unified playback system across supported sources.
5. THE HelloDJ_Platform SHALL provide the Web_UI for configuration and administration.

### Requirement 7: DynamoDB Data Layer and Hot-Path Caching

**User Story:** As the Platform_Owner, I want data stored in AWS-native services with hot paths served from low-latency stores, so that the platform is fast, managed, and free of self-hosted databases.

#### Acceptance Criteria

1. THE Data_Layer SHALL use Amazon DynamoDB as the primary persistent data store for the HelloDJ_Platform.
2. THE HelloDJ_Platform SHALL exclude PostgreSQL from the Data_Layer.
3. THE HelloDJ_Platform SHALL exclude SQLite from the Data_Layer.
4. THE Hot_Path_Store SHALL serve the search cache from DynamoDB.
5. THE Hot_Path_Store SHALL serve session and queue state from DynamoDB.
6. WHERE read latency for the search cache or session and queue state must be minimized, THE Hot_Path_Store SHALL use DynamoDB Accelerator (DAX) or DynamoDB global tables.

### Requirement 8: Authentication Model (Cognito plus Discord OAuth)

**User Story:** As the Platform_Owner, I want a managed authentication model using Cognito and Discord OAuth, so that I remove custom authentication code and give users a familiar Discord login.

#### Acceptance Criteria

1. THE HelloDJ_Platform SHALL remove the legacy custom authentication implementation.
2. THE Cognito_Service SHALL authenticate the Platform_Owner as the administrator.
3. WHEN a person registers with the HelloDJ_Platform, THE Cognito_Service SHALL authenticate the registration.
4. WHEN a Registered_User or Appointed_User logs in for day-to-day access, THE Discord_OAuth_Service SHALL authenticate the login by default.
5. WHERE a Registered_User or Appointed_User requires account recovery, THE Cognito_Service SHALL authenticate the recovery.
6. THE HelloDJ_Platform SHALL retain the Cognito_Service for administrator authentication, initial registration, and account recovery.

### Requirement 9: Tidal Source OAuth Redesign

**User Story:** As the Platform_Owner, I want a first-party Tidal OAuth integration with a single correct application ID, so that Tidal playback no longer depends on the fragile Android key-splitting workaround.

#### Acceptance Criteria

1. THE Tidal_OAuth_Service SHALL authenticate Tidal source access using a single Tidal application identifier.
2. THE Tidal_OAuth_Service SHALL use a HelloDJ-owned OAuth callback endpoint.
3. THE HelloDJ_Platform SHALL remove the legacy Tidal authentication approach that splits a key across two client identifiers.
4. WHEN a Tidal access token expires, THE Tidal_OAuth_Service SHALL refresh the token using the HelloDJ-owned OAuth integration.
5. THE Tidal_OAuth_Service SHALL operate independently of the Cognito_Service.

### Requirement 10: Observability and Analytics Stack

**User Story:** As the Platform_Owner, I want comprehensive logging, metrics, dashboards, alarms, and analytics, so that I can monitor, diagnose, and analyze the platform without being present at all times.

#### Acceptance Criteria

1. THE Log_Store SHALL store HelloDJ_Platform logs in Amazon S3 using Hive-partitioned format.
2. THE Log_Service SHALL collect HelloDJ_Platform logs through Amazon CloudWatch Logs.
3. THE Metrics_Service SHALL publish HelloDJ_Platform metrics to Amazon CloudWatch.
4. THE Metrics_Service SHALL provide Amazon CloudWatch dashboards for the HelloDJ_Platform.
5. WHEN a metric crosses a defined alarm threshold, THE Metrics_Service SHALL raise a CloudWatch alarm and send a notification.
6. THE Glue_Crawler SHALL catalog the Log_Store for analytics.
7. THE Analytics_Query_Service SHALL run Amazon Athena queries and jobs over the cataloged Log_Store.
8. THE Analytics_Dashboard_Service SHALL provide Amazon QuickSight dashboards over analytics data.
9. THE HelloDJ_Platform SHALL exclude Prometheus from the Observability_Stack.

### Requirement 11: Multi-Stage Deployment Pipeline

**User Story:** As the Platform_Owner, I want a Beta, Gamma, and Prod deployment pipeline, so that changes are validated in successive stages before reaching production.

#### Acceptance Criteria

1. THE Deployment_Pipeline SHALL provide a Beta stage, a Gamma stage, and a Prod stage.
2. WHEN a change is promoted, THE Deployment_Pipeline SHALL deploy the change to the Beta stage before the Gamma stage.
3. WHEN a change is promoted, THE Deployment_Pipeline SHALL deploy the change to the Gamma stage before the Prod stage.
4. IF a Deployment_Stage deployment fails, THEN THE Deployment_Pipeline SHALL halt promotion to the next Deployment_Stage.

### Requirement 12: Route 53 Environment Naming and Production CNAME

**User Story:** As the Platform_Owner, I want a consistent DNS naming scheme with production aliased to the apex domain, so that each environment is addressable and production is reachable at hellodj.bot.

#### Acceptance Criteria

1. THE DNS_Service SHALL host the DNS zone for the domain hellodj.bot.
2. THE DNS_Service SHALL name each non-production environment as <stage>.<region>.hellodj.bot.
3. THE DNS_Service SHALL create a CNAME from the production environment name to hellodj.bot.
4. WHEN the Prod stage for a region is provisioned, THE DNS_Service SHALL create the DNS record prod.<region>.hellodj.bot.

### Requirement 13: Application Refactor, Modularization, and PEP 8 Compliance

**User Story:** As the Platform_Owner, I want the application refactored into clean modules that follow PEP 8, so that the codebase is maintainable and free of monolithic files.

#### Acceptance Criteria

1. THE HelloDJ_Platform SHALL organize application code into discrete modules with separated concerns.
2. THE HelloDJ_Platform SHALL comply with PEP 8 style rules for all Python source files.
3. THE HelloDJ_Platform SHALL keep each Python source file within a defined maximum line count established in the design phase.
4. IF a Python source file violates PEP 8 rules, THEN THE Deployment_Pipeline SHALL report the violation during the build stage.

### Requirement 14: Professional Web UI

**User Story:** As a Registered_User, I want a professionally designed web interface, so that the platform looks polished and trustworthy rather than generic.

#### Acceptance Criteria

1. THE Web_UI SHALL present a professionally designed interface consistent with the modern-web-ui design standard.
2. THE Web_UI SHALL use Flask, HTMX, Alpine.js, and Tailwind CSS version 4 as its technology stack.
3. THE Web_UI SHALL apply the dark glassmorphism music aesthetic defined in the modern-web-ui design standard.
4. THE Web_UI SHALL meet WCAG AA color contrast criteria for text and user interface elements.

### Requirement 15: Per-Component Isolation and Individual Deployability

**User Story:** As the Platform_Owner, I want each component optimized and independently deployable, so that I can upgrade one part of the platform without redeploying everything.

#### Acceptance Criteria

1. THE HelloDJ_Platform SHALL package each Component as an independently deployable unit.
2. WHEN a single Component is upgraded, THE Deployment_Pipeline SHALL deploy that Component without redeploying the other Components.
3. THE HelloDJ_Platform SHALL allow each Component to be versioned independently of the other Components.

### Requirement 16: Autoscaling with CPU, RAM, and GPU Pressure Awareness

**User Story:** As the Platform_Owner, I want the platform to scale automatically on CPU, RAM, and GPU pressure, so that it runs and scales without my active management and the transcode host is never overwhelmed.

#### Acceptance Criteria

1. THE Autoscaler SHALL scale HelloDJ_Platform capacity without Platform_Owner intervention.
2. WHEN CPU utilization exceeds a defined threshold, THE Autoscaler SHALL add capacity.
3. WHEN memory utilization exceeds a defined threshold, THE Autoscaler SHALL add capacity.
4. WHEN GPU pressure on the Transcode_Host exceeds a defined threshold, THE Autoscaler SHALL add Transcode_Host capacity.
5. WHEN CPU utilization, memory utilization, and GPU pressure fall below their defined scale-in thresholds, THE Autoscaler SHALL reduce capacity.

### Requirement 17: Connection Draining and Graceful Shutdown

**User Story:** As a Registered_User, I want in-progress playback and connections to drain gracefully during deploys and scaling, so that infrastructure updates do not interrupt my experience.

#### Acceptance Criteria

1. WHEN a host, container, or GPU_Node is scheduled for termination, THE HelloDJ_Platform SHALL perform Connection_Draining before termination.
2. WHILE Connection_Draining is in progress, THE HelloDJ_Platform SHALL stop routing new connections to the draining host, container, or GPU_Node.
3. WHILE Connection_Draining is in progress, THE HelloDJ_Platform SHALL allow in-flight tasks on the draining host, container, or GPU_Node to complete within a defined drain timeout.
4. WHEN an infrastructure update is applied, THE HelloDJ_Platform SHALL complete the update without downtime to active playback sessions.
5. IF an in-flight task does not complete within the defined drain timeout, THEN THE HelloDJ_Platform SHALL terminate the task and record a termination event.

### Requirement 18: Cross-Region Performance and Edge Caching

**User Story:** As the Platform_Owner, I want edge caching and a multi-region-ready architecture, so that the platform performs well across regions and can expand to multiple regions later without redesign.

#### Acceptance Criteria

1. THE HelloDJ_Platform SHALL operate in a single AWS region at launch.
2. WHERE content can be cached at the edge, THE Edge_Cache_Service SHALL serve the content through Amazon CloudFront.
3. THE HelloDJ_Platform SHALL structure infrastructure so that additional regions can be added without redesigning the existing architecture.
4. WHERE a managed AWS service can replace a self-hosted capability, THE HelloDJ_Platform SHALL use the managed AWS service.

### Requirement 19: Clean-Slate Migration of Admin Bootstrap Credentials

**User Story:** As the Platform_Owner, I want only my initial admin login migrated and everything else started fresh, so that the AWS platform begins clean without carrying over legacy data.

#### Acceptance Criteria

1. THE HelloDJ_Platform SHALL migrate only the Admin_Bootstrap_Credential from the legacy platform.
2. THE HelloDJ_Platform SHALL initialize all data other than the Admin_Bootstrap_Credential as new data on AWS.
3. WHEN the Platform_Owner logs in for the first time on AWS, THE HelloDJ_Platform SHALL authenticate the Platform_Owner using the Admin_Bootstrap_Credential through the Cognito_Service.
4. THE HelloDJ_Platform SHALL exclude legacy playback, session, playlist, and configuration data from the migration.

### Requirement 20: Three-Tier Cost Model

**User Story:** As the Platform_Owner, I want the total running cost presented in three tiers, so that I can choose a budget with clear tradeoffs and no surprises.

#### Acceptance Criteria

1. THE Cost_Model SHALL present total estimated AWS running cost in three Cost_Tiers: Minimum, Recommended, and Recommended-with-Headroom.
2. THE Cost_Model SHALL itemize the estimated cost of compute, GPU, Data_Layer, Edge_Cache_Service, Log_Store, and the Observability_Stack for each Cost_Tier.
3. THE Cost_Model SHALL base every price on live AWS pricing verified during the design phase rather than assumed values.
4. THE Cost_Model SHALL state the AWS region and pricing date for which the estimates are valid.
5. THE Minimum Cost_Tier SHALL represent the lowest-cost viable configuration that still satisfies the functional requirements.
6. THE Recommended-with-Headroom Cost_Tier SHALL include reserve capacity above the Recommended Cost_Tier for demand spikes.

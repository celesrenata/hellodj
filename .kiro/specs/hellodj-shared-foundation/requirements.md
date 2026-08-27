# Requirements Document

## Introduction

This feature is a CDK topology refactor of the implemented `aws-saas-replatform` platform
(`platform/infra/`). Its single governing principle is:

> **One stage's worth of HARDWARE, three stages' worth of SOFTWARE.**

The AWS foundation — the VPC, the EKS control plane, the CPU node fleet, the DAX cluster, the
Application Load Balancer, and the Network Load Balancer — is provisioned **exactly once** and
**shared** across all three deployment stages. Beta, Staging, and Production are **not** three copies
of the hardware; they are **three namespaced sets of container workloads** deployed onto that one
shared foundation, isolated by **endpoint** (a Kubernetes namespace `hellodj-<stage>` plus a hostname
`<stage>.<region>.hellodj.bot`), never by separate infrastructure.

This spec **extends** the "single host, three stages isolated by endpoint" principle that
`hellodj-nix-native-delivery` Requirement 8 already established for the **GPU** (one shared GPU AMI,
one time-sliced Karpenter GPU NodePool named `transcode-gpu`, namespaces `hellodj-<stage>`, hostnames
`<stage>.<region>.hellodj.bot`, no per-stage GPU instance). That endpoint-isolation model is now
applied to the **entire foundation**: the GPU was already shared; this spec makes the VPC, EKS
control plane, CPU node groups, DAX, and load balancers shared in the same way.

### Reconciliation with the current implementation (verified facts)

The following are the verified current state of `platform/infra/` and are the baseline this spec
changes; they are not re-derived here:

- **Foundation is instantiated once, keyed to a single stage.** In `bin/hellodj.ts` the
  `NetworkStack`, `EksStack`, `DataStack`, `EdgeStack`, `AuthStack`, `ObservabilityStack`, and
  `AnalyticsStack` are each instantiated a single time with an id suffixed by `config.stage`. A plain
  `cdk deploy` therefore stands up **one** stage's foundation.
- **The software layer already supports per-namespace deployment.** `WorkloadsStack`
  (`workloads-stack.ts`) plus `component-workloads.ts` already render the 12 platform components into
  a per-stage namespace `hellodj-<stage>` with a `stageEndpoint()` hostname
  `<stage>.<region>.hellodj.bot` and hostname-based Ingress routing. This is the "software" layer that
  matches `hellodj-nix-native-delivery` Requirement 8 and is preserved unchanged in principle by this
  spec.
- **The pipeline currently deploys placeholders.** `pipeline-stack.ts` adds three `HelloDjStage`
  stages in fixed order `beta → staging → production`, but each stage currently deploys only a
  `HelloDjPlaceholderStack`. This spec requires the pipeline's three stages to deploy the per-stage
  **software** workloads and to never instantiate their own foundation hardware.
- **Current idle cost of one stage's hardware** (AWS on-demand baseline the design records):
  EKS control plane ~$73/mo, two on-demand app nodes ~$110/mo, three NAT gateways ~$99/mo, DAX
  ~$29/mo, ALB + NLB ~$36/mo, GPU scaled to zero ~$0 — roughly **$340-400/mo idle** for one stage's
  foundation.
- **The failure mode this spec prevents.** If the pipeline's three stages each instantiated their own
  foundation stacks, the hardware would triple to roughly **$1000+/mo idle**. The foundation MUST be a
  singleton so three software stages cost approximately **1x** the hardware, not 3x.

### Locked decisions (baked into the acceptance criteria below)

- **Minimal always-on floor:** the shared application on-demand node group floor is **one** small
  Graviton node (change `eks-stack.ts` `appOnDemandNodegroup` `minSize`/`desiredSize` from 2 to 1)
  that carries all three namespaces' idle pods. The Spot, transcode, and GPU NodePools keep scaling to
  zero.
- **One NAT gateway:** change `network-stack.ts` from one NAT gateway per AZ (`natGateways: maxAzs`)
  to a single NAT gateway (`natGateways: 1`), accepting the reduced egress high-availability posture
  for the recorded saving.
- **One shared DAX cluster** (single node, `dax.t3.small`, unchanged), **one ALB**, and **one NLB**,
  all shared across the three stages; host-based routing per stage is already provided by the
  existing `stageEndpoint()` hostnames.
- **Target idle bill:** approximately **$180-220/mo** for all three software stages combined on the
  single shared foundation.

### Supersedes

This spec **supersedes any latent per-stage-foundation instantiation.** No configuration, pipeline
stage, or stack may create a second copy of the VPC, EKS control plane, CPU node fleet, DAX, ALB, or
NLB for an additional stage. The foundation is a singleton.

## Glossary

- **Shared_Foundation**: The single, once-provisioned set of AWS infrastructure resources shared by
  all three stages — the VPC, the EKS control plane and its shared CPU node fleet, the DAX cluster,
  the Application Load Balancer, the Network Load Balancer, and the shared GPU AMI + time-sliced GPU
  NodePool. Provisioned exactly once.
- **Software_Stage**: One of the three deployment stages — Beta, Staging, or Production — realized as
  a namespaced set of container workloads (the 12 platform components) on the Shared_Foundation,
  carrying no dedicated foundation hardware.
- **Stage_Namespace**: The Kubernetes namespace `hellodj-<stage>` that isolates one Software_Stage's
  workloads on the shared EKS cluster.
- **Stage_Endpoint**: The distinct endpoint that identifies one Software_Stage on the
  Shared_Foundation, composed of the Stage_Namespace `hellodj-<stage>` and the hostname
  `<stage>.<region>.hellodj.bot` (the existing `stageEndpoint()` model).
- **Stage_Hostname**: The DNS hostname `<stage>.<region>.hellodj.bot` that routes to exactly one
  Software_Stage's Stage_Namespace.
- **Node_Floor**: The minimum always-on capacity of the shared application on-demand node group — one
  small Graviton node carrying all three Stage_Namespaces' idle pods.
- **CPU_Node_Fleet**: The shared EKS node groups (the on-demand application node group with the
  Node_Floor, the Spot application node group, and the CPU transcode node group) that all three
  Software_Stages schedule pods onto.
- **Foundation_Singleton_Invariant**: The rule that the Shared_Foundation is provisioned exactly once
  and never duplicated per stage.
- **Software_Component**: One of the 12 independently deployable platform components (the catalog in
  `component-workloads.ts`) deployed per Stage_Namespace.
- **Deployment_Pipeline**: The CDK Pipelines pipeline (`pipeline-stack.ts`) that promotes the three
  Software_Stages in fixed order Beta → Staging → Production and halts on failure.
- **Idle_Cost_Model**: The itemized estimate of the Shared_Foundation's monthly cost when no stage is
  under load, bounded to approximately 1x a single stage's hardware.
- **NAT_Egress**: The single NAT gateway providing outbound internet egress for the private subnets of
  the Shared_Foundation.
- **Nix_Native_R8_Model**: The "single host, three stages isolated by endpoint" GPU model established
  by `hellodj-nix-native-delivery` Requirement 8 (one shared GPU AMI, one time-sliced `transcode-gpu`
  NodePool, `hellodj-<stage>` namespaces, `<stage>.<region>.hellodj.bot` hostnames, no per-stage GPU
  instance) that this spec extends to the whole Shared_Foundation.

## Requirements

### Requirement 1: The foundation is provisioned exactly once and shared

**User Story:** As the platform owner, I want the VPC, EKS cluster, CPU node fleet, DAX, ALB, and NLB
provisioned once and shared across all stages, so that I pay for one stage's worth of hardware rather
than three.

#### Acceptance Criteria

1. THE Shared_Foundation SHALL provision the VPC exactly once and share the VPC across the Beta,
   Staging, and Production Software_Stages.
2. THE Shared_Foundation SHALL provision the EKS control plane exactly once and share the EKS control
   plane across the Beta, Staging, and Production Software_Stages.
3. THE Shared_Foundation SHALL provision the CPU_Node_Fleet exactly once and share the CPU_Node_Fleet
   across the Beta, Staging, and Production Software_Stages.
4. THE Shared_Foundation SHALL provision the DAX cluster exactly once and share the DAX cluster across
   the Beta, Staging, and Production Software_Stages.
5. THE Shared_Foundation SHALL provision the Application Load Balancer exactly once and share the
   Application Load Balancer across the Beta, Staging, and Production Software_Stages.
6. THE Shared_Foundation SHALL provision the Network Load Balancer exactly once and share the Network
   Load Balancer across the Beta, Staging, and Production Software_Stages.
7. WHEN the CDK application is synthesized, THE Shared_Foundation SHALL synthesize no more than one
   VPC, no more than one EKS control plane, no more than one CPU_Node_Fleet, no more than one DAX
   cluster, no more than one Application Load Balancer, and no more than one Network Load Balancer
   across the whole application (a synthesis that provisions zero of a given foundation resource is
   permitted), and every provisioned Stage_Namespace SHALL reference those single resources.
8. IF a deployment attempts to provision a second VPC, EKS control plane, CPU_Node_Fleet, DAX cluster,
   Application Load Balancer, or Network Load Balancer for an additional Software_Stage, THEN THE
   Shared_Foundation SHALL fail synthesis with an error identifying the duplicated resource type and
   SHALL produce no deployable application.

### Requirement 2: Stages are namespaced software workloads on the shared cluster

**User Story:** As the platform owner, I want each stage deployed as a distinct namespaced set of
container workloads on the one shared cluster, so that the three stages coexist without duplicating
hardware.

#### Acceptance Criteria

1. THE HelloDJ_Platform SHALL deploy each of the Beta, Staging, and Production Software_Stages as a
   distinct set of the 12 Software_Components on the single shared EKS cluster.
2. IF one Software_Stage fails to deploy while other Software_Stages deploy successfully, THEN THE
   HelloDJ_Platform SHALL continue operating the successfully-deployed Software_Stages rather than
   tearing them down.
3. THE HelloDJ_Platform SHALL isolate each Software_Stage by the Stage_Endpoint composed of the
   Stage_Namespace `hellodj-<stage>` and the Stage_Hostname `<stage>.<region>.hellodj.bot`.
4. THE HelloDJ_Platform SHALL deploy every Software_Stage using the existing WorkloadsStack and
   `stageEndpoint()` model without introducing a separate workload-rendering mechanism per stage.
5. THE HelloDJ_Platform SHALL deploy the Software_Components of every Software_Stage onto the shared
   CPU_Node_Fleet and the shared time-sliced GPU NodePool.
6. WHERE a Software_Stage requires transcode capacity, THE HelloDJ_Platform SHALL schedule that
   stage's transcode workloads onto the single shared time-sliced `transcode-gpu` NodePool consistent
   with the Nix_Native_R8_Model.

### Requirement 3: The pipeline promotes software stages without provisioning per-stage hardware

**User Story:** As the release manager, I want the pipeline to promote the three software stages in
fixed order without ever standing up per-stage hardware, so that promotion never triples the bill.

#### Acceptance Criteria

1. THE Deployment_Pipeline SHALL deploy, for each of the Beta, Staging, and Production stages, that
   stage's Software_Component workloads onto the Shared_Foundation.
2. THE Deployment_Pipeline SHALL promote the Software_Stages in the fixed order Beta → Staging →
   Production.
3. IF a Software_Stage deployment does not succeed, THEN THE Deployment_Pipeline SHALL halt promotion
   to the next Software_Stage, SHALL leave the already-succeeded Software_Stages deployed and
   operating, and SHALL block any new Software_Stage deployment from starting until the failed stage
   is resolved.
4. THE Deployment_Pipeline SHALL deploy no VPC, EKS control plane, CPU_Node_Fleet, DAX cluster,
   Application Load Balancer, or Network Load Balancer as part of any Software_Stage deployment.
5. WHEN the Deployment_Pipeline deploys a Software_Stage, THE Deployment_Pipeline SHALL deploy only
   that stage's namespaced Software_Component workloads and SHALL reference the pre-provisioned
   Shared_Foundation resources.

### Requirement 4: Minimal always-on node floor and single-NAT egress

**User Story:** As the budget owner, I want the always-on capacity trimmed to one node and one NAT
gateway, so that the shared foundation's idle cost is minimized while still carrying all three stages'
idle pods.

#### Acceptance Criteria

1. THE shared application on-demand node group SHALL set both its minimum size and its desired size to
   exactly one node, establishing a single-node Node_Floor of one small Graviton node.
2. THE Node_Floor SHALL schedule and run the idle pods of all three Stage_Namespaces (`hellodj-beta`,
   `hellodj-staging`, `hellodj-production`) on the single always-on node.
3. THE Shared_Foundation SHALL provision exactly one NAT gateway for outbound egress from all private
   subnets of the VPC.
4. WHILE the Node_Floor's single on-demand node has no pods scheduled onto it, THE Shared_Foundation
   SHALL maintain the Node_Floor at its one-node minimum and SHALL NOT scale it below one node.
5. WHEN the Spot application node group has no pod scheduled onto it, THE Shared_Foundation SHALL scale
   that node group to exactly zero nodes.
6. WHEN the CPU transcode node group has no pod scheduled onto it, THE Shared_Foundation SHALL scale
   that node group to exactly zero nodes.
7. WHEN the time-sliced `transcode-gpu` NodePool has no active transcode workload, THE Shared_Foundation
   SHALL scale that NodePool to exactly zero nodes, consistent with the Nix_Native_R8_Model.
8. THE Shared_Foundation SHALL provision exactly one DAX cluster of exactly one node (`dax.t3.small`)
   shared across all three Software_Stages.

### Requirement 5: Endpoint isolation guarantees

**User Story:** As a platform user, I want a request to one stage's hostname to reach only that
stage's workloads, so that the stages do not interfere with each other on the shared foundation.

#### Acceptance Criteria

1. WHEN a request targets one Software_Stage's Stage_Hostname `<stage>.<region>.hellodj.bot`, THE
   HelloDJ_Platform SHALL route that request to the workloads in that stage's corresponding
   Stage_Namespace `hellodj-<stage>` only.
2. THE HelloDJ_Platform SHALL NOT route a request targeting one Software_Stage's Stage_Hostname to any
   other Software_Stage's Stage_Namespace workloads.
3. IF a request targets a hostname that does not match any provisioned Stage_Hostname, THEN THE
   HelloDJ_Platform SHALL NOT route that request to any Stage_Namespace's workloads and SHALL return a
   routing rejection indicating no matching host.
4. THE HelloDJ_Platform SHALL derive each Stage_Hostname as `<stage>.<region>.hellodj.bot` from exactly
   one stage value and exactly one region value, consistent with the existing `stageEndpoint()` and
   `dns_naming` single source of truth.
5. THE HelloDJ_Platform SHALL preserve the endpoint-isolation routing semantics defined by the
   Nix_Native_R8_Model (the `route_endpoint` / Property 9 hostname-to-namespace match).

### Requirement 6: Bounded, itemized idle cost model

**User Story:** As the budget owner, I want the shared foundation's idle cost itemized and bounded to
about one stage's hardware, so that I can confirm three software stages cost roughly 1x, not 3x.

#### Acceptance Criteria

1. THE Idle_Cost_Model SHALL itemize a separate estimated monthly idle cost line, in US dollars, for
   each of the following six resources: the EKS control plane, the Node_Floor, the single NAT gateway,
   the DAX cluster, the Application Load Balancer, and the Network Load Balancer.
2. THE Idle_Cost_Model SHALL state that the shared time-sliced `transcode-gpu` NodePool contributes
   exactly zero US dollars of idle cost while scaled to zero nodes.
3. THE Idle_Cost_Model SHALL state a total idle cost whose value is at most 1.5 times the recorded idle
   cost of a single stage's foundation hardware, rather than approximately three times that cost.
4. THE Idle_Cost_Model SHALL state a target total monthly idle cost within the inclusive range of 180
   to 220 US dollars for all three Software_Stages combined on the Shared_Foundation.
5. THE Idle_Cost_Model SHALL state the AWS region (us-east-1) and an explicit pricing-reference date
   for which the itemized estimates are valid.

### Requirement 7: Reconciliation and regression preservation

**User Story:** As the platform maintainer, I want the existing isolation model, GPU single-host
model, stage naming, and CDK tests to remain intact, so that this refactor changes topology without
breaking established behavior.

#### Acceptance Criteria

1. THE HelloDJ_Platform SHALL preserve the existing per-namespace Software_Component isolation
   provided by the WorkloadsStack and `stageEndpoint()` model.
2. THE HelloDJ_Platform SHALL preserve the single-host GPU model established by the
   Nix_Native_R8_Model (one shared GPU AMI, one time-sliced `transcode-gpu` NodePool, no per-stage GPU
   instance).
3. THE HelloDJ_Platform SHALL preserve the stage naming Beta, Staging, and Production across the CDK
   application and the Deployment_Pipeline.
4. THE HelloDJ_Platform SHALL actively execute the existing CDK test suite as part of this refactor's
   verification and SHALL pass the CDK test suite with zero failing tests.
5. THE HelloDJ_Platform SHALL preserve every existing user-facing capability of the Software_Components
   through this topology refactor.

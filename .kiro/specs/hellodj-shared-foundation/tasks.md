# Implementation Plan: HelloDJ Shared Foundation

## Overview

This plan converts the seven-requirement `hellodj-shared-foundation` design into an incremental,
test-driven sequence. This spec is a **CDK topology refactor** that **amends the already-implemented**
`aws-saas-replatform` platform under `platform/infra/`; it does not build from scratch. Its governing
principle is **one stage's worth of HARDWARE, three stages' worth of SOFTWARE**: the VPC, EKS control
plane, CPU node fleet, DAX, ALB, and NLB become stage-independent singletons shared by three namespaced
`WorkloadsStack` deployments (`hellodj-beta`/`-staging`/`-production`), isolated only by endpoint.

The design gives **exact file-level deltas** on verified current code. This plan implements those deltas
in the design's dependency order:

1. **Pure/unit-testable CDK helper first** — the new `lib/foundation.ts` (`FoundationRefs`,
   `FOUNDATION_SINGLETON_TYPES`, `assertFoundationSingleton`), which the composition and the pipeline
   synth-gate consume.
2. **Foundation stack deltas** — `network-stack.ts` single NAT, `eks-stack.ts` node floor of one +
   stage-independent names + a `minSize < 1` guard, `data-stack.ts` (confirm singleton, unchanged).
3. **Composition** — `bin/hellodj.ts` drops the `-${stage}` suffix on the shared stacks, instantiates
   three `WorkloadsStack`s on the one cluster, and calls `assertFoundationSingleton(app)` before synth.
4. **Pipeline refactor** — `pipeline-stack.ts` removes `HelloDjPlaceholderStack`, each `HelloDjStage`
   deploys a namespaced `WorkloadsStack` referencing the pre-provisioned foundation (threaded via
   `FoundationRefs`), foundation deployed once outside the per-stage `cdk.Stage`.
5. **Endpoint-isolation / ALB-404 wiring** — confirm host-based Ingress per hostname, set the ALB
   default action to a fixed 404, reuse `route_endpoint` / Property 9 (no new property test).
6. **Docs + cost model** — `platform/infra/ARCHITECTURE.md` sync per the *Keep-Architecture-Docs-in-Sync*
   steering, plus the itemized idle-cost model.
7. **Update the failing sibling suites** for the renamed stack ids + node-floor change.
8. **Final checkpoint** — actively run the whole jest suite to zero failures and `cdk synth` clean.

**Language:** TypeScript (`aws-cdk-lib` CDK) for all changes here; the reused endpoint-isolation pure
logic is the existing Python `hellodj_platform_logic.endpoint_routing.route_endpoint`. Fixed by the
design; no language selection is required (the design uses concrete languages, no pseudocode).

**Testing stack:** the existing platform CDK stack — **jest** + `aws-cdk-lib/assertions` `Template` for
the structural template assertions that dominate this refactor. **No new property-based tests are
written**: the one pure, input-varying decision (endpoint isolation) is **already** `hellodj-nix-native-delivery`
**Property 9** (`route_endpoint`), reused unchanged via its existing Hypothesis test
(`test_endpoint_routing_property.py`) and fast-check mirror (`stage-model-properties.test.ts`). The
`Foundation_Singleton_Invariant` (R1.7/R1.8) is a resource-count template assertion, not a PBT concern.

## Tasks

- [x] 1. Add the shared-foundation singleton helper (`lib/foundation.ts`)
  - [x] 1.1 Implement `FoundationRefs`, `FOUNDATION_SINGLETON_TYPES`, and `assertFoundationSingleton`
    - Create `platform/infra/lib/foundation.ts` exporting the `FoundationRefs` interface (`cluster:
      eks.ICluster`, `data: WorkloadsDataRefs`, `secrets: WorkloadsSecretRefs`, `aiTaskRole: iam.IRole`)
      that every `Software_Stage`'s `WorkloadsStack` consumes as shared handles (not copies)
    - Add the `FOUNDATION_SINGLETON_TYPES` map to the CloudFormation resource types that MUST be
      singletons: `AWS::EC2::VPC`, the CDK EKS control-plane resource (`Custom::AWSCDK-EKS-Cluster`),
      `AWS::DAX::Cluster`, `AWS::EC2::NatGateway`, `AWS::EKS::Nodegroup`, and
      `AWS::ElasticLoadBalancingV2::LoadBalancer` (disambiguating ALB `Type: application` from NLB
      `Type: network`)
    - Implement `assertFoundationSingleton(app: cdk.App): void` that synthesizes the app, counts each
      foundation type across ALL synthesized templates, and throws (failing synth, producing no
      deployable app) when any exceeds one, with an error message naming the duplicated resource type;
      a count of zero for any type is permitted
    - _Requirements: 1.7, 1.8_

  - [x] 1.2 Write unit tests for `assertFoundationSingleton` counting and the type map
    - In `platform/infra/test/foundation.test.ts`, assert `FOUNDATION_SINGLETON_TYPES` maps to the exact
      CloudFormation type strings; assert the counting logic passes on synthetic apps with 0 and 1 of a
      type and throws (naming the type) on 2 of a type; assert ALB vs NLB are disambiguated by the
      `Type` property
    - _Requirements: 1.7, 1.8_

- [x] 2. Apply the network-stack single-NAT delta (`network-stack.ts`)
  - [x] 2.1 Change `natGateways: maxAzs` to `natGateways: 1`
    - In `platform/infra/lib/network-stack.ts`, change the VPC construct from `natGateways: maxAzs` to
      `natGateways: 1` for a single NAT gateway across all private subnets; keep `maxAzs` at 3 so subnets
      and the ALB/NLB remain multi-AZ; leave the single shared ALB and single shared NLB unchanged
    - Drop the per-stage `stage` prop usage that only fed stage name tags now that the stack is a
      stage-independent singleton (keep the stack functional with no stage suffix)
    - _Requirements: 4.3, 1.5, 1.6_

  - [x] 2.2 Write/extend the single-NAT and singleton-LB assertions
    - In `platform/infra/test/network-stack.test.ts`, assert exactly **1** `AWS::EC2::NatGateway` and
      exactly one ALB (`Type: application`) and one NLB (`Type: network`); update any existing NAT-count
      assertion that expected 3
    - _Requirements: 4.3, 1.5, 1.6_

- [x] 3. Apply the eks-stack node-floor delta and stage-independent naming (`eks-stack.ts`)
  - [x] 3.1 Set the on-demand app node group floor to one and add a `minSize < 1` guard
    - In `platform/infra/lib/eks-stack.ts`, change `appOnDemandNodegroup` from `minSize: 2, desiredSize:
      2` to `minSize: 1, desiredSize: 1` (keep `maxSize: 10`), establishing the single-node `Node_Floor`;
      keep the instance as the small `m7g.large` (2 vCPU / 8 GiB) and record `m7g.xlarge` as the
      documented single-node fallback in a comment (transcode/visualizer is off the floor)
    - Add a synth-time guard that throws if the on-demand node group `minSize < 1`, preserving the
      always-on floor (R4.4)
    - _Requirements: 4.1, 4.4_

  - [x] 3.2 Make cluster and node-group names stage-independent; keep scale-to-zero groups unchanged
    - In `platform/infra/lib/eks-stack.ts`, make `clusterName` stage-independent (`hellodj` instead of
      `hellodj-${stage}`) and drop the `-${stage}` token from node-group names (`hellodj-app-ondemand`,
      `hellodj-app-spot`, `hellodj-transcode`); leave `appSpotNodegroup` and `transcodeNodegroup` at
      `minSize: 0, desiredSize: 0` and the Karpenter `transcode-gpu` NodePool
      (`consolidationPolicy: WhenEmpty` + `consolidateAfter`) unchanged — the GPU model is preserved
      verbatim
    - _Requirements: 4.5, 4.6, 4.7, 7.2_

  - [x] 3.3 Write the node-floor, guard, and scale-to-zero assertions
    - In `platform/infra/test/eks-stack.test.ts`, assert the `AppOnDemand` node group `ScalingConfig` has
      `MinSize: 1` and `DesiredSize: 1` and `instanceTypes` includes `m7g.large`; assert `app-spot` and
      `transcode` node groups have `MinSize: 0`; assert the `minSize < 1` guard throws; assert the
      cluster name and node-group names carry no stage suffix
    - _Requirements: 4.1, 4.4, 4.5, 4.6_

- [x] 4. Confirm the data-stack DAX singleton (`data-stack.ts`, unchanged)
  - [x] 4.1 Assert one DAX node `dax.t3.small` `replicationFactor: 1` stays singleton
    - In `platform/infra/test/data-stack.test.ts`, assert exactly one `AWS::DAX::Cluster` with
      `NodeType: dax.t3.small` and `ReplicationFactor: 1` (no code change to `data-stack.ts`; the stack
      is instantiated once as a stage-independent singleton by the composition)
    - _Requirements: 4.8, 1.4_

- [x] 5. Rewrite the composition for one foundation + three software stages (`bin/hellodj.ts`)
  - [x] 5.1 Drop the `-${stage}` suffix on the shared foundation stacks
    - In `platform/infra/bin/hellodj.ts`, instantiate `NetworkStack`, `EksStack`, `DataStack`,
      `EdgeStack`, `AuthStack`, `ObservabilityStack`, and `AnalyticsStack` each once with
      stage-independent ids (`hellodj-network`, `hellodj-eks`, `hellodj-data`, `hellodj-edge`,
      `hellodj-auth`, `hellodj-observability`, `hellodj-analytics`); drop the `stage` prop from stacks
      that only used it for resource names/tags
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6_

  - [x] 5.2 Instantiate three `WorkloadsStack`s on the one shared cluster
    - In `platform/infra/bin/hellodj.ts`, replace the single `WorkloadsStack` with one per stage over
      `[Beta, Staging, Production]` (`hellodj-workloads-beta`/`-staging`/`-production`), each passing the
      SAME `eks.cluster`, `data.*`, `auth.*` (secrets + `aiTaskRole`) references and differing only by
      `stage`/`region` (hence namespace + hostname); add stack dependencies on `eks`, `data`, and `auth`
    - _Requirements: 2.1, 2.4, 2.5, 1.7_

  - [x] 5.3 Call `assertFoundationSingleton(app)` before `app.synth()`
    - In `platform/infra/bin/hellodj.ts`, import and invoke `assertFoundationSingleton(app)` immediately
      before `app.synth()` so a duplicated foundation resource fails synth (naming the type) and produces
      no deployable app
    - _Requirements: 1.7, 1.8_

  - [x] 5.4 Write the whole-app foundation-singleton assertions
    - In `platform/infra/test/foundation.test.ts` (or `bin` composition test), synthesize the whole app
      and assert across all templates exactly **one** `AWS::EC2::VPC`, one EKS control-plane resource,
      one each of the app-ondemand/app-spot/transcode node groups (not per-stage), one
      `AWS::DAX::Cluster`, one ALB, and one NLB; assert `assertFoundationSingleton(app)` passes
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7_

  - [x] 5.5 Write the duplicate-fails-synth assertion
    - In `platform/infra/test/foundation.test.ts`, construct an app that instantiates a second
      `NetworkStack` (or `EksStack`/`DataStack`) and assert `assertFoundationSingleton` **throws** with a
      message naming the duplicated resource type and that no deployable app is produced
    - _Requirements: 1.8_

- [x] 6. Checkpoint — helper, foundation deltas, and composition complete
  - Ensure all `foundation`, `network-stack`, `eks-stack`, and `data-stack` jest tests pass, ask the user
    if questions arise.

- [x] 7. Refactor the pipeline to software-only stages (`pipeline-stack.ts`)
  - [x] 7.1 Remove `HelloDjPlaceholderStack`; deploy a namespaced `WorkloadsStack` per stage
    - In `platform/infra/lib/pipeline-stack.ts`, delete `HelloDjPlaceholderStack`; extend
      `HelloDjStageProps` with `foundation: FoundationRefs` and `region: string`; have each
      `HelloDjStage` construct a `hellodj-workloads-<promotionStage>` `WorkloadsStack` referencing
      `props.foundation.cluster/data/secrets/aiTaskRole` — SOFTWARE ONLY, creating no VPC/EKS/DAX/ALB/NLB
    - _Requirements: 3.1, 3.5_

  - [x] 7.2 Deploy the foundation once, outside the per-stage `cdk.Stage`
    - In `platform/infra/lib/pipeline-stack.ts`, keep the `Shared_Foundation` OUT of any `HelloDjStage`;
      implement the SELECTED option — the pipeline manages only the three software stages and references
      the pre-provisioned foundation by its stable stage-independent names/ARNs (the foundation is
      deployed once before the software stages), so no `HelloDjStage` can ever instantiate foundation
      hardware
    - _Requirements: 3.4, 3.5_

  - [x] 7.3 Preserve promotion order, halt-on-failure, and block-new-deploys
    - In `platform/infra/lib/pipeline-stack.ts`, keep the three `HelloDjStage`s added in
      `PROMOTION_ORDER` (`beta → staging → production`) with CDK Pipelines' native sequential deploy and
      halt-on-first-failure; leave earlier succeeded stages running and block a new promotion until the
      failed stage is resolved; also wire `assertFoundationSingleton` as a synth-time gate in the build
      step so a duplicate never reaches deploy
    - _Requirements: 3.2, 3.3, 2.2, 1.8_

  - [x] 7.4 Write the pipeline-shape and zero-foundation-per-stage assertions
    - In `platform/infra/test/pipeline-stack.test.ts`, assert three stages in order
      `beta → staging → production`, each deploying a `WorkloadsStack` (not `HelloDjPlaceholderStack`,
      which is removed), and the foundation added once outside any `HelloDjStage`; synthesize a
      `HelloDjStage` (and each `hellodj-workloads-<stage>` stack) and assert its template has **0** of
      `AWS::EC2::VPC`, the EKS control-plane resource, `AWS::EC2::NatGateway`, `AWS::DAX::Cluster`,
      `AWS::ElasticLoadBalancingV2::LoadBalancer`, and `AWS::EKS::Nodegroup`, while containing the
      namespaced manifests (Namespace `hellodj-<stage>`, 12 Deployments, host-scoped Ingress)
    - _Requirements: 3.1, 3.2, 3.4, 3.5_

- [x] 8. Wire endpoint isolation and the ALB no-match 404 (`workloads-stack.ts`)
  - [x] 8.1 Confirm host-based Ingress per namespace and set the ALB default action to a fixed 404
    - In `platform/infra/lib/workloads-stack.ts`, confirm each instance binds its single Ingress rule to
      `host: this.stageEndpoint.hostname` with backends only in `this.namespace` (no topology change to
      the per-namespace rendering); add/confirm the shared ALB default action is a fixed-response **404
      (no matching host)** so a request whose hostname matches no provisioned `Stage_Hostname` is routed
      to no namespace (via `alb.ingress.kubernetes.io/actions.*` default or the controller default rule)
    - _Requirements: 5.1, 5.2, 5.3_

  - [x] 8.2 Write/extend the endpoint-isolation wiring assertions (reuse `endpoint-isolation.test.ts`)
    - In `platform/infra/test/endpoint-isolation.test.ts`, assert each stage's Ingress rule `host` equals
      `stageEndpoint(stage, region).hostname`, its backends are only that namespace's Services, the GPU
      NodePool name is the shared stage-independent `transcode-gpu`, and each stage's `hls-transcode`
      Deployment carries the transcode toleration/`nodeSelector`; assert the ALB default action is a
      fixed 404 for an unmatched host; confirm reuse of `route_endpoint` / Property 9 (no new property
      test)
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 2.5, 2.6_

  - [x] 8.3 Write the three-namespace / 12-component / per-stage-independence assertions
    - In `platform/infra/test/endpoint-isolation.test.ts` (or a new `software-stages.test.ts`),
      synthesize all three `WorkloadsStack`s and assert three distinct namespaces
      (`hellodj-beta`/`-staging`/`-production`), each with **12** component Deployments referencing the
      same shared cluster, and `COMPONENT_WORKLOADS.length === 12`; assert the three stacks share no
      mutable resource and produce disjoint namespaced resource sets (partial-deploy tolerance)
    - _Requirements: 2.1, 2.2, 2.4_

- [x] 9. Update `ARCHITECTURE.md` and encode the idle cost model
  - [x] 9.1 Sync `platform/infra/ARCHITECTURE.md` to the refactored topology
    - In `platform/infra/ARCHITECTURE.md`, update to reflect: stage-independent foundation stack ids
      (drop `-STAGE` on the shared stacks), the single-NAT VPC, the on-demand node-group floor of 1
      (`m7g.large` default; `m7g.xlarge` fallback), the GPU-default transcode/visualizer path (CPU
      software-render as spin-up/fallback only, sized to bridge GPU spin-up not to match a 185H), the
      three `hellodj-workloads-<stage>` software stacks on one cluster, the pipeline deploying
      software-only stages after a once-deployed foundation, and refresh the `synth` validation line
      (stack count + jest test count) to the post-refactor numbers
    - _Requirements: 6.1, 6.5, 7.2, 7.3_

  - [x] 9.2 Record the itemized idle cost model
    - In `platform/infra/ARCHITECTURE.md` (and/or a cost-model doc), itemize the six USD idle lines — EKS
      control plane, `Node_Floor`, single NAT, DAX, ALB, NLB — state the `transcode-gpu` NodePool at
      **$0** idle while scaled to zero, state the total is ≤ 1.5× a single stage's recorded hardware and
      within the inclusive **$180–220/mo** target, and state region **us-east-1** with pricing-reference
      date **2026-08-24**
    - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5_

  - [x] 9.3 Write the cost-model doc-lint / example assertion
    - In `platform/infra/test/` (e.g. `cost-model.test.ts`), assert the itemized total is within
      $180–220/mo and ≤ 1.5× the recorded single-stage baseline, the six lines are present, the GPU line
      is `$0` idle, and the region (`us-east-1`) + date (`2026-08-24`) are stated
    - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5_

- [x] 10. Update the failing sibling suites for renamed ids and the node-floor change
  - [x] 10.1 Reconcile the existing suites to the stage-independent ids and node floor
    - Update `platform/infra/test/network-stack.test.ts`, `eks-stack.test.ts`, `pipeline-stack.test.ts`,
      and `data-stack.test.ts` for the renamed stage-independent stack ids and the node-floor change
      (any assertion still expecting `-${stage}` suffixes, `minSize: 2`, `natGateways: 3`, or the removed
      `HelloDjPlaceholderStack` must be reconciled), so the whole suite matches the refactored topology
    - _Requirements: 7.1, 7.3, 7.4_

- [x] 11. Final checkpoint — run the whole suite to zero failures and synth clean
  - [x] 11.1 Actively run the jest CDK suite and `cdk synth`, fix to zero failures
    - From `platform/infra`, actively execute `npx jest --ci` and drive it to **zero** failing tests
      (R7.4), and run `npx cdk synth` to confirm a clean synthesis passing `assertFoundationSingleton`;
      confirm the reused endpoint-isolation Property 9 and promotion Property 10 remain green, and that
      the `Nix_Native_R8_Model` GPU single-host model, the `beta/staging/production` stage naming, and
      the 12-component catalog are preserved
    - _Requirements: 7.1, 7.2, 7.3, 7.4, 7.5_
  - [x] 11.2 Final checkpoint
    - Ensure all tests pass and `cdk synth` is clean, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional test sub-tasks and can be skipped for a faster MVP; core
  implementation tasks are never optional. Per the workflow, `*` sub-tasks are NOT implemented by the
  agent unless explicitly requested.
- This is a topology refactor that **amends** existing `platform/infra/` code — most tasks are bounded
  file-level deltas from the design, not greenfield construction.
- Verification is overwhelmingly **CDK template assertion** (jest + `aws-cdk-lib/assertions`). **No new
  property-based tests are written**: the one pure decision (endpoint isolation) reuses the existing
  `route_endpoint` / Property 9; the `Foundation_Singleton_Invariant` is a resource-count assertion.
- Each task references specific requirement clauses and exact file paths for traceability.
- Checkpoints (tasks 6, 11) provide incremental validation points.
- Every task is coding-only: no manual AWS console steps, no deployments, no user acceptance testing.
- Per the *Keep-Architecture-Docs-in-Sync* steering, `ARCHITECTURE.md` is updated in the same change as
  the topology deltas (task 9).

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "2.1", "3.1", "4.1"] },
    { "id": 1, "tasks": ["1.2", "2.2", "3.2", "8.1"] },
    { "id": 2, "tasks": ["3.3", "5.1"] },
    { "id": 3, "tasks": ["5.2", "5.3"] },
    { "id": 4, "tasks": ["5.4", "5.5", "7.1"] },
    { "id": 5, "tasks": ["7.2", "7.3", "8.2", "8.3"] },
    { "id": 6, "tasks": ["7.4", "9.1"] },
    { "id": 7, "tasks": ["9.2", "10.1"] },
    { "id": 8, "tasks": ["9.3", "11.1"] }
  ]
}
```

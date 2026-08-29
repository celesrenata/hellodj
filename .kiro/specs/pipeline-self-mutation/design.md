# Design Document

## Overview

Re-enable CDK Pipelines `selfMutation` so a CDK git change applies immediately
through the pipeline (pipeline definition + foundation stacks like
`hellodj-eks` + workloads), removing the manual `cdk deploy hellodj-eks` /
`cdk deploy hellodj-pipeline` two-step. The blocker is well-documented but not
yet root-caused at the construct level: with `selfMutation: true` the pipeline
redeploys its own stack, which references the stage stacks, whose K8s manifests
apply through the EKS-scoped Kubectl_Handler Lambda across a stack boundary —
and that invocation fails.

This is a **diagnose-then-fix** feature. The design does NOT prescribe the exact
code fix up front (that would be guessing); it prescribes reproducing the
failure, recording the concrete root cause, then applying the smallest
structural change that lets self-mutation redeploy safely, validated on beta and
reversible.

## Current state (facts)

- `hellodj-cdk/infra/lib/pipeline-stack.ts` sets `selfMutation: false` with the
  documented reason: stage stacks apply K8s manifests via the EKS kubectl
  handler Lambda; self-mutation redeploying the pipeline stack triggers
  cross-stack custom-resource failures.
- The `cdk-standalone-package` design already recommends keeping it off until
  the kubectl-handler cross-stack issue is resolved, and defers re-enabling to
  a separate spec (this one).
- Workloads' K8s manifests currently live on the **`hellodj-eks`** foundation
  stack (via `cluster.addManifest`), NOT on the per-stage `WorkloadsStack` the
  pipeline deploys — this was itself a workaround so `cdk deploy hellodj-eks`
  rolls pods. The pipeline's deploy stages therefore already deploy relatively
  little; the cross-stack coupling that breaks self-mutation is the pipeline
  stack → stage stacks → EKS kubectl handler chain.

## Architecture

```
CodeCommit push (hellodj-cdk primary synth source)
        │
        ▼
  CDK Pipeline (hellodj-pipeline)
   ├── [today] selfMutation: FALSE
   │     Source → Build(synth) → ComponentBuilds → beta → staging → prod
   │     • rebuilds component images (ECR)
   │     • deploys per-stage WorkloadsStack ONLY
   │     • does NOT redeploy the pipeline or foundation stacks
   │           ⇒ manual `cdk deploy hellodj-eks` / `hellodj-pipeline`
   │
   └── [target] selfMutation: TRUE
         SelfMutate → Source → Build → ComponentBuilds → beta → staging → prod
         • SelfMutate step redeploys the pipeline stack from git
         • foundation/infra CDK changes apply automatically
         • BLOCKER: pipeline stack → stage stacks → EKS-scoped
           Kubectl_Handler cross-stack custom resource fails
                 ⇒ RESOLVE (R2) before flipping the flag
```

The whole feature is the transition from the top branch to the bottom branch,
gated on removing the cross-stack Kubectl_Handler failure edge.

## Components and Interfaces

- **`pipeline-stack.ts`** — flips `selfMutation` and (per the diagnosis) adjusts
  how manifest-bearing stacks are referenced so the pipeline stack carries no
  failing cross-stack kubectl custom-resource edge. The single point that
  enables auto-apply.
- **`eks-stack.ts` / kubectl provider wiring** — the EKS cluster's kubectl role
  + manifest provider. The fix makes every manifest-applying stack reference a
  stable, stack-independent kubectl role/provider (by name/ARN), not a
  cross-stack `Ref` the self-mutation redeploy re-orders.
- **`bin/hellodj.ts`** — the app composition; unchanged except where stack
  references must be by name/output to break the dependency edge.
- **Synth gate (`getBuildCommands`)** — unchanged; continues to enforce the
  Foundation_Singleton_Invariant on every run.
- **`tools/deploy_workloads.sh`** — retained as the manual roll fallback.

No new runtime components; this is a pipeline/infra wiring change only.

## Data Models

No data models. This feature changes pipeline/CloudFormation stack wiring only —
no DynamoDB items, no secrets, no persistent application state. The only
"state" touched is the CloudFormation stack dependency graph (the cross-stack
custom-resource edge being removed) and the `selfMutation` boolean.

## Investigation phase (must precede any fix)

### Reproduce
Enable `selfMutation: true` on an ISOLATED synth / a throwaway pipeline (or a
`cdk deploy --no-execute` changeset inspection) and capture the exact failure:
which custom resource, which stack boundary, which handler ARN. Do this without
touching the live `hellodj-pipeline` until the fix is known.

### Enumerate cross-stack handler references
Grep the synthesized templates for `Custom::AWSCDK-EKS-KubernetesResource` (and
the kubectl provider Lambda references) and record every stack that references
the EKS-stack-scoped provider. The fix targets exactly those.

### Candidate root-cause shapes (to confirm, not assume)
- The CDK-managed kubectl **provider/handler** is a singleton in the EKS stack;
  any stack applying a manifest gets a cross-stack `Fn::ImportValue` to that
  provider. Under self-mutation the pipeline stack's update re-evaluates those
  imports in an order CloudFormation rejects.
- OR the imported-cluster `kubectlRoleArn` / OIDC wiring resolves differently
  during a self-mutation redeploy of the pipeline stack.

The design's fix is chosen AFTER the reproduction identifies which shape is
real.

## Candidate fixes (decide after diagnosis)

The design commits to the PROPERTY (no failing cross-stack handler invocation
under self-mutation), not a premature mechanism. Options, in preference order:

1. **Stack-independent kubectl provider.** Give the EKS cluster a stable,
   stack-independent kubectl role + provider (already partially true — the
   shared foundation provisions a stable kubectl role) and ensure every
   manifest-applying stack references it by stable name/ARN (import by name, not
   cross-stack `Ref`), so a self-mutation redeploy of the pipeline stack does
   not re-order a cross-stack custom-resource dependency.

2. **Remove manifests from any pipeline-referenced stack.** Since manifests
   already live on `hellodj-eks` (deployed outside the pipeline's stage stacks),
   confirm NO pipeline-deployed stage stack carries a kubectl custom resource;
   if one does, move it to the foundation. Then the pipeline stack references
   only manifest-free stacks and self-mutation has no cross-stack handler to
   fail on.

3. **Isolate the pipeline stack** so it does not reference the manifest-bearing
   stacks at all (reference by name/output only), removing the dependency edge
   self-mutation walks.

Whichever the diagnosis supports, the goal is identical: `selfMutation: true`
redeploys the pipeline without invoking the EKS kubectl handler cross-stack.

## Re-enable + auto-apply

- Flip `selfMutation: true` in `pipeline-stack.ts`.
- Bootstrap ONCE with the final manual `cdk deploy hellodj-pipeline` to install
  the self-mutating pipeline; thereafter pipeline-definition AND foundation
  changes (e.g. the `hellodj-eks` GPU idle window) auto-apply on a push.
- Preserve the fixed Beta → Staging → Production order + halt-on-failure and the
  immutable-tag auto-roll (self-mutation deploys foundation config; component
  images still come from ComponentBuilds with the commit tag).

## Correctness Properties

### Property 1: No cross-stack handler failure under self-mutation
With `selfMutation: true`, a pipeline redeploy completes without any
`Custom::AWSCDK-EKS-KubernetesResource` invocation failing across a stack
boundary (reproduced-then-fixed; verified by a successful self-mutating run).
**Validates: Requirements 2.1, 3.1**

### Property 2: Infra git change auto-applies
A pushed Foundation_Stack change (a benign stack output on beta) is reflected on
the deployed stack after the pipeline run WITHOUT any manual `cdk deploy`.
**Validates: Requirements 3.2, 3.3, 5.1**

### Property 3: Foundation singleton preserved
No self-mutation run introduces a second VPC/EKS/DAX/ALB/NLB; the synth gate's
Foundation_Singleton_Invariant still passes.
**Validates: Requirements 2.3, 5.3**

### Property 4: Image roll unchanged
ComponentBuilds still pushes `:latest` + commit tag, and a self-mutation
foundation deploy does not force pods to `:latest` unexpectedly.
**Validates: Requirements 4.1, 4.2**

### Property 5: Reversible
Reverting to `selfMutation: false` restores the documented Manual_Two_Step with
no stuck pipeline or data loss.
**Validates: Requirements 5.2**

## Error Handling

- If reproduction cannot isolate the failure, HALT and record findings — do not
  flip self-mutation on the live pipeline blind (R1.3).
- If a self-mutation deploy fails mid-run, revert `pipeline-stack.ts` to
  `selfMutation: false`, `cdk deploy hellodj-pipeline`, and fall back to the
  manual two-step (R5.2).
- The `tools/deploy_workloads.sh` wrapper stays as the manual roll fallback.

## Testing Strategy

- CDK unit (`jest`): assert `selfMutation: true` once flipped; assert no
  pipeline-referenced stage stack carries a kubectl custom resource (guards the
  regression that caused the original disablement); existing suites still pass.
- Synth: `cdk synth` clean; Foundation_Singleton_Invariant gate passes.
- Live validation (beta): push a benign `hellodj-eks` output change and confirm
  it auto-applies via the pipeline (Property 2) before trusting it for real
  infra changes.
- Reversibility drill: confirm the revert path (Property 5) in a dry-run.

## Deployment

- Investigation + fix live in `hellodj-cdk` (`pipeline-stack.ts`, EKS/kubectl
  wiring). Deploy the fix with `cd infra && npx cdk deploy hellodj-pipeline`
  (the LAST required manual pipeline deploy) to install the self-mutating
  pipeline, then verify auto-apply on beta.
- Update steering docs to replace the two-step rules with the auto-deploy
  reality.

## Scope note

This feature ONLY re-enables safe end-to-end auto-deploy of CDK changes. It does
NOT change the component build model (Nix OCI images via ComponentBuilds), the
promotion order/gates, or the workloads' runtime behavior. The GPU idle window,
multi-bot runtime, etc. are unaffected except that their FUTURE infra changes
will auto-apply on a push once this lands.

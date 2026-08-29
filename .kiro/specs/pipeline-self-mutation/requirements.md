# Requirements Document

## Introduction

Today the HelloDJ CI/CD pipeline has `selfMutation: false`
(`hellodj-cdk/infra/lib/pipeline-stack.ts`). A normal CDK Pipeline reads CDK
changes from git on each run, re-synthesizes, and **auto-applies** the pipeline
and infrastructure stacks — but self-mutation was deliberately disabled because
the stage stacks apply Kubernetes manifests via the EKS cluster's kubectl
handler Lambda, and self-mutation's attempt to redeploy the pipeline stack (which
references those stage stacks) triggers cross-stack custom-resource invocations
that fail (the handler is scoped to the EKS stack).

The consequence is the "deploy two-step" the operator keeps hitting: a
CodeCommit push rebuilds component **images**, but any change to
foundation/infra stacks (e.g. `hellodj-eks`, where the GPU NodePool + idle
window live) and to the pipeline definition itself does **not** auto-apply — it
must be deployed manually with `cdk deploy hellodj-eks` / `cdk deploy
hellodj-pipeline`. This was explicitly deferred by the `cdk-standalone-package`
design ("Re-enabling `selfMutation` is a separate, riskier investigation … only
after confirming the kubectl-handler cross-stack issue is resolved; out of
scope for the repo split").

This feature is that investigation: resolve the kubectl-handler cross-stack
blocker and re-enable end-to-end auto-deploy so a CDK git change applies
immediately through the pipeline (pipeline definition + foundation infra +
workloads), eliminating the manual `cdk deploy hellodj-eks` /
`cdk deploy hellodj-pipeline` steps — WITHOUT reintroducing the cross-stack
custom-resource failure.

## Glossary

- **Self_Mutation**: The CDK Pipelines feature where the pipeline, on each run,
  synthesizes the CDK app from source and updates its OWN definition (and the
  stacks it deploys) before running the stages — so a `pipeline-stack.ts` or
  infra change in git applies automatically without a manual `cdk deploy`.
- **Kubectl_Handler**: The Lambda (provisioned by the EKS cluster construct)
  that applies Kubernetes manifests (`cluster.addManifest`) as CloudFormation
  custom resources. It is scoped to the EKS (`hellodj-eks`) stack.
- **Foundation_Stacks**: The once-deployed shared stacks (`hellodj-network`,
  `hellodj-eks`, `hellodj-data`, `hellodj-auth`, …) that the pipeline references
  but currently does not deploy on a push.
- **Stage_Stacks**: The per-stage `WorkloadsStack`s the pipeline deploys
  (beta/staging/production), historically the carrier of the cross-stack
  custom-resource references that broke self-mutation.
- **Manual_Two_Step**: The current workflow — a push rebuilds images, then an
  operator runs `cdk deploy hellodj-eks` (foundation/manifest change) and/or
  `cdk deploy hellodj-pipeline` (pipeline-definition change) by hand.
- **Cross_Stack_Custom_Resource_Failure**: The failure mode where
  self-mutation redeploys the pipeline stack, which references stage stacks
  whose K8s manifests invoke the EKS-scoped Kubectl_Handler across a stack
  boundary, and the invocation fails.

## Requirements

### Requirement 1: Diagnose the cross-stack kubectl-handler blocker

**User Story:** As a platform operator, I want the exact reason self-mutation
fails documented and reproduced, so that the fix targets the real root cause
rather than a guess.

#### Acceptance Criteria

1. THE investigation SHALL reproduce (in a non-production synth/deploy dry-run
   or an isolated stack) the Cross_Stack_Custom_Resource_Failure that occurs
   when `selfMutation` is enabled, and capture the concrete error.
2. THE investigation SHALL identify every place a manifest/custom-resource
   crosses a stack boundary to the EKS-scoped Kubectl_Handler under
   self-mutation.
3. THE investigation SHALL record the findings (root cause + affected
   constructs) in the design before any fix is implemented (no guessing).

### Requirement 2: Resolve the kubectl-handler cross-stack coupling

**User Story:** As a platform operator, I want the kubectl-handler wiring
restructured so manifests apply without a failing cross-stack invocation, so
that self-mutation can redeploy the pipeline safely.

#### Acceptance Criteria

1. THE design SHALL restructure the kubectl-handler ↔ manifest ownership so no
   manifest custom resource invokes the Kubectl_Handler across a stack boundary
   that self-mutation cannot satisfy (e.g. a stack-independent kubectl role, or
   colocating manifests with the handler's stack).
2. THE change SHALL preserve the existing per-stage isolation (namespace
   `hellodj-<stage>`, hostname routing) and the immutable-image-tag auto-roll
   behavior.
3. WHEN the restructure is applied, THE full stack set SHALL still synthesize
   (`cdk synth`) and the Foundation_Singleton_Invariant gate SHALL still pass.

### Requirement 3: Re-enable self-mutation end-to-end

**User Story:** As a platform operator, I want a CDK git change to apply
immediately through the pipeline, so that I no longer run
`cdk deploy hellodj-eks` / `cdk deploy hellodj-pipeline` by hand.

#### Acceptance Criteria

1. WHEN the kubectl-handler blocker is resolved (R2), THE pipeline SHALL set
   `selfMutation: true`.
2. WHEN a CDK change is pushed to the primary synth source, THE pipeline SHALL
   re-synthesize and apply the updated pipeline definition automatically (no
   manual `cdk deploy hellodj-pipeline`).
3. WHEN a Foundation_Stack change (e.g. the `hellodj-eks` GPU idle window) is
   pushed, THE pipeline SHALL apply it automatically (no manual
   `cdk deploy hellodj-eks`), so infra git changes take effect on a push.
4. THE pipeline SHALL retain the fixed Beta → Staging → Production promotion
   order and halt-on-failure.

### Requirement 4: No regression in image build / roll behavior

**User Story:** As a platform operator, I want the existing component
image-build and pod-roll behavior unchanged, so that enabling self-mutation
does not break how workloads update.

#### Acceptance Criteria

1. THE ComponentBuilds stage SHALL continue to build + push per-component
   images (`:latest` + immutable commit tag) as it does today.
2. THE immutable commit-hash image tag SHALL continue to drive automatic pod
   rolls; a self-mutation deploy of the foundation SHALL NOT force pods to
   `:latest` unexpectedly.
3. THE `tools/deploy_workloads.sh` wrapper SHALL remain functional as a manual
   fallback (defense in depth), even though it is no longer required for the
   normal flow.

### Requirement 5: Safe rollout and reversibility

**User Story:** As a platform operator, I want the self-mutation cutover to be
reversible and validated, so that a problem does not brick the pipeline's
ability to deploy.

#### Acceptance Criteria

1. THE cutover SHALL be validated on the beta path first, confirming a pushed
   CDK change (a benign, observable one such as a stack output) auto-applies
   before relying on it for real infra changes.
2. IF the self-mutation deploy fails, THEN the change SHALL be revertible to
   `selfMutation: false` + the documented Manual_Two_Step without data loss or
   a stuck pipeline.
3. THE change SHALL NOT alter the Foundation_Singleton_Invariant (no second
   VPC/EKS/DAX/ALB/NLB), which the synth gate continues to enforce.

### Requirement 6: Gates, deployment, and docs

**User Story:** As a platform operator, I want the change to pass the repo gates
and update the workflow docs, so that the new auto-deploy reality is captured.

#### Acceptance Criteria

1. THE change SHALL pass `cd infra && npx tsc --noEmit && npx jest` and any
   Python gates for touched shared logic.
2. THE pipeline-definition change SHALL itself be deployed via the (final)
   manual `cdk deploy hellodj-pipeline` bootstrap ONCE to flip self-mutation on;
   thereafter pipeline-definition changes auto-apply (R3.2).
3. THE steering docs (`session-context.md`, `website-debug-context.md`,
   `hellodj-architecture.md`, `nixos-workflow.md` where relevant) SHALL be
   updated to replace the "deploy two-step / selfMutation OFF" rules with the
   new auto-deploy reality (docs-in-sync rule).

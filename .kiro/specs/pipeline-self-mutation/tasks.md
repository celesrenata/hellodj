# Implementation Plan

## Overview

Diagnose the cross-stack kubectl-handler failure FIRST (no guessing), apply the
smallest structural fix that removes the failing edge, then flip
`selfMutation: true` and validate auto-apply on beta with a reversible cutover.
All work is in `hellodj-cdk`.

## Tasks

- [x] 1. Reproduce + root-cause the self-mutation failure
  - FINDING: the blocker is already resolved by the current architecture.
    Synth with `selfMutation: true` succeeds and injects the `UpdatePipeline`
    stage; the `hellodj-pipeline` stack template contains ZERO
    `Custom::AWSCDK-EKS-KubernetesResource` and no cross-stack kubectl
    reference. The manifests moved to per-stage `WorkloadsStack`s (each imports
    the cluster with its OWN `KubectlV36Layer`), deployed as separate CFN stacks
    via pipeline actions — so the SelfMutate step (which redeploys only the
    pipeline stack) has no EKS-scoped handler to invoke cross-stack.
  - Enable `selfMutation: true` in an ISOLATED synth / throwaway pipeline (or
    inspect a `cdk deploy --no-execute` changeset) — do NOT touch the live
    `hellodj-pipeline`. Capture the exact `Custom::AWSCDK-EKS-KubernetesResource`
    / kubectl-provider failure, the stack boundary, and the handler ARN.
  - Enumerate every stack that references the EKS-scoped kubectl provider by
    grepping the synthesized templates.
  - Record findings (root cause + affected constructs) in the design's
    Investigation section before writing any fix.
  - _Requirements: 1.1, 1.2, 1.3_

- [x] 2. Remove the cross-stack kubectl-handler failure edge
  - Already satisfied by the current architecture (task 1 finding): manifests on
    per-stage WorkloadsStacks with their own kubectl layer; pipeline stack
    template has no kubectl custom resource. Added a regression-guard test
    (`test/pipeline-selfmutation.test.ts`) asserting the UpdatePipeline stage
    carries only a SelfMutate action (no manifest/kubectl action).
  - Apply the diagnosis-selected fix (preference order): (a) reference a stable,
    stack-independent kubectl role/provider by name/ARN from every
    manifest-applying stack; (b) confirm/ensure NO pipeline-deployed stage stack
    carries a kubectl custom resource (move any to the foundation); (c) isolate
    the pipeline stack so it references manifest-bearing stacks by name/output
    only.
  - Preserve per-stage isolation + immutable-tag auto-roll.
  - `cdk synth` clean; Foundation_Singleton_Invariant gate passes.
  - CDK test: assert no pipeline-referenced stage stack carries a kubectl custom
    resource (regression guard for the original disablement).
  - _Requirements: 2.1, 2.2, 2.3_

- [x] 3. Flip selfMutation on + preserve build/roll behavior
  - Set `selfMutation: true` in `pipeline-stack.ts` (comment updated with the
    resolved-blocker rationale). Rewrote `pipeline-selfmutation.test.ts` to
    assert self-mutation ON (UpdatePipeline stage + SelfMutate action +
    `selfMutationEnabled === true`). ComponentBuilds + immutable-tag roll
    unchanged; promotion order/halt-on-failure unchanged. tsc + jest (346) pass.
  - Set `selfMutation: true` in `pipeline-stack.ts`.
  - Confirm ComponentBuilds still pushes `:latest` + commit tag and the
    immutable-tag auto-roll is intact; a foundation self-mutation deploy must
    not force pods to `:latest`.
  - Keep `tools/deploy_workloads.sh` as the manual fallback.
  - CDK tests: `selfMutation: true` asserted; promotion order + halt-on-failure
    unchanged; existing suites pass.
  - _Requirements: 3.1, 3.4, 4.1, 4.2, 4.3_

- [ ] 4. Bootstrap the self-mutating pipeline + validate auto-apply on beta
  - Deploy the fix ONCE with `cd infra && npx cdk deploy hellodj-pipeline` (the
    LAST required manual pipeline deploy) to install the self-mutating pipeline.
  - Validate: push a benign observable `hellodj-eks` change (e.g. a stack
    output) and confirm it auto-applies via the pipeline run with NO manual
    `cdk deploy hellodj-eks` (Property 2).
  - Confirm a `pipeline-stack.ts` change now auto-applies on a push (Property 1
    verified end-to-end by a green self-mutating run).
  - _Requirements: 3.2, 3.3, 5.1, 6.2_

- [ ] 5. Reversibility drill
  - Dry-run / document the revert: `selfMutation: false` +
    `cdk deploy hellodj-pipeline` restores the Manual_Two_Step with no stuck
    pipeline or data loss.
  - _Requirements: 5.2_

- [ ] 6. Gates + docs
  - `cd infra && npx tsc --noEmit && npx jest`; Python gates for any touched
    shared logic; Foundation_Singleton_Invariant gate green.
  - Update steering: `session-context.md`, `website-debug-context.md`,
    `hellodj-architecture.md` (and `nixos-workflow.md` if referenced) — replace
    the "selfMutation OFF / deploy two-step / manual `cdk deploy hellodj-eks`"
    rules with the new auto-deploy reality (a push applies CDK changes).
  - _Requirements: 6.1, 6.3_

## Task Dependency Graph

```
1 (reproduce + root-cause) ──▶ 2 (remove cross-stack edge) ──▶ 3 (flip selfMutation)
                                                                   └──▶ 4 (bootstrap + validate beta)
                                                                            └──▶ 5 (reversibility drill)
6 (gates + docs) ── depends on all
```

- 1 MUST precede 2 (no fix before the failure is reproduced/root-caused).
- 2 → 3 → 4 → 5 are strictly sequential (each depends on the prior being green).
- 6 last.

```json
{
  "waves": [
    { "wave": 1, "tasks": [1] },
    { "wave": 2, "tasks": [2] },
    { "wave": 3, "tasks": [3] },
    { "wave": 4, "tasks": [4] },
    { "wave": 5, "tasks": [5] },
    { "wave": 6, "tasks": [6] }
  ]
}
```

## Notes

- Diagnose before fixing — the original disablement comment tells us the shape
  (stage stacks → EKS kubectl handler cross-stack), but the exact failing
  construct MUST be reproduced, not assumed (no-guessing rule).
- This is the spec the `cdk-standalone-package` design explicitly deferred:
  "Re-enabling `selfMutation` is a separate, riskier investigation … only after
  confirming the kubectl-handler cross-stack issue is resolved."
- The ONE-TIME manual `cdk deploy hellodj-pipeline` in task 4 is unavoidable
  (you can't self-mutate a pipeline that isn't yet self-mutating); every deploy
  after that is automatic.
- All work is in `hellodj-cdk`; infra deploys via `cdk deploy`, not a push,
  until self-mutation is live (after which pushes suffice).

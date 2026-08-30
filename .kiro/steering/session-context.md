# Session Context — CI/CD Pipeline & Deployment (2026-08-27)

inclusion: auto

## DEPLOYMENT RULES — READ THIS FIRST

**DO NOT build or push Docker images locally.** The CI/CD pipeline handles all
image builds and deployments. The correct workflow is:

1. Fix source code in the local repo (CDK-only changes go to the `hellodj-cdk`
   repo; bot/component source changes stay in `hellodj`)
2. Commit and push to CodeCommit
3. The pipeline builds Nix OCI images on ARM64 CodeBuild, pushes to ECR, and deploys to EKS
4. If the pipeline stack itself needs updating: `cd infra && npx cdk deploy hellodj-pipeline --profile hellodj --require-approval never` (run from the `hellodj-cdk` package)

### Self-mutation is ENABLED (updated 2026-08-29, pipeline-self-mutation spec)

`selfMutation: true` in the pipeline. The pipeline has an `UpdatePipeline`
(SelfMutate) stage (Source → Build → **UpdatePipeline** → ComponentBuilds →
beta → staging → production). A CDK git push self-mutates the **pipeline stack
itself** (its own `pipeline-stack.ts` template: install/build commands,
nix.conf, cache config, stages) and then rebuilds images + deploys the per-stage
**WorkloadsStacks**.

**CORRECTION (2026-08-29): self-mutation does NOT deploy the foundation stacks.**
The foundation stacks — `hellodj-eks`, `hellodj-data`, `hellodj-auth`,
`hellodj-network` — are top-level `app` stacks in `bin/hellodj.ts`, deployed
ONCE **outside** the pipeline. They are NOT pipeline stages, and CDK Pipelines
self-mutation only rewrites the pipeline stack's own template — it does not
`cdk deploy hellodj-eks`. So a change to a FOUNDATION stack (e.g. adding the
`hellodj-kubectl` role to `hellodj-eks`, or GPU NodePool / idle window / env /
IAM changes there) requires an explicit
`cd infra && npx cdk deploy hellodj-eks --profile hellodj --require-approval never`.
Only `pipeline-stack.ts` changes and the per-stage WorkloadsStack manifests ride
a plain push. (Real incident: the new `hellodj-kubectl` role sat undeployed
until a manual `cdk deploy hellodj-eks` created it; the WorkloadsStack kubectl
handler failed `sts:AssumeRole` on the not-yet-existent role until then.)

Why it's safe now (the old blocker is gone): the K8s manifests live on the
per-stage `WorkloadsStack`s (each imports the shared cluster with its OWN
`KubectlV36Layer`), deployed as separate CFN stacks via pipeline actions. The
SelfMutate step redeploys ONLY the pipeline stack, whose template carries zero
`Custom::AWSCDK-EKS-KubernetesResource` — so there is no cross-stack kubectl
handler for it to invoke (the failure that originally forced
`selfMutation: false`).

Workflow now (both CDK and component source):

1. Edit source (`hellodj-cdk` for CDK, `hellodj` for components).
2. Commit + push to CodeCommit.
3. The pipeline self-mutates + rebuilds images + deploys. Done — no manual
   `cdk deploy`.

Fallback (only if the self-mutating pipeline is itself broken): the one-time
manual `cd infra && npx cdk deploy hellodj-pipeline` reinstalls the pipeline,
and reverting to `selfMutation: false` restores the old manual two-step.

**DO NOT use `docker build`, `docker push`, `docker buildx`, or any manual
image push to ECR.** The pipeline is the only path to production images.

**DO NOT attempt to `kubectl set image` or `kubectl patch` deployments as a
permanent fix.** Those are diagnostic steps only. Permanent fixes go through
source → commit → push → pipeline.

## Pipeline Architecture

| Stage | What Happens |
|-------|-------------|
| Source | CodeCommit push (any of 6 repos) triggers pipeline. Primary synth source is `hellodj-cdk`; `hellodj` is an additional input for the component builds |
| Synth | CDK synth produces CloudFormation templates (synths from `hellodj-cdk`) |
| ComponentBuilds | 12 parallel Nix builds on ARM64 CodeBuild → push `:latest` + `:$COMMIT` to ECR |
| Beta Deploy | Workloads stack applied to `hellodj-beta` namespace |
| Staging Deploy | Workloads stack applied to `hellodj-staging` namespace |
| Production Deploy | Workloads stack applied to `hellodj-production` namespace |

### Image Tagging Strategy

- Pipeline pushes TWO tags per component: `latest` and `$CODEBUILD_RESOLVED_SOURCE_VERSION` (commit hash)
- Workloads use `:latest` with `imagePullPolicy: Always` (rolling deploy on each pipeline run)
- Commit-hash tags provide auditability and rollback targets

### Key Commands

```bash
# Deploy pipeline stack changes (self-mutation disabled) — from the hellodj-cdk package
cd infra && npx cdk deploy hellodj-pipeline --profile hellodj --require-approval never

# Deploy foundation (EKS + networking + data) changes — from the hellodj-cdk package
cd infra && npx cdk deploy hellodj-eks hellodj-network hellodj-data --profile hellodj --require-approval never

# Run CDK tests — from the hellodj-cdk package
cd infra && npx jest

# Run synth only (validate) — from the hellodj-cdk package
cd infra && npx cdk synth hellodj-pipeline --quiet

# Push to CodeCommit (triggers pipeline)
# MUST prefix AWS_PROFILE=hellodj — the repo's git credential.helper is the
# bare `aws codecommit credential-helper $@` with NO profile, so a plain
# `git push codecommit main` fails with a misleading
# "fatal: repository '.../v1/repos/hellodj/' not found" (it's an AUTH failure,
# not a missing repo). The env var makes the helper authenticate as `hellodj`.
AWS_PROFILE=hellodj git push codecommit main

# Check pipeline execution status
AWS_PROFILE=hellodj aws codepipeline get-pipeline-state --name hellodj-pipeline --region us-east-1
```

### Debugging Deployed Workloads (diagnostic only, not permanent fixes)

```bash
# Get kubeconfig
AWS_PROFILE=hellodj aws eks update-kubeconfig --name hellodj --region us-east-1 --kubeconfig /tmp/hellodj-eks-kubeconfig

# Check pod status
KUBECONFIG=/tmp/hellodj-eks-kubeconfig kubectl get pods -n hellodj-beta

# Check pod logs
KUBECONFIG=/tmp/hellodj-eks-kubeconfig kubectl logs <pod> -n hellodj-beta

# Describe pod (scheduling issues, image pull errors)
KUBECONFIG=/tmp/hellodj-eks-kubeconfig kubectl describe pod <pod> -n hellodj-beta
```

## Current State (2026-08-27)

### What's Fixed This Session (source changes, not yet pushed)

1. **`PLACEHOLDER_IMAGE_TAG` → `latest`** — workloads-stack.ts now defaults to `:latest` instead of `TODO-pipeline-injected-tag`
2. **`imagePullPolicy: Always`** — ensures fresh pulls on each deploy
3. **Karpenter IRSA** — eks-stack.ts now creates a ServiceAccount with IAM role for Karpenter (was crashing with IMDS 401)
4. **Pipeline image tagging** — `getComponentBuildCommands` now greps for `hellodj-<component>` instead of blindly taking `head -1` from docker images (was pushing wrong images to wrong repos)
5. **Web-UI Nix flake** — includes `hellodj_platform_logic` package (was missing, caused ModuleNotFoundError)

### What Still Needs Fixing

1. **Karpenter interrupt queue** — The SQS queue `hellodj-beta-karpenter-interruption` doesn't exist yet (needs a CDK resource or manual creation)
2. **Web-UI `hellodj_platform_logic` in Nix** — The flake copies the source but doesn't install it as a proper Python package; may need `PYTHONPATH` env in the image config
3. **Component Nix flakes** — Many components still lack `flake.nix` (pipeline build step will fail for those)
4. **Tidal Token Refresh** — `status=401`, needs credential refresh
5. **Self-mutation** — NOW ENABLED (2026-08-29). The kubectl-handler
   cross-stack blocker no longer applies (manifests on per-stage WorkloadsStacks
   with their own kubectl layer); `selfMutation: true`, pipeline has an
   `UpdatePipeline` stage, CDK git pushes auto-apply. See the "Self-mutation is
   ENABLED" section above.

### Pipeline Facts

- **Pipeline name**: `hellodj-pipeline`
- **AWS profile**: `hellodj` (account `874927898283`, region `us-east-1`)
- **EKS cluster name**: `hellodj`
- **Source of truth**: CodeCommit (6 repos, incl. the new `hellodj-cdk` for the CDK app/gates/shared logic; primary synth source is `hellodj-cdk`), NOT GitHub
- **Namespaces**: `hellodj-beta`, `hellodj-staging`, `hellodj-production`
- **ECR registry**: `874927898283.dkr.ecr.us-east-1.amazonaws.com/hellodj/<component>`
- **Node architecture**: ARM64 (Graviton `m7g.large` / `c7g.large`)
- **Tests**: 226 CDK tests passing
- **Ruff**: 0.16.4, target `py314`
- **CodeBuild image**: `aws/codebuild/amazonlinux-aarch64-standard:3.0` (ARM64 native, privileged for docker)

### On-Prem (separate from AWS — still active)

- **Cluster**: gremlin nodes (10.1.1.12–15), namespace `hellodj-service`
- **Bot image**: `registry.celestium.life/hellodj/bot:shader-presets-2026-08-24`
- **Lavalink image**: `registry.celestium.life/hellodj/lavalink:audio-pipe-2026-08-23`
- **Registry**: `registry.celestium.life` (Harbor)

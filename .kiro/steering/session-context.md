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

### CRITICAL: Self-mutation is DISABLED

Because `selfMutation: false` in the pipeline, **the CodeBuild buildspecs are
frozen at `cdk deploy` time.** Changes to `pipeline-stack.ts` (install commands,
component build commands, nix.conf setup, cache config, etc.) DO NOT take effect
by pushing to CodeCommit alone.

The pipeline-stack.ts edits now live in `hellodj-cdk/infra/lib/pipeline-stack.ts`.
The workflow for ANY change to `pipeline-stack.ts`:

1. Edit `infra/lib/pipeline-stack.ts` (in the `hellodj-cdk` repo)
2. Commit + push to CodeCommit (so source matches)
3. **`cd infra && npx cdk deploy hellodj-pipeline`** (from the `hellodj-cdk` package) ← updates the CodeBuild buildspecs
4. **`aws codepipeline start-pipeline-execution --name hellodj-pipeline`** ← runs a fresh execution with the new buildspecs

If you skip step 3, the pipeline keeps running the OLD buildspec even though
CodeCommit has your new code. This is the #1 gotcha — the source in CodeCommit
and the buildspec baked into CodeBuild are TWO SEPARATE THINGS when self-mutation
is off.

Changes to component source (bot code, web-ui app.py, flake.nix, etc.) DO take
effect on a plain push — only `pipeline-stack.ts` changes need the `cdk deploy`.

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
git push codecommit main

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
5. **Self-mutation** — Still disabled; kubectl handler cross-stack Lambda issue

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

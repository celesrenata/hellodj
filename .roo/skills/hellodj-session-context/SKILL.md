---
name: hellodj-session-context
description: Authoritative deployment rules and AWS CI/CD pipeline state for HelloDJ. Use when handling the AWS EKS/SaaS pipeline (CodeCommit → Nix CodeBuild → ECR → EKS), deploying to hellodj-beta/staging/production, or reasoning about self-mutation-disabled pipeline behavior, image tagging, and current pipeline fix state.
---

# HelloDJ Session Context — CI/CD Pipeline & Deployment

## When to use

- Working with the AWS SaaS pipeline: CodeCommit → Nix CodeBuild (ARM64) → ECR → EKS (`hellodj-beta`/`staging`/`production`)
- Reasoning about the self-mutation-disabled pipeline and its frozen buildspecs
- Checking current pipeline fix state, image tags, or the on-prem Harbor deployment
- Any change that must go through source → commit → push → pipeline (NOT local docker build)

## When NOT to use

- On-prem Harbor/kustomize image build and deploy (use `hellodj-deployment`)
- Understanding service topology or playback internals (use `hellodj-architecture`)
- Debugging web-ui pages or design (use `hellodj-website-debug` / `hellodj-modern-web-ui`)

## CRITICAL deployment rules — READ FIRST

1. **DO NOT build or push Docker images locally.** The CI/CD pipeline builds all images (Nix OCI on ARM64 CodeBuild) and pushes to ECR. Fix source → commit → push to CodeCommit → pipeline rebuilds.
2. **Self-mutation is DISABLED.** The CodeBuild buildspecs are frozen at `cdk deploy` time. Changes to `pipeline-stack.ts` (install commands, component build commands, nix.conf setup, cache config) DO NOT take effect by pushing to CodeCommit alone. The source in CodeCommit and the buildspec baked into CodeBuild are TWO SEPARATE THINGS.
3. **Infra manifest/IAM changes** (workloads-stack.ts, eks-stack.ts, auth-stack.ts, edge-stack.ts, foundation.ts, bin/hellodj.ts) deploy via `cd platform/infra && npx cdk deploy <stack>` — NOT by pushing to CodeCommit. The workloads Kubernetes manifests live in the `hellodj-eks` stack (attached via `eks.cluster.addManifest`), so `cdk deploy hellodj-eks` applies web-ui env + IRSA changes.
4. **Component source changes** (bot code, web-ui `*.py`, templates, flake.nix) DO take effect on a plain CodeCommit push (pipeline rebuilds the image).
5. **`:latest` + imagePullPolicy: Always** — but a running pod won't re-pull a new `:latest` until it restarts. After the pipeline pushes a new image: `KUBECONFIG=/tmp/hellodj-eks-kubeconfig kubectl rollout restart deploy/<svc> -n hellodj-beta`.
6. **Pipeline backlog**: rapid pushes queue multiple executions on OLD revisions. Stop stale ones and start fresh on HEAD.

### Workflow for ANY change to `pipeline-stack.ts`

1. Edit `pipeline-stack.ts`
2. Commit + push to CodeCommit (so source matches)
3. `cd platform/infra && npx cdk deploy hellodj-pipeline` ← updates the CodeBuild buildspecs
4. `aws codepipeline start-pipeline-execution --name hellodj-pipeline` ← runs a fresh execution with the new buildspecs

If you skip step 3, the pipeline keeps running the OLD buildspec. This is the #1 gotcha.

**DO NOT** use `docker build`/`docker push`/`docker buildx`/manual ECR push to production. **DO NOT** use `kubectl set image`/`kubectl patch` as a permanent fix — those are diagnostic only.

## Pipeline architecture

| Stage | What Happens |
|-------|-------------|
| Source | CodeCommit push (any of 5 repos) triggers pipeline |
| Synth | CDK synth produces CloudFormation templates |
| ComponentBuilds | 12 parallel Nix builds on ARM64 CodeBuild → push `:latest` + `:$COMMIT` to ECR |
| Beta Deploy | Workloads stack applied to `hellodj-beta` |
| Staging Deploy | Workloads stack applied to `hellodj-staging` |
| Production Deploy | Workloads stack applied to `hellodj-production` |

Image tagging: pipeline pushes TWO tags per component — `latest` and `$CODEBUILD_RESOLVED_SOURCE_VERSION` (commit hash). Workloads use `:latest` with `imagePullPolicy: Always`; commit-hash tags provide auditability/rollback targets.

## Key commands

```bash
# Deploy pipeline stack changes (self-mutation disabled)
cd platform/infra && npx cdk deploy hellodj-pipeline --profile hellodj --require-approval never

# Deploy foundation (EKS + networking + data) changes
cd platform/infra && npx cdk deploy hellodj-eks hellodj-network hellodj-data --profile hellodj --require-approval never

# CDK tests / synth only (validate)
cd platform/infra && npx jest
cd platform/infra && npx cdk synth hellodj-pipeline --quiet

# Push to CodeCommit (triggers pipeline)
git push codecommit main

# Check pipeline execution status
AWS_PROFILE=hellodj aws codepipeline get-pipeline-state --name hellodj-pipeline --region us-east-1
```

## Debugging deployed workloads (diagnostic only)

```bash
AWS_PROFILE=hellodj aws eks update-kubeconfig --name hellodj --region us-east-1 --kubeconfig /tmp/hellodj-eks-kubeconfig
KUBECONFIG=/tmp/hellodj-eks-kubeconfig kubectl get pods -n hellodj-beta
KUBECONFIG=/tmp/hellodj-eks-kubeconfig kubectl logs <pod> -n hellodj-beta
KUBECONFIG=/tmp/hellodj-eks-kubeconfig kubectl describe pod <pod> -n hellodj-beta
```

## Pipeline facts

- Pipeline `hellodj-pipeline`; AWS profile `hellodj` (account `874927898283`, region `us-east-1`); EKS cluster `hellodj`
- Source of truth: CodeCommit (5 repos), NOT GitHub
- Namespaces: `hellodj-beta`, `hellodj-staging`, `hellodj-production`
- ECR: `874927898283.dkr.ecr.us-east-1.amazonaws.com/hellodj/<component>`
- Node architecture: ARM64 (Graviton `m7g.large` / `c7g.large`)
- Tests: 226 CDK tests passing; Ruff 0.16.4 target `py314`
- CodeBuild image: `aws/codebuild/amazonlinux-aarch64-standard:3.0` (ARM64 native, privileged for docker)

## Current state (2026-08-27)

### Fixed this session (source changes, not yet pushed)

1. `PLACEHOLDER_IMAGE_TAG` → `latest` (workloads-stack.ts no longer uses `TODO-pipeline-injected-tag`)
2. `imagePullPolicy: Always` (fresh pulls on each deploy)
3. Karpenter IRSA — eks-stack.ts creates a ServiceAccount with IAM role (was crashing with IMDS 401)
4. Pipeline image tagging — `getComponentBuildCommands` now greps for `hellodj-<component>` instead of `head -1` (was pushing wrong images to wrong repos)
5. Web-UI Nix flake — includes `hellodj_platform_logic` package (was missing, caused ModuleNotFoundError)

### Still needs fixing

1. Karpenter interrupt queue — SQS `hellodj-beta-karpenter-interruption` doesn't exist yet
2. Web-UI `hellodj_platform_logic` in Nix — flake copies source but doesn't install as proper Python package; may need `PYTHONPATH` env
3. Component Nix flakes — many components still lack `flake.nix` (pipeline build will fail for those)
4. Tidal token refresh — `status=401`, needs credential refresh
5. Self-mutation — still disabled; kubectl handler cross-stack Lambda issue

### On-prem (separate from AWS — still active)

- Cluster: gremlin nodes (10.1.1.12–15), namespace `hellodj-service`
- Bot image `registry.celestium.life/hellodj/bot:shader-presets-2026-08-24`; Lavalink `audio-pipe-2026-08-23`; registry Harbor

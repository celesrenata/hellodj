/**
 * Deployment pipeline stack for the HelloDJ AWS platform (Beta -> Staging -> Production).
 *
 * Implements the multi-stage continuous-delivery pipeline the design mandates
 * (design "Deployment Pipeline (Beta -> Staging -> Production)") as a CDK Pipelines
 * (CodePipeline/CodeBuild) construct:
 *
 *   * **Fixed promotion order Beta -> Staging -> Production.** The three
 *     {@link HelloDjStage} stages are added to the pipeline in the exact order
 *     produced by the Python `hellodj_platform_logic.promotion` module's
 *     `PROMOTION_ORDER` (Beta, Staging, Production). CDK Pipelines stages/waves are
 *     inherently *sequential*: CodePipeline runs each stage's actions to
 *     completion before starting the next, and a failed action stops the
 *     execution. That sequential-with-halt behavior is exactly the realization
 *     of `promotion.promote()` ordering + halt-on-failure, so the pipeline and
 *     the pure decision logic share one source of truth for the sequence
 *     (Requirements 11.1, 11.2, 11.3, 11.4).
 *
 *   * **Halt promotion on stage failure.** Because the stages are added
 *     sequentially, a deploy failure in Beta stops promotion to Staging, and a
 *     failure in Staging stops promotion to Production — the pipeline never deploys a
 *     stage whose predecessor did not succeed (Requirement 11.4, mirroring
 *     `promotion.promote()`).
 *
 *   * **Per-component paths for independent promotion.** Each platform
 *     Component (bot-core, orchestrator, lavalink, web-ui, ...) gets its own
 *     CodeBuild build step keyed by component name, so a single component can
 *     be rebuilt/promoted without redeploying the others (Requirement 15.2).
 *     The per-component steps are exposed via {@link componentBuildSteps} and
 *     feed the synth step's `additionalInputs`.
 *
 *   * **Build-stage gate hook points for tasks 18.2-18.4.** The synth step's
 *     command list is assembled from {@link getBuildCommands}, and the
 *     per-component steps expose {@link getComponentBuildCommands}. Both leave
 *     clearly-marked, append-only extension points where the later gate tasks
 *     wire their checks:
 *       - task 18.2: Nix base-image gate (`base_image_gate.py`)
 *       - task 18.3: PEP 8 / 500-line gate (`ruff` + line-count check)
 *       - task 18.4: per-component dependency-compatibility gate
 *         (`dependency_gate.py`)
 *     The gate tasks push their commands onto {@link GATE_HOOK_MARKER}-tagged
 *     arrays rather than rewriting the pipeline wiring.
 *
 * The concrete AWS resources each {@link HelloDjStage} deploys are wired in by
 * the end-to-end task (20.1); here each stage instantiates a minimal
 * synthesizable placeholder stack so the pipeline compiles, synthesizes, and
 * models the correct number of ordered stages for the assertion tests (task
 * 18.5).
 *
 *   * **Software-only stages on the pre-provisioned Shared_Foundation.** Each
 *     {@link HelloDjStage} deploys only its namespaced {@link WorkloadsStack}
 *     referencing the once-deployed foundation; a failed stage halts promotion
 *     but leaves earlier succeeded stages running and blocks a new promotion
 *     until it is resolved (R3.2, R3.3, R2.2). The synth step enforces the
 *     `Foundation_Singleton_Invariant` gate (R1.8) via `cdk synth` before any
 *     deploy stage runs (see {@link getBuildCommands}).
 *
 * _Requirements: 11.1, 11.2, 11.3, 11.4, 15.2, 3.2, 3.3, 2.2, 1.8_
 */
import * as cdk from 'aws-cdk-lib';
import * as codecommit from 'aws-cdk-lib/aws-codecommit';
import * as codebuild from 'aws-cdk-lib/aws-codebuild';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as efs from 'aws-cdk-lib/aws-efs';
import * as eks from 'aws-cdk-lib/aws-eks';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as secretsmanager from 'aws-cdk-lib/aws-secretsmanager';
import {
  CodePipeline,
  CodePipelineSource,
  CodeBuildStep,
} from 'aws-cdk-lib/pipelines';
import { Construct } from 'constructs';
import { KubectlV36Layer } from '@aws-cdk/lambda-layer-kubectl-v36';
import { FoundationRefs } from './foundation';
import { WorkloadsStack } from './workloads-stack';
import {
  CORE_TABLE_NAME,
  SEARCH_CACHE_TABLE_NAME,
  SESSION_TABLE_NAME,
} from './data-stack';

// ---------------------------------------------------------------------------
// Promotion order — single source of truth mirror
// ---------------------------------------------------------------------------
//
// Mirrors `PROMOTION_ORDER` in the Python `hellodj_platform_logic.promotion`
// module, which itself derives from `DeploymentStage.order` (Beta=0, Staging=1,
// Production=2). The pipeline adds one CDK Pipelines stage per entry in this exact
// order, so the sequential CodePipeline execution *is* the realization of
// `promotion.promote()` ordering (R11.1-R11.3) and its halt-on-failure edge
// (R11.4). Keeping the order here as a named constant (rather than an ad-hoc
// literal) makes the shared-source-of-truth relationship explicit and lets the
// assertion test (task 18.5) check the stage count/order against it.

/** The fixed Beta -> Staging -> Production promotion order (mirror of promotion.PROMOTION_ORDER). */
export const PROMOTION_ORDER = ['beta', 'staging', 'production'] as const;

/** A single deployment stage name in the fixed promotion order. */
export type PromotionStageName = (typeof PROMOTION_ORDER)[number];

// ---------------------------------------------------------------------------
// Per-component build paths (Requirement 15.2 — independent promotion)
// ---------------------------------------------------------------------------
//
// Each entry is one independently deployable Component from the design's
// "Component Decomposition" table. A dedicated per-component CodeBuild step
// keyed by this name gives each Component its own build/deploy path, so a
// single Component can be promoted without rebuilding or redeploying the
// others (R15.2, R15.3). Later tasks (20.1) attach the actual per-component
// deploy actions; the per-component build steps created here establish the
// isolated path and the dependency-gate hook point (task 18.4).

/** The independently deployable platform Components (design Component Decomposition). */
export const PLATFORM_COMPONENTS = [
  'discord-bot-core',
  'playback-orchestrator',
  'lavalink',
  'tidal-stream',
  'spotify-stream',
  'yt-cipher',
  'potoken-server',
  'activity-backend',
  'hls-transcode',
  'voice-pipeline',
  'web-ui',
  'config-renderer',
] as const;

/** A single independently deployable Component name. */
export type PlatformComponent = (typeof PLATFORM_COMPONENTS)[number];

// ---------------------------------------------------------------------------
// Build-stage gate hook points (tasks 18.2, 18.3, 18.4)
// ---------------------------------------------------------------------------
//
// GATE HOOK POINT (18.2 / 18.3 / 18.4): the build-stage gate tasks append
// their check commands to the arrays returned by `getBuildCommands()` (synth
// step, whole-repo gates) and `getComponentBuildCommands()` (per-component
// gates). The marker below is searched for by those tasks so the extension
// points are unambiguous; do not remove it.

/** Marker string tagging every build-stage gate extension point (tasks 18.2-18.4). */
export const GATE_HOOK_MARKER = 'HELLODJ_BUILD_GATE_HOOK';

// ---------------------------------------------------------------------------
// Nix + tooling install commands for CodeBuild (runs before the synth/gate cmds)
// ---------------------------------------------------------------------------
//
// The synth step runs on the default CDK Pipelines CodeBuild image which lacks
// Nix. The platform requires Nix for closure resolution (R7.2/7.3/7.7) and
// ruff for style gating (R13.2-13.4). These install commands configure:
//   * Nix (multi-user, flakes enabled) wired to the S3 binary cache as a
//     substituter so `nix path-info --store` can verify closures (R7.7).
//   * ruff for PEP 8 / style gate (R13.2).
//   * AWS git credential helper for CodeCommit source resolution (R2.2).

/** S3-backed Nix binary cache URI (matches closures.toml [cache].uri). */
export const NIX_CACHE_S3_URI = 's3://hellodj-nix-cache';

/** Public key for the Nix cache signing key (narinfo verification). */
export const NIX_CACHE_PUBLIC_KEY =
  'hellodj-nix-cache:OZtAgL5UxJUnnl//7W/On1SReSVdGkcFHKQFJUk1IDo=';

/**
 * Install commands that provision Nix + Python tooling in the CodeBuild env.
 * These run once before any build/gate command and configure:
 *   - Nix with flakes + the S3 binary cache as a substituter
 *   - ruff (PEP 8 linter)
 *   - AWS git credential helper for CodeCommit
 */
export function getInstallCommands(): string[] {
  return [
    // AL2023 ARM64 ships Node 18 by default; CDK needs >= 20.
    // Node 22 is available as a namespaced package in AL2023.
    'dnf install -y nodejs22 && alternatives --set node /usr/bin/node22 || ln -sf /usr/bin/node22 /usr/local/bin/node',
    // Install Nix with persistent EFS-backed /nix.
    // EFS is mounted at /mnt/nix-store by CodeBuild. We bind-mount it over
    // the entire /nix so the installer's temp-install-dir and /nix/store are
    // on the same filesystem (rename() requires same-fs). The installer writes
    // everything to EFS; next run it's already there.
    'mkdir -p /nix && if [ -d /mnt/nix-store ]; then mount --bind /mnt/nix-store /nix; fi',
    // Clean corrupted EFS state: receipt exists but no actual Nix binary.
    'if [ -f /nix/receipt.json ] && [ ! -x /nix/var/nix/profiles/default/bin/nix ]; then echo "Corrupted EFS state, wiping"; rm -rf /nix/*; fi',
    // If Nix was already installed (EFS has previous run's data), start daemon.
    // Otherwise fresh install.
    'if [ -x /nix/var/nix/profiles/default/bin/nix ]; then echo "Nix on EFS"; /nix/var/nix/profiles/default/bin/nix daemon & sleep 2; else curl --proto "=https" --tlsv1.2 -sSf -L https://install.determinate.systems/nix | sh -s -- install --no-confirm; fi',
    // Source nix-daemon env for the rest of the build.
    '. /nix/var/nix/profiles/default/etc/profile.d/nix-daemon.sh',
    // Configure Nix daemon: wire the S3 binary cache as a substituter + trusted.
    // `trusted-substituters` means Nix accepts unsigned narinfos from this cache.
    `echo 'extra-substituters = ${NIX_CACHE_S3_URI}' >> /etc/nix/nix.conf`,
    `echo 'extra-trusted-substituters = ${NIX_CACHE_S3_URI}' >> /etc/nix/nix.conf`,
    `echo 'extra-trusted-users = root' >> /etc/nix/nix.conf`,
    `echo 'trusted-public-keys = cache.nixos.org-1:6NCHdD59X431o0gWypbMrAURkbJ16ZPMQFGspcDShjY= ${NIX_CACHE_PUBLIC_KEY}' >> /etc/nix/nix.conf`,
    'systemctl restart nix-daemon 2>/dev/null || true',
    // Decrypt the Nix cache signing key from sops (KMS-backed).
    'curl -fsSL -o /usr/local/bin/sops https://github.com/getsops/sops/releases/download/v3.9.4/sops-v3.9.4.linux.arm64 && chmod +x /usr/local/bin/sops',
    `sops --decrypt --input-type binary --output-type binary $CODEBUILD_SRC_DIR/platform/secrets/nix-cache-key.sec.enc > /tmp/nix-cache-key.sec`,
    // AWS git credential helper for CodeCommit (R2.2/R2.3).
    'git config --global credential.helper "!aws codecommit credential-helper $@"',
    'git config --global credential.UseHttpPath true',
    // Install ruff for the PEP 8 / style gate (R13.2).
    'pip install ruff==0.16.4',
  ];
}

/**
 * Assemble the synth/build-stage command list for the whole-repo build.
 *
 * **Working directory:** The CodePipeline sources the repo root from CodeCommit.
 * CDK commands run from `platform/infra/` (where `cdk.json` + `package.json`
 * live). Python gate commands run from `platform/` (where `tools/` lives and
 * the scripts self-resolve `PLATFORM_ROOT`). Every command is prefixed with the
 * correct `cd` so paths resolve regardless of CodeBuild's CWD.
 *
 * **No-build (metadata-only synth/gate) step (R6.3, R6.4, R10.1, R10.2).** The
 * selected `Build_Trigger` is **GitHub Actions with Nix**, which compiles every
 * OCI image and the GPU AMI and publishes them to the S3-backed
 * `Nix_Binary_Cache` / ECR. CDK Pipelines is **retained for orchestration and
 * deploy only**: this synth step runs `cdk synth`, the compliance gates, and a
 * **resolve + verify** of the prebuilt GPU AMI/closures — it never compiles an
 * image or the AMI on CodeBuild, so **no CodeBuild compute is billed for
 * building** (R6.3, R6.4). Because the synth step is the pipeline's build stage
 * and CDK Pipelines runs the build stage to completion before any deploy stage,
 * this build/synth (and its gates) **precedes** every stage deploy (R10.1).
 *
 * **Synth-time Foundation_Singleton_Invariant gate (R1.8).** The `npx cdk
 * synth` command below is itself a gate: it runs `bin/hellodj.ts`, which calls
 * `assertFoundationSingleton(app)` before `app.synth()`. If a second
 * VPC/EKS/DAX/NAT/node-group/ALB/NLB is ever introduced, synth throws and this
 * build step fails with an error naming the duplicated type, so a duplicate
 * foundation resource can never reach a deploy stage (R1.8). The assertion
 * itself is defined once in `lib/foundation.ts`; the pipeline enforces it by
 * running synth, not by re-implementing the check.
 *
 * The returned array is also the extension point for the repo-wide build-stage
 * gates. It starts with the CDK synth commands and then a clearly-marked,
 * append-only hook section where later tasks push their gate commands:
 *
 *   * task 18.2 (Nix base-image gate): invoke `base_image_gate.py` to reject
 *     any non-Nix (ubuntu/debian) base image and fail the build (R5.4, R5.7).
 *   * task 18.3 (PEP 8 / line-count gate): run `ruff` + the 500-line-max check
 *     and fail the build on style/line-count violations (R13.2-R13.4).
 *
 * Those tasks should append to the array (or splice after the
 * {@link GATE_HOOK_MARKER} echo) rather than editing the pipeline wiring, so
 * the synth step and its ordering stay stable.
 *
 * @param extraCommands additional commands appended after the gate hook (used
 *   by the gate tasks and tests to inject their checks).
 */
export function getBuildCommands(extraCommands: string[] = []): string[] {
  return [
    // Source Nix env (installed by installCommands).
    '. /nix/var/nix/profiles/default/etc/profile.d/nix-daemon.sh',
    // CDK synth: run from platform/infra/ where cdk.json + package.json live.
    // Use $CODEBUILD_SRC_DIR for absolute pathing since each command in the
    // CodeBuild script may not preserve CWD from prior `cd` commands.
    'cd $CODEBUILD_SRC_DIR/platform/infra && npm ci',
    'cd $CODEBUILD_SRC_DIR/platform/infra && npx cdk synth',
    // Metadata-only: RESOLVE + VERIFY the prebuilt GPU AMI.
    'cd $CODEBUILD_SRC_DIR/platform && python3 tools/resolve_closure.py --ami --verify',
    `echo "${GATE_HOOK_MARKER}: repo-wide base-image (18.2) + PEP8/line-count (18.3) gates run here"`,
    'cd $CODEBUILD_SRC_DIR/platform && python3 tools/gate_base_image.py',
    'cd $CODEBUILD_SRC_DIR/platform && python3 tools/gate_style.py',
    'cd $CODEBUILD_SRC_DIR/platform && python3 tools/gate_pins.py',
    ...extraCommands,
  ];
}

/**
 * Assemble the per-component build command list for one Component.
 *
 * **Working directory:** Commands run from the repo root (CodeBuild CWD after
 * checkout). Python tools are under `platform/tools/`, so every invocation is
 * prefixed with `cd platform &&`.
 *
 * **No-build (resolve/verify-closure) step (R6.3, R6.4, R10.1, R10.2).** Under
 * the `hellodj-nix-native-delivery` reconciliation the selected `Build_Trigger`
 * is **GitHub Actions with Nix** — it, and only it, compiles the OCI images and
 * the GPU AMI, then publishes the resulting closures to the S3-backed
 * `Nix_Binary_Cache` and the images to ECR. CDK Pipelines is **retained for
 * orchestration/deploy only**, so this per-component step does **metadata-only**
 * work: it **resolves** the component's prebuilt closure by its Nix store-path
 * hash and **verifies** the closure/image is retrievable from the cache/ECR
 * before the artifact is marked available for stage deploy. It never compiles an
 * image or the AMI, so **no CodeBuild compute is billed for building** (R6.3,
 * R6.4). Missing-closure handling (halt + surface the store path, never
 * substitute a non-cache artifact) mirrors the pure `resolve_closure` decision
 * function (R7.4).
 *
 * The returned array remains the extension point for the per-component
 * dependency-compatibility gate:
 *
 *   * task 18.4: invoke `dependency_gate.py` for `component` to decide
 *     ARM64-only vs x86-64 fallback and document any x86 dependency
 *     (R4.1-R4.5).
 *
 * @param component the Component this build path belongs to.
 * @param extraCommands additional commands appended after the gate hook.
 */
export function getComponentBuildCommands(
  component: PlatformComponent,
  extraCommands: string[] = [],
): string[] {
  return [
    // Source Nix env.
    '. /nix/var/nix/profiles/default/etc/profile.d/nix-daemon.sh || true',
    // Run the dependency gate (ARM64 compatibility check).
    `cd $CODEBUILD_SRC_DIR/platform && python3 tools/gate_dependencies.py --component ${component}`,
    // Build the Nix OCI image for aarch64-linux natively (CodeBuild is ARM64).
    `cd $CODEBUILD_SRC_DIR/platform/components/${component} && nix build .#packages.aarch64-linux.image --no-link --print-out-paths > /tmp/${component}-image-path.txt || echo "SKIP: nix build failed for ${component}"`,
    // Load + tag + push to ECR, then push closure to S3 Nix cache.
    `set +e; if [ -s /tmp/${component}-image-path.txt ]; then ` +
      `IMAGE_PATH=$(head -1 /tmp/${component}-image-path.txt); ` +
      `if [ -f "$IMAGE_PATH" ]; then ` +
        `ACCOUNT=$(aws sts get-caller-identity --query Account --output text); ` +
        `REPO="$ACCOUNT.dkr.ecr.$AWS_REGION.amazonaws.com/hellodj/${component}"; ` +
        `aws ecr get-login-password --region $AWS_REGION | docker login --username AWS --password-stdin "$ACCOUNT.dkr.ecr.$AWS_REGION.amazonaws.com"; ` +
        `docker load < "$IMAGE_PATH"; ` +
        `BUILT_TAG=$(docker images --format "{{.Repository}}:{{.Tag}}" | grep -v "^$" | head -1); ` +
        `docker tag "$BUILT_TAG" "$REPO:$CODEBUILD_RESOLVED_SOURCE_VERSION"; ` +
        `docker tag "$BUILT_TAG" "$REPO:latest"; ` +
        `docker push "$REPO:$CODEBUILD_RESOLVED_SOURCE_VERSION"; ` +
        `docker push "$REPO:latest"; ` +
        `echo "pushed $REPO:$CODEBUILD_RESOLVED_SOURCE_VERSION"; ` +
        `nix copy --to '${NIX_CACHE_S3_URI}' --secret-key-files /tmp/nix-cache-key.sec "$IMAGE_PATH" 2>/dev/null || true; ` +
        `nix-collect-garbage --delete-older-than 7d 2>/dev/null || true; ` +
      `else echo "image path not a file: $IMAGE_PATH"; cat /tmp/${component}-image-path.txt; exit 1; fi; ` +
    `else echo "no image built for ${component}"; cat /tmp/${component}-image-path.txt 2>/dev/null; exit 1; fi`,
    ...extraCommands,
  ];
}

/**
 * Properties for {@link HelloDjStage}.
 */
export interface HelloDjStageProps extends cdk.StageProps {
  /** The promotion stage this deployment stage represents (beta/staging/production). */
  readonly promotionStage: PromotionStageName;

  /**
   * The pre-provisioned {@link Shared_Foundation} handles this stage's
   * {@link WorkloadsStack} references — the ONE shared EKS cluster, the shared
   * DynamoDB tables + DAX endpoint, the shared Secrets Manager entries, and the
   * shared keyless AI task role. Every stage receives the SAME handles (not
   * per-stage copies), so promoting three software stages never duplicates the
   * foundation hardware (R3.1, R3.5).
   */
  readonly foundation: FoundationRefs;

  /**
   * The AWS region used to derive this stage's Ingress hostname
   * `<stage>.<region>.hellodj.bot`.
   */
  readonly region: string;
}

/**
 * One deployable environment in the pipeline (Beta, Staging, or Production).
 *
 * A `cdk.Stage` groups the stacks that the pipeline deploys together for a
 * single environment. The pipeline adds these in the fixed
 * {@link PROMOTION_ORDER}; because CDK Pipelines runs stages sequentially and
 * halts on failure, adding Beta then Staging then Production realizes the
 * `promotion.promote()` order + halt-on-failure directly (R11.1-R11.4).
 *
 * **SOFTWARE ONLY (R3.1, R3.4, R3.5).** Each stage deploys exactly one
 * namespaced {@link WorkloadsStack} (`hellodj-workloads-<stage>`) that adds the
 * 12 components' Kubernetes manifests to the pre-provisioned shared cluster in
 * the `hellodj-<stage>` namespace. It references the {@link Shared_Foundation}
 * handles passed via {@link HelloDjStageProps.foundation} and creates NO
 * VPC/EKS control plane/DAX/ALB/NLB/node group — so CDK Pipelines' per-stage
 * deploy can never triple the foundation hardware (R3.4).
 */
export class HelloDjStage extends cdk.Stage {
  /** The promotion stage name (beta/staging/production) this stage deploys. */
  public readonly promotionStage: PromotionStageName;

  /** This stage's namespaced software workloads on the shared foundation. */
  public readonly workloads: WorkloadsStack;

  constructor(scope: Construct, id: string, props: HelloDjStageProps) {
    super(scope, id, props);
    this.promotionStage = props.promotionStage;

    // SOFTWARE ONLY: a namespaced WorkloadsStack referencing the
    // pre-provisioned Shared_Foundation. No VPC/EKS/DAX/ALB/NLB is created here
    // (R3.4) — the stack only adds Kubernetes manifests (namespace,
    // Deployments, Services, HPAs, Ingress) to the shared cluster.
    this.workloads = new WorkloadsStack(
      this,
      `hellodj-workloads-${props.promotionStage}`,
      {
        env: props.env,
        stage: props.promotionStage,
        region: props.region,
        cluster: props.foundation.cluster,
        data: props.foundation.data,
        secrets: props.foundation.secrets,
        aiTaskRole: props.foundation.aiTaskRole,
      },
    );
  }
}

/**
 * Properties for {@link PipelineStack}.
 */
export interface PipelineStackProps extends cdk.StackProps {
  /**
   * The connection/repository the pipeline builds from. When unset a
   * clearly-marked placeholder GitHub source is used so the pipeline
   * synthesizes without a live connection (the deployment task supplies the
   * real source).
   */
  readonly repoString?: string;

  /**
   * The git branch the pipeline tracks.
   *
   * @default 'main'
   */
  readonly branch?: string;

  /**
   * The platform VPC for CodeBuild to run in (required for EFS mount).
   * When set, CodeBuild projects run in the VPC's private subnets with NAT
   * egress, and the persistent /nix/store EFS is mounted.
   */
  readonly vpc?: ec2.IVpc;

  /**
   * The pre-provisioned {@link Shared_Foundation} handles the per-stage
   * {@link WorkloadsStack}s reference. Under the SELECTED option (design
   * "Change 3b") the foundation is deployed once, OUTSIDE any pipeline stage,
   * and the pipeline references it by its stable, stage-independent
   * names/ARNs. When unset, {@link PipelineStack} imports those pre-provisioned
   * resources by their stable identifiers (see {@link importFoundationRefs}) so
   * no {@link HelloDjStage} ever instantiates foundation hardware (R3.4).
   *
   * NOTE: The final foundation-once ordering + synth gate is finalized in task
   * 7.2/7.3; task 7.1 threads these handles into the software-only stages.
   */
  readonly foundation?: FoundationRefs;

  /**
   * ARN of the CodeConnections (GitHub v2) connection the pipeline sources
   * from. When set, the token-less connection source is used; the connection
   * must be authorized once in the AWS console before the pipeline can run.
   * When unset, the OAuth-token `gitHub()` source is used.
   */
  readonly connectionArn?: string;
}

/** Placeholder source repo used when no real repository is wired in yet. */
export const PLACEHOLDER_REPO = 'celesrenata/hellodj';

/**
 * Stable, stage-independent name of the shared EKS cluster.
 *
 * Reconciled to the REAL value: `EksStack` (`eks-stack.ts`) creates the cluster
 * with `clusterName: 'hellodj'` (stage-independent, per this spec's Change 1a),
 * so the pipeline imports it by this exact name.
 */
export const SHARED_CLUSTER_NAME = 'hellodj';

/**
 * Stable, stage-independent name of the shared DAX cluster.
 *
 * Reconciled to the REAL value: `DataStack` (`data-stack.ts`) creates the DAX
 * cluster with `clusterName: 'hellodj-dax'` (stage-independent), so the
 * pipeline imports it by this exact name.
 */
export const SHARED_DAX_CLUSTER_NAME = 'hellodj-dax';

/**
 * Prefix under which the shared Secrets Manager entries live.
 *
 * DESIGN vs CURRENT-CODE RECONCILIATION (documented per task 7.2): the design's
 * "Change 3b" mandates the once-deployed `Shared_Foundation` expose STABLE,
 * STAGE-INDEPENDENT names (`hellodj/<leaf>`). The current `AuthStack`
 * (`auth-stack.ts`) still takes a `stage` prop and stage-suffixes its secrets as
 * `hellodj/<stage>/<leaf>` (and its AI role as `hellodj-ai-task-<stage>`) — a
 * latent inconsistency inherited from the pre-refactor `aws-saas-replatform`
 * code that belongs to the foundation/composition tasks (5.x), NOT this
 * pipeline-only task. Per the task guidance ("prefer correctness; if uncertain,
 * keep the stable stage-independent form and note it"), the import below uses
 * the design-mandated stage-independent names so the pipeline references a
 * single shared secret set. When `AuthStack` is made stage-independent (dropping
 * the `-<stage>`/`/<stage>/` tokens) these imports already match; until then the
 * names are threaded via cross-stack export/props (`PipelineStackProps.foundation`),
 * which takes precedence over this import.
 */
export const SHARED_SECRET_PREFIX = 'hellodj';

/** Stable, stage-independent name of the shared keyless AI task role. */
export const SHARED_AI_TASK_ROLE_NAME = 'hellodj-ai-task';

/**
 * Stable, stage-independent name of the IAM role EKS uses to issue kubectl
 * commands against the shared cluster.
 *
 * An IMPORTED EKS cluster (`eks.Cluster.fromClusterAttributes`) can only have
 * Kubernetes manifests applied to it (`cluster.addManifest`, which every
 * software-only {@link HelloDjStage}'s {@link WorkloadsStack} does) when it is
 * given a `kubectlRoleArn` — otherwise the imported cluster throws
 * `"kubectlRole" is not defined, cannot issue kubectl commands against this
 * cluster` at synth time. The once-deployed foundation (`EksStack`) provisions
 * this role with a stable stage-independent name so the pipeline (and any
 * software stage) can reference it when importing the shared cluster (R3.1,
 * R3.5). When the foundation is threaded via
 * {@link PipelineStackProps.foundation}, that authoritative, already
 * synth-capable cluster handle takes precedence over this import.
 */
export const SHARED_KUBECTL_ROLE_NAME = 'hellodj-kubectl';

/**
 * The Beta -> Staging -> Production deployment pipeline.
 *
 * Exposes the underlying {@link CodePipeline}, the ordered {@link stages}, the
 * ordered {@link stageNames}, and the per-component {@link componentBuildSteps}
 * as public props so the assertion tests (task 18.5) and the end-to-end wiring
 * (task 20.1) can introspect the shape without reaching into private state.
 */
export class PipelineStack extends cdk.Stack {
  /** The underlying CDK Pipelines CodePipeline construct. */
  public readonly pipeline: CodePipeline;

  /** The deployment stages in fixed promotion order (Beta, Staging, Production). */
  public readonly stages: HelloDjStage[];

  /** The ordered stage names (mirror of {@link PROMOTION_ORDER}). */
  public readonly stageNames: PromotionStageName[];

  /**
   * The per-component build steps, keyed by Component name. Each is an
   * isolated build path enabling independent single-component promotion
   * (R15.2). These are fed into the synth step as `additionalInputs`.
   */
  public readonly componentBuildSteps: Record<string, CodeBuildStep>;

  constructor(scope: Construct, id: string, props: PipelineStackProps = {}) {
    super(scope, id, props);

    // Persistent Nix store cache on EFS. A SINGLE build project (not 12
    // parallel) mounts EFS at /mnt/nix-store, bind-mounts it over /nix, and
    // builds all 12 components sequentially. No race. Second run is instant.
    let nixStoreFs: efs.FileSystem | undefined;
    if (props.vpc) {
      const efsSg = new ec2.SecurityGroup(this, 'NixStoreEfsSg', {
        vpc: props.vpc,
        description: 'NFS access for CodeBuild Nix store EFS',
        allowAllOutbound: true,
      });
      efsSg.addIngressRule(
        ec2.Peer.ipv4(props.vpc.vpcCidrBlock),
        ec2.Port.tcp(2049),
        'NFS from VPC (CodeBuild)',
      );
      nixStoreFs = new efs.FileSystem(this, 'NixStoreMaxIoEfs', {
        vpc: props.vpc,
        performanceMode: efs.PerformanceMode.MAX_IO,
        throughputMode: efs.ThroughputMode.BURSTING,
        removalPolicy: cdk.RemovalPolicy.DESTROY,
        lifecyclePolicy: efs.LifecyclePolicy.AFTER_30_DAYS,
        securityGroup: efsSg,
      });
    }

    const branch = props.branch ?? 'main';
    // Source from the private CodeCommit `hellodj` repository (R2.1/R3.1).
    // The source of truth has moved off public GitHub into CodeCommit; the
    // pipeline uses the native CodeCommit source action which authenticates
    // via the pipeline's service role (IAM-based, no OAuth token needed).
    const repo = codecommit.Repository.fromRepositoryName(
      this, 'SourceRepo', props.repoString ?? 'hellodj',
    );
    const source = CodePipelineSource.codeCommit(repo, branch);

    // ---------------------------------------------------------------------------
    // Additional source triggers: the four JVM fork repos (R2.1/R3.1).
    // A push to ANY of these repos (on their designated build branch) triggers
    // the pipeline, so a fork change (lavaplayer patch, LavaSrc update, etc.)
    // flows through the same build/gate/promote pipeline as a hellodj push.
    // These are wired as additionalInputs to the synth step; CDK Pipelines
    // creates a CodePipeline source action per input, and ANY source action
    // change triggers the pipeline execution.
    // ---------------------------------------------------------------------------
    const forkSources: Record<string, ReturnType<typeof CodePipelineSource.codeCommit>> = {};
    const forkRepos: Array<{ name: string; branch: string }> = [
      { name: 'Lavalink', branch: 'dev' },
      { name: 'lavaplayer', branch: 'main' },
      { name: 'LavaSrc', branch: 'tidal-v2-api' },
      { name: 'youtube-source', branch: 'main' },
    ];
    for (const fork of forkRepos) {
      const forkRepo = codecommit.Repository.fromRepositoryName(
        this, `ForkRepo-${fork.name}`, fork.name,
      );
      forkSources[fork.name] = CodePipelineSource.codeCommit(forkRepo, fork.branch);
    }

    // ALL component builds run in the SINGLE synth step (sequentially) so they
    // share one Nix install and one EFS mount. No per-component parallel projects.
    this.componentBuildSteps = {};

    // Build all components as part of the synth step commands.
    const allComponentBuildCommands: string[] = [];
    for (const component of PLATFORM_COMPONENTS) {
      allComponentBuildCommands.push(...getComponentBuildCommands(component));
    }

    // The synth/build stage. Installs Nix (or reuses from EFS), runs CDK synth,
    // then builds all 12 component images sequentially and pushes to ECR.
    const synth = new CodeBuildStep('synth', {
      input: source,
      primaryOutputDirectory: 'platform/infra/cdk.out',
      installCommands: getInstallCommands(),
      commands: [...getBuildCommands(), ...allComponentBuildCommands],
      additionalInputs: {
        'forks/Lavalink': forkSources['Lavalink'],
        'forks/lavaplayer': forkSources['lavaplayer'],
        'forks/LavaSrc': forkSources['LavaSrc'],
        'forks/youtube-source': forkSources['youtube-source'],
      },
    });

    this.pipeline = new CodePipeline(this, 'pipeline', {
      pipelineName: 'hellodj-pipeline',
      synth,
      // Self-mutation is DISABLED. The pipeline stack includes stage stacks that
      // apply Kubernetes manifests via the EKS stack's kubectl handler Lambda.
      // Self-mutation tries to deploy the pipeline stack (which references those
      // stage stacks), triggering cross-stack custom resource invocations that
      // fail because the kubectl handler is scoped to the EKS stack. Pipeline
      // definition changes are deployed manually with `cdk deploy hellodj-pipeline`.
      selfMutation: false,
      // Use LARGE compute on ARM64 (Graviton) for all CodeBuild projects.
      // Native aarch64 builds — no QEMU. AL2023 ARM64 image (3.0) has Node 20+.
      // Privileged mode is required for docker daemon (image build + push).
      // When VPC is supplied, CodeBuild runs in-VPC with a persistent EFS-backed
      // /nix/store so subsequent builds skip all downloads.
      codeBuildDefaults: {
        ...(props.vpc && nixStoreFs ? {
          vpc: props.vpc,
          subnetSelection: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
          fileSystemLocations: [
            codebuild.FileSystemLocation.efs({
              identifier: 'nix_store',
              location: `${nixStoreFs.fileSystemId}.efs.${this.region}.amazonaws.com:/`,
              mountPoint: '/mnt/nix-store',
            }),
          ],
        } : {}),
        buildEnvironment: {
          computeType: cdk.aws_codebuild.ComputeType.LARGE,
          buildImage: cdk.aws_codebuild.LinuxArmBuildImage.fromCodeBuildImageId(
            'aws/codebuild/amazonlinux-aarch64-standard:3.0',
          ),
          privileged: true,
        },
        rolePolicy: [
          // ECR push permissions for Nix-built OCI images.
          new iam.PolicyStatement({
            effect: iam.Effect.ALLOW,
            actions: [
              'ecr:GetAuthorizationToken',
            ],
            resources: ['*'],
          }),
          new iam.PolicyStatement({
            effect: iam.Effect.ALLOW,
            actions: [
              'ecr:BatchCheckLayerAvailability',
              'ecr:GetDownloadUrlForLayer',
              'ecr:BatchGetImage',
              'ecr:PutImage',
              'ecr:InitiateLayerUpload',
              'ecr:UploadLayerPart',
              'ecr:CompleteLayerUpload',
              'ecr:DescribeRepositories',
              'ecr:CreateRepository',
            ],
            resources: [
              `arn:aws:ecr:${this.region}:${this.account}:repository/hellodj/*`,
            ],
          }),
          // CodeCommit pull for flake inputs referencing the fork repos.
          new iam.PolicyStatement({
            effect: iam.Effect.ALLOW,
            actions: [
              'codecommit:GitPull',
              'codecommit:GetBranch',
              'codecommit:GetCommit',
              'codecommit:GetRepository',
              'codecommit:ListBranches',
            ],
            resources: [
              `arn:aws:codecommit:${this.region}:${this.account}:hellodj`,
              `arn:aws:codecommit:${this.region}:${this.account}:Lavalink`,
              `arn:aws:codecommit:${this.region}:${this.account}:lavaplayer`,
              `arn:aws:codecommit:${this.region}:${this.account}:LavaSrc`,
              `arn:aws:codecommit:${this.region}:${this.account}:youtube-source`,
            ],
          }),
          // S3 Nix binary cache — read (substituter) + write (push after build).
          new iam.PolicyStatement({
            effect: iam.Effect.ALLOW,
            actions: [
              's3:GetObject',
              's3:PutObject',
              's3:ListBucket',
            ],
            resources: [
              'arn:aws:s3:::hellodj-nix-cache',
              'arn:aws:s3:::hellodj-nix-cache/*',
            ],
          }),
          // KMS decrypt for sops (Nix cache signing key).
          new iam.PolicyStatement({
            effect: iam.Effect.ALLOW,
            actions: ['kms:Decrypt'],
            resources: [
              `arn:aws:kms:${this.region}:${this.account}:key/3a087d5f-2c33-4cfa-adfe-fdcad27bbe0d`,
            ],
          }),
        ],
      },
    });

    // Add one deployment stage per entry in the fixed promotion order
    // (beta → staging → production). CDK Pipelines runs these SEQUENTIALLY and
    // halts on the FIRST failure, which *is* the realization of
    // promotion.promote() ordering (R3.2, R11.1-R11.3) and its halt-on-failure
    // edge (R3.3, R11.4):
    //
    //   * Fixed order (R3.2): each stage is a distinct CodePipeline stage added
    //     in PROMOTION_ORDER; CodePipeline only starts a stage after its
    //     predecessor stage completes, so promotion always flows
    //     beta → staging → production.
    //   * Halt on failure + leave earlier stages running (R3.3): a failed
    //     deploy action stops the pipeline execution before the next stage
    //     begins, so a Beta failure never promotes to Staging and a Staging
    //     failure never promotes to Production. CodePipeline does NOT roll back
    //     or tear down the already-succeeded upstream stages — they stay
    //     deployed and operating (R2.2 / R3.3).
    //   * Block new promotions until resolved (R3.3): the pipeline is a single
    //     serial execution graph; a stopped (failed) execution does not advance
    //     to the next stage, and CodePipeline's default superseding/serial
    //     behavior means the failed stage must be re-run to green before
    //     promotion continues — no new Software_Stage deploy starts past the
    //     failed stage until it is resolved.
    //
    // This native sequential-with-halt behavior needs no extra wiring; the loop
    // below simply preserves the three stages in PROMOTION_ORDER. The synth
    // stage above already ran assertFoundationSingleton via `cdk synth`
    // (R1.8), so no stage reached here could carry a duplicate foundation
    // resource.
    //
    // SOFTWARE-ONLY stages (R3.1, R3.4, R3.5). Each stage deploys only its
    // namespaced WorkloadsStack, referencing the pre-provisioned
    // Shared_Foundation. The foundation is supplied via props (deployed once,
    // outside any pipeline stage — design "Change 3b" SELECTED option) or, when
    // absent, imported by its stable stage-independent names/ARNs so a stage
    // can never re-create foundation hardware.
    const region = props.env?.region ?? this.region;
    const foundation = props.foundation ?? this.importFoundationRefs(region);

    this.stages = [];
    this.stageNames = [];
    for (const stageName of PROMOTION_ORDER) {
      const stage = new HelloDjStage(this, `hellodj-${stageName}-stage`, {
        promotionStage: stageName,
        env: props.env,
        foundation,
        region,
      });
      this.pipeline.addStage(stage);
      this.stages.push(stage);
      this.stageNames.push(stageName);
    }
  }

  /**
   * Import the pre-provisioned {@link Shared_Foundation} by its stable,
   * stage-independent names/ARNs (design "Change 3b" SELECTED option).
   *
   * The foundation (VPC, EKS control plane + CPU_Node_Fleet, DAX, ALB, NLB) is
   * deployed **once, outside** the pipeline; the pipeline only references it.
   * Importing (rather than constructing) the cluster/tables/secrets/role
   * guarantees no {@link HelloDjStage} can instantiate foundation hardware
   * (R3.4). The imported handles carry the same stable identifiers the
   * composition (`bin/hellodj.ts`) provisions.
   *
   * Name reconciliation (task 7.2): the cluster (`hellodj`) and DAX cluster
   * (`hellodj-dax`) names match `EksStack`/`DataStack` verbatim; the table names
   * come from `data-stack.ts` exports. The secret names and AI-role name use the
   * design-mandated stage-independent form (see {@link SHARED_SECRET_PREFIX}) —
   * `AuthStack` currently stage-suffixes them, an inconsistency owned by the
   * foundation/composition tasks; when the foundation is threaded via
   * {@link PipelineStackProps.foundation} those authoritative handles take
   * precedence over this import.
   */
  private importFoundationRefs(region: string): FoundationRefs {
    // IMPORTANT: an imported EKS cluster must be given a `kubectlRoleArn` for
    // `cluster.addManifest` to synthesize — every software-only stage's
    // WorkloadsStack adds Kubernetes manifests (namespace, Deployments,
    // Services, HPAs, Ingress). Without it, synth throws `"kubectlRole" is not
    // defined, cannot issue kubectl commands against this cluster`. The
    // once-deployed foundation provisions a stable, stage-independent kubectl
    // role ({@link SHARED_KUBECTL_ROLE_NAME}); import the cluster WITH that role
    // ARN so the pipeline's software-only stages can render their manifests
    // against the shared cluster (R3.1, R3.5).
    // An imported cluster also needs an OpenID Connect provider so
    // `cluster.addServiceAccount` (IRSA) synthesizes for each component;
    // without it the imported cluster throws `"openIdConnectProvider" is not
    // defined for this imported cluster`. Import the shared cluster's OIDC
    // provider by its stable ARN. The provider URL is the standard EKS OIDC
    // issuer path; the concrete issuer id is only known post-deploy, so this
    // import uses a stable placeholder issuer segment — the authoritative,
    // already synth-capable cluster handle threaded via
    // {@link PipelineStackProps.foundation} takes precedence over this import.
    const oidcProvider =
      iam.OpenIdConnectProvider.fromOpenIdConnectProviderArn(
        this,
        'SharedClusterOidc',
        cdk.Stack.of(this).formatArn({
          service: 'iam',
          region: '',
          resource: 'oidc-provider',
          resourceName: `oidc.eks.${region}.amazonaws.com/id/${SHARED_CLUSTER_NAME}`,
        }),
      );

    const cluster = eks.Cluster.fromClusterAttributes(this, 'SharedCluster', {
      clusterName: SHARED_CLUSTER_NAME,
      kubectlRoleArn: cdk.Stack.of(this).formatArn({
        service: 'iam',
        region: '',
        resource: 'role',
        resourceName: SHARED_KUBECTL_ROLE_NAME,
      }),
      kubectlLayer: new KubectlV36Layer(this, 'KubectlLayer'),
      openIdConnectProvider: oidcProvider,
    });

    const data = {
      coreTable: dynamodb.Table.fromTableName(
        this,
        'SharedCoreTable',
        CORE_TABLE_NAME,
      ),
      searchCacheTable: dynamodb.Table.fromTableName(
        this,
        'SharedSearchCacheTable',
        SEARCH_CACHE_TABLE_NAME,
      ),
      sessionTable: dynamodb.Table.fromTableName(
        this,
        'SharedSessionTable',
        SESSION_TABLE_NAME,
      ),
      // The DAX discovery endpoint is a stable per-cluster address the
      // once-deployed foundation exposes (DataStack.daxEndpoint, which resolves
      // `CfnCluster.attrClusterDiscoveryEndpoint` at deploy time). The real AWS
      // DAX discovery-endpoint shape is
      // `<clusterName>.<hash>.dax-clusters.<region>.amazonaws.com:8111` — the
      // `<hash>` segment is only known post-deploy, so when the foundation is
      // not threaded via props this import falls back to the stable
      // hash-less form; the authoritative value is supplied via
      // `PipelineStackProps.foundation.data.daxEndpoint` (cross-stack export),
      // which takes precedence over this import.
      daxEndpoint: `${SHARED_DAX_CLUSTER_NAME}.dax-clusters.${region}.amazonaws.com:8111`,
    };

    // Stable, stage-independent secret names (design "Change 3b"). See
    // SHARED_SECRET_PREFIX for the AuthStack stage-suffix reconciliation note.
    const secrets = {
      discordBotToken: secretsmanager.Secret.fromSecretNameV2(
        this,
        'SharedDiscordBotTokenSecret',
        `${SHARED_SECRET_PREFIX}/discord-bot-token`,
      ),
      tidalRefresh: secretsmanager.Secret.fromSecretNameV2(
        this,
        'SharedTidalRefreshSecret',
        `${SHARED_SECRET_PREFIX}/tidal-refresh`,
      ),
      spotify: secretsmanager.Secret.fromSecretNameV2(
        this,
        'SharedSpotifySecret',
        `${SHARED_SECRET_PREFIX}/spotify`,
      ),
      ytCipher: secretsmanager.Secret.fromSecretNameV2(
        this,
        'SharedYtCipherSecret',
        `${SHARED_SECRET_PREFIX}/yt-cipher-secret`,
      ),
    };

    const aiTaskRole: iam.IRole = iam.Role.fromRoleName(
      this,
      'SharedAiTaskRole',
      SHARED_AI_TASK_ROLE_NAME,
    );

    return { cluster, data, secrets, aiTaskRole };
  }
}

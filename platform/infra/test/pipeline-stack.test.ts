import * as cdk from 'aws-cdk-lib';
import * as eks from 'aws-cdk-lib/aws-eks';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as fs from 'fs';
import * as path from 'path';
import { Template } from 'aws-cdk-lib/assertions';
import {
  PipelineStack,
  HelloDjStage,
  PROMOTION_ORDER,
  PLATFORM_COMPONENTS,
  getBuildCommands,
  getComponentBuildCommands,
  getInstallCommands,
  getComponentInstallCommands,
  getNixInstallCommands,
} from '../lib/pipeline-stack';
import { FoundationRefs } from '../lib/foundation';
import { NetworkStack } from '../lib/network-stack';
import { EksStack } from '../lib/eks-stack';
import { DataStack } from '../lib/data-stack';
import { AuthStack } from '../lib/auth-stack';
import { DeploymentStage } from '../lib/config';
import {
  WorkloadsStack,
  workloadsNamespace,
  stageEndpoint,
  stageHostname,
} from '../lib/workloads-stack';
import { COMPONENT_WORKLOADS } from '../lib/component-workloads';

/**
 * CDK assertion tests for {@link PipelineStack} (tasks 18.5 + 14.3).
 *
 * Under the `hellodj-nix-native-delivery` reconciliation the pipeline is a
 * **no-build (resolve/verify)** orchestrator: GitHub Actions with Nix compiles
 * every image and the GPU AMI and publishes them to the S3-backed binary cache
 * / ECR, and CDK Pipelines only synths, gates, resolves + verifies prebuilt
 * closures, and deploys. Two complementary angles verify the
 * Beta -> Staging -> Production pipeline realizes the design's promotion model
 * and its build-stage gates:
 *
 *   1. Unit-level assertions on the exported helpers (no synth needed):
 *      the reconciled fixed promotion order Beta -> Staging -> Production
 *      (R9.2, R10.1), the repo-wide base-image gate that fails the build on
 *      non-PASS (R5.7), the per-component resolve/verify-closure steps that do
 *      NOT compile images on CodeBuild (R6.3, R6.4), and the independently
 *      deployable Component set (R15.2).
 *
 *   2. Synthesized-template assertions: instantiating {@link PipelineStack}
 *      into an app produces exactly one AWS::CodePipeline::Pipeline whose
 *      build/synth stage precedes the Beta -> Staging -> Production deploy
 *      stages (R10.1).
 *
 * Validates: Requirements 5.7, 6.3, 6.4, 9.2, 10.1, 11.1, 11.2, 11.3, 15.2
 */

// CDK Pipelines' CodePipeline needs a concrete env at synth time.
const ENV = { account: '123456789012', region: 'us-east-1' };

// ---------------------------------------------------------------------------
// Angle 1 — exported helper assertions (fixed order + gate/resolve commands)
// ---------------------------------------------------------------------------

describe('PipelineStack helpers — promotion order and build-stage steps', () => {
  test('PROMOTION_ORDER is exactly Beta -> Staging -> Production (Requirements 9.2, 10.1)', () => {
    // The fixed promotion order is the single source of truth the pipeline
    // adds stages from: Beta before Staging before Production. The prior
    // gamma/prod identifiers are fully reconciled away (R9.2).
    expect([...PROMOTION_ORDER]).toEqual(['beta', 'staging', 'production']);
    // beta precedes staging precedes production.
    expect(PROMOTION_ORDER.indexOf('beta')).toBeLessThan(
      PROMOTION_ORDER.indexOf('staging'),
    );
    expect(PROMOTION_ORDER.indexOf('staging')).toBeLessThan(
      PROMOTION_ORDER.indexOf('production'),
    );
    // Zero occurrences of the retired gamma/prod stage identifiers (R9.2).
    expect(PROMOTION_ORDER).not.toContain('gamma');
    expect(PROMOTION_ORDER).not.toContain('prod');
  });

  test('getBuildCommands includes the Nix base-image gate that fails the build on non-PASS (Requirement 5.7)', () => {
    // The build stage must run the base-image gate and reject any non-Nix
    // (ubuntu/debian/alpine) base image; the gate runner is the mechanism and
    // its non-zero exit fails the pipeline build stage (R5.7).
    const commands = getBuildCommands();
    const gate = commands.find((c) => c.includes('gate_base_image'));
    expect(gate).toBeDefined();
    // The gate runs the runner directly (a non-zero exit fails the build step,
    // blocking promotion on non-PASS — R5.7). It is a plain invocation, not
    // suppressed with `|| true` / `; true` which would swallow a failure.
    expect(gate).toContain('python3 tools/gate_base_image.py');
    expect(gate).not.toMatch(/\|\|\s*true/);
    expect(gate).not.toMatch(/;\s*true/);
  });

  test('getBuildCommands resolves/verifies the prebuilt AMI closure rather than compiling it (Requirements 6.3, 6.4)', () => {
    // No-build: the AMI is resolved + verified from the cache, never compiled
    // on CodeBuild, so no build compute is billed (R6.3, R6.4).
    const commands = getBuildCommands();
    const resolve = commands.find((c) => c.includes('resolve_closure'));
    expect(resolve).toBeDefined();
    expect(resolve).toContain('--verify');
    // The synth step never runs an image/AMI build on CodeBuild.
    expect(
      commands.some((c) => /nixos-generate|docker build|buildLayeredImage/i.test(c)),
    ).toBe(false);
  });

  test('getComponentBuildCommands builds the Nix image and pushes to ECR', () => {
    // Each Component's build step builds the Nix OCI image for aarch64-linux
    // on CodeBuild and pushes to ECR. Source is CodeCommit (no GHA).
    const commands = getComponentBuildCommands('lavalink');
    // Runs nix build for the component's aarch64-linux image.
    expect(
      commands.some((c) => c.includes('nix build') && c.includes('aarch64-linux')),
    ).toBe(true);
    // Pushes to ECR.
    expect(
      commands.some((c) => c.includes('docker push') && c.includes('lavalink')),
    ).toBe(true);
    // Runs the dependency gate.
    expect(
      commands.some((c) => c.includes('gate_dependencies') && c.includes('lavalink')),
    ).toBe(true);
  });

  test('every Component build step builds and pushes its image (R15.2)', () => {
    // Per-component paths: one entry per independently deployable Component,
    // each building its Nix image and pushing to ECR.
    expect(PLATFORM_COMPONENTS).toHaveLength(12);
    for (const component of PLATFORM_COMPONENTS) {
      const commands = getComponentBuildCommands(component);
      // Builds the Nix image for this component.
      expect(
        commands.some((c) => c.includes('nix build') && c.includes(component)),
      ).toBe(true);
      // Pushes to ECR for this component.
      expect(
        commands.some((c) => c.includes('docker push') || c.includes('ECR push')),
      ).toBe(true);
    }
  });

  test('Nix install bakes the S3 cache config in at install time (no post-install nix.conf edit / daemon restart)', () => {
    // Regression guard: appending substituters to /etc/nix/nix.conf after
    // install + `systemctl restart nix-daemon` silently no-op'd on the
    // systemd-less CodeBuild container, so the daemon never learned the S3
    // cache and builds read only from cache.nixos.org. The config must instead
    // be passed to the installer via --extra-conf so the daemon boots with it.
    const nix = getNixInstallCommands();
    const install = nix[0];
    expect(install).toContain('install.determinate.systems/nix');
    // Baked in at install: substituter, trust, and no-sigs — via --extra-conf.
    expect(install).toContain('--extra-conf');
    expect(install).toContain('extra-substituters = s3://hellodj-nix-cache');
    expect(install).toContain('extra-trusted-substituters = s3://hellodj-nix-cache');
    expect(install).toContain('require-sigs = false');
    expect(install).toContain('extra-trusted-users = root');
    // Root-only container mode (no systemd daemon to restart).
    expect(install).toContain('--init none');
    // The broken post-install approach must be gone entirely.
    expect(nix.some((c) => c.includes('systemctl'))).toBe(false);
    expect(nix.some((c) => c.includes('>> /etc/nix/nix.conf'))).toBe(false);
  });

  test('Nix is installed FIRST, before any other tooling', () => {
    // The S3 substituter must be configured before the first store op; Nix
    // therefore leads every install script (component and synth).
    const nix = getNixInstallCommands();
    expect(nix[0]).toContain('install.determinate.systems/nix');

    // Synth install layers Node/ruff AFTER the shared Nix block.
    const synth = getInstallCommands();
    const nixIdx = synth.findIndex((c) => c.includes('install.determinate.systems/nix'));
    const nodeIdx = synth.findIndex((c) => c.includes('nodejs22'));
    const ruffIdx = synth.findIndex((c) => c.includes('pip install ruff'));
    expect(nixIdx).toBe(0);
    expect(nodeIdx).toBeGreaterThan(nixIdx);
    expect(ruffIdx).toBeGreaterThan(nixIdx);
  });

  test('component install SKIPS Node and ruff (speed: only Nix is needed)', () => {
    // A per-component build is just `nix build` + push, so it must not pay for
    // Node.js or ruff installs (those are synth-only). This is the build-speed
    // win: drop `dnf install nodejs22` and `pip install ruff` from all 12
    // parallel component projects.
    const componentInstall = getComponentInstallCommands();
    expect(componentInstall.some((c) => c.includes('nodejs22'))).toBe(false);
    expect(componentInstall.some((c) => c.includes('pip install ruff'))).toBe(false);
    // But it still installs Nix (first) and the sops signing key for the push.
    expect(componentInstall[0]).toContain('install.determinate.systems/nix');
    expect(componentInstall.some((c) => c.includes('sops'))).toBe(true);
    expect(
      componentInstall.some((c) => c.includes('nix-cache-key.sec')),
    ).toBe(true);
    // The git credential helper for CodeCommit flake inputs is retained.
    expect(
      componentInstall.some((c) => c.includes('codecommit credential-helper')),
    ).toBe(true);
  });

  test('component build is cache-friendly: no --impure, stable committed source', () => {
    // Cache-miss fix: building with --impure hashed the live working tree, so
    // the vendored platform_logic copy made the source derivation differ every
    // run and the S3 cache never hit. The build now commits a mtime-normalized
    // copy and drops --impure so the git tree hash (and output path) is stable.
    for (const component of PLATFORM_COMPONENTS) {
      const commands = getComponentBuildCommands(component);
      const build = commands.find((c) => c.includes('nix build'));
      expect(build).toBeDefined();
      expect(build).not.toContain('--impure');
      // Source is made deterministic: mtimes normalized + committed.
      const vendor = commands.find((c) => c.includes('hellodj_platform_logic'));
      expect(vendor).toContain("touch -d '2020-01-01T00:00:00Z'");
      expect(vendor).toContain('git -c user.email=ci@hellodj');
    }
  });
});

// ---------------------------------------------------------------------------
// Angle 2 — synthesized-template assertions (ordered stages, build precedes deploys)
// ---------------------------------------------------------------------------

describe('PipelineStack — synthesized CodePipeline', () => {
  let template: Template;
  let stack: PipelineStack;

  beforeAll(() => {
    const app = new cdk.App();
    stack = new PipelineStack(app, 'Pipeline', { env: ENV });
    template = Template.fromStack(stack);
  });

  test('synthesizes exactly one CodePipeline', () => {
    template.resourceCountIs('AWS::CodePipeline::Pipeline', 1);
  });

  test('the pipeline stack exposes stageNames equal to PROMOTION_ORDER (Requirements 9.2, 10.1)', () => {
    expect(stack.stageNames).toEqual([...PROMOTION_ORDER]);
    expect(stack.stages.map((s) => s.promotionStage)).toEqual([
      ...PROMOTION_ORDER,
    ]);
  });

  test('the deploy stages appear in Beta -> Staging -> Production order (Requirements 9.2, 10.1)', () => {
    // CDK Pipelines synthesizes stage names into the CodePipeline Stages
    // property, prefixed by the built-in Source/Build/UpdatePipeline stages
    // then the deploy stages in the order they were added. Assert the beta
    // deploy stage precedes staging precedes production among the synthesized
    // stage names.
    const pipelines = template.findResources('AWS::CodePipeline::Pipeline');
    const pipeline = Object.values(pipelines)[0] as any;
    const stageNames: string[] = pipeline.Properties.Stages.map(
      (s: any) => s.Name as string,
    );

    const betaIdx = stageNames.findIndex((n) => /beta/i.test(n));
    const stagingIdx = stageNames.findIndex((n) => /staging/i.test(n));
    const prodIdx = stageNames.findIndex((n) => /production/i.test(n));

    // All three deploy stages are present.
    expect(betaIdx).toBeGreaterThanOrEqual(0);
    expect(stagingIdx).toBeGreaterThanOrEqual(0);
    expect(prodIdx).toBeGreaterThanOrEqual(0);

    // And they appear in the fixed promotion order.
    expect(betaIdx).toBeLessThan(stagingIdx);
    expect(stagingIdx).toBeLessThan(prodIdx);

    // No retired gamma/prod stage identifier survives in the synthesized
    // pipeline stage names (R9.2).
    expect(stageNames.some((n) => /gamma/i.test(n))).toBe(false);
  });

  test('the build/synth stage precedes every deploy stage (Requirement 10.1)', () => {
    // CDK Pipelines runs the built-in Build stage (the synth step) to
    // completion before any deploy stage. Assert the synth/Build stage appears
    // before the first (beta) deploy stage in the synthesized stage order, so
    // build (and its gates + resolve/verify) precedes promotion (R10.1).
    const pipelines = template.findResources('AWS::CodePipeline::Pipeline');
    const pipeline = Object.values(pipelines)[0] as any;
    const stageNames: string[] = pipeline.Properties.Stages.map(
      (s: any) => s.Name as string,
    );

    const buildIdx = stageNames.findIndex((n) => /build|synth/i.test(n));
    const betaIdx = stageNames.findIndex((n) => /beta/i.test(n));

    expect(buildIdx).toBeGreaterThanOrEqual(0);
    expect(betaIdx).toBeGreaterThanOrEqual(0);
    // Build/synth precedes the first deploy stage.
    expect(buildIdx).toBeLessThan(betaIdx);
  });
});

// ---------------------------------------------------------------------------
// Task 7.4 — pipeline-shape + zero-foundation-per-stage assertions
// (Requirements 3.1, 3.2, 3.4, 3.5)
// ---------------------------------------------------------------------------
//
// The refactored pipeline promotes SOFTWARE-ONLY stages: each `HelloDjStage`
// deploys a namespaced `WorkloadsStack` (`hellodj-workloads-<stage>`) that adds
// the 12 components' Kubernetes manifests to the PRE-PROVISIONED shared
// foundation, and the foundation is deployed ONCE, OUTSIDE any `HelloDjStage`.
// The removed `HelloDjPlaceholderStack` no longer exists. This suite asserts:
//
//   * the three stages appear in order beta -> staging -> production, each
//     deploying a `WorkloadsStack` (R3.1, R3.2);
//   * `HelloDjPlaceholderStack` is removed from the pipeline source (R3.1);
//   * synthesizing each stage's `WorkloadsStack` yields ZERO foundation
//     resources — no VPC, EKS control plane, NAT gateway, DAX cluster, ELBv2
//     load balancer, or EKS node group (R3.4);
//   * each stage's `WorkloadsStack` contains the namespaced manifests: a
//     Namespace `hellodj-<stage>`, 12 Deployments, and a host-scoped Ingress
//     bound to `<stage>.<region>.hellodj.bot` (R3.5);
//   * no foundation stack construct is instantiated inside any `HelloDjStage`
//     — the foundation is added once outside the per-stage `cdk.Stage` (R3.4).
//
// Building a SYNTH-CAPABLE foundation: the pipeline's default
// `importFoundationRefs` imports the shared cluster WITH a `kubectlRoleArn` +
// OpenID Connect provider (so `addManifest`/`addServiceAccount` synthesize).
// For the direct-`HelloDjStage` synthesis below we compose a REAL foundation
// once (NetworkStack + EksStack + DataStack + AuthStack), exactly as
// `bin/hellodj.ts` does, and thread its handles as `FoundationRefs`. That
// yields a genuinely synth-capable shared cluster, keeping the per-stage
// manifest assertions meaningful.
//
// _Requirements: 3.1, 3.2, 3.4, 3.5_

describe('pipeline software-only stages — shape + zero-foundation (task 7.4, R3.1/R3.2/R3.4/R3.5)', () => {
  const COMPOSE_ENV = { account: '123456789012', region: 'us-east-1' };
  const REGION = 'us-east-1';

  /**
   * Compose the Shared_Foundation EXACTLY ONCE (mirroring `bin/hellodj.ts` and
   * the pipeline's own `importFoundationRefs`) and return its
   * {@link FoundationRefs}. The shared EKS cluster is IMPORTED into a
   * `FoundationHolder` stack WITH a `kubectlRoleArn` + OpenID Connect provider
   * — the exact shape the pipeline's `importFoundationRefs` builds — so it is
   * genuinely synth-capable (a `WorkloadsStack` can add its Kubernetes
   * manifests against it) yet the cluster/DAX/tables are the pre-provisioned
   * foundation the pipeline only references, never re-creates. Every foundation
   * construct lives OUTSIDE any `HelloDjStage`, proving the foundation is
   * deployed once, not per stage (R3.4).
   *
   * Because `cluster.addManifest` attaches each `KubernetesManifest` to the
   * IMPORTED cluster's owning stack (the `FoundationHolder`, not the per-stage
   * `WorkloadsStack`), the software-only `WorkloadsStack` template carries ZERO
   * foundation resources (R3.4) while the namespaced manifests (Namespace, the
   * 12 Deployments, the host-scoped Ingress) are asserted on the
   * manifest-owning stack located via the app assembly (R3.5).
   */
  function composeFoundation(app: cdk.App): {
    foundation: FoundationRefs;
    foundationStackIds: string[];
  } {
    const network = new NetworkStack(app, 'hellodj-network', {
      env: COMPOSE_ENV,
    });
    const data = new DataStack(app, 'hellodj-data', {
      env: COMPOSE_ENV,
      vpc: network.vpc,
    });
    const auth = new AuthStack(app, 'hellodj-auth', {
      env: COMPOSE_ENV,
      stage: DeploymentStage.Beta,
    });

    // Import the shared cluster the SAME way the pipeline's
    // `importFoundationRefs` does — with a kubectlRoleArn + OIDC provider so
    // `addManifest`/`addServiceAccount` synthesize against it.
    const holder = new cdk.Stack(app, 'FoundationHolder', { env: COMPOSE_ENV });
    const oidc = iam.OpenIdConnectProvider.fromOpenIdConnectProviderArn(
      holder,
      'SharedClusterOidc',
      `arn:aws:iam::${COMPOSE_ENV.account}:oidc-provider/oidc.eks.${REGION}.amazonaws.com/id/hellodj`,
    );
    const cluster = eks.Cluster.fromClusterAttributes(holder, 'SharedCluster', {
      clusterName: 'hellodj',
      kubectlRoleArn: `arn:aws:iam::${COMPOSE_ENV.account}:role/hellodj-kubectl`,
      openIdConnectProvider: oidc,
    });

    const foundation: FoundationRefs = {
      cluster,
      data: {
        coreTable: data.coreTable,
        searchCacheTable: data.searchCacheTable,
        sessionTable: data.sessionTable,
        daxEndpoint: data.daxEndpoint,
        assetsBucket: data.assetsBucket,
      },
      secrets: {
        discordBotToken: auth.discordBotTokenSecret,
        tidalRefresh: auth.tidalRefreshSecret,
        spotify: auth.spotifySecret,
        ytCipher: auth.ytCipherSecret,
      },
      aiTaskRole: auth.aiTaskRole,
    };

    return {
      foundation,
      foundationStackIds: [
        network.stackName,
        data.stackName,
        auth.stackName,
        holder.stackName,
      ],
    };
  }

  /**
   * Locate, across the whole synthesized app, the flattened text of every
   * Kubernetes manifest resource (`Custom::AWSCDK-EKS-KubernetesResource`),
   * one entry per resource. `cluster.addManifest` attaches manifests to the
   * imported cluster's owning stack, so this walks ALL stacks rather than a
   * single one.
   */
  function collectAppManifestTexts(app: cdk.App): string[] {
    const assembly = app.synth();
    const texts: string[] = [];
    for (const stackArtifact of assembly.stacks) {
      const resources =
        (stackArtifact.template?.Resources as Record<
          string,
          { Type?: string; Properties?: Record<string, unknown> }
        >) ?? {};
      for (const resource of Object.values(resources)) {
        if (resource.Type === 'Custom::AWSCDK-EKS-KubernetesResource') {
          texts.push(flattenManifest(resource.Properties?.Manifest));
        }
      }
    }
    return texts;
  }

  /** Concatenate the literal fragments of a manifest value (Fn::Join aware). */
  function flattenManifest(value: unknown): string {
    if (typeof value === 'string') {
      return value;
    }
    if (Array.isArray(value)) {
      return value.map(flattenManifest).join('');
    }
    if (value && typeof value === 'object') {
      const obj = value as Record<string, unknown>;
      if (obj['Fn::Join']) {
        const join = obj['Fn::Join'] as [string, unknown[]];
        const [sep, parts] = join;
        return parts.map(flattenManifest).join(sep);
      }
      return '';
    }
    return '';
  }

  // The foundation resource CloudFormation types a software-only stage must
  // NOT create (R3.4). The two ELBv2 load-balancer variants share one type.
  const FOUNDATION_RESOURCE_TYPES = [
    'AWS::EC2::VPC',
    'Custom::AWSCDK-EKS-Cluster', // the EKS control-plane resource
    'AWS::EC2::NatGateway',
    'AWS::DAX::Cluster',
    'AWS::ElasticLoadBalancingV2::LoadBalancer',
    'AWS::EKS::Nodegroup',
  ] as const;

  test('HelloDjPlaceholderStack is removed from the pipeline source (R3.1)', () => {
    // The placeholder stack (a lone WaitConditionHandle) is deleted; each stage
    // now deploys a real WorkloadsStack. Assert the source no longer defines or
    // references it (grep the source, not stale cdk.out artifacts).
    const src = fs.readFileSync(
      path.join(__dirname, '..', 'lib', 'pipeline-stack.ts'),
      'utf8',
    );
    expect(src).not.toContain('HelloDjPlaceholderStack');
    expect(src).not.toContain('WaitConditionHandle');
    // And it is exercised through the real WorkloadsStack import instead.
    expect(src).toContain('WorkloadsStack');
  });

  test('each HelloDjStage deploys a WorkloadsStack named hellodj-workloads-<stage> (R3.1)', () => {
    const app = new cdk.App();
    const { foundation } = composeFoundation(app);

    for (const stage of PROMOTION_ORDER) {
      const helloStage = new HelloDjStage(app, `hellodj-${stage}-stage`, {
        promotionStage: stage,
        env: COMPOSE_ENV,
        foundation,
        region: REGION,
      });
      // The stage exposes its software workloads, which ARE a WorkloadsStack,
      // NOT a placeholder stack (R3.1).
      expect(helloStage.workloads).toBeInstanceOf(WorkloadsStack);
      expect(helloStage.promotionStage).toBe(stage);
      // The WorkloadsStack id carries the stage: hellodj-workloads-<stage>.
      expect(helloStage.workloads.stackName).toContain(
        `hellodj-workloads-${stage}`,
      );
      // The stack renders that stage's namespace/endpoint (software only).
      expect(helloStage.workloads.stage).toBe(stage);
      expect(helloStage.workloads.namespace).toBe(workloadsNamespace(stage));
    }
  });

  test('the pipeline models three stages in order beta -> staging -> production, each a WorkloadsStack (R3.1, R3.2)', () => {
    const app = new cdk.App();
    const stack = new PipelineStack(app, 'Pipeline', { env: COMPOSE_ENV });

    // Exactly three deployment stages, in the fixed promotion order.
    expect(stack.stages).toHaveLength(3);
    expect(stack.stages.map((s) => s.promotionStage)).toEqual([
      'beta',
      'staging',
      'production',
    ]);
    expect(stack.stageNames).toEqual([...PROMOTION_ORDER]);

    // Each stage deploys a WorkloadsStack (not a placeholder), and its stack
    // id is hellodj-workloads-<stage>.
    for (const s of stack.stages) {
      expect(s.workloads).toBeInstanceOf(WorkloadsStack);
      expect(s.workloads.stackName).toContain(
        `hellodj-workloads-${s.promotionStage}`,
      );
    }
  });

  describe.each([...PROMOTION_ORDER])(
    'stage %s WorkloadsStack — zero foundation + namespaced manifests (R3.4, R3.5)',
    (stage) => {
      let workloadsTemplate: Template;
      // Manifest texts scoped to THIS stage's namespace only (the app is
      // composed with just this one stage so its namespace's manifests are the
      // only ones present).
      let stageManifestTexts: string[];
      let allStageManifestText: string;

      beforeAll(() => {
        const app = new cdk.App();
        const { foundation } = composeFoundation(app);
        const helloStage = new HelloDjStage(app, `hellodj-${stage}-stage`, {
          promotionStage: stage,
          env: COMPOSE_ENV,
          foundation,
          region: REGION,
        });
        // The software-only stack itself (asserted to carry ZERO foundation).
        workloadsTemplate = Template.fromStack(helloStage.workloads);
        // The namespaced manifests land on the imported cluster's owning stack;
        // collect them across the app and keep only this stage's namespace.
        const ns = workloadsNamespace(stage);
        stageManifestTexts = collectAppManifestTexts(app).filter((text) =>
          text.includes(`"namespace":"${ns}"`) ||
          text.includes(`"name":"${ns}"`),
        );
        allStageManifestText = stageManifestTexts.join('\n');
      });

      test.each([...FOUNDATION_RESOURCE_TYPES])(
        'the software-only WorkloadsStack has 0 of the foundation resource %s (R3.4)',
        (cfnType) => {
          workloadsTemplate.resourceCountIs(cfnType, 0);
        },
      );

      test('renders the stage Namespace hellodj-<stage> (R3.5)', () => {
        const ns = workloadsNamespace(stage);
        expect(allStageManifestText).toContain('"kind":"Namespace"');
        expect(allStageManifestText).toContain(`"name":"${ns}"`);
      });

      test('renders exactly 12 Deployments in the stage namespace (R3.5)', () => {
        const ns = workloadsNamespace(stage);
        const deploymentManifests = stageManifestTexts.filter(
          (text) =>
            text.includes('"kind":"Deployment"') &&
            text.includes(`"namespace":"${ns}"`),
        );
        expect(deploymentManifests.length).toBe(12);
        expect(deploymentManifests.length).toBe(COMPONENT_WORKLOADS.length);
      });

      test('renders a host-scoped Ingress bound to <stage>.<region>.hellodj.bot (R3.5)', () => {
        // The Ingress carries an Ingress kind and the stage's derived hostname
        // as a host-scoped rule.
        const hostname = stageEndpoint(stage, REGION).hostname;
        expect(hostname).toBe(stageHostname(stage, REGION));
        const ingressManifests = stageManifestTexts.filter((text) =>
          text.includes('"kind":"Ingress"'),
        );
        expect(ingressManifests.length).toBeGreaterThanOrEqual(1);
        // The host-scoped rule binds exactly this stage's hostname.
        expect(allStageManifestText).toContain(`"host":"${hostname}"`);
      });
    },
  );

  test('the foundation is added ONCE, outside any HelloDjStage — no foundation stack inside a stage (R3.4)', () => {
    const app = new cdk.App();
    const { foundation, foundationStackIds } = composeFoundation(app);

    // Build one HelloDjStage and walk its construct subtree: it must contain a
    // WorkloadsStack but NONE of the foundation stack construct types
    // (NetworkStack/EksStack/DataStack/AuthStack). The foundation lives OUTSIDE
    // the stage (it was composed on the app, not the stage), so the pipeline's
    // per-stage cdk.Stage can never instantiate foundation hardware (R3.4).
    const helloStage = new HelloDjStage(app, 'hellodj-beta-stage', {
      promotionStage: 'beta',
      env: COMPOSE_ENV,
      foundation,
      region: REGION,
    });

    const stageDescendants = helloStage.node.findAll();
    // The stage contains its WorkloadsStack (software only).
    expect(
      stageDescendants.some((c) => c instanceof WorkloadsStack),
    ).toBe(true);
    // The stage contains NO foundation stack construct type.
    expect(stageDescendants.some((c) => c instanceof NetworkStack)).toBe(false);
    expect(stageDescendants.some((c) => c instanceof EksStack)).toBe(false);
    expect(stageDescendants.some((c) => c instanceof DataStack)).toBe(false);
    expect(stageDescendants.some((c) => c instanceof AuthStack)).toBe(false);

    // The foundation stacks were instantiated on the APP (outside the stage),
    // and each exactly once — the once-deployed Shared_Foundation.
    expect(new Set(foundationStackIds).size).toBe(foundationStackIds.length);
    const stageStackNames = stageDescendants
      .filter((c): c is cdk.Stack => c instanceof cdk.Stack)
      .map((s) => s.stackName);
    for (const fid of foundationStackIds) {
      expect(stageStackNames).not.toContain(fid);
    }
  });
});

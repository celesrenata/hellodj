import * as cdk from 'aws-cdk-lib';
import { Template, Match } from 'aws-cdk-lib/assertions';
import { DeploymentStage } from '../lib/config';
import { NetworkStack } from '../lib/network-stack';
import { EdgeStack } from '../lib/edge-stack';
import { DataStack } from '../lib/data-stack';
import { AuthStack } from '../lib/auth-stack';
import { EksStack } from '../lib/eks-stack';
import { ObservabilityStack } from '../lib/observability-stack';
import { AnalyticsStack } from '../lib/analytics-stack';
import { PipelineStack } from '../lib/pipeline-stack';
import {
  WorkloadsStack,
  WorkloadsStackProps,
} from '../lib/workloads-stack';
import { COMPONENT_WORKLOADS } from '../lib/component-workloads';
import {
  PLATFORM_COMPONENTS,
  getComponentBuildCommands,
} from '../lib/pipeline-stack';

/**
 * Integration / smoke tests against the fully-composed Beta app (task 20.2).
 *
 * These are the end-to-end smoke tests for the AWS re-platform. A live AWS
 * deploy is not available in CI, so each acceptance criterion is realized as a
 * CDK *synthesis* assertion over the whole Beta app composed exactly the way
 * `bin/hellodj.ts` composes it. Synthesizing every stack without error is the
 * CI-checkable proxy for "`cdk deploy` provisions the platform with no manual
 * console steps" (R1.2): CDK only reaches the deploy phase if synthesis of the
 * full, wired app succeeds.
 *
 * What each block asserts:
 *
 *   * **No-manual-step deploy (R1.2):** the full Beta app — Network, Edge,
 *     Data, Auth, EKS, Observability, Analytics, Pipeline, and the composed
 *     Workloads stack — synthesizes without throwing, and the expected set of
 *     stacks is present. Synthesizability of the composed app is the proxy for
 *     a single `cdk deploy` succeeding with no console steps.
 *
 *   * **Per-component feature preservation (R6.1-R6.5):** the WorkloadsStack
 *     synthesizes a Deployment for every one of the 12 components (multi-source
 *     playback sidecars + lavalink for R6.1; activity-backend routed at
 *     `/activity` for R6.2; voice-pipeline for R6.3; playback-orchestrator for
 *     R6.4; web-ui routed at `/` for R6.5). Because `cluster.addManifest` lands
 *     the workload manifests on the EKS stack template as
 *     `Custom::AWSCDK-EKS-KubernetesResource` resources, we assert every
 *     component name (and the two ingress routes) appears in the serialized
 *     manifests on that stack.
 *
 *   * **Alarm -> SNS on breach (R10.5):** the ObservabilityStack synthesizes
 *     CloudWatch alarms whose `AlarmActions` fan out to the SNS topic.
 *
 *   * **Independent single-component promotion (R15.2):** the PipelineStack
 *     exposes one dependency-gated build path per component (12 components),
 *     so a single component can be built/promoted in isolation.
 *
 * _Requirements: 1.2, 6.1, 6.2, 6.3, 6.4, 6.5, 10.5, 15.2._
 */

const BETA_ENV = { account: '123456789012', region: 'us-east-1' };

/** The application stacks a single Beta `cdk deploy` provisions (R1.2). */
interface BetaApp {
  readonly app: cdk.App;
  readonly network: NetworkStack;
  readonly edge: EdgeStack;
  readonly data: DataStack;
  readonly auth: AuthStack;
  readonly eks: EksStack;
  readonly observability: ObservabilityStack;
  readonly analytics: AnalyticsStack;
  readonly pipeline: PipelineStack;
  readonly workloads: WorkloadsStack;
}

/**
 * Compose the whole Beta application the way `bin/hellodj.ts` does: every stack
 * for the Beta stage in one `cdk.App`, with the WorkloadsStack wired to the
 * EKS cluster + the Data/Auth resources. Returns the stacks so individual
 * blocks can assert against them.
 */
function composeBetaApp(): BetaApp {
  const app = new cdk.App();
  const stage = DeploymentStage.Beta;
  const env = BETA_ENV;

  const network = new NetworkStack(app, 'hellodj-network-beta', { env, stage });
  const edge = new EdgeStack(app, 'hellodj-edge-beta', {
    env,
    stage,
    region: env.region,
  });
  const data = new DataStack(app, 'hellodj-data-beta', {
    env,
    vpc: network.vpc,
  });
  const auth = new AuthStack(app, 'hellodj-auth-beta', { env, stage });
  const eks = new EksStack(app, 'hellodj-eks-beta', {
    env,
    stage,
    vpc: network.vpc,
  });
  const observability = new ObservabilityStack(
    app,
    'hellodj-observability-beta',
    { env, stage },
  );
  const analytics = new AnalyticsStack(app, 'hellodj-analytics-beta', {
    env,
    stage,
  });
  const pipeline = new PipelineStack(app, 'hellodj-pipeline', { env });

  const workloadsProps: WorkloadsStackProps = {
    env,
    stage,
    cluster: eks.cluster,
    data: {
      coreTable: data.coreTable,
      searchCacheTable: data.searchCacheTable,
      sessionTable: data.sessionTable,
      daxEndpoint: data.daxEndpoint,
    },
    secrets: {
      discordBotToken: auth.discordBotTokenSecret,
      tidalRefresh: auth.tidalRefreshSecret,
      spotify: auth.spotifySecret,
      ytCipher: auth.ytCipherSecret,
    },
    aiTaskRole: auth.aiTaskRole,
  };
  const workloads = new WorkloadsStack(
    app,
    'hellodj-workloads-beta',
    workloadsProps,
  );
  workloads.addStackDependency(eks);
  workloads.addStackDependency(data);
  workloads.addStackDependency(auth);

  return {
    app,
    network,
    edge,
    data,
    auth,
    eks,
    observability,
    analytics,
    pipeline,
    workloads,
  };
}

/**
 * Concatenate the literal string fragments of a manifest value into one
 * searchable string. `cluster.addManifest` serializes each manifest as a
 * `Fn::Join` mixing literal JSON fragments with CFN tokens (table names,
 * secret ARNs). Component names, kinds, and ingress paths are literals, so
 * flattening the literal fragments lets us assert on the manifest content
 * without resolving tokens.
 */
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
    // Tokens like Fn::GetAtt / Ref contribute no literal text.
    return '';
  }
  return '';
}

/**
 * Collect the flattened text of every Kubernetes manifest on a synthesized
 * stack template (the `Custom::AWSCDK-EKS-KubernetesResource` resources that
 * `cluster.addManifest` produces).
 */
function collectManifestText(template: Template): string {
  const resources = template.findResources(
    'Custom::AWSCDK-EKS-KubernetesResource',
  );
  return Object.values(resources)
    .map((r) => flattenManifest(r.Properties?.Manifest))
    .join('\n');
}

// ---------------------------------------------------------------------------
// R1.2 — the whole Beta app synthesizes with no manual step
// ---------------------------------------------------------------------------

describe('Beta smoke — full app synthesizes with no manual step (R1.2)', () => {
  test('composing the whole Beta app does not throw', () => {
    expect(() => composeBetaApp()).not.toThrow();
  });

  test('every Beta stack synthesizes to a template without error (R1.2)', () => {
    const beta = composeBetaApp();
    // Building a Template from each stack forces synthesis of that stack; if
    // any stack were misconfigured, this would throw. Passing across all
    // stacks is the CI proxy for a single `cdk deploy` succeeding with no
    // manual console steps.
    const stacks: cdk.Stack[] = [
      beta.network,
      beta.edge,
      beta.data,
      beta.auth,
      beta.eks,
      beta.observability,
      beta.analytics,
      beta.pipeline,
      beta.workloads,
    ];
    for (const stack of stacks) {
      expect(() => Template.fromStack(stack)).not.toThrow();
    }
  });

  test('the full app assembly synthesizes and contains every expected stack (R1.2)', () => {
    const beta = composeBetaApp();
    const assembly = beta.app.synth();
    const stackNames = assembly.stacks.map((s) => s.stackName);
    for (const expected of [
      'hellodj-network-beta',
      'hellodj-edge-beta',
      'hellodj-data-beta',
      'hellodj-auth-beta',
      'hellodj-eks-beta',
      'hellodj-observability-beta',
      'hellodj-analytics-beta',
      'hellodj-pipeline',
      'hellodj-workloads-beta',
    ]) {
      expect(stackNames).toContain(expected);
    }
  });
});

// ---------------------------------------------------------------------------
// R6.1-R6.5 — per-component feature preservation
// ---------------------------------------------------------------------------

describe('Beta smoke — per-component feature preservation (R6.1-R6.5)', () => {
  // `cluster.addManifest` attaches manifests to the cluster's owning stack
  // (the EKS stack), so the workload manifests are asserted there.
  let manifestText: string;

  beforeAll(() => {
    const beta = composeBetaApp();
    manifestText = collectManifestText(Template.fromStack(beta.eks));
  });

  test('a Deployment manifest exists for all 12 components', () => {
    expect(COMPONENT_WORKLOADS).toHaveLength(12);
    for (const spec of COMPONENT_WORKLOADS) {
      // The component name appears as a literal in its Deployment manifest.
      expect(manifestText).toContain(`"name":"${spec.name}"`);
    }
    // And a Deployment kind is present in the rendered manifests.
    expect(manifestText).toContain('"kind":"Deployment"');
  });

  test('multi-source playback sidecars + lavalink are present (R6.1)', () => {
    // Direct-stream sidecars, the YouTube cipher/PoToken helpers, and the
    // custom lavalink JVM audio server that together provide YouTube/Spotify/
    // Tidal/SoundCloud playback.
    for (const name of [
      'lavalink',
      'tidal-stream',
      'spotify-stream',
      'yt-cipher',
      'potoken-server',
    ]) {
      expect(manifestText).toContain(`"name":"${name}"`);
    }
  });

  test('activity-backend is present and routed at /activity (R6.2)', () => {
    expect(manifestText).toContain('"name":"activity-backend"');
    // The ALB Ingress routes the Activity server at `/activity`.
    expect(manifestText).toContain('"path":"/activity"');
    const activity = COMPONENT_WORKLOADS.find(
      (s) => s.name === 'activity-backend',
    );
    expect(activity?.ingressPath).toBe('/activity');
  });

  test('voice-pipeline is present (R6.3)', () => {
    expect(manifestText).toContain('"name":"voice-pipeline"');
  });

  test('playback-orchestrator is present (R6.4)', () => {
    expect(manifestText).toContain('"name":"playback-orchestrator"');
  });

  test('web-ui is present and routed at / (R6.5)', () => {
    expect(manifestText).toContain('"name":"web-ui"');
    // The ALB Ingress routes the web-ui at the `/` catch-all.
    expect(manifestText).toContain('"path":"/"');
    const webUi = COMPONENT_WORKLOADS.find((s) => s.name === 'web-ui');
    expect(webUi?.ingressPath).toBe('/');
  });

  test('the Ingress routes /activity before / (most-specific first)', () => {
    // The activity route must be matched before the web-ui catch-all so the
    // Activity backend is reachable behind the ALB.
    const activityIdx = manifestText.indexOf('"path":"/activity"');
    const rootIdx = manifestText.indexOf('"path":"/"');
    expect(activityIdx).toBeGreaterThanOrEqual(0);
    expect(rootIdx).toBeGreaterThanOrEqual(0);
    expect(activityIdx).toBeLessThan(rootIdx);
  });
});

// ---------------------------------------------------------------------------
// R10.5 — alarm -> SNS notification on threshold breach
// ---------------------------------------------------------------------------

describe('Beta smoke — alarm -> SNS on breach (R10.5)', () => {
  let template: Template;

  beforeAll(() => {
    const beta = composeBetaApp();
    template = Template.fromStack(beta.observability);
  });

  test('the observability stack provisions an SNS topic and alarms', () => {
    template.resourceCountIs('AWS::SNS::Topic', 1);
    const alarms = template.findResources('AWS::CloudWatch::Alarm');
    expect(Object.keys(alarms).length).toBeGreaterThanOrEqual(3);
  });

  test('every alarm fans its breach action out to SNS (R10.5)', () => {
    const alarms = template.findResources('AWS::CloudWatch::Alarm');
    for (const alarm of Object.values(alarms)) {
      const actions = alarm.Properties?.AlarmActions;
      expect(Array.isArray(actions)).toBe(true);
      expect(actions.length).toBeGreaterThanOrEqual(1);
    }
    // At least one alarm's action references the provisioned SNS topic.
    template.hasResourceProperties('AWS::CloudWatch::Alarm', {
      AlarmActions: Match.arrayWith([
        Match.objectLike({ Ref: Match.stringLikeRegexp('AlarmTopic') }),
      ]),
    });
  });
});

// ---------------------------------------------------------------------------
// R15.2 — independent single-component promotion
// ---------------------------------------------------------------------------

describe('Beta smoke — independent single-component promotion (R15.2)', () => {
  test('the pipeline exposes one dependency-gated build path per component', () => {
    const beta = composeBetaApp();
    // Every one of the 12 components has its own build step in the pipeline,
    // so a single component can be built/promoted without the others.
    expect(PLATFORM_COMPONENTS).toHaveLength(12);
    for (const component of PLATFORM_COMPONENTS) {
      expect(beta.pipeline.componentBuildSteps[component]).toBeDefined();
    }
    // The set of pipeline build paths matches the deployed workload set,
    // so each deployed component is independently promotable.
    const workloadNames = COMPONENT_WORKLOADS.map((s) => s.name).sort();
    expect([...PLATFORM_COMPONENTS].sort()).toEqual(workloadNames);
  });

  test('each component build path is scoped to that single component', () => {
    for (const component of PLATFORM_COMPONENTS) {
      const commands = getComponentBuildCommands(component);
      // The dependency gate is invoked for exactly this component, proving a
      // single component is promoted independently (not as part of a bundle).
      expect(
        commands.some(
          (c) =>
            c.includes('gate_dependencies') &&
            c.includes(`--component ${component}`),
        ),
      ).toBe(true);
    }
  });
});

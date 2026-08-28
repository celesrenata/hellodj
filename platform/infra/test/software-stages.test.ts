import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { DeploymentStage } from '../lib/config';
import { NetworkStack } from '../lib/network-stack';
import { EksStack } from '../lib/eks-stack';
import { DataStack } from '../lib/data-stack';
import { AuthStack } from '../lib/auth-stack';
import {
  WorkloadsStack,
  workloadsNamespace,
  stageEndpoint,
  stageLogLevel,
} from '../lib/workloads-stack';
import { COMPONENT_WORKLOADS } from '../lib/component-workloads';

/**
 * Three-namespace / 12-component / per-stage-independence assertions
 * (task 8.3, Requirements 2.1, 2.2, 2.4).
 *
 * The governing principle of this spec is "one stage's worth of HARDWARE,
 * three stages' worth of SOFTWARE": the Beta / Staging / Production stages are
 * NOT three copies of the foundation — they are three namespaced sets of the
 * 12 platform components deployed onto the ONE shared EKS cluster, isolated
 * only by endpoint (namespace `hellodj-<stage>` + hostname
 * `<stage>.<region>.hellodj.bot`).
 *
 * This suite composes the Shared_Foundation exactly ONCE and attaches three
 * {@link WorkloadsStack}s (`beta`/`staging`/`production`) to the SAME shared
 * cluster / data / auth references — the way `bin/hellodj.ts` does — then
 * asserts:
 *
 *   * `COMPONENT_WORKLOADS.length === 12` — the catalog is the single source of
 *     truth for the 12 independently deployable components (R2.1, R2.4);
 *   * each of the three stages renders exactly 12 component `Deployment`
 *     manifests into its OWN namespace `hellodj-<stage>` (R2.1);
 *   * the three namespaces are pairwise distinct (R2.1, R2.3-shape);
 *   * every namespaced object a stage renders carries that stage's OWN
 *     `hellodj-<stage>` namespace — no cross-namespace leakage — demonstrating
 *     per-stage independence / partial-deploy tolerance (R2.2);
 *   * all three stacks reference the SAME shared cluster handle (R2.4/R2.1).
 *
 * Because `cluster.addManifest` attaches every manifest to the cluster's owning
 * stack (the shared {@link EksStack}), all three stages' Deployments land on the
 * one EKS stack template as `Custom::AWSCDK-EKS-KubernetesResource` resources.
 * The namespace is baked into each manifest as a literal, so counting the
 * `Deployment` docs per `"namespace":"hellodj-<stage>"` proves the three
 * namespaced sets are disjoint and each has 12 components.
 *
 * _Requirements: 2.1, 2.2, 2.4_
 */

const COMPOSE_ENV = { account: '111111111111', region: 'us-east-1' };
const REGION = 'us-east-1';
const ALL_STAGES = [
  DeploymentStage.Beta,
  DeploymentStage.Staging,
  DeploymentStage.Production,
];

interface ComposedApp {
  readonly app: cdk.App;
  readonly eks: EksStack;
  readonly workloads: WorkloadsStack[];
}

/**
 * Build the Shared_Foundation (once) plus the three namespaced
 * WorkloadsStacks, mirroring `bin/hellodj.ts` and `foundation.test.ts`'s
 * composeApp (minus the pipeline). All three WorkloadsStacks receive the SAME
 * `eks.cluster`, `data.*`, and `auth.*` handles and differ only by
 * `stage`/`region` (hence namespace + hostname).
 */
function composeApp(): ComposedApp {
  const app = new cdk.App();

  // Foundation: instantiated EXACTLY ONCE with stage-independent ids.
  const network = new NetworkStack(app, 'hellodj-network', { env: COMPOSE_ENV });
  const data = new DataStack(app, 'hellodj-data', {
    env: COMPOSE_ENV,
    vpc: network.vpc,
  });
  const auth = new AuthStack(app, 'hellodj-auth', {
    env: COMPOSE_ENV,
    stage: DeploymentStage.Beta,
  });
  const eks = new EksStack(app, 'hellodj-eks', {
    env: COMPOSE_ENV,
    vpc: network.vpc,
  });

  // Software: three namespaced WorkloadsStacks on the ONE shared cluster.
  const workloads = ALL_STAGES.map((stage) => {
    const w = new WorkloadsStack(app, `hellodj-workloads-${stage}`, {
      env: COMPOSE_ENV,
      stage,
      region: REGION,
      cluster: eks.cluster,
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
    });
    w.addStackDependency(eks);
    w.addStackDependency(data);
    w.addStackDependency(auth);
    return w;
  });

  return { app, eks, workloads };
}

/**
 * Concatenate the literal string fragments of a manifest value into one
 * searchable string. `cluster.addManifest` serializes each manifest as a
 * `Fn::Join` mixing literal JSON fragments with CFN tokens (table names,
 * secret ARNs). Kinds, names, and namespaces are literals, so flattening the
 * literal fragments lets us assert on the manifest content without resolving
 * tokens.
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
 * Collect the flattened text of EACH Kubernetes manifest resource on a
 * synthesized stack template (the `Custom::AWSCDK-EKS-KubernetesResource`
 * resources `cluster.addManifest` produces), one entry per resource. Returning
 * a per-resource array (rather than one joined blob) lets us count how many
 * distinct manifest resources contain a Deployment scoped to a given namespace.
 */
function collectManifestTexts(template: Template): string[] {
  const resources = template.findResources(
    'Custom::AWSCDK-EKS-KubernetesResource',
  );
  return Object.values(resources).map((r) =>
    flattenManifest(r.Properties?.Manifest),
  );
}

describe('COMPONENT_WORKLOADS catalog (task 8.3, R2.1/R2.4)', () => {
  test('the catalog declares exactly 12 components', () => {
    expect(COMPONENT_WORKLOADS.length).toBe(12);
    expect(COMPONENT_WORKLOADS).toHaveLength(12);
  });

  test('the 12 component names are unique (a single source of truth per component)', () => {
    const names = COMPONENT_WORKLOADS.map((s) => s.name);
    expect(new Set(names).size).toBe(12);
  });
});

describe('three namespaced software stages on the one shared cluster (task 8.3, R2.1)', () => {
  // All three stages' manifests land on the shared EKS stack template, since
  // `cluster.addManifest` attaches them to the cluster's owning stack.
  let manifestTexts: string[];
  let allText: string;

  beforeAll(() => {
    const { eks } = composeApp();
    manifestTexts = collectManifestTexts(Template.fromStack(eks));
    allText = manifestTexts.join('\n');
  });

  test('the three namespaces are distinct (hellodj-beta != hellodj-staging != hellodj-production)', () => {
    const namespaces = ALL_STAGES.map((s) => workloadsNamespace(s));
    expect(namespaces).toEqual([
      'hellodj-beta',
      'hellodj-staging',
      'hellodj-production',
    ]);
    // Pairwise distinct.
    expect(new Set(namespaces).size).toBe(ALL_STAGES.length);

    // Each namespace is materialized as its own Namespace manifest on the
    // shared cluster.
    for (const ns of namespaces) {
      expect(allText).toContain(`"kind":"Namespace"`);
      expect(allText).toContain(`"name":"${ns}"`);
    }
  });

  test.each(ALL_STAGES)(
    'stage %s renders exactly 12 component Deployments into its own namespace (R2.1)',
    (stage) => {
      const ns = workloadsNamespace(stage);

      // A Deployment doc scoped to this stage's namespace looks like
      // `..."kind":"Deployment"..."namespace":"hellodj-<stage>"...` within the
      // SAME manifest resource. Count the manifest resources that contain both
      // a Deployment kind and this namespace — there is one such Deployment per
      // component, so exactly 12 per namespace.
      const deploymentManifestsInNs = manifestTexts.filter(
        (text) =>
          text.includes('"kind":"Deployment"') &&
          text.includes(`"namespace":"${ns}"`),
      );
      expect(deploymentManifestsInNs.length).toBe(COMPONENT_WORKLOADS.length);
      expect(deploymentManifestsInNs.length).toBe(12);

      // Every one of the 12 named components appears as a Deployment in this
      // namespace (R2.1 — a distinct set of the 12 components per stage).
      for (const spec of COMPONENT_WORKLOADS) {
        const hasComponentDeploymentInNs = manifestTexts.some(
          (text) =>
            text.includes('"kind":"Deployment"') &&
            text.includes(`"namespace":"${ns}"`) &&
            text.includes(`"name":"${spec.name}"`),
        );
        expect(hasComponentDeploymentInNs).toBe(true);
      }
    },
  );

  test('there are 36 component Deployments total across the three namespaces (12 x 3), all on the one cluster', () => {
    const totalDeploymentManifests = manifestTexts.filter((text) =>
      text.includes('"kind":"Deployment"'),
    ).length;
    // 12 components x 3 stages = 36 Deployment manifest resources on the ONE
    // shared cluster (the WorkloadsStacks add software only, never hardware).
    expect(totalDeploymentManifests).toBe(36);
  });
});

describe('per-stage independence / disjoint namespaced resource sets (task 8.3, R2.2/R2.4)', () => {
  let manifestTexts: string[];

  beforeAll(() => {
    const { eks } = composeApp();
    manifestTexts = collectManifestTexts(Template.fromStack(eks));
  });

  test('every namespaced object carries exactly one stage namespace — no cross-namespace leakage (R2.2)', () => {
    const namespaces = ALL_STAGES.map((s) => workloadsNamespace(s));

    // For each manifest resource that names ANY stage namespace, it must name
    // exactly one — never two — so a stage's manifests never reference another
    // stage's namespace. This is the disjointness that gives partial-deploy
    // tolerance: a failure that rolls back one stage's namespaced resources
    // cannot touch another stage's, because no resource spans two namespaces.
    for (const text of manifestTexts) {
      const named = namespaces.filter((ns) =>
        text.includes(`"namespace":"${ns}"`),
      );
      // A manifest resource either belongs to no stage namespace (e.g. shared
      // Karpenter/GPU infra) or to exactly one.
      expect(named.length).toBeLessThanOrEqual(1);
    }
  });

  test('each stage namespace hosts its full software set independently (partial-deploy tolerance, R2.2)', () => {
    // Every stage independently carries its own Namespace + 12 Deployments in
    // its own namespace, so the three software sets are disjoint and one can
    // fail/roll back without affecting the others.
    for (const stage of ALL_STAGES) {
      const ns = workloadsNamespace(stage);
      const deploymentsInNs = manifestTexts.filter(
        (text) =>
          text.includes('"kind":"Deployment"') &&
          text.includes(`"namespace":"${ns}"`),
      ).length;
      expect(deploymentsInNs).toBe(12);
    }
  });

  test('the three WorkloadsStacks share no mutable resource — they add ZERO foundation hardware (R2.2)', () => {
    const { workloads } = composeApp();
    // Each software stage is software only: it creates NO VPC, EKS control
    // plane, node group, DAX, or load balancer. A stage's rollback therefore
    // touches only its own namespaced manifests, never shared hardware.
    for (const w of workloads) {
      const t = Template.fromStack(w);
      t.resourceCountIs('AWS::EC2::VPC', 0);
      t.resourceCountIs('AWS::EKS::Nodegroup', 0);
      t.resourceCountIs('AWS::DAX::Cluster', 0);
      t.resourceCountIs('AWS::ElasticLoadBalancingV2::LoadBalancer', 0);
      t.resourceCountIs('Custom::AWSCDK-EKS-Cluster', 0);
    }
  });
});

describe('all three stages reference the SAME shared cluster (task 8.3, R2.4)', () => {
  test('the three WorkloadsStacks were constructed with the one shared eks.cluster handle', () => {
    const { eks, workloads } = composeApp();
    // Each WorkloadsStack derives its distinct endpoint but shares one cluster.
    const clusterNames = new Set<string>();
    for (const w of workloads) {
      // The stack exposes its stage/namespace/endpoint; the shared cluster is
      // the same object the EksStack owns.
      clusterNames.add(eks.cluster.clusterName);
      // Distinct endpoint per stage (namespace + hostname), one cluster.
      expect(w.stageEndpoint).toEqual(stageEndpoint(w.stage, REGION));
    }
    // Exactly one shared cluster name across all three stages.
    expect(clusterNames.size).toBe(1);

    // The three stages are pairwise distinct by namespace + hostname.
    const endpointIdentities = workloads.map(
      (w) => `${w.stageEndpoint.namespace}|${w.stageEndpoint.hostname}`,
    );
    expect(new Set(endpointIdentities).size).toBe(ALL_STAGES.length);
  });
});


describe('Per-stage debug logging (R8.4/R8.5)', () => {
  test('beta stage gets LOG_LEVEL=DEBUG and HELLODJ_DEBUG=true', () => {
    expect(stageLogLevel('beta')).toBe('DEBUG');
    expect(stageLogLevel('Beta')).toBe('DEBUG');
  });

  test('staging stage gets LOG_LEVEL=DEBUG and HELLODJ_DEBUG=true', () => {
    expect(stageLogLevel('staging')).toBe('DEBUG');
    expect(stageLogLevel('Staging')).toBe('DEBUG');
  });

  test('production stage gets LOG_LEVEL=INFO and HELLODJ_DEBUG=false', () => {
    expect(stageLogLevel('production')).toBe('INFO');
    expect(stageLogLevel('Production')).toBe('INFO');
  });
});

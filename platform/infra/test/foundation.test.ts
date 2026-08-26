import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import {
  FOUNDATION_SINGLETON_TYPES,
  LOAD_BALANCER_TYPES,
  assertFoundationSingleton,
} from '../lib/foundation';
import { DeploymentStage } from '../lib/config';
import { NetworkStack } from '../lib/network-stack';
import { EksStack } from '../lib/eks-stack';
import { DataStack } from '../lib/data-stack';
import { AuthStack } from '../lib/auth-stack';
import { WorkloadsStack } from '../lib/workloads-stack';

/**
 * Unit tests for the `Foundation_Singleton_Invariant` helper (task 1.2).
 *
 * These exercise two concerns:
 *
 *   1. {@link FOUNDATION_SINGLETON_TYPES} maps each foundation kind to the exact
 *      CloudFormation type string the design records (R1.7/R1.8), and
 *      {@link LOAD_BALANCER_TYPES} disambiguates the shared ALB from the shared
 *      NLB by the ELBv2 `Type` property (R1.5/R1.6).
 *   2. {@link assertFoundationSingleton} counts each foundation type across ALL
 *      synthesized templates: it PASSES on synthetic apps with 0 and 1 of a
 *      type (zero is permitted, R1.7) and THROWS — naming the duplicated type —
 *      on 2 of a type (R1.8), including ALB-vs-NLB disambiguation via `Type`.
 *
 * The counting logic is exercised with raw {@link cdk.CfnResource}s so each test
 * controls the exact count of a single foundation resource type in isolation,
 * independent of any real foundation stack's incidental resources.
 *
 * _Requirements: 1.7, 1.8_
 */

const TEST_ENV = { account: '111111111111', region: 'us-east-1' };

/**
 * Add `count` raw CloudFormation resources of `cfnType` to a stack, optionally
 * stamping each with a `Type` property (used to distinguish ALB vs NLB, both of
 * which share the `AWS::ElasticLoadBalancingV2::LoadBalancer` CloudFormation
 * type).
 */
function addResources(
  stack: cdk.Stack,
  idPrefix: string,
  cfnType: string,
  count: number,
  properties?: Record<string, unknown>,
): void {
  for (let i = 0; i < count; i += 1) {
    new cdk.CfnResource(stack, `${idPrefix}${i}`, {
      type: cfnType,
      properties,
    });
  }
}

describe('FOUNDATION_SINGLETON_TYPES (task 1.2, R1.7/R1.8)', () => {
  test('maps each foundation kind to the exact CloudFormation type string', () => {
    expect(FOUNDATION_SINGLETON_TYPES.vpc).toBe('AWS::EC2::VPC');
    expect(FOUNDATION_SINGLETON_TYPES.eks).toBe('Custom::AWSCDK-EKS-Cluster');
    expect(FOUNDATION_SINGLETON_TYPES.dax).toBe('AWS::DAX::Cluster');
    expect(FOUNDATION_SINGLETON_TYPES.nat).toBe('AWS::EC2::NatGateway');
    expect(FOUNDATION_SINGLETON_TYPES.nodegroup).toBe('AWS::EKS::Nodegroup');
    expect(FOUNDATION_SINGLETON_TYPES.loadBalancer).toBe(
      'AWS::ElasticLoadBalancingV2::LoadBalancer',
    );
  });

  test('disambiguates the ALB and NLB by the ELBv2 Type property', () => {
    // Both load balancers share one CloudFormation type; the `Type` property is
    // the only discriminator (R1.5 application, R1.6 network).
    expect(LOAD_BALANCER_TYPES.alb).toBe('application');
    expect(LOAD_BALANCER_TYPES.nlb).toBe('network');
  });
});

describe('assertFoundationSingleton counting (task 1.2, R1.7/R1.8)', () => {
  test('passes on an app with ZERO of a foundation type (R1.7)', () => {
    const app = new cdk.App();
    // A stack carrying only a non-foundation resource — zero VPCs, EKS, DAX, etc.
    const stack = new cdk.Stack(app, 'ZeroStack', { env: TEST_ENV });
    addResources(stack, 'Bucket', 'AWS::S3::Bucket', 1);

    expect(() => assertFoundationSingleton(app)).not.toThrow();
  });

  test('passes on an app with exactly ONE of each foundation type (R1.7)', () => {
    const app = new cdk.App();
    const stack = new cdk.Stack(app, 'OneEachStack', { env: TEST_ENV });
    addResources(stack, 'Vpc', FOUNDATION_SINGLETON_TYPES.vpc, 1);
    addResources(stack, 'Eks', FOUNDATION_SINGLETON_TYPES.eks, 1);
    addResources(stack, 'Dax', FOUNDATION_SINGLETON_TYPES.dax, 1);
    addResources(stack, 'Nat', FOUNDATION_SINGLETON_TYPES.nat, 1);
    addResources(stack, 'Ng', FOUNDATION_SINGLETON_TYPES.nodegroup, 1);
    addResources(stack, 'Alb', FOUNDATION_SINGLETON_TYPES.loadBalancer, 1, {
      Type: LOAD_BALANCER_TYPES.alb,
    });
    addResources(stack, 'Nlb', FOUNDATION_SINGLETON_TYPES.loadBalancer, 1, {
      Type: LOAD_BALANCER_TYPES.nlb,
    });

    expect(() => assertFoundationSingleton(app)).not.toThrow();
  });

  test('counts a foundation type across MULTIPLE stacks (still one total passes)', () => {
    // One VPC in each of two stacks would be two total; one VPC split across
    // stacks is still a single VPC overall. Here: exactly one VPC, in a second
    // stack, alongside a non-foundation resource in the first.
    const app = new cdk.App();
    const a = new cdk.Stack(app, 'StackA', { env: TEST_ENV });
    addResources(a, 'Bucket', 'AWS::S3::Bucket', 1);
    const b = new cdk.Stack(app, 'StackB', { env: TEST_ENV });
    addResources(b, 'Vpc', FOUNDATION_SINGLETON_TYPES.vpc, 1);

    expect(() => assertFoundationSingleton(app)).not.toThrow();
  });

  test('throws naming the VPC when TWO VPCs are synthesized (R1.8)', () => {
    const app = new cdk.App();
    const stack = new cdk.Stack(app, 'TwoVpcStack', { env: TEST_ENV });
    addResources(stack, 'Vpc', FOUNDATION_SINGLETON_TYPES.vpc, 2);

    expect(() => assertFoundationSingleton(app)).toThrow(
      FOUNDATION_SINGLETON_TYPES.vpc,
    );
    expect(() => assertFoundationSingleton(app)).toThrow(
      /Foundation_Singleton_Invariant violated/,
    );
  });

  test('detects a duplicate split ACROSS two stacks (R1.8)', () => {
    // Two DAX clusters in separate stacks — the naïve "three copies of the
    // hardware" failure mode — must still be caught by the whole-app count.
    const app = new cdk.App();
    const a = new cdk.Stack(app, 'DaxA', { env: TEST_ENV });
    addResources(a, 'Dax', FOUNDATION_SINGLETON_TYPES.dax, 1);
    const b = new cdk.Stack(app, 'DaxB', { env: TEST_ENV });
    addResources(b, 'Dax', FOUNDATION_SINGLETON_TYPES.dax, 1);

    expect(() => assertFoundationSingleton(app)).toThrow(
      FOUNDATION_SINGLETON_TYPES.dax,
    );
  });

  test.each([
    ['EKS control plane', FOUNDATION_SINGLETON_TYPES.eks],
    ['DAX cluster', FOUNDATION_SINGLETON_TYPES.dax],
    ['NAT gateway', FOUNDATION_SINGLETON_TYPES.nat],
    ['EKS node group', FOUNDATION_SINGLETON_TYPES.nodegroup],
  ])('throws naming the duplicated %s type on 2 of it (R1.8)', (_label, cfnType) => {
    const app = new cdk.App();
    const stack = new cdk.Stack(app, 'DupStack', { env: TEST_ENV });
    addResources(stack, 'Dup', cfnType, 2);

    expect(() => assertFoundationSingleton(app)).toThrow(cfnType);
  });
});

describe('assertFoundationSingleton ALB/NLB disambiguation (task 1.2, R1.5/R1.6)', () => {
  test('one ALB + one NLB (same CFN type, different Type prop) passes', () => {
    const app = new cdk.App();
    const stack = new cdk.Stack(app, 'OneAlbOneNlb', { env: TEST_ENV });
    addResources(stack, 'Alb', FOUNDATION_SINGLETON_TYPES.loadBalancer, 1, {
      Type: LOAD_BALANCER_TYPES.alb,
    });
    addResources(stack, 'Nlb', FOUNDATION_SINGLETON_TYPES.loadBalancer, 1, {
      Type: LOAD_BALANCER_TYPES.nlb,
    });

    // Two load-balancer resources of one CFN type, but one of each `Type`, is
    // the intended shared foundation — it must NOT be flagged as a duplicate.
    expect(() => assertFoundationSingleton(app)).not.toThrow();
  });

  test('throws on TWO ALBs (both Type: application) (R1.5/R1.8)', () => {
    const app = new cdk.App();
    const stack = new cdk.Stack(app, 'TwoAlbStack', { env: TEST_ENV });
    addResources(stack, 'Alb', FOUNDATION_SINGLETON_TYPES.loadBalancer, 2, {
      Type: LOAD_BALANCER_TYPES.alb,
    });

    expect(() => assertFoundationSingleton(app)).toThrow(
      FOUNDATION_SINGLETON_TYPES.loadBalancer,
    );
    // The error should identify the ALB variant specifically.
    expect(() => assertFoundationSingleton(app)).toThrow(
      new RegExp(`Type: ${LOAD_BALANCER_TYPES.alb}`),
    );
  });

  test('throws on TWO NLBs (both Type: network) (R1.6/R1.8)', () => {
    const app = new cdk.App();
    const stack = new cdk.Stack(app, 'TwoNlbStack', { env: TEST_ENV });
    addResources(stack, 'Nlb', FOUNDATION_SINGLETON_TYPES.loadBalancer, 2, {
      Type: LOAD_BALANCER_TYPES.nlb,
    });

    expect(() => assertFoundationSingleton(app)).toThrow(
      new RegExp(`Type: ${LOAD_BALANCER_TYPES.nlb}`),
    );
  });
});

/**
 * Duplicate-fails-synth assertion using a REAL foundation stack (task 5.5).
 *
 * The preceding suites drive the counting logic with raw
 * {@link cdk.CfnResource}s so each foundation type is controlled in isolation.
 * This suite instead builds a self-contained {@link cdk.App} with TWO real
 * {@link NetworkStack}s — each of which provisions an `AWS::EC2::VPC` (plus a
 * NAT gateway and the shared ALB/NLB) — to prove the end-to-end invariant on
 * genuine foundation hardware: a second copy of a Shared_Foundation stack makes
 * `assertFoundationSingleton` THROW at synth time, naming the duplicated
 * CloudFormation type, and produces no deployable application (R1.8).
 *
 * The app is constructed inside each test (its own `cdk.App`) so it never
 * conflicts with the whole-app composition assertions elsewhere in this file.
 *
 * _Requirements: 1.8_
 */
describe('assertFoundationSingleton on a duplicated real foundation stack (task 5.5, R1.8)', () => {
  test('THROWS naming AWS::EC2::VPC when TWO NetworkStacks are instantiated (R1.8)', () => {
    // A self-contained app with two real NetworkStacks. Each NetworkStack
    // provisions its own VPC (and NAT gateway + ALB + NLB), so the whole-app
    // count of AWS::EC2::VPC is two — a duplicated Shared_Foundation.
    const app = new cdk.App();
    new NetworkStack(app, 'FoundationNetworkA', { env: TEST_ENV });
    new NetworkStack(app, 'FoundationNetworkB', { env: TEST_ENV });

    // The invariant must fail synth, naming the duplicated CloudFormation type.
    expect(() => assertFoundationSingleton(app)).toThrow(
      FOUNDATION_SINGLETON_TYPES.vpc,
    );
    expect(() => assertFoundationSingleton(app)).toThrow(
      /Foundation_Singleton_Invariant violated/,
    );
    // And it must explicitly state that no deployable application is produced.
    expect(() => assertFoundationSingleton(app)).toThrow(
      /No deployable application is produced\./,
    );
  });

  test('the thrown error also names the other duplicated foundation types (NAT, ALB, NLB) (R1.8)', () => {
    // Two NetworkStacks duplicate more than just the VPC: each also provisions
    // a single NAT gateway, one ALB (Type: application), and one NLB
    // (Type: network). The whole-app count of each is therefore two, so the
    // error should enumerate all of them alongside the VPC.
    const app = new cdk.App();
    new NetworkStack(app, 'FoundationNetworkA', { env: TEST_ENV });
    new NetworkStack(app, 'FoundationNetworkB', { env: TEST_ENV });

    let thrown: Error | undefined;
    try {
      assertFoundationSingleton(app);
    } catch (err) {
      thrown = err as Error;
    }

    // A second NetworkStack must have failed synth (no deployable app).
    expect(thrown).toBeInstanceOf(Error);
    const message = thrown?.message ?? '';
    expect(message).toContain(FOUNDATION_SINGLETON_TYPES.vpc);
    expect(message).toContain(FOUNDATION_SINGLETON_TYPES.nat);
    // Both load-balancer variants are duplicated and each is named by its
    // disambiguating ELBv2 `Type` property (R1.5 application / R1.6 network).
    expect(message).toContain(
      `${FOUNDATION_SINGLETON_TYPES.loadBalancer} (Type: ${LOAD_BALANCER_TYPES.alb})`,
    );
    expect(message).toContain(
      `${FOUNDATION_SINGLETON_TYPES.loadBalancer} (Type: ${LOAD_BALANCER_TYPES.nlb})`,
    );
  });
});

// ---------------------------------------------------------------------------
// Whole-app foundation-singleton composition assertions (task 5.4)
// ---------------------------------------------------------------------------

/**
 * Whole-app foundation-singleton assertions (task 5.4, R1.1-R1.7).
 *
 * This suite builds the actual `Shared_Foundation` + three `Software_Stage`
 * composition the way `bin/hellodj.ts` does — the foundation stacks
 * (`NetworkStack`, `EksStack`, `DataStack`, `AuthStack`) instantiated EXACTLY
 * ONCE with stage-independent ids, and three `WorkloadsStack`s
 * (`beta`/`staging`/`production`) attached to the SAME shared cluster, data,
 * and auth references — then asserts the governing principle "one stage's worth
 * of HARDWARE, three stages' worth of SOFTWARE":
 *
 *   * across ALL synthesized templates there is exactly ONE `AWS::EC2::VPC`
 *     (R1.1), ONE EKS control-plane resource (R1.2), ONE `AWS::DAX::Cluster`
 *     (R1.4), ONE ALB (`Type: application`, R1.5), and ONE NLB
 *     (`Type: network`, R1.6);
 *   * exactly THREE managed node groups TOTAL — the single shared
 *     app-ondemand + app-spot + transcode groups (R1.3) — NOT one set per
 *     stage (which would be nine);
 *   * {@link assertFoundationSingleton} passes on the composed app (R1.7).
 *
 * The pipeline stack is intentionally omitted from this composition: task 5.4
 * concerns the foundation-singleton + workloads topology, and the pipeline's
 * own zero-foundation-per-stage assertions are task 7.4. Omitting it lets this
 * suite verify the foundation composition independently of the in-progress
 * pipeline refactor.
 *
 * _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7_
 */
describe('whole-app foundation singleton composition (task 5.4, R1.1-R1.7)', () => {
  const COMPOSE_ENV = { account: '111111111111', region: 'us-east-1' };
  const REGION = 'us-east-1';
  const ALL_STAGES = [
    DeploymentStage.Beta,
    DeploymentStage.Staging,
    DeploymentStage.Production,
  ];

  /**
   * Build the Shared_Foundation (once) plus the three namespaced
   * WorkloadsStacks, mirroring `bin/hellodj.ts` (minus the pipeline). Returns
   * the app and the foundation stack handles for per-template assertions.
   */
  function composeApp(): {
    app: cdk.App;
    network: NetworkStack;
    eks: EksStack;
    data: DataStack;
    auth: AuthStack;
    workloads: WorkloadsStack[];
  } {
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

    // Software: three namespaced WorkloadsStacks on the ONE shared cluster,
    // differing only by stage (hence namespace + hostname).
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

    return { app, network, eks, data, auth, workloads };
  }

  /**
   * Count, across ALL stacks in the app, resources of `cfnType` — optionally
   * filtered to those whose `Type` property equals `lbType` (used to
   * distinguish the ALB from the NLB, which share one CloudFormation type).
   */
  function countAcrossApp(
    app: cdk.App,
    cfnType: string,
    lbType?: string,
  ): number {
    const assembly = app.synth();
    let total = 0;
    for (const stackArtifact of assembly.stacks) {
      const resources =
        (stackArtifact.template?.Resources as Record<
          string,
          { Type?: string; Properties?: Record<string, unknown> }
        >) ?? {};
      for (const resource of Object.values(resources)) {
        if (resource.Type !== cfnType) {
          continue;
        }
        if (lbType !== undefined && resource.Properties?.Type !== lbType) {
          continue;
        }
        total += 1;
      }
    }
    return total;
  }

  test('the whole app synthesizes exactly ONE VPC (R1.1)', () => {
    const { app } = composeApp();
    expect(countAcrossApp(app, FOUNDATION_SINGLETON_TYPES.vpc)).toBe(1);
  });

  test('the whole app synthesizes exactly ONE EKS control plane (R1.2)', () => {
    const { app } = composeApp();
    expect(countAcrossApp(app, FOUNDATION_SINGLETON_TYPES.eks)).toBe(1);
  });

  test('the whole app synthesizes exactly THREE node groups total — the shared app-ondemand/app-spot/transcode, NOT per-stage (R1.3)', () => {
    const { app } = composeApp();
    // Three shared node groups live in the ONE EksStack (app-ondemand, app-spot,
    // transcode). Counting across the whole app yields 3 — never 9 (3 stages ×
    // 3) — because the WorkloadsStacks add only namespaced Kubernetes manifests,
    // no node groups.
    expect(countAcrossApp(app, FOUNDATION_SINGLETON_TYPES.nodegroup)).toBe(3);
  });

  test('the three node groups are the shared app-ondemand/app-spot/transcode by NodegroupName — exactly one each, NOT per-stage (R1.3)', () => {
    const { app } = composeApp();
    const assembly = app.synth();

    // Collect every AWS::EKS::Nodegroup NodegroupName across ALL stacks. If the
    // node groups were provisioned per-stage, each shared name would appear
    // three times (or carry a `-${stage}` suffix); the shared-foundation
    // topology yields exactly one of each stage-independent name.
    const nodegroupNames: string[] = [];
    for (const stackArtifact of assembly.stacks) {
      const resources =
        (stackArtifact.template?.Resources as Record<
          string,
          { Type?: string; Properties?: Record<string, unknown> }
        >) ?? {};
      for (const resource of Object.values(resources)) {
        if (resource.Type !== FOUNDATION_SINGLETON_TYPES.nodegroup) {
          continue;
        }
        const name = resource.Properties?.NodegroupName as string | undefined;
        if (name !== undefined) {
          nodegroupNames.push(name);
        }
      }
    }

    // Exactly one occurrence of each stage-independent shared node group name.
    const occurrences = (name: string): number =>
      nodegroupNames.filter((n) => n === name).length;
    expect(occurrences('hellodj-app-ondemand')).toBe(1);
    expect(occurrences('hellodj-app-spot')).toBe(1);
    expect(occurrences('hellodj-transcode')).toBe(1);
    // And no other node group names leaked in (e.g. per-stage suffixed ones).
    expect(nodegroupNames.sort()).toEqual([
      'hellodj-app-ondemand',
      'hellodj-app-spot',
      'hellodj-transcode',
    ]);
  });

  test('the whole app synthesizes exactly ONE DAX cluster (R1.4)', () => {
    const { app } = composeApp();
    expect(countAcrossApp(app, FOUNDATION_SINGLETON_TYPES.dax)).toBe(1);
  });

  test('the whole app synthesizes exactly ONE ALB (Type: application) (R1.5)', () => {
    const { app } = composeApp();
    expect(
      countAcrossApp(
        app,
        FOUNDATION_SINGLETON_TYPES.loadBalancer,
        LOAD_BALANCER_TYPES.alb,
      ),
    ).toBe(1);
  });

  test('the whole app synthesizes exactly ONE NLB (Type: network) (R1.6)', () => {
    const { app } = composeApp();
    expect(
      countAcrossApp(
        app,
        FOUNDATION_SINGLETON_TYPES.loadBalancer,
        LOAD_BALANCER_TYPES.nlb,
      ),
    ).toBe(1);
  });

  test('the single EksStack owns the three shared node groups (not the WorkloadsStacks)', () => {
    const { eks, workloads } = composeApp();
    const eksTemplate = Template.fromStack(eks);
    eksTemplate.resourceCountIs('AWS::EKS::Nodegroup', 3);
    // Each software stage adds ZERO node groups — it is software only.
    for (const w of workloads) {
      Template.fromStack(w).resourceCountIs('AWS::EKS::Nodegroup', 0);
    }
  });

  test('assertFoundationSingleton passes on the composed app (R1.7)', () => {
    const { app } = composeApp();
    expect(() => assertFoundationSingleton(app)).not.toThrow();
  });
});

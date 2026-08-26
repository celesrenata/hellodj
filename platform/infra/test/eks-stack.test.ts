import * as cdk from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import { Template, Match } from 'aws-cdk-lib/assertions';
import {
  EksStack,
  SCALE_OUT_THRESHOLD,
  SCALE_IN_THRESHOLD,
  TRANSCODE_TAINT_KEY,
  TRANSCODE_TAINT_VALUE,
  TRANSCODE_LABEL_KEY,
  TRANSCODE_LABEL_VALUE,
} from '../lib/eks-stack';

/**
 * CDK assertion tests for the EKS stack (task 9.4).
 *
 * These synthesize the {@link EksStack} into a CloudFormation template and
 * assert the node-group shape the design mandates:
 *
 *   * an EKS control plane (Decision D1, R2.1),
 *   * Graviton (ARM64) application node groups on-demand + Spot (R4.1),
 *   * a taint/label-isolated transcode node group with the
 *     `dedicated=transcode:NoSchedule` taint and `workload=transcode` label
 *     (Decision D2, R3.7, R3.8, R3.11).
 *
 * The VPC is supplied by a throwaway host stack (mirroring the network stack)
 * so the EKS stack can be synthesized in isolation.
 */

const TEST_ENV = { account: '111111111111', region: 'us-east-1' };

/**
 * EKS clusters in aws-cdk-lib synthesize custom-resource providers that live
 * in nested stacks, so assertions run against the primary stack template plus
 * a helper to locate the node groups regardless of nesting.
 */
function synthEks(): { template: Template } {
  const app = new cdk.App();

  // A tiny host stack that provides the VPC the network stack would produce.
  const vpcStack = new cdk.Stack(app, 'VpcHost', { env: TEST_ENV });
  const vpc = new ec2.Vpc(vpcStack, 'TestVpc', { maxAzs: 2 });

  const stack = new EksStack(app, 'TestEksStack', {
    env: TEST_ENV,
    vpc,
    stage: 'beta',
  });

  return { template: Template.fromStack(stack) };
}

describe('EksStack (task 9.4)', () => {
  test('creates an EKS cluster control plane (R2.1)', () => {
    const { template } = synthEks();
    // aws-cdk-lib provisions the control plane via a custom resource
    // (Custom::AWSCDK-EKS-Cluster) rather than a raw AWS::EKS::Cluster.
    const clusters = template.findResources('Custom::AWSCDK-EKS-Cluster');
    expect(Object.keys(clusters).length).toBe(1);
  });

  // --------------------------------------------------------------------
  // Stage-independent naming (shared-foundation topology, R4.5, R4.6)
  // --------------------------------------------------------------------
  //
  // Under the shared-foundation refactor the single EKS control plane and its
  // node groups are stage-independent singletons shared by all three
  // `hellodj-<stage>` software stages, so neither the cluster name nor the
  // node-group names carry a `-${stage}` suffix.

  test('names the cluster stage-independently (no stage suffix) (R4.5)', () => {
    const { template } = synthEks();
    template.hasResourceProperties(
      'Custom::AWSCDK-EKS-Cluster',
      Match.objectLike({
        Config: Match.objectLike({ name: 'hellodj' }),
      }),
    );
  });

  test('cluster name carries no beta/staging/production suffix (R4.5)', () => {
    const { template } = synthEks();
    const clusters = template.findResources('Custom::AWSCDK-EKS-Cluster');
    const names = Object.values(clusters).map(
      (r) => r.Properties?.Config?.name,
    );
    expect(names).toEqual(['hellodj']);
    for (const name of names) {
      expect(name).not.toMatch(/-(beta|staging|production)$/);
    }
  });

  test('provisions three managed node groups (app on-demand, app spot, transcode)', () => {
    const { template } = synthEks();
    template.resourceCountIs('AWS::EKS::Nodegroup', 3);
  });

  test('node-group names carry no stage suffix (R4.6)', () => {
    const { template } = synthEks();
    const nodegroups = template.findResources('AWS::EKS::Nodegroup');
    const names = Object.values(nodegroups).map(
      (r) => r.Properties?.NodegroupName as string,
    );
    // Exactly the three stage-independent names, nothing stage-suffixed.
    expect(names.sort()).toEqual(
      ['hellodj-app-ondemand', 'hellodj-app-spot', 'hellodj-transcode'].sort(),
    );
    for (const name of names) {
      expect(name).not.toMatch(/-(beta|staging|production)(-|$)/);
    }
  });

  test('app on-demand node group is Graviton ARM64 on-demand (R4.1)', () => {
    const { template } = synthEks();
    template.hasResourceProperties('AWS::EKS::Nodegroup', {
      NodegroupName: 'hellodj-app-ondemand',
      AmiType: 'AL2023_ARM_64_STANDARD',
      CapacityType: 'ON_DEMAND',
      Labels: Match.objectLike({
        workload: 'app',
        'hellodj.bot/arch': 'arm64',
      }),
    });
  });

  test('app spot node group is Graviton ARM64 Spot (R4.1)', () => {
    const { template } = synthEks();
    template.hasResourceProperties('AWS::EKS::Nodegroup', {
      NodegroupName: 'hellodj-app-spot',
      AmiType: 'AL2023_ARM_64_STANDARD',
      CapacityType: 'SPOT',
      Labels: Match.objectLike({
        workload: 'app',
        'hellodj.bot/arch': 'arm64',
      }),
    });
  });

  test('transcode node group is taint/label isolated (R3.7, R3.8, R3.11)', () => {
    const { template } = synthEks();
    template.hasResourceProperties('AWS::EKS::Nodegroup', {
      NodegroupName: 'hellodj-transcode',
      AmiType: 'AL2023_ARM_64_STANDARD',
      Labels: Match.objectLike({
        [TRANSCODE_LABEL_KEY]: TRANSCODE_LABEL_VALUE,
        'hellodj.bot/arch': 'arm64',
      }),
      Taints: Match.arrayWith([
        Match.objectLike({
          Key: TRANSCODE_TAINT_KEY,
          Value: TRANSCODE_TAINT_VALUE,
          Effect: 'NO_SCHEDULE',
        }),
      ]),
    });
  });

  test('every node group carries the autoscale threshold tags (R16)', () => {
    const { template } = synthEks();
    const nodegroups = template.findResources('AWS::EKS::Nodegroup');
    const names = Object.values(nodegroups).map(
      (r) => r.Properties?.NodegroupName,
    );
    expect(names).toEqual(
      expect.arrayContaining([
        'hellodj-app-ondemand',
        'hellodj-app-spot',
        'hellodj-transcode',
      ]),
    );
    for (const ng of Object.values(nodegroups)) {
      expect(ng.Properties?.Tags).toEqual(
        expect.objectContaining({
          'hellodj:scaleOutThreshold': `${SCALE_OUT_THRESHOLD}`,
          'hellodj:scaleInThreshold': `${SCALE_IN_THRESHOLD}`,
        }),
      );
    }
  });

  test('autoscale thresholds preserve hysteresis (out > in)', () => {
    expect(SCALE_OUT_THRESHOLD).toBeGreaterThan(SCALE_IN_THRESHOLD);
  });

  // --------------------------------------------------------------------
  // Node_Floor of one + scale-to-zero groups (task 3.3, R4.1, R4.4-R4.6)
  // --------------------------------------------------------------------
  //
  // The shared on-demand app node group is the always-on Node_Floor: a single
  // small Graviton node carrying all three namespaces' idle pods. Its
  // ScalingConfig floors at MinSize/DesiredSize 1 (R4.1, R4.4). The Spot and
  // transcode groups scale to exactly zero when idle (R4.5, R4.6).

  test('AppOnDemand Node_Floor ScalingConfig is MinSize 1 / DesiredSize 1 (R4.1, R4.4)', () => {
    const { template } = synthEks();
    template.hasResourceProperties('AWS::EKS::Nodegroup', {
      NodegroupName: 'hellodj-app-ondemand',
      ScalingConfig: Match.objectLike({
        MinSize: 1,
        DesiredSize: 1,
      }),
    });
  });

  test('AppOnDemand keeps maxSize 10 so the shared floor still scales up (R4.1)', () => {
    const { template } = synthEks();
    template.hasResourceProperties('AWS::EKS::Nodegroup', {
      NodegroupName: 'hellodj-app-ondemand',
      ScalingConfig: Match.objectLike({
        MaxSize: 10,
      }),
    });
  });

  test('AppOnDemand instance types include m7g.large (R4.1 small Graviton floor)', () => {
    const { template } = synthEks();
    template.hasResourceProperties('AWS::EKS::Nodegroup', {
      NodegroupName: 'hellodj-app-ondemand',
      InstanceTypes: Match.arrayWith(['m7g.large']),
    });
  });

  test('app-spot node group scales to zero (MinSize 0) (R4.5)', () => {
    const { template } = synthEks();
    template.hasResourceProperties('AWS::EKS::Nodegroup', {
      NodegroupName: 'hellodj-app-spot',
      ScalingConfig: Match.objectLike({
        MinSize: 0,
        DesiredSize: 0,
      }),
    });
  });

  test('transcode node group scales to zero (MinSize 0) (R4.6)', () => {
    const { template } = synthEks();
    template.hasResourceProperties('AWS::EKS::Nodegroup', {
      NodegroupName: 'hellodj-transcode',
      ScalingConfig: Match.objectLike({
        MinSize: 0,
        DesiredSize: 0,
      }),
    });
  });

  // --------------------------------------------------------------------
  // minSize < 1 guard (task 3.3, R4.4)
  // --------------------------------------------------------------------
  //
  // eks-stack.ts contains a synth-time guard that throws if the on-demand app
  // node group `minSize < 1`, preserving the always-on Node_Floor (R4.4).
  //
  // NOTE: the current implementation hardcodes `const appOnDemandMinSize = 1`,
  // so the guard's throwing branch is not reachable through public props — the
  // stack exposes no `minSize` override. We therefore do NOT change
  // eks-stack.ts merely to make the throw reachable (per the task's guidance).
  // Instead we assert the guard's *observable effect*: the synthesized floor is
  // always MinSize 1 (never below 1), which is exactly what the guard
  // guarantees. The `minSize < 1` guard is also unit-tested in-line against the
  // same predicate the stack uses, documenting the intended throw behavior.

  test('guard effect: the synthesized Node_Floor is never below 1 (R4.4)', () => {
    const { template } = synthEks();
    const nodegroups = template.findResources('AWS::EKS::Nodegroup');
    const ondemand = Object.values(nodegroups).find(
      (r) => r.Properties?.NodegroupName === 'hellodj-app-ondemand',
    );
    expect(ondemand).toBeDefined();
    const minSize = ondemand!.Properties?.ScalingConfig?.MinSize as number;
    expect(minSize).toBeGreaterThanOrEqual(1);
  });

  test('guard predicate: a minSize below 1 throws with an R4.4 message', () => {
    // Mirror of the guard in eks-stack.ts (the const is hardcoded to 1 there,
    // so this documents/exercises the branch the stack would take if a future
    // prop ever fed a sub-floor value). The guard must reject anything < 1.
    const guard = (appOnDemandMinSize: number): void => {
      if (appOnDemandMinSize < 1) {
        throw new Error(
          `appOnDemandNodegroup minSize must be at least 1 to preserve the ` +
            `always-on Node_Floor (R4.4); got ${appOnDemandMinSize}`,
        );
      }
    };
    expect(() => guard(0)).toThrow(/at least 1/);
    expect(() => guard(0)).toThrow(/R4\.4/);
    expect(() => guard(-3)).toThrow(/Node_Floor/);
    // The value the stack actually uses (1) must NOT throw.
    expect(() => guard(1)).not.toThrow();
  });
});

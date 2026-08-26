import * as cdk from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import { Template } from 'aws-cdk-lib/assertions';
import {
  EksStack,
  GPU_INSTANCE_TYPE,
  GPU_DRAIN_TIMEOUT_SECONDS,
  GPU_RESOURCE_KEY,
  GPU_TIME_SLICING_REPLICAS,
  KARPENTER_GPU_NODEPOOL_NAME,
  TRANSCODE_TAINT_KEY,
  TRANSCODE_TAINT_VALUE,
  DEFAULT_GPU_IDLE_WINDOW_SECONDS,
  MIN_GPU_IDLE_WINDOW_SECONDS,
  MAX_GPU_IDLE_WINDOW_SECONDS,
} from '../lib/eks-stack';

/**
 * CDK assertion tests for the hybrid GPU transcode wiring (task 16.4).
 *
 * Task 16.3 extended {@link EksStack} with the "gas/electric" hybrid GPU model
 * (Decision D3): Karpenter (Helm) provisions a warm `g5g.xlarge` **Spot** GPU
 * node **from the pre-baked NixOS AMI**, a **time-sliced NVIDIA device plugin**
 * advertises `nvidia.com/gpu` replicas so many transcode pods share one
 * physical T4G, the NodePool **scales to zero** on coast-down, and a Spot
 * reclaim **gracefully drains** GPU jobs within the 120 s drain window.
 *
 * These tests synthesize the stack and assert that isolated GPU node group and
 * device-plugin shape is present:
 *
 *   * the Karpenter controller Helm release exists,
 *   * the Karpenter `EC2NodeClass` + `NodePool` manifest provisions a
 *     taint-isolated `g5g.xlarge` **Spot** GPU node from the baked AMI, caps
 *     `nvidia.com/gpu`, sets the 120 s `terminationGracePeriod` drain window,
 *     and consolidates empty nodes to zero (scale-to-zero),
 *   * the time-slicing NVIDIA device-plugin manifest (ConfigMap advertising
 *     `nvidia.com/gpu` replicas + DaemonSet) is applied.
 *
 * The Karpenter CRDs and the device plugin are applied as CDK
 * `KubernetesManifest`s, which aws-cdk-lib synthesizes into
 * `Custom::AWSCDK-EKS-KubernetesResource` resources whose `Manifest` property
 * holds the manifest JSON (interleaved with CloudFormation tokens for values
 * such as the cluster name). Rather than assert exact structural shape through
 * those tokens, each test serializes the candidate resource(s) to a string and
 * asserts the design-mandated values are present — the same approach the
 * runtime kubectl apply ultimately sees.
 *
 * _Requirements: 3.11, 17.1_
 */

const TEST_ENV = { account: '111111111111', region: 'us-east-1' };

/** Synthesize the EKS stack against a throwaway VPC host stack. */
function synthEks(gpuIdleWindowSeconds?: number): Template {
  const app = new cdk.App();

  const vpcStack = new cdk.Stack(app, 'VpcHost', { env: TEST_ENV });
  const vpc = new ec2.Vpc(vpcStack, 'TestVpc', { maxAzs: 2 });

  const stack = new EksStack(app, 'TestEksStack', {
    env: TEST_ENV,
    vpc,
    stage: 'beta',
    gpuIdleWindowSeconds,
    // Supply a valid registered AMI id so the shared GPU NodePool /
    // EC2NodeClass is present: it is gated on a REAL baked AMI (the placeholder
    // sentinel is rejected by Karpenter's admission webhook at deploy time, so
    // the pool is only emitted once the nix-native-delivery pipeline injects a
    // registered AMI id). These tests assert the pool's shape, so they model
    // the AMI-injected case.
    bakedGpuAmiId: 'ami-0123456789abcdef0',
  });

  return Template.fromStack(stack);
}

/**
 * Serialize every applied Kubernetes manifest resource to a single string so
 * assertions can look for the design-mandated values regardless of the
 * CloudFormation tokens interleaved into the manifest JSON.
 */
function kubernetesManifestsAsString(template: Template): string {
  const resources = template.findResources(
    'Custom::AWSCDK-EKS-KubernetesResource',
  );
  return JSON.stringify(resources);
}

describe('EksStack hybrid GPU transcode wiring (task 16.4)', () => {
  test('installs the Karpenter controller Helm release (D3)', () => {
    const template = synthEks();
    // aws-cdk-lib applies Helm charts via a custom resource
    // (Custom::AWSCDK-EKS-HelmChart) rather than a raw AWS::EKS::HelmChart.
    const charts = template.findResources('Custom::AWSCDK-EKS-HelmChart');
    const serialized = JSON.stringify(charts);
    expect(Object.keys(charts).length).toBeGreaterThanOrEqual(1);
    // The Karpenter chart is pulled from the public ECR OCI registry.
    expect(serialized).toContain('karpenter');
  });

  test('applies at least one Karpenter/device-plugin Kubernetes manifest', () => {
    const template = synthEks();
    const resources = template.findResources(
      'Custom::AWSCDK-EKS-KubernetesResource',
    );
    expect(Object.keys(resources).length).toBeGreaterThanOrEqual(1);
  });

  describe('Karpenter EC2NodeClass + NodePool manifest (R3.11, R17.1)', () => {
    test('provisions a g5g.xlarge Spot GPU node group', () => {
      const manifests = kubernetesManifestsAsString(synthEks());
      // g5g.xlarge instance type restriction (smallest G5g, R3.8).
      expect(manifests).toContain(GPU_INSTANCE_TYPE);
      expect(manifests).toContain('g5g.xlarge');
      // Spot capacity type (the "gas engine" runs only while it earns cost).
      expect(manifests).toContain('spot');
      // Karpenter NodePool + EC2NodeClass carry the shared GPU pool name.
      expect(manifests).toContain(KARPENTER_GPU_NODEPOOL_NAME);
      expect(manifests).toContain('EC2NodeClass');
      expect(manifests).toContain('NodePool');
    });

    test('is taint-isolated with dedicated=transcode:NoSchedule (Decision D2)', () => {
      const manifests = kubernetesManifestsAsString(synthEks());
      expect(manifests).toContain(TRANSCODE_TAINT_KEY); // 'dedicated'
      expect(manifests).toContain(TRANSCODE_TAINT_VALUE); // 'transcode'
      expect(manifests).toContain('NoSchedule');
    });

    test('launches from the baked NixOS GPU AMI (amiFamily Custom + amiSelectorTerms id)', () => {
      const manifests = kubernetesManifestsAsString(synthEks());
      // NixOS is a custom image, so the AMI family is Custom and the AMI is
      // selected explicitly by id via amiSelectorTerms.
      expect(manifests).toContain('Custom');
      expect(manifests).toContain('amiSelectorTerms');
      expect(manifests).toContain('amiFamily');
    });

    test('caps GPU capacity via limits nvidia.com/gpu', () => {
      const manifests = kubernetesManifestsAsString(synthEks());
      expect(manifests).toContain(GPU_RESOURCE_KEY); // 'nvidia.com/gpu'
      expect(manifests).toContain('limits');
    });

    test('sets the 120s terminationGracePeriod drain window (R17.1)', () => {
      const manifests = kubernetesManifestsAsString(synthEks());
      expect(manifests).toContain('terminationGracePeriod');
      expect(manifests).toContain(`${GPU_DRAIN_TIMEOUT_SECONDS}s`); // '120s'
    });

    test('scales to zero via WhenEmpty consolidation after the default idle window (R8.5)', () => {
      const manifests = kubernetesManifestsAsString(synthEks());
      // WhenEmpty fires only when zero transcode pods remain, so the GPU is
      // never torn down while under load (R8.6); consolidateAfter is the
      // continuous idle window that must elapse first — default 300 s (R8.5),
      // matching GpuIdleConfig / gpu_idle_decision.
      expect(manifests).toContain('consolidationPolicy');
      expect(manifests).toContain('WhenEmpty');
      expect(manifests).toContain('consolidateAfter');
      expect(DEFAULT_GPU_IDLE_WINDOW_SECONDS).toBe(300);
      expect(manifests).toContain(`${DEFAULT_GPU_IDLE_WINDOW_SECONDS}s`); // '300s'
      // The old hardcoded 30 s window (below the 60 s floor) is gone.
      expect(manifests).not.toContain('"consolidateAfter":"30s"');
    });

    test('wires a configurable idle window into consolidateAfter (R8.5)', () => {
      const manifests = kubernetesManifestsAsString(synthEks(600));
      expect(manifests).toContain('consolidateAfter');
      expect(manifests).toContain('600s');
    });

    test('accepts the idle-window range bounds [60, 900] (R8.5)', () => {
      expect(() => synthEks(MIN_GPU_IDLE_WINDOW_SECONDS)).not.toThrow();
      expect(() => synthEks(MAX_GPU_IDLE_WINDOW_SECONDS)).not.toThrow();
      expect(kubernetesManifestsAsString(synthEks(MIN_GPU_IDLE_WINDOW_SECONDS))).toContain(
        `${MIN_GPU_IDLE_WINDOW_SECONDS}s`,
      );
      expect(kubernetesManifestsAsString(synthEks(MAX_GPU_IDLE_WINDOW_SECONDS))).toContain(
        `${MAX_GPU_IDLE_WINDOW_SECONDS}s`,
      );
    });

    test('rejects an idle window outside [60, 900] like GpuIdleConfig (R8.5)', () => {
      // Mirrors GpuIdleConfig.__post_init__ rejecting <60 or >900.
      expect(() => synthEks(MIN_GPU_IDLE_WINDOW_SECONDS - 1)).toThrow(/60.*900|R8\.5/);
      expect(() => synthEks(MAX_GPU_IDLE_WINDOW_SECONDS + 1)).toThrow(/60.*900|R8\.5/);
      expect(() => synthEks(0)).toThrow(/R8\.5/);
    });
  });

  describe('time-sliced NVIDIA device plugin manifest (R3.11)', () => {
    test('advertises nvidia.com/gpu with the configured time-slicing replicas', () => {
      const manifests = kubernetesManifestsAsString(synthEks());
      // The time-slicing ConfigMap presents each physical GPU as N replicas.
      expect(manifests).toContain('time-slicing-config');
      expect(manifests).toContain('timeSlicing');
      expect(manifests).toContain(GPU_RESOURCE_KEY); // 'nvidia.com/gpu'
      expect(manifests).toContain(`replicas: ${GPU_TIME_SLICING_REPLICAS}`); // 'replicas: 4'
    });

    test('deploys the device plugin as a DaemonSet on GPU nodes', () => {
      const manifests = kubernetesManifestsAsString(synthEks());
      expect(manifests).toContain('DaemonSet');
      expect(manifests).toContain('nvidia-device-plugin-daemonset');
      // The DaemonSet only lands on GPU nodes via the GPU node label.
      expect(manifests).toContain('hellodj.bot/gpu');
    });
  });
});

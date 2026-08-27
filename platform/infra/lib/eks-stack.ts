/**
 * EKS cluster stack for the HelloDJ AWS platform.
 *
 * Implements Decision D1 (Orchestrator = Amazon EKS) and the node-group shape
 * the design mandates:
 *
 *   * A Graviton (ARM64) application managed node group split across an
 *     on-demand baseline and a cost-optimized Spot node group, so the app
 *     fleet defaults to Graviton per Requirement 4.1 and the design's
 *     "App node group - Graviton on-demand + spot".
 *   * A taint/label-isolated transcode node group (Decision D2 co-location)
 *     reserved for the `hls-transcode` workload via a
 *     `dedicated=transcode:NoSchedule` taint and a `workload=transcode` label,
 *     so only transcode pods (which tolerate the taint and target the label)
 *     land there (Requirements 3.7, 3.8, 3.11).
 *
 * Cluster autoscaling is driven by CPU / RAM / GPU pressure using the exact
 * thresholds from the Python `autoscale.py` decision module (scale out at 70%,
 * scale in at 40%) so infrastructure-as-code and runtime share one source of
 * truth (Decision D1, Requirements 16.1-16.5). Those thresholds are encoded
 * here as constants and surfaced as tags on the cluster and node groups.
 *
 * Task 16.3 wires the hybrid "gas/electric" GPU transcode model (Decision D3)
 * on top of the taint/label-isolated transcode group: Karpenter provisions a
 * warm `g5g.xlarge` **Spot** GPU node **from the pre-baked minimal NixOS AMI**
 * (task 16.2) on spin-up, the **time-sliced NVIDIA device plugin** advertises
 * `nvidia.com/gpu` replicas so many transcode pods share one physical T4G, the
 * NodePool **scales to zero** on coast-down (billing stops), and a Spot
 * reclaim **gracefully drains** GPU jobs back to the CPU path within the
 * `DEFAULT_DRAIN_TIMEOUT_SECONDS` (120 s) drain window (Requirements 3.2, 3.3,
 * 3.11, 16.4, 17.1-17.4). The CPU (`c7g`) transcode floor remains the always-on
 * "electric motor" so the ≤5 s Interactive_Latency_Budget holds during a cold
 * GPU spin-up or a Spot interruption.
 *
 * The VPC is supplied via props (produced by the network stack, task 9.2) so
 * the wiring is explicit even while the network stack is written concurrently.
 *
 * _Requirements: 2.1, 2.2, 3.2, 3.3, 3.7, 3.8, 3.11, 4.1, 16.1, 16.2, 16.3,
 * 16.4, 16.5, 17.1, 17.2, 17.3, 17.4_
 */
import * as cdk from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as eks from 'aws-cdk-lib/aws-eks';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import { KubectlV36Layer } from '@aws-cdk/lambda-layer-kubectl-v36';
import { Construct } from 'constructs';

// ---------------------------------------------------------------------------
// Autoscaling thresholds — single source of truth mirror
// ---------------------------------------------------------------------------
//
// These mirror `DEFAULT_SCALE_OUT_THRESHOLD` / `DEFAULT_SCALE_IN_THRESHOLD` in
// the Python `hellodj_platform_logic.autoscale` / `types` modules (0.70 / 0.40).
// The autoscaler adds capacity when any of CPU/RAM/GPU pressure exceeds the
// scale-out threshold and removes capacity only when all three are below the
// scale-in threshold (Requirements 16.2-16.5). The gap between them provides
// hysteresis. Kept as constants (and surfaced as tags) so the Cluster
// Autoscaler / HPA wiring added in later tasks reads identical values.

/** Scale-out utilization fraction: add capacity above this (design/autoscale.py). */
export const SCALE_OUT_THRESHOLD = 0.7;

/** Scale-in utilization fraction: remove capacity only below this (design/autoscale.py). */
export const SCALE_IN_THRESHOLD = 0.4;

/** Taint key isolating the transcode node group (Decision D2, R3.11). */
export const TRANSCODE_TAINT_KEY = 'dedicated';

/** Taint value isolating the transcode node group (Decision D2, R3.11). */
export const TRANSCODE_TAINT_VALUE = 'transcode';

/** Node label selecting the transcode node group for `hls-transcode` pods. */
export const TRANSCODE_LABEL_KEY = 'workload';

/** Node label value selecting the transcode node group. */
export const TRANSCODE_LABEL_VALUE = 'transcode';

// ---------------------------------------------------------------------------
// Hybrid GPU transcode model — Karpenter g5g Spot from the baked AMI (D3)
// ---------------------------------------------------------------------------
//
// These mirror the design's "gas/electric" hybrid transcode model (Decision
// D3) and are surfaced so the Karpenter NodePool / EC2NodeClass and the
// time-sliced NVIDIA device plugin read a single source of truth.

/** The GPU instance the hybrid model spins up on demand (smallest G5g, R3.8). */
export const GPU_INSTANCE_TYPE = 'g5g.xlarge';

/**
 * Connection-draining timeout (seconds) mirroring
 * `DEFAULT_DRAIN_TIMEOUT_SECONDS` (120 s) in the Python
 * `hellodj_platform_logic.types` / `draining` modules (R17.3). On a Spot
 * reclaim the GPU node is given this long to drain in-flight transcode jobs
 * back to the CPU path before the instance is reclaimed (R17.1, R17.2, R17.5).
 */
export const GPU_DRAIN_TIMEOUT_SECONDS = 120;

/**
 * The GPU resource key the time-sliced NVIDIA device plugin advertises. Many
 * transcode pods share one physical T4G via time-slicing replicas (Decision
 * D1 / D3, R3.11).
 */
export const GPU_RESOURCE_KEY = 'nvidia.com/gpu';

/**
 * Number of time-slicing replicas advertised per physical GPU. The single warm
 * `g5g.xlarge` T4G is presented as this many schedulable `nvidia.com/gpu`
 * units so concurrent transcode/visualizer jobs share it rather than each
 * demanding a whole GPU (GPU_Sharing, R3.11).
 */
export const GPU_TIME_SLICING_REPLICAS = 4;

// ---------------------------------------------------------------------------
// GPU scale-to-zero idle window — single source of truth mirror (R8.5, R8.6)
// ---------------------------------------------------------------------------
//
// These mirror `GpuIdleConfig` in the Python
// `hellodj_platform_logic.types` module and the pure `gpu_idle_decision`
// function (`hellodj_platform_logic.gpu_idle`), which is the authoritative
// model of the scale-to-zero decision. The shared, time-sliced Karpenter
// `transcode-gpu` NodePool that all three stages' transcode pods target scales
// to zero once it has had **no active transcode workload for a continuous idle
// window** — default 300 s, configurable within [60, 900] s — via Karpenter's
// `consolidationPolicy: WhenEmpty` + `consolidateAfter: <idle window>`
// (R8.5). A GPU-requiring workload arriving while the pool is at zero triggers
// scale-up to serve it, because Karpenter provisions a node for the pending
// GPU pod (R8.6). The CPU transcode floor covers spin-up latency so the ≤5 s
// interactive budget holds while the GPU climbs back from zero.

/**
 * Default GPU scale-to-zero idle window in seconds (R8.5). Mirrors
 * `GpuIdleConfig.idle_window_seconds`'s 300 s default: after this much
 * continuous idle time with no active transcode workload the shared GPU
 * NodePool consolidates empty nodes to zero.
 */
export const DEFAULT_GPU_IDLE_WINDOW_SECONDS = 300;

/**
 * Minimum configurable GPU idle window in seconds (R8.5). Mirrors the lower
 * bound `GpuIdleConfig.__post_init__` enforces; a shorter window would flap the
 * GPU node at every brief lull.
 */
export const MIN_GPU_IDLE_WINDOW_SECONDS = 60;

/**
 * Maximum configurable GPU idle window in seconds (R8.5). Mirrors the upper
 * bound `GpuIdleConfig.__post_init__` enforces; a longer window would keep the
 * GPU billing long after the last job drained.
 */
export const MAX_GPU_IDLE_WINDOW_SECONDS = 900;

/**
 * Karpenter NodePool name for the hybrid GPU transcode nodes.
 *
 * This name is intentionally **stage-independent** (no `beta`/`staging`/
 * `production` suffix): Beta, Staging, and Production are consolidated onto the
 * single shared GPU host and all three schedule their transcode pods onto this
 * one time-sliced Karpenter GPU NodePool from the single shared
 * {@link EksStackProps.bakedGpuAmiId} GPU AMI. There is **no separate GPU
 * instance per stage** (task 15.1, R8.3, R8.4) — stages are isolated only by
 * their distinct `StageEndpoint` (namespace + hostname), wired in the
 * workloads stack, never by a per-stage GPU fleet.
 */
export const KARPENTER_GPU_NODEPOOL_NAME = 'transcode-gpu';

/**
 * Scheduling weight of the shared GPU NodePool (R8.6 scale-up).
 *
 * Karpenter reactively provisions a node for a **pending** pod whose scheduling
 * constraints only this NodePool satisfies — this is the scale-up-on-arrival
 * mechanism that mirrors the inverse of `gpu_idle_decision`: while
 * `active_jobs > 0` the GPU is never at zero because a pending transcode pod
 * requesting `nvidia.com/gpu` (which tolerates the transcode taint + selects
 * `hellodj.bot/gpu=true`) has nowhere else to land, so Karpenter launches a
 * `g5g.xlarge` Spot GPU node to serve it. A positive `weight` makes this the
 * preferred pool for those GPU pods over any other candidate, so a
 * GPU-requiring workload arriving while the pool sits at zero deterministically
 * drives scale-up here (R8.6) rather than to an unintended pool.
 */
export const KARPENTER_GPU_NODEPOOL_WEIGHT = 100;

/**
 * Upper bound on schedulable time-sliced GPU units the shared NodePool will
 * scale up to (R8.6). Mirrors {@link GPU_TIME_SLICING_REPLICAS}: the pool
 * scales up from zero on workload arrival only as far as this ceiling, so a
 * burst of pending transcode pods provisions GPU capacity to serve them and no
 * further. Kept as the NodePool `limits['nvidia.com/gpu']` so the scale-up
 * ceiling and the device plugin's advertised replica count are one value.
 */
export const KARPENTER_GPU_NODEPOOL_LIMIT = GPU_TIME_SLICING_REPLICAS;

/**
 * Placeholder baked GPU AMI id used when {@link EksStackProps.bakedGpuAmiId} is
 * not supplied. The deployment pipeline (which builds + registers the AMI from
 * `infra/ami/`, task 16.2) injects the real id; this sentinel keeps synthesis
 * and type-checking working and is clearly flagged as a TODO in the template.
 */
export const PLACEHOLDER_GPU_AMI_ID = 'ami-PLACEHOLDER-baked-nixos-gpu';

/**
 * Default Graviton (ARM64) instance types for the application node group.
 *
 * The design specifies a Graviton-first app fleet (R4.1). `m7g` (general
 * purpose) covers the bot / orchestrator / web / activity workloads and `c7g`
 * (compute optimized) covers the CPU-bound libx264 software-transcode floor.
 */
export const DEFAULT_APP_INSTANCE_TYPES = ['m7g.large', 'c7g.large'];

/**
 * Properties for {@link EksStack}.
 *
 * `vpc` is required and provided by the network stack (task 9.2). Declaring it
 * explicitly keeps the wiring visible even though the network stack is being
 * written concurrently.
 */
export interface EksStackProps extends cdk.StackProps {
  /** The multi-AZ VPC the EKS cluster and its node groups run in (task 9.2). */
  readonly vpc: ec2.IVpc;

  /** The deployment stage name (beta/staging/production) used in resource names. */
  readonly stage?: string;

  /**
   * The Kubernetes control-plane version.
   *
   * @default KubernetesVersion.V1_36
   */
  readonly kubernetesVersion?: eks.KubernetesVersion;

  /**
   * The kubectl/Helm Lambda layer the cluster's kubectl handler uses.
   *
   * `aws-cdk-lib` requires a kubectl layer for a managed EKS cluster. A default
   * placeholder layer is created from a bundled asset when none is supplied;
   * production deployments should pass the `@aws-cdk/lambda-layer-kubectl-vXX`
   * layer that matches {@link kubernetesVersion}.
   *
   * @default - a placeholder layer built from the bundled `kubectl-layer` asset
   */
  readonly kubectlLayer?: lambda.ILayerVersion;

  /**
   * Instance types for the Graviton application node group.
   *
   * @default DEFAULT_APP_INSTANCE_TYPES (m7g.large + c7g.large)
   */
  readonly appInstanceTypes?: string[];

  /**
   * The registered AMI id of the pre-baked minimal NixOS GPU node image
   * (task 16.2, `infra/ami/`). Karpenter's `EC2NodeClass` launches the hybrid
   * `g5g.xlarge` Spot GPU nodes directly from this AMI so boot reduces to
   * "kernel → initrd → mount pre-realized Nix store → start transcode unit"
   * (Decision D3).
   *
   * The deployment pipeline builds + registers the AMI and injects its id
   * here. When unset, {@link PLACEHOLDER_GPU_AMI_ID} is used and the synthesized
   * `EC2NodeClass` carries a `hellodj.bot/baked-ami: TODO-*` annotation so the
   * placeholder is obvious and never silently ships.
   *
   * @default PLACEHOLDER_GPU_AMI_ID (a clearly-marked TODO sentinel)
   */
  readonly bakedGpuAmiId?: string;

  /**
   * The GPU scale-to-zero idle window, in seconds (R8.5).
   *
   * After the shared time-sliced Karpenter `transcode-gpu` NodePool has had no
   * active transcode workload for this many continuous seconds, its empty GPU
   * nodes consolidate to zero (Karpenter `consolidationPolicy: WhenEmpty` +
   * `consolidateAfter`), so the GPU bills only under load. A GPU-requiring
   * workload arriving while the pool is at zero triggers scale-up (R8.6).
   *
   * Must be within [{@link MIN_GPU_IDLE_WINDOW_SECONDS},
   * {@link MAX_GPU_IDLE_WINDOW_SECONDS}] = [60, 900] seconds, mirroring the
   * range `GpuIdleConfig` enforces and the pure `gpu_idle_decision` function
   * reasons over; an out-of-range value throws at synth time, exactly as the
   * Python `GpuIdleConfig.__post_init__` rejects it at construction.
   *
   * @default DEFAULT_GPU_IDLE_WINDOW_SECONDS (300 s)
   */
  readonly gpuIdleWindowSeconds?: number;
}

/**
 * The EKS cluster plus its Graviton application node groups (on-demand + Spot)
 * and a taint/label-isolated transcode node group.
 */
export class EksStack extends cdk.Stack {
  /** The provisioned EKS cluster. */
  public readonly cluster: eks.Cluster;

  /** The on-demand Graviton application node group (baseline capacity). */
  public readonly appOnDemandNodegroup: eks.Nodegroup;

  /** The Spot Graviton application node group (cost-optimized burst capacity). */
  public readonly appSpotNodegroup: eks.Nodegroup;

  /** The taint/label-isolated Graviton transcode node group (Decision D2). */
  public readonly transcodeNodegroup: eks.Nodegroup;

  /** The Karpenter controller Helm release provisioning the hybrid GPU nodes. */
  public readonly karpenterChart: eks.HelmChart;

  /**
   * The Karpenter `EC2NodeClass` + `NodePool` manifest that provisions
   * `g5g.xlarge` Spot GPU nodes from the baked AMI and scales them to zero.
   */
  public readonly gpuNodePoolManifest?: eks.KubernetesManifest;

  /**
   * The time-sliced NVIDIA device-plugin manifest (ConfigMap + DaemonSet) that
   * advertises `nvidia.com/gpu` replicas on the warm GPU node (R3.11).
   */
  public readonly nvidiaDevicePluginManifest: eks.KubernetesManifest;

  /** The resolved baked GPU AMI id used by the Karpenter `EC2NodeClass`. */
  public readonly bakedGpuAmiId: string;

  /**
   * The resolved GPU scale-to-zero idle window in seconds, wired into the
   * Karpenter NodePool's `consolidateAfter` (R8.5). Default 300 s, validated to
   * [60, 900] s, mirroring `GpuIdleConfig` / `gpu_idle_decision`.
   */
  public readonly gpuIdleWindowSeconds: number;

  /** The builder Nix store local cache tier manifest (R4.7/R4.8). */
  public readonly builderCacheTierManifest: eks.KubernetesManifest;

  constructor(scope: Construct, id: string, props: EksStackProps) {
    super(scope, id, props);

    const stage = props.stage ?? 'beta';
    const k8sVersion = props.kubernetesVersion ?? eks.KubernetesVersion.V1_36;
    const kubectlLayer =
      props.kubectlLayer ?? this.defaultKubectlLayer(k8sVersion);
    const appInstanceTypes =
      props.appInstanceTypes ?? DEFAULT_APP_INSTANCE_TYPES;
    this.bakedGpuAmiId = props.bakedGpuAmiId ?? PLACEHOLDER_GPU_AMI_ID;

    // Resolve + validate the GPU scale-to-zero idle window (R8.5). This
    // mirrors `GpuIdleConfig.__post_init__`, which rejects any window outside
    // [60, 900] s: a value below the floor flaps the node at every brief lull,
    // and a value above the ceiling keeps the GPU billing long after the last
    // job drained. Enforcing it here — at synth time — keeps infrastructure and
    // the pure `gpu_idle_decision` model in lockstep on the same valid range.
    this.gpuIdleWindowSeconds =
      props.gpuIdleWindowSeconds ?? DEFAULT_GPU_IDLE_WINDOW_SECONDS;
    if (
      !Number.isFinite(this.gpuIdleWindowSeconds) ||
      this.gpuIdleWindowSeconds < MIN_GPU_IDLE_WINDOW_SECONDS ||
      this.gpuIdleWindowSeconds > MAX_GPU_IDLE_WINDOW_SECONDS
    ) {
      throw new Error(
        `gpuIdleWindowSeconds must be within ` +
          `[${MIN_GPU_IDLE_WINDOW_SECONDS}, ${MAX_GPU_IDLE_WINDOW_SECONDS}] seconds (R8.5); ` +
          `got ${this.gpuIdleWindowSeconds}`,
      );
    }

    // -----------------------------------------------------------------------
    // EKS control plane (Decision D1, R2.1)
    // -----------------------------------------------------------------------
    //
    // `defaultCapacity: 0` keeps the cluster free of an implicit node group so
    // capacity is expressed only through the explicit Graviton node groups
    // below. The control plane runs in the multi-AZ VPC from the network stack.
    // Stage-independent cluster name (shared-foundation topology): the single
    // EKS control plane is a stage-independent singleton shared by all three
    // `hellodj-<stage>` software stages, so the cluster carries no `-${stage}`
    // suffix (R4.5-R4.7, R7.2). Stages are isolated only by namespace +
    // hostname, never by a per-stage cluster.
    this.cluster = new eks.Cluster(this, 'Cluster', {
      clusterName: 'hellodj',
      version: k8sVersion,
      kubectlLayer,
      vpc: props.vpc,
      vpcSubnets: [{ subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS }],
      defaultCapacity: 0,
      endpointAccess: eks.EndpointAccess.PUBLIC_AND_PRIVATE,
    });

    // Surface the autoscaling thresholds on the cluster so the Cluster
    // Autoscaler / HPA wiring added by later tasks reads a single source of
    // truth matching `autoscale.py` (R16.1-R16.5).
    cdk.Tags.of(this.cluster).add('hellodj:scaleOutThreshold', `${SCALE_OUT_THRESHOLD}`);
    cdk.Tags.of(this.cluster).add('hellodj:scaleInThreshold', `${SCALE_IN_THRESHOLD}`);

    // -----------------------------------------------------------------------
    // Graviton application node group — on-demand Node_Floor of one (R4.1, R4.4)
    // -----------------------------------------------------------------------
    //
    // This is the always-on `Node_Floor` (R4.1): a SINGLE small Graviton node
    // carrying the idle pods of all three `hellodj-<stage>` namespaces. Under
    // the shared-foundation topology Beta/Staging/Production are three
    // namespaced software stages on this one on-demand baseline, so the floor
    // drops from two nodes to one (`minSize`/`desiredSize` 2 → 1). `maxSize: 10`
    // is kept so real load still scales the shared floor up, and the Spot burst
    // group (`AppSpot`, 0→20) absorbs request load above the idle floor.
    //
    // The instance stays the small `m7g.large` (2 vCPU / 8 GiB) default from
    // `DEFAULT_APP_INSTANCE_TYPES`; transcode/visualizer is deliberately OFF the
    // floor (it idles on the scale-to-zero transcode / `transcode-gpu` pools),
    // so the floor holds only three idle `lavalink` JVMs plus small Python idle
    // pods and the system daemonsets. If measured idle memory pressure ever
    // exceeds `m7g.large`, the documented single-node fallback is `m7g.xlarge`
    // (still ONE node), not a second node.
    const appOnDemandMinSize = 1;
    const appOnDemandDesiredSize = 1;

    // Synth-time guard preserving the always-on Node_Floor (R4.4): the shared
    // on-demand app node group must never be allowed below one node, even when
    // idle. A `minSize < 1` would let the Cluster Autoscaler drop the floor to
    // zero and strand all three namespaces' idle pods, so we fail synthesis
    // rather than ship a foundation that can scale its floor away.
    if (appOnDemandMinSize < 1) {
      throw new Error(
        `appOnDemandNodegroup minSize must be at least 1 to preserve the ` +
          `always-on Node_Floor (R4.4); got ${appOnDemandMinSize}`,
      );
    }

    this.appOnDemandNodegroup = this.cluster.addNodegroupCapacity('AppOnDemand', {
      nodegroupName: 'hellodj-app-ondemand',
      amiType: eks.NodegroupAmiType.AL2023_ARM_64_STANDARD,
      capacityType: eks.CapacityType.ON_DEMAND,
      instanceTypes: appInstanceTypes.map((t) => new ec2.InstanceType(t)),
      minSize: appOnDemandMinSize,
      desiredSize: appOnDemandDesiredSize,
      maxSize: 10,
      labels: {
        'workload': 'app',
        'hellodj.bot/arch': 'arm64',
        'hellodj.bot/capacity': 'on-demand',
      },
      tags: this.autoscaleTags(),
    });

    // -----------------------------------------------------------------------
    // Graviton application node group — Spot burst capacity (R4.1)
    // -----------------------------------------------------------------------
    this.appSpotNodegroup = this.cluster.addNodegroupCapacity('AppSpot', {
      nodegroupName: 'hellodj-app-spot',
      amiType: eks.NodegroupAmiType.AL2023_ARM_64_STANDARD,
      capacityType: eks.CapacityType.SPOT,
      instanceTypes: appInstanceTypes.map((t) => new ec2.InstanceType(t)),
      minSize: 0,
      desiredSize: 0,
      maxSize: 20,
      labels: {
        'workload': 'app',
        'hellodj.bot/arch': 'arm64',
        'hellodj.bot/capacity': 'spot',
      },
      tags: this.autoscaleTags(),
    });

    // -----------------------------------------------------------------------
    // Transcode node group — taint/label isolated, co-located (Decision D2)
    // -----------------------------------------------------------------------
    //
    // Reserved for the `hls-transcode` workload. The
    // `dedicated=transcode:NoSchedule` taint keeps general app pods off these
    // nodes; only transcode pods that tolerate the taint and select the
    // `workload=transcode` label schedule here (R3.7, R3.8, R3.11).
    //
    // This managed group is the always-on Graviton CPU "electric motor" that
    // serves the libx264 software-transcode floor (Decision D3 default). It
    // scales to zero when idle but covers spin-up latency so the ≤5 s
    // Interactive_Latency_Budget holds. The warm, time-sliced `g5g.xlarge`
    // Spot GPU "gas engine" is provisioned separately by Karpenter from the
    // pre-baked NixOS AMI (below) so a GPU is present only while it earns its
    // cost (Decision D3, R3.2, R3.3, R3.11).
    this.transcodeNodegroup = this.cluster.addNodegroupCapacity('Transcode', {
      nodegroupName: 'hellodj-transcode',
      amiType: eks.NodegroupAmiType.AL2023_ARM_64_STANDARD,
      capacityType: eks.CapacityType.SPOT,
      instanceTypes: [new ec2.InstanceType('c7g.xlarge')],
      // minSize 0 so the group can scale to zero when there is no transcode
      // demand; the CPU floor covers spin-up latency (Decision D3, R3.11).
      minSize: 0,
      desiredSize: 0,
      maxSize: 8,
      labels: {
        [TRANSCODE_LABEL_KEY]: TRANSCODE_LABEL_VALUE,
        'hellodj.bot/arch': 'arm64',
      },
      taints: [
        {
          key: TRANSCODE_TAINT_KEY,
          value: TRANSCODE_TAINT_VALUE,
          effect: eks.TaintEffect.NO_SCHEDULE,
        },
      ],
      tags: this.autoscaleTags(),
    });

    // -----------------------------------------------------------------------
    // AWS Load Balancer Controller — reconciles Kubernetes Ingress resources
    // into ALB listeners/target groups. Create an IAM role for the controller's
    // ServiceAccount (IRSA) and let the Helm chart create/annotate the SA.
    // -----------------------------------------------------------------------
    const lbControllerRole = new cdk.aws_iam.Role(this, 'AwsLbControllerRole', {
      assumedBy: new cdk.aws_iam.FederatedPrincipal(
        this.cluster.openIdConnectProvider.openIdConnectProviderArn,
        {
          StringEquals: {
            [`${this.cluster.clusterOpenIdConnectIssuer}:sub`]:
              'system:serviceaccount:kube-system:aws-load-balancer-controller',
            [`${this.cluster.clusterOpenIdConnectIssuer}:aud`]: 'sts.amazonaws.com',
          },
        },
        'sts:AssumeRoleWithWebIdentity',
      ),
    });
    lbControllerRole.addManagedPolicy(
      cdk.aws_iam.ManagedPolicy.fromAwsManagedPolicyName('ElasticLoadBalancingFullAccess'),
    );
    lbControllerRole.addManagedPolicy(
      cdk.aws_iam.ManagedPolicy.fromAwsManagedPolicyName('AmazonEC2ReadOnlyAccess'),
    );

    this.cluster.addHelmChart('AwsLoadBalancerController', {
      chart: 'aws-load-balancer-controller',
      release: 'aws-load-balancer-controller',
      repository: 'https://aws.github.io/eks-charts',
      namespace: 'kube-system',
      version: '1.10.1',
      values: {
        clusterName: this.cluster.clusterName,
        serviceAccount: {
          create: true,
          name: 'aws-load-balancer-controller',
          annotations: {
            'eks.amazonaws.com/role-arn': lbControllerRole.roleArn,
          },
        },
        nodeSelector: { workload: 'app' },
        region: cdk.Stack.of(this).region,
        vpcId: props.vpc.vpcId,
      },
    });

    // -----------------------------------------------------------------------
    // Hybrid GPU "gas engine" — Karpenter + baked-AMI g5g Spot + time-slicing
    // -----------------------------------------------------------------------
    this.karpenterChart = this.installKarpenter(stage);
    // The GPU NodePool's `EC2NodeClass` launches nodes from the pre-baked
    // NixOS GPU AMI. Karpenter's admission webhook validates the AMI id at
    // apply time and REJECTS the placeholder sentinel
    // (`ami-PLACEHOLDER-...` fails the `ami-[0-9a-z]+` pattern), which would
    // fail the manifest and roll back the whole cluster. The AMI is built +
    // registered out-of-band (nix-native-delivery task 16.2) and injected via
    // `bakedGpuAmiId`; until a REAL id is supplied, we skip the GPU NodePool
    // entirely. Karpenter + the device plugin still install, and the shared
    // scale-to-zero GPU pool is added the moment a registered AMI id is wired
    // in — so the foundation deploys cleanly without a baked AMI, and no
    // transcode capacity is lost (the pool is idle/zero until then anyway).
    this.gpuNodePoolManifest =
      this.bakedGpuAmiId === PLACEHOLDER_GPU_AMI_ID
        ? undefined
        : this.addGpuNodePool();
    this.nvidiaDevicePluginManifest = this.addNvidiaDevicePlugin();

    // Serialize the cluster's manifest/helm applies so they are submitted to a
    // freshly-created EKS control plane ONE AT A TIME rather than as a
    // concurrent burst. CDK applies KubernetesManifest/HelmChart custom
    // resources in parallel by default; against a brand-new cluster that burst
    // trips the EKS control-plane API rate limit
    // (`TooManyRequestsException: Rate Exceeded`) and fails a manifest apply,
    // rolling back the whole stack. An explicit dependency chain
    // (Karpenter chart → NVIDIA device plugin → GPU NodePool CRDs) forces
    // sequential application, which stays under the throttle and also honors
    // the real ordering constraint that Karpenter must exist before its
    // NodePool/EC2NodeClass CRDs are applied.
    this.nvidiaDevicePluginManifest.node.addDependency(this.karpenterChart);
    this.gpuNodePoolManifest?.node.addDependency(this.nvidiaDevicePluginManifest);

    // -----------------------------------------------------------------------
    // Local Nix cache tier (R4.7/R4.8) — node-local persistent /nix store.
    // Builder pods on the same node reuse closures from the local store before
    // falling through to the S3 binary cache. The local tier NEVER becomes the
    // cross-stage source — S3 remains the shared build-once store for
    // Beta/Staging/Production. The local tier is a performance optimization:
    // it eliminates redundant S3 fetches for closures already present on the
    // node from a previous build.
    // -----------------------------------------------------------------------
    this.builderCacheTierManifest = this.addBuilderCacheTier();

    // Outputs the later edge/pipeline/observability stacks (and task 16.3)
    // consume to reference the cluster without re-deriving it.
    new cdk.CfnOutput(this, 'ClusterName', {
      value: this.cluster.clusterName,
      description: 'EKS cluster name for HelloDJ workloads (Decision D1).',
    });

    // The baked GPU AMI id Karpenter launches g5g Spot nodes from. When the
    // placeholder is in effect this surfaces the TODO in the deploy output.
    new cdk.CfnOutput(this, 'BakedGpuAmiId', {
      value: this.bakedGpuAmiId,
      description:
        'Pre-baked minimal NixOS GPU AMI id for the Karpenter transcode NodePool ' +
        '(task 16.2). Placeholder until the pipeline injects the registered id.',
    });

    // The GPU scale-to-zero idle window wired into the NodePool's
    // `consolidateAfter` (R8.5) — surfaced so operators can confirm the shared
    // GPU pool's configured coast-down window matches `gpu_idle_decision`.
    new cdk.CfnOutput(this, 'GpuIdleWindowSeconds', {
      value: `${this.gpuIdleWindowSeconds}`,
      description:
        'GPU scale-to-zero idle window (seconds) for the shared transcode GPU ' +
        'NodePool consolidateAfter (R8.5, default 300, range 60-900).',
    });

    // Surface the scale-up ceiling the shared GPU pool provisions to on
    // workload arrival (R8.6). Together with GpuIdleWindowSeconds this exposes
    // both halves of `gpu_idle_decision`: scale-to-zero after the idle window
    // with no active jobs, and scale-up (to at most this many time-sliced GPU
    // units) the moment a GPU-requiring workload arrives.
    new cdk.CfnOutput(this, 'GpuScaleUpLimit', {
      value: `${KARPENTER_GPU_NODEPOOL_LIMIT}`,
      description:
        'Max time-sliced nvidia.com/gpu units the shared transcode GPU ' +
        'NodePool scales up to on GPU-requiring workload arrival (R8.6).',
    });
  }

  /**
   * Install Karpenter via Helm so it can provision the hybrid `g5g.xlarge`
   * Spot GPU node from the baked AMI on spin-up and consolidate it back to
   * zero on coast-down (Decision D3, R3.2, R3.3, R3.11, 16.4).
   *
   * Karpenter (rather than a managed node group) drives the GPU capacity
   * because it launches nodes directly from an arbitrary AMI (the pre-baked
   * NixOS image), reacts to pending GPU pods in seconds, and consolidates
   * empty nodes to zero — exactly the "pay-while-climbing" behaviour the cost
   * model depends on.
   */
  private installKarpenter(stage: string): eks.HelmChart {
    return this.cluster.addHelmChart('Karpenter', {
      chart: 'karpenter',
      release: 'karpenter',
      repository: 'oci://public.ecr.aws/karpenter/karpenter',
      namespace: 'karpenter',
      createNamespace: true,
      // Pinned so the CRD schema the manifests below target is deterministic;
      // production bumps this in lockstep with the manifest apiVersion.
      version: '1.0.6',
      values: {
        settings: {
          clusterName: this.cluster.clusterName,
          // The interruption queue lets Karpenter observe Spot rebalance /
          // reclaim notices and cordon+drain the node ahead of termination
          // (graceful downshift to CPU — R17.1, R17.2).
          interruptionQueue: `hellodj-${stage}-karpenter-interruption`,
        },
        // Karpenter itself runs on the always-on app fleet, never on the
        // ephemeral GPU nodes it manages.
        nodeSelector: { workload: 'app' },
        controller: {
          resources: {
            requests: { cpu: '0.25', memory: '256Mi' },
            limits: { cpu: '0.5', memory: '512Mi' },
          },
        },
      },
    });
  }

  /**
   * Apply the Karpenter `EC2NodeClass` + `NodePool` that provision the hybrid
   * GPU transcode nodes.
   *
   * The `EC2NodeClass` launches **`g5g.xlarge` Spot** instances directly from
   * the **pre-baked minimal NixOS GPU AMI** (task 16.2). The `NodePool`:
   *   * restricts capacity to Spot + `g5g.xlarge` (smallest G5g meeting load,
   *     R3.8) on arm64 (shares the fleet architecture, R3.7),
   *   * applies the `dedicated=transcode:NoSchedule` taint and
   *     `workload=transcode` label so only transcode pods land there
   *     (Decision D2, R3.11) — identical isolation to the managed CPU group,
   *   * caps GPU count and **consolidates empty nodes to zero** after the
   *     configured idle window (default 300 s, [60, 900] s) so the node is
   *     billed only between spin-up and scale-to-zero (R3.3, 16.4, R8.5, R8.6),
   *   * sets a `terminationGracePeriod` equal to the 120 s drain window so a
   *     Spot reclaim drains GPU jobs back to the CPU path gracefully
   *     (R17.1-17.4).
   */
  private addGpuNodePool(): eks.KubernetesManifest {
    const usingPlaceholder = this.bakedGpuAmiId === PLACEHOLDER_GPU_AMI_ID;

    // Karpenter discovers the VPC subnets and cluster security groups by the
    // standard EKS ownership tag `kubernetes.io/cluster/<name>: owned`.
    const clusterDiscoveryTag: { [key: string]: string } = {
      [`kubernetes.io/cluster/${this.cluster.clusterName}`]: 'owned',
    };

    const ec2NodeClass = {
      apiVersion: 'karpenter.k8s.aws/v1',
      kind: 'EC2NodeClass',
      metadata: {
        name: KARPENTER_GPU_NODEPOOL_NAME,
        annotations: {
          // Make a placeholder AMI unmistakable in the applied manifest.
          'hellodj.bot/baked-ami': usingPlaceholder
            ? `TODO-inject-registered-ami:${this.bakedGpuAmiId}`
            : this.bakedGpuAmiId,
        },
      },
      spec: {
        // Launch directly from the pre-baked NixOS GPU AMI (task 16.2). The
        // NixOS image is custom, so the AMI family is `Custom` and the AMI is
        // selected explicitly by id.
        amiFamily: 'Custom',
        amiSelectorTerms: [{ id: this.bakedGpuAmiId }],
        // Small ~16 GiB gp3 root — the trimmed closure + tmpfs HLS scratch
        // means the node needs almost no durable storage (matches gpu-node.nix).
        blockDeviceMappings: [
          {
            deviceName: '/dev/xvda',
            ebs: {
              volumeSize: '16Gi',
              volumeType: 'gp3',
              deleteOnTermination: true,
              encrypted: true,
            },
          },
        ],
        // Discover subnets/SGs the network stack tags for the cluster.
        subnetSelectorTerms: [{ tags: clusterDiscoveryTag }],
        securityGroupSelectorTerms: [{ tags: clusterDiscoveryTag }],
        // IAM instance role: CloudWatch / ECR / Bedrock via the node role, no
        // static credentials on the host (matches the baked AMI contract).
        role: this.transcodeNodegroup.role.roleName,
      },
    };

    const nodePool = {
      apiVersion: 'karpenter.sh/v1',
      kind: 'NodePool',
      metadata: { name: KARPENTER_GPU_NODEPOOL_NAME },
      spec: {
        // Scale-up on GPU-requiring workload arrival (R8.6). A positive
        // `weight` makes this the preferred NodePool Karpenter provisions from
        // when a pending pod requests `nvidia.com/gpu` (and tolerates the
        // transcode taint + selects `hellodj.bot/gpu=true`). Because no other
        // pool satisfies those constraints, a GPU-requiring workload arriving
        // while the pool is scaled to zero deterministically drives a
        // `g5g.xlarge` Spot GPU node to launch here to serve it — the exact
        // inverse of `gpu_idle_decision` (which only returns scale-to-zero when
        // `active_jobs <= 0`), so the GPU is never at zero under load.
        weight: KARPENTER_GPU_NODEPOOL_WEIGHT,
        template: {
          metadata: {
            labels: {
              [TRANSCODE_LABEL_KEY]: TRANSCODE_LABEL_VALUE,
              'hellodj.bot/arch': 'arm64',
              'hellodj.bot/capacity': 'spot',
              'hellodj.bot/gpu': 'true',
            },
          },
          spec: {
            nodeClassRef: {
              group: 'karpenter.k8s.aws',
              kind: 'EC2NodeClass',
              name: KARPENTER_GPU_NODEPOOL_NAME,
            },
            // Only transcode pods (which tolerate this taint + select the
            // label) schedule onto the GPU node (Decision D2, R3.11).
            taints: [
              {
                key: TRANSCODE_TAINT_KEY,
                value: TRANSCODE_TAINT_VALUE,
                effect: 'NoSchedule',
              },
            ],
            requirements: [
              {
                key: 'karpenter.sh/capacity-type',
                operator: 'In',
                values: ['spot'],
              },
              {
                key: 'node.kubernetes.io/instance-type',
                operator: 'In',
                values: [GPU_INSTANCE_TYPE],
              },
              {
                key: 'kubernetes.io/arch',
                operator: 'In',
                values: ['arm64'],
              },
            ],
            // Give a reclaimed Spot node the full 120 s drain window to move
            // in-flight GPU jobs back to the CPU path before termination
            // (R17.1, R17.3, R17.5; mirrors DEFAULT_DRAIN_TIMEOUT_SECONDS).
            terminationGracePeriod: `${GPU_DRAIN_TIMEOUT_SECONDS}s`,
          },
        },
        // Scale-up ceiling (R8.6): the pool scales up from zero on workload
        // arrival only as far as this many time-sliced `nvidia.com/gpu` units.
        // A burst of pending transcode pods provisions GPU capacity to serve
        // them and no further; CPU covers everything else. Kept equal to the
        // device plugin's advertised replica count so the scale-up ceiling and
        // the sharing factor are one value ({@link KARPENTER_GPU_NODEPOOL_LIMIT}).
        limits: { [GPU_RESOURCE_KEY]: KARPENTER_GPU_NODEPOOL_LIMIT },
        disruption: {
          // Scale the shared GPU pool to zero once it has been **empty** — no
          // active transcode workload — for the configured idle window
          // (default 300 s, configurable within [60, 900] s). This is the
          // scale-to-zero decision R8.5 mandates and the pure
          // `gpu_idle_decision` function models: `consolidationPolicy:
          // WhenEmpty` fires only when zero pods remain (never under load, so
          // the GPU is never torn down with active jobs — R8.6), and
          // `consolidateAfter` is the continuous idle window that must elapse
          // first. The window doubles as the hysteresis that prevents flapping
          // at every brief lull, while the always-on CPU floor covers spin-up
          // latency when a workload later drives scale-up from zero (R8.6).
          consolidationPolicy: 'WhenEmpty',
          consolidateAfter: `${this.gpuIdleWindowSeconds}s`,
        },
      },
    };

    return this.cluster.addManifest('GpuTranscodeNodePool', ec2NodeClass, nodePool);
  }

  /**
   * Deploy the NVIDIA device plugin as a DaemonSet with a **time-slicing**
   * ConfigMap so the single warm `g5g.xlarge` T4G is advertised as
   * {@link GPU_TIME_SLICING_REPLICAS} schedulable `nvidia.com/gpu` units.
   *
   * Time-slicing (the EKS-native GPU_Sharing primitive Decision D1 selected)
   * lets many concurrent transcode/visualizer pods share one physical GPU
   * rather than each demanding a whole GPU (R3.11). The DaemonSet only lands on
   * the GPU nodes: it tolerates the transcode taint and selects the
   * `hellodj.bot/gpu=true` label the NodePool stamps on GPU nodes.
   */
  private addNvidiaDevicePlugin(): eks.KubernetesManifest {
    const namespace = 'nvidia-device-plugin';

    const ns = {
      apiVersion: 'v1',
      kind: 'Namespace',
      metadata: { name: namespace },
    };

    // Time-slicing config: present each physical GPU as N replicas.
    const timeSlicingConfig = {
      apiVersion: 'v1',
      kind: 'ConfigMap',
      metadata: { name: 'time-slicing-config', namespace },
      data: {
        'any': [
          'version: v1',
          'flags:',
          '  migStrategy: none',
          'sharing:',
          '  timeSlicing:',
          '    resources:',
          `    - name: ${GPU_RESOURCE_KEY}`,
          `      replicas: ${GPU_TIME_SLICING_REPLICAS}`,
          '',
        ].join('\n'),
      },
    };

    const daemonSet = {
      apiVersion: 'apps/v1',
      kind: 'DaemonSet',
      metadata: { name: 'nvidia-device-plugin-daemonset', namespace },
      spec: {
        selector: { matchLabels: { name: 'nvidia-device-plugin-ds' } },
        updateStrategy: { type: 'RollingUpdate' },
        template: {
          metadata: { labels: { name: 'nvidia-device-plugin-ds' } },
          spec: {
            // Only run on the hybrid GPU nodes: tolerate the transcode taint
            // and the standard GPU taint, and select the GPU node label.
            tolerations: [
              {
                key: TRANSCODE_TAINT_KEY,
                operator: 'Equal',
                value: TRANSCODE_TAINT_VALUE,
                effect: 'NoSchedule',
              },
              {
                key: GPU_RESOURCE_KEY,
                operator: 'Exists',
                effect: 'NoSchedule',
              },
            ],
            nodeSelector: { 'hellodj.bot/gpu': 'true' },
            priorityClassName: 'system-node-critical',
            containers: [
              {
                name: 'nvidia-device-plugin-ctr',
                image: 'nvcr.io/nvidia/k8s-device-plugin:v0.16.2',
                env: [
                  // Point the plugin at the time-slicing ConfigMap so it
                  // advertises the replica count above (GPU_Sharing, R3.11).
                  { name: 'CONFIG_FILE', value: '/config/any' },
                ],
                securityContext: {
                  allowPrivilegeEscalation: false,
                  capabilities: { drop: ['ALL'] },
                },
                volumeMounts: [
                  { name: 'device-plugin', mountPath: '/var/lib/kubelet/device-plugins' },
                  { name: 'config', mountPath: '/config' },
                ],
              },
            ],
            volumes: [
              {
                name: 'device-plugin',
                hostPath: { path: '/var/lib/kubelet/device-plugins' },
              },
              {
                name: 'config',
                configMap: { name: 'time-slicing-config' },
              },
            ],
          },
        },
      },
    };

    return this.cluster.addManifest(
      'NvidiaDevicePlugin',
      ns,
      timeSlicingConfig,
      daemonSet,
    );
  }

  /**
   * Provisions the node-local Nix store cache tier for builder pods (R4.7/R4.8).
   * Builder pods mount /nix from a hostPath so the store persists across pod
   * restarts on the same node. S3 remains the shared cross-stage source; the
   * local tier is a pull-through performance optimization only.
   */
  private addBuilderCacheTier(): eks.KubernetesManifest {
    return new eks.KubernetesManifest(this, 'BuilderNixCacheTier', {
      cluster: this.cluster,
      manifest: [
        {
          apiVersion: 'v1',
          kind: 'Namespace',
          metadata: { name: 'hellodj-builders' },
        },
        {
          apiVersion: 'v1',
          kind: 'PersistentVolume',
          metadata: {
            name: 'nix-store-local',
            labels: { 'hellodj.bot/purpose': 'nix-builder-cache' },
          },
          spec: {
            capacity: { storage: '50Gi' },
            accessModes: ['ReadWriteOnce'],
            persistentVolumeReclaimPolicy: 'Retain',
            storageClassName: 'nix-store-local',
            local: { path: '/var/lib/nix' },
            nodeAffinity: {
              required: {
                nodeSelectorTerms: [{
                  matchExpressions: [{
                    key: 'kubernetes.io/os',
                    operator: 'In',
                    values: ['linux'],
                  }],
                }],
              },
            },
          },
        },
        {
          apiVersion: 'storage.k8s.io/v1',
          kind: 'StorageClass',
          metadata: { name: 'nix-store-local' },
          provisioner: 'kubernetes.io/no-provisioner',
          volumeBindingMode: 'WaitForFirstConsumer',
        },
        {
          apiVersion: 'v1',
          kind: 'PersistentVolumeClaim',
          metadata: {
            name: 'nix-store-cache',
            namespace: 'hellodj-builders',
          },
          spec: {
            accessModes: ['ReadWriteOnce'],
            storageClassName: 'nix-store-local',
            resources: { requests: { storage: '50Gi' } },
          },
        },
        // -------------------------------------------------------------------
        // Git credential helper configuration for CodeCommit auth (R2.2/R2.3/R2.5/R2.6).
        // Builder pods use IRSA/pod-identity to assume the build IAM role.
        // This ConfigMap provides the git config that enables the AWS
        // credential helper — mounted at /etc/gitconfig in builder pods so
        // `git` authenticates to CodeCommit from the assumed role with no
        // static credential. Error mapping (R2.5/R2.6):
        //   HTTP 403 / credential denial → authentication failure (naming the
        //     input, not proceeding on partial/stale source)
        //   HTTP 404 / "repository does not exist" → missing repo/branch
        //     (naming the input, distinguished from auth failure)
        // -------------------------------------------------------------------
        {
          apiVersion: 'v1',
          kind: 'ConfigMap',
          metadata: {
            name: 'git-codecommit-config',
            namespace: 'hellodj-builders',
            labels: { 'hellodj.bot/purpose': 'codecommit-credential-helper' },
          },
          data: {
            // Global git config enabling the AWS CodeCommit credential helper.
            // Uses the IAM role assumed via IRSA/pod-identity — no static
            // credential stored or transmitted (R2.3).
            gitconfig: [
              '[credential]',
              '    helper = !aws codecommit credential-helper $@',
              '    UseHttpPath = true',
              '',
              '# Error class mapping for build failures (R2.5/R2.6):',
              '# HTTP 403 = authentication failure (credential denial)',
              '# HTTP 404 = missing repo/branch (repository does not exist)',
              '# The build script maps these exit codes to distinct error',
              '# classes, naming the failing input in either case.',
            ].join('\n'),
          },
        },
      ],
    });
  }

  /**
   * Build the standard autoscale tag set applied to every node group.
   *
   * Encoding the scale-out/scale-in thresholds as node-group tags gives the
   * Cluster Autoscaler / HPA configuration a consistent, discoverable source
   * matching `autoscale.py` (R16).
   */
  private autoscaleTags(): { [key: string]: string } {
    return {
      'hellodj:scaleOutThreshold': `${SCALE_OUT_THRESHOLD}`,
      'hellodj:scaleInThreshold': `${SCALE_IN_THRESHOLD}`,
    };
  }

  /**
   * Create a default kubectl/Helm Lambda layer from the bundled asset.
   *
   * `aws-cdk-lib` requires a kubectl layer for a managed EKS cluster. The
   * default is the version-matched `@aws-cdk/lambda-layer-kubectl-v36` layer
   * (bundling kubectl + helm compatible with the EKS 1.36 control plane); a
   * caller may still override it via {@link EksStackProps.kubectlLayer}.
   */
  private defaultKubectlLayer(
    _version: eks.KubernetesVersion,
  ): lambda.ILayerVersion {
    return new KubectlV36Layer(this, 'KubectlLayer');
  }
}

/**
 * Per-component workload definitions for the HelloDJ EKS fleet (task 20.1).
 *
 * This module is the single, declarative catalog of the 12 platform Components
 * (design "Component Decomposition") and the Kubernetes shape each one takes on
 * the EKS cluster: container port, node placement (app node group vs the
 * taint/label-isolated transcode node group), resource requests/limits, the
 * autoscaling (HPA) thresholds, whether it needs a Service, its ALB/CloudFront
 * routing path (if any), and which platform data/secret/AI dependencies it
 * consumes.
 *
 * `WorkloadsStack` (workloads-stack.ts) consumes this catalog to synthesize the
 * Deployment + Service + HPA manifests and the IRSA/Pod-Identity env wiring, so
 * the workload topology lives in one readable place rather than being smeared
 * across the stack construction code.
 *
 * The autoscaling thresholds mirror `autoscale.py` (scale out at 70%) via the
 * `SCALE_OUT_THRESHOLD` constant re-exported from `eks-stack.ts`, keeping IaC
 * and the pure decision logic on one source of truth (Requirements 16.1-16.5).
 *
 * _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 15.1, 15.2, 18.4_
 */

import { SCALE_OUT_THRESHOLD } from './eks-stack';

/**
 * Which platform dependency a component needs env/IAM wiring for. The
 * `WorkloadsStack` translates each flag into environment variables (table
 * names, DAX endpoint, secret ARNs) and IAM grants on the component's service
 * account role (IRSA / EKS Pod Identity — no static keys).
 */
export interface ComponentDependencies {
  /** Reads/writes the `hellodj-core` single table. */
  readonly coreTable?: boolean;
  /** Reads/writes the `hellodj-search-cache` hot table (via DAX). */
  readonly searchCache?: boolean;
  /** Reads/writes the `hellodj-session` hot table (via DAX). */
  readonly sessionTable?: boolean;
  /** Needs the DAX cluster discovery endpoint for hot-path reads (R7.6). */
  readonly dax?: boolean;
  /** Reads the Discord bot token secret. */
  readonly discordBotToken?: boolean;
  /** Reads the first-party Tidal OAuth refresh token secret (R9.2). */
  readonly tidalRefresh?: boolean;
  /** Reads the Spotify credentials secret. */
  readonly spotify?: boolean;
  /** Reads the yt-cipher shared secret. */
  readonly ytCipher?: boolean;
  /**
   * Uses the per-guild bot-avatar assets S3 bucket. The web-ui WRITES avatar
   * bytes (`guild/<gid>/bot-avatar/<hash>.<ext>`); the discord-bot-core READS
   * them. `WorkloadsStack` grants the right access per component and injects
   * `HELLODJ_ASSETS_BUCKET` into the container env.
   */
  readonly assetsBucket?: boolean;
  /**
   * Needs the keyless Bedrock/Transcribe/Polly AI task role (voice-pipeline).
   * When set, the component's pod runs as the shared `aiTaskRole` from the
   * auth stack instead of a component-scoped role (design "Secrets": AI via
   * IAM task roles, no static keys).
   */
  readonly aiTaskRole?: boolean;
}

/** The node group a component's pods are scheduled onto. */
export enum NodePlacement {
  /** The Graviton application node group (`workload=app`). */
  App = 'app',
  /**
   * The taint/label-isolated transcode node group
   * (`workload=transcode`, tolerating `dedicated=transcode:NoSchedule`).
   */
  Transcode = 'transcode',
}

/** Autoscaling (HPA) configuration for a component. */
export interface HpaConfig {
  /** Minimum replica count (the always-available floor). */
  readonly minReplicas: number;
  /** Maximum replica count the HPA may scale to. */
  readonly maxReplicas: number;
  /**
   * Target average CPU utilization percentage. Defaults to the platform
   * scale-out threshold (70%) mirrored from `autoscale.py` so the HPA and the
   * cluster autoscaler share one source of truth (R16.2).
   */
  readonly targetCpuPercent?: number;
}

/** Container compute request/limit for a component. */
export interface ResourceSpec {
  readonly cpuRequest: string;
  readonly memoryRequest: string;
  readonly cpuLimit: string;
  readonly memoryLimit: string;
}

/** A fully declarative description of one platform component's workload. */
export interface ComponentWorkloadSpec {
  /** Component name (matches the `components/<name>/` directory and ECR repo). */
  readonly name: string;
  /** Short human description surfaced as a pod annotation. */
  readonly description: string;
  /** The container's listening port, if the component serves traffic. */
  readonly port?: number;
  /** Node group placement. */
  readonly placement: NodePlacement;
  /** Whether a ClusterIP Service should be created for this component. */
  readonly needsService: boolean;
  /** Compute request/limit. */
  readonly resources: ResourceSpec;
  /** Horizontal Pod Autoscaler config. */
  readonly hpa: HpaConfig;
  /** Platform dependencies (drive env + IAM wiring). */
  readonly dependencies: ComponentDependencies;
  /**
   * If set, the ALB Ingress routes this path prefix to the component's Service
   * (consistent with the edge/CloudFront routing, R18.4). Only the two
   * user-facing HTTP entry points (`web-ui` at `/`, `activity-backend` at
   * `/activity/`) carry a route; everything else is internal.
   */
  readonly ingressPath?: string;
  /**
   * Ingress path type for the ALB rule (`Prefix` by default). The web-ui
   * catch-all uses `Prefix` on `/`.
   */
  readonly ingressPathType?: 'Prefix' | 'Exact' | 'ImplementationSpecific';
}

/**
 * Default HPA target CPU utilization percent — the platform scale-out
 * threshold (70%) mirrored from `autoscale.py` (R16.2).
 */
export const DEFAULT_HPA_TARGET_CPU_PERCENT = Math.round(
  SCALE_OUT_THRESHOLD * 100,
);

/** A modest default resource spec for the lightweight Python services. */
const LIGHT_RESOURCES: ResourceSpec = {
  cpuRequest: '100m',
  memoryRequest: '256Mi',
  cpuLimit: '500m',
  memoryLimit: '512Mi',
};

/** A heavier resource spec for the JVM / media-adjacent services. */
const HEAVY_RESOURCES: ResourceSpec = {
  cpuRequest: '500m',
  memoryRequest: '1Gi',
  cpuLimit: '2',
  memoryLimit: '2Gi',
};

/** The transcode resource spec (CPU libx264 floor + optional NVENC). */
const TRANSCODE_RESOURCES: ResourceSpec = {
  cpuRequest: '1',
  memoryRequest: '1Gi',
  cpuLimit: '4',
  memoryLimit: '4Gi',
};

/**
 * The full catalog of the 12 platform components and their workload shapes.
 *
 * Ordering mirrors the design's Component Decomposition table. Each entry is
 * independently deployable (R15.1): the pipeline builds one Nix OCI image per
 * component and the `WorkloadsStack` renders one Deployment/Service/HPA per
 * component, so a single component can be upgraded in isolation (R15.2).
 */
export const COMPONENT_WORKLOADS: ComponentWorkloadSpec[] = [
  {
    name: 'discord-bot-core',
    description:
      'Discord gateway, cog/command registration, guild policy, watchdogs (R6.1, R6.3).',
    placement: NodePlacement.App,
    needsService: false,
    resources: LIGHT_RESOURCES,
    // Gateway scales by shard count, not CPU; keep a fixed small floor.
    hpa: { minReplicas: 1, maxReplicas: 3 },
    dependencies: {
      coreTable: true,
      sessionTable: true,
      discordBotToken: true,
      // Reads per-guild bot-avatar bytes the web-ui uploaded to S3.
      assetsBucket: true,
    },
  },
  {
    name: 'playback-orchestrator',
    description:
      'Router, classifier, content filter, bans, single writer of session/queue state (R6.1, R6.4).',
    port: 8080,
    placement: NodePlacement.App,
    needsService: true,
    resources: LIGHT_RESOURCES,
    hpa: { minReplicas: 2, maxReplicas: 10 },
    dependencies: {
      coreTable: true,
      searchCache: true,
      sessionTable: true,
      dax: true,
    },
  },
  {
    name: 'lavalink',
    description:
      'Custom fMP4 HLS + SABR + LavaSrc JVM audio server; config from config-renderer (R6.1).',
    port: 2333,
    placement: NodePlacement.App,
    needsService: true,
    resources: HEAVY_RESOURCES,
    hpa: { minReplicas: 1, maxReplicas: 5 },
    dependencies: { ytCipher: true },
  },
  {
    name: 'tidal-stream',
    description:
      'Direct Tidal streaming sidecar; first-party single-app-id OAuth (R6.1, R9.x).',
    port: 8801,
    placement: NodePlacement.App,
    needsService: true,
    resources: LIGHT_RESOURCES,
    hpa: { minReplicas: 1, maxReplicas: 4 },
    dependencies: { tidalRefresh: true },
  },
  {
    name: 'spotify-stream',
    description: 'Direct Spotify streaming sidecar (Rust librespot) (R6.1).',
    port: 8802,
    placement: NodePlacement.App,
    needsService: true,
    resources: LIGHT_RESOURCES,
    hpa: { minReplicas: 1, maxReplicas: 4 },
    dependencies: { spotify: true },
  },
  {
    name: 'yt-cipher',
    description:
      'Remote YouTube signature deciphering (Nix-rebuilt, no Debian) (R6.1).',
    port: 8001,
    placement: NodePlacement.App,
    needsService: true,
    resources: LIGHT_RESOURCES,
    hpa: { minReplicas: 1, maxReplicas: 4 },
    dependencies: { ytCipher: true },
  },
  {
    name: 'potoken-server',
    description:
      'YouTube proof-of-origin token provider (Nix-rebuilt, no Debian) (R6.1).',
    port: 4416,
    placement: NodePlacement.App,
    needsService: true,
    resources: LIGHT_RESOURCES,
    hpa: { minReplicas: 1, maxReplicas: 3 },
    dependencies: {},
  },
  {
    name: 'activity-backend',
    description:
      'Discord Activity server (video/whiteboard/visualizer/lyrics) + WebSocket hub (R6.2, R18.4).',
    port: 8090,
    placement: NodePlacement.App,
    needsService: true,
    resources: LIGHT_RESOURCES,
    hpa: { minReplicas: 2, maxReplicas: 10 },
    dependencies: { coreTable: true, sessionTable: true },
    // Routed behind the ALB/CloudFront at `/activity/` (R18.4), consistent
    // with the edge stack's HLS/`/activity/` split.
    ingressPath: '/activity',
    ingressPathType: 'Prefix',
  },
  {
    name: 'hls-transcode',
    description:
      'HLS transcode (libx264 default / NVENC on G5g) + visualizer rendering (R6.2). ' +
      'Schedules onto the single shared time-sliced Karpenter GPU NodePool across all ' +
      'three stages — no per-stage GPU instance (task 15.1, R8.3, R8.4).',
    port: 8095,
    placement: NodePlacement.Transcode,
    needsService: true,
    resources: TRANSCODE_RESOURCES,
    hpa: { minReplicas: 1, maxReplicas: 20 },
    dependencies: {},
  },
  {
    name: 'voice-pipeline',
    description:
      'Local wakeword ONNX; STT/intent/TTS via Bedrock/Transcribe/Polly over the keyless AI task role (R6.3, R18.4).',
    placement: NodePlacement.App,
    needsService: false,
    resources: LIGHT_RESOURCES,
    hpa: { minReplicas: 1, maxReplicas: 6 },
    dependencies: { sessionTable: true, aiTaskRole: true },
  },
  {
    name: 'web-ui',
    description:
      'Flask + HTMX + Alpine + Tailwind v4 config/admin UI; Cognito + Discord + Tidal OAuth flows (R6.5).',
    port: 8080,
    placement: NodePlacement.App,
    needsService: true,
    resources: LIGHT_RESOURCES,
    hpa: { minReplicas: 2, maxReplicas: 8 },
    dependencies: {
      coreTable: true,
      discordBotToken: true,
      tidalRefresh: true,
      spotify: true,
      // Writes per-guild bot-avatar bytes to S3 for the bot to read.
      assetsBucket: true,
    },
    // Catch-all web entry point routed behind the ALB/CloudFront at `/`
    // (R18.4). Declared after `/activity` so the more-specific activity rule
    // is matched first by the Ingress.
    ingressPath: '/',
    ingressPathType: 'Prefix',
  },
  {
    name: 'config-renderer',
    description:
      'Renders complete lavalink application.yml from Secrets Manager + DynamoDB; runs as an init/Job (R6.1, R7.3).',
    placement: NodePlacement.App,
    needsService: false,
    resources: LIGHT_RESOURCES,
    // A one-shot renderer; a single replica is sufficient (kept out of HPA
    // scaling in the stack, but the config is present for completeness).
    hpa: { minReplicas: 1, maxReplicas: 1 },
    dependencies: { coreTable: true, discordBotToken: true, ytCipher: true },
  },
];

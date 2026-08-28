/**
 * Shared_Foundation singleton helper for the HelloDJ platform (task 1.1).
 *
 * This module encodes the governing principle of the `hellodj-shared-foundation`
 * spec — **one stage's worth of HARDWARE, three stages' worth of SOFTWARE** —
 * as two concrete artifacts the composition (`bin/hellodj.ts`) and the pipeline
 * synth-gate (`pipeline-stack.ts`) consume:
 *
 *   * {@link FoundationRefs} — the set of shared handles every `Software_Stage`'s
 *     {@link WorkloadsStack} receives. The three stages differ only by
 *     `stage`/`region`; they all reference the SAME cluster, data tables + DAX
 *     endpoint, secrets, and keyless AI task role (shared handles, not copies).
 *   * {@link FOUNDATION_SINGLETON_TYPES} + {@link assertFoundationSingleton} —
 *     the `Foundation_Singleton_Invariant` (R1.7/R1.8): the whole synthesized
 *     app contains NO MORE THAN ONE of each foundation resource type. A count of
 *     zero is permitted (R1.7); a second instance throws — failing synth and
 *     producing no deployable app — with an error naming the duplicated type
 *     (R1.8).
 *
 * _Requirements: 1.7, 1.8_
 */
import * as cdk from 'aws-cdk-lib';
import * as eks from 'aws-cdk-lib/aws-eks';
import * as iam from 'aws-cdk-lib/aws-iam';
import { WorkloadsDataRefs, WorkloadsSecretRefs } from './workloads-stack';

/**
 * The shared handles every `Software_Stage`'s {@link WorkloadsStack} consumes.
 *
 * These are references to the once-provisioned {@link Shared_Foundation}
 * resources — the single EKS cluster, the single DAX-backed data layer, the
 * shared Secrets Manager entries, and the shared keyless AI task role. The
 * three stages (`beta`/`staging`/`production`) receive the SAME instances of
 * these handles (not per-stage copies), so attaching three namespaced software
 * stages to the foundation never duplicates the underlying hardware (R1).
 */
export interface FoundationRefs {
  /** The ONE shared EKS cluster all three stages deploy their manifests onto. */
  readonly cluster: eks.ICluster;

  /** The shared DynamoDB tables + DAX endpoint (from `DataStack`). */
  readonly data: WorkloadsDataRefs;

  /** The shared Secrets Manager entries (from `AuthStack`). */
  readonly secrets: WorkloadsSecretRefs;

  /**
   * The shared keyless AI task role (Bedrock/Transcribe/Polly, from
   * `AuthStack`) the voice-pipeline component assumes.
   */
  readonly aiTaskRole: iam.IRole;

  /**
   * The Cognito web-ui app client id (from `AuthStack`), injected into the
   * web-ui container so the admin/registration/recovery hosted-UI flows work
   * (R8.2, R8.3, R8.5). Optional so imported foundations without it still
   * synthesize.
   */
  readonly cognitoClientId?: string;

  /**
   * The Discord OAuth application client id, injected into the web-ui
   * container so day-to-day Discord login works (R8.4). Optional.
   */
  readonly discordClientId?: string;

  /**
   * The shared Flask session signing key value (from an `AuthStack`-owned
   * Secrets Manager secret), placed into the web-ui's Kubernetes Secret so all
   * replicas sign session cookies with the same key (prevents OAuth-callback
   * session loss). Optional.
   */
  readonly flaskSessionKey?: string;

  /**
   * The Cognito user pool id (from `AuthStack`), injected into the web-ui so
   * the admin panel can manage all accounts via the Cognito admin APIs.
   * Optional.
   */
  readonly cognitoUserPoolId?: string;
}

/**
 * The CloudFormation resource types that constitute the {@link Shared_Foundation}
 * and MUST be singletons across the whole synthesized app
 * (`Foundation_Singleton_Invariant`, R1.7/R1.8).
 *
 * The Application Load Balancer and Network Load Balancer are BOTH the
 * CloudFormation type `AWS::ElasticLoadBalancingV2::LoadBalancer`; they are
 * disambiguated by the resource's `Type` property (`application` for the ALB,
 * `network` for the NLB) so each is counted — and bounded to one — separately
 * (see {@link assertFoundationSingleton}).
 */
export const FOUNDATION_SINGLETON_TYPES = {
  /** The VPC (R1.1). */
  vpc: 'AWS::EC2::VPC',
  /** The CDK EKS control-plane custom resource (R1.2). */
  eks: 'Custom::AWSCDK-EKS-Cluster',
  /** The DAX cluster (R1.4). */
  dax: 'AWS::DAX::Cluster',
  /** The single NAT gateway for private-subnet egress (R4.3). */
  nat: 'AWS::EC2::NatGateway',
  /** A managed EKS node group of the shared CPU_Node_Fleet (R1.3). */
  nodegroup: 'AWS::EKS::Nodegroup',
  /**
   * The ALB and NLB share this CloudFormation type; the `Type` property
   * (`application` vs `network`) disambiguates them (R1.5, R1.6).
   */
  loadBalancer: 'AWS::ElasticLoadBalancingV2::LoadBalancer',
} as const;

/**
 * The ELBv2 `Type` property values that distinguish the shared Application Load
 * Balancer (R1.5) from the shared Network Load Balancer (R1.6), both of which
 * are the CloudFormation type
 * {@link FOUNDATION_SINGLETON_TYPES.loadBalancer}.
 */
export const LOAD_BALANCER_TYPES = {
  /** The shared Application Load Balancer (R1.5). */
  alb: 'application',
  /** The shared Network Load Balancer (R1.6). */
  nlb: 'network',
} as const;

/** The `Type` property key on an ELBv2 load balancer resource. */
const LOAD_BALANCER_TYPE_PROP = 'Type';

/**
 * A single counted foundation resource kind and its human-readable name for the
 * duplicate-detection error message (R1.8).
 */
interface FoundationCount {
  /** The name reported in the thrown error when this kind is duplicated. */
  readonly label: string;
  /** How many times this kind appears across all synthesized templates. */
  count: number;
}

/**
 * Enforce the `Foundation_Singleton_Invariant` (R1.7/R1.8).
 *
 * Synthesizes the CDK app and counts each {@link FOUNDATION_SINGLETON_TYPES}
 * resource across ALL synthesized templates. The two load-balancer variants
 * (ALB `Type: application`, NLB `Type: network`) are counted separately even
 * though they share one CloudFormation type. If any foundation kind appears
 * more than once, this throws — failing synth and producing no deployable app
 * (R1.8) — with an error naming the duplicated resource type. A count of zero
 * for any kind is permitted (R1.7).
 *
 * This is invoked in `bin/hellodj.ts` immediately before `app.synth()` and is
 * wired as a synth-time gate in the pipeline build step, so a duplicated
 * foundation resource can never reach a deploy.
 *
 * @param app the CDK app to inspect
 * @throws Error naming the duplicated foundation resource type when any
 *   foundation kind is synthesized more than once
 */
export function assertFoundationSingleton(app: cdk.App): void {
  // Synthesize once and inspect every stack's template. Passing an existing
  // synthesized assembly through is harmless — CDK returns the cached assembly.
  const assembly = app.synth();

  // Initialize a counter per distinct foundation kind. The two load-balancer
  // variants are tracked separately from the raw CloudFormation type.
  const counts: Record<string, FoundationCount> = {
    vpc: { label: FOUNDATION_SINGLETON_TYPES.vpc, count: 0 },
    eks: { label: FOUNDATION_SINGLETON_TYPES.eks, count: 0 },
    dax: { label: FOUNDATION_SINGLETON_TYPES.dax, count: 0 },
    nat: { label: FOUNDATION_SINGLETON_TYPES.nat, count: 0 },
    alb: {
      label: `${FOUNDATION_SINGLETON_TYPES.loadBalancer} (Type: ${LOAD_BALANCER_TYPES.alb})`,
      count: 0,
    },
    nlb: {
      label: `${FOUNDATION_SINGLETON_TYPES.loadBalancer} (Type: ${LOAD_BALANCER_TYPES.nlb})`,
      count: 0,
    },
  };

  // The CPU_Node_Fleet is ONE shared fleet made of SEVERAL distinctly-named
  // managed node groups (`hellodj-app-ondemand`, `hellodj-app-spot`,
  // `hellodj-transcode`). The singleton invariant for the fleet is therefore
  // "no node-group NAME is provisioned more than once" — the per-stage
  // duplication failure mode would produce e.g. two `hellodj-app-ondemand`
  // groups — NOT "no more than one node group total" (which the shared fleet's
  // three distinct groups would falsely trip). Node groups are counted per
  // NodegroupName; groups synthesized without an explicit name are bucketed
  // under a single `<unnamed>` key so multiple unnamed groups are still caught.
  const UNNAMED_NODEGROUP = '<unnamed>';
  const nodegroupCounts = new Map<string, number>();

  for (const stackArtifact of assembly.stacks) {
    const resources: Record<string, { Type?: string; Properties?: Record<string, unknown> }> =
      (stackArtifact.template?.Resources as Record<
        string,
        { Type?: string; Properties?: Record<string, unknown> }
      >) ?? {};

    for (const resource of Object.values(resources)) {
      const cfnType = resource.Type;
      if (!cfnType) {
        continue;
      }
      switch (cfnType) {
        case FOUNDATION_SINGLETON_TYPES.vpc:
          counts.vpc.count += 1;
          break;
        case FOUNDATION_SINGLETON_TYPES.eks:
          counts.eks.count += 1;
          break;
        case FOUNDATION_SINGLETON_TYPES.dax:
          counts.dax.count += 1;
          break;
        case FOUNDATION_SINGLETON_TYPES.nat:
          counts.nat.count += 1;
          break;
        case FOUNDATION_SINGLETON_TYPES.nodegroup: {
          // Count per NodegroupName so the shared fleet's distinctly-named
          // groups each count once, but a per-stage duplicate of any single
          // name is flagged.
          const ngName =
            (resource.Properties?.['NodegroupName'] as string | undefined) ??
            UNNAMED_NODEGROUP;
          nodegroupCounts.set(ngName, (nodegroupCounts.get(ngName) ?? 0) + 1);
          break;
        }
        case FOUNDATION_SINGLETON_TYPES.loadBalancer: {
          // Disambiguate the shared ALB from the shared NLB by the `Type` prop.
          const lbType = resource.Properties?.[LOAD_BALANCER_TYPE_PROP];
          if (lbType === LOAD_BALANCER_TYPES.alb) {
            counts.alb.count += 1;
          } else if (lbType === LOAD_BALANCER_TYPES.nlb) {
            counts.nlb.count += 1;
          }
          break;
        }
        default:
          break;
      }
    }
  }

  // Fold the per-name node-group counts into the duplicate check: any single
  // NodegroupName provisioned more than once is a per-stage duplication of the
  // shared CPU_Node_Fleet and violates the invariant.
  for (const [ngName, count] of nodegroupCounts) {
    if (count > 1) {
      const nameSuffix =
        ngName === UNNAMED_NODEGROUP ? '' : ` (NodegroupName: ${ngName})`;
      counts[`nodegroup:${ngName}`] = {
        label: `${FOUNDATION_SINGLETON_TYPES.nodegroup}${nameSuffix}`,
        count,
      };
    }
  }

  // Any foundation kind synthesized more than once violates the invariant.
  const duplicated = Object.values(counts).filter((c) => c.count > 1);
  if (duplicated.length > 0) {
    const details = duplicated
      .map((c) => `${c.label} (found ${c.count}, expected at most 1)`)
      .join('; ');
    throw new Error(
      'Foundation_Singleton_Invariant violated: the Shared_Foundation must be ' +
        'provisioned exactly once and shared across all stages, but the ' +
        'synthesized app duplicates the following foundation resource ' +
        `type(s): ${details}. No deployable application is produced.`,
    );
  }
}

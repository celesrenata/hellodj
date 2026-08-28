/**
 * Component workloads stack for the HelloDJ AWS platform (task 20.1).
 *
 * This is the end-to-end wiring stack that composes the 12 independently
 * deployable platform Components (design "Component Decomposition") into
 * concrete EKS workloads on the cluster the {@link EksStack} provisions, so a
 * single `cdk deploy` stands up the whole platform with no manual console
 * steps (Requirements 1.2, 1.3, 1.4).
 *
 * For each component in {@link COMPONENT_WORKLOADS} it adds, via
 * `cluster.addManifest`:
 *
 *   * a **ServiceAccount** annotated for **EKS Pod Identity / IRSA** so the
 *     pod assumes a per-component IAM role and reaches DynamoDB, DAX, Secrets
 *     Manager, and (for voice-pipeline) Bedrock/Transcribe/Polly with **no
 *     static keys** (design "Secrets");
 *   * a **Deployment** referencing the component's **Nix-built OCI image**
 *     (an ECR image URI; the concrete tag is injected by the pipeline, task
 *     18.1 — a clearly-marked `TODO`/placeholder tag is used until then), with
 *     the correct **node placement** (app node group, or the taint/label
 *     isolated transcode node group with a matching toleration for
 *     `hls-transcode`), and **env wiring** for the DynamoDB table names, the
 *     DAX endpoint, and the Secrets Manager secret ARNs it depends on;
 *   * a **HorizontalPodAutoscaler** keyed to the platform scale-out threshold
 *     (70%, mirrored from `autoscale.py`) between the component's min/max
 *     replicas (Requirements 16.1-16.5);
 *   * a **Service** for the components that serve traffic.
 *
 * It then adds a single **ALB Ingress** routing `web-ui` (`/`) and
 * `activity-backend` (`/activity/`) behind the load balancer, consistent with
 * the CloudFront/edge routing (Requirement 18.4).
 *
 * The stack is **additive**: it depends only on the cluster + data + auth
 * resources passed in via {@link WorkloadsStackProps} (the sibling stacks'
 * exposed props), and it is instantiated in `bin/hellodj.ts` alongside the
 * existing stacks so the composed app synthesizes and deploys as one unit
 * (R1.2). It grants each component's role least-privilege access to exactly
 * the tables/secrets its {@link ComponentDependencies} declare.
 *
 * _Requirements: 1.2, 1.3, 1.4, 6.1, 6.2, 6.3, 6.4, 6.5, 15.1, 15.2, 18.4_
 */
import * as cdk from 'aws-cdk-lib';
import * as eks from 'aws-cdk-lib/aws-eks';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as secretsmanager from 'aws-cdk-lib/aws-secretsmanager';
import { Construct } from 'constructs';
import {
  COMPONENT_WORKLOADS,
  ComponentWorkloadSpec,
  DEFAULT_HPA_TARGET_CPU_PERCENT,
  NodePlacement,
} from './component-workloads';
import {
  TRANSCODE_TAINT_KEY,
  TRANSCODE_TAINT_VALUE,
  TRANSCODE_LABEL_KEY,
  TRANSCODE_LABEL_VALUE,
} from './eks-stack';

/** The DNS zone every stage endpoint is a subdomain of (mirrors `dns_naming`). */
export const HELLODJ_ZONE = 'hellodj.bot';

/** The default deployment stage when none is supplied. */
export const DEFAULT_STAGE = 'beta';

/**
 * The per-stage Kubernetes namespace all of a stage's HelloDJ workloads run in
 * (task 15.1, R8.2). Beta/Staging/Production are consolidated onto the single
 * shared GPU host and isolated by a distinct {@link StageEndpoint} — namespace
 * `hellodj-<stage>`, port, and hostname `<stage>.<region>.hellodj.bot`. There
 * is no separate GPU instance per stage (R8.3): the shared time-sliced
 * Karpenter GPU NodePool + single GPU AMI in {@link EksStack} serve all three.
 *
 * @param stage the deployment stage (`beta` / `staging` / `production`)
 */
export function workloadsNamespace(stage: string): string {
  return `hellodj-${stage}`;
}

/**
 * Derive the per-stage Ingress hostname `<stage>.<region>.hellodj.bot`,
 * mirroring the Python `dns_naming.derive_env_name` single source of truth so
 * IaC and runtime agree on every name (R8.2, R8.7, R9.3). Both a stage and a
 * region are required — a request to one Stage_Endpoint's hostname reaches only
 * that stage's namespace (R8.7).
 */
export function stageHostname(stage: string, region: string): string {
  const stageLabel = stage.trim().toLowerCase();
  const regionLabel = region.trim().toLowerCase();
  if (!stageLabel || !regionLabel) {
    throw new Error(
      'both a stage and a region are required to derive a stage hostname',
    );
  }
  return `${stageLabel}.${regionLabel}.${HELLODJ_ZONE}`;
}

/**
 * A single stage's isolated endpoint on the shared GPU host — the TypeScript
 * mirror of the Python `hellodj_platform_logic.types.StageEndpoint` (R8.2).
 * The three stages differ only by their `namespace` and `hostname`; they share
 * one cluster, one GPU AMI, and one time-sliced GPU NodePool (R8.3, R8.4).
 */
export interface StageEndpoint {
  /** The deployment stage: `beta` / `staging` / `production`. */
  readonly stage: string;
  /** The Kubernetes namespace `hellodj-<stage>` isolating the stage. */
  readonly namespace: string;
  /** The Ingress listener port the stage serves on. */
  readonly port: number;
  /** The hostname `<stage>.<region>.hellodj.bot` routing to this stage. */
  readonly hostname: string;
}

/** The HTTPS port every stage's hostname-routed Ingress listens on. */
export const STAGE_ENDPOINT_PORT = 443;

/**
 * The IngressGroup name that merges the three per-namespace stage Ingresses
 * (`hellodj-beta` / `-staging` / `-production`) onto the **single shared ALB**
 * (R1.5). Because each stage renders its own `Ingress` object in its own
 * namespace, the AWS Load Balancer Controller only provisions one ALB for all
 * three when they declare the **same** `alb.ingress.kubernetes.io/group.name`
 * (a stage-independent constant, so the ALB is a foundation singleton, not one
 * per stage).
 */
export const SHARED_ALB_GROUP_NAME = 'hellodj';

/**
 * The name of the synthetic ALB action (referenced by an
 * `alb.ingress.kubernetes.io/actions.<name>` annotation) that returns a fixed
 * **404 (no matching host)** response. It is bound to a hostless catch-all rule
 * so a request whose hostname matches no provisioned {@link StageEndpoint}
 * hostname is routed to **no** namespace and explicitly rejected (R5.3).
 */
export const ALB_DEFAULT_404_ACTION = 'default-404';

/**
 * The fixed-response 404 action config the AWS Load Balancer Controller renders
 * as the shared ALB's catch-all rule for an unmatched host (R5.3). This is the
 * JSON value of the `alb.ingress.kubernetes.io/actions.<name>` annotation.
 */
export const ALB_DEFAULT_404_ACTION_CONFIG = {
  type: 'fixed-response',
  fixedResponseConfig: {
    contentType: 'text/plain',
    statusCode: '404',
    messageBody: 'no matching host',
  },
} as const;

/**
 * The log level a stage's workloads run at.
 *
 * Non-production stages (`beta`, `staging`) run with **debug logging on** so
 * issues are diagnosable pre-promotion; **production runs at `INFO`** (debug
 * off) to keep the Log_Store lean and avoid leaking verbose internals. This is
 * surfaced to every component as the `LOG_LEVEL` / `HELLODJ_DEBUG` env vars.
 *
 * @param stage the deployment stage (`beta` / `staging` / `production`)
 * @returns `'DEBUG'` for beta/staging, `'INFO'` for production
 */
export function stageLogLevel(stage: string): 'DEBUG' | 'INFO' {
  return stage.trim().toLowerCase() === 'production' ? 'INFO' : 'DEBUG';
}

/**
 * Build the {@link StageEndpoint} for a stage/region: namespace
 * `hellodj-<stage>`, hostname `<stage>.<region>.hellodj.bot`, HTTPS port. This
 * is the CDK mirror of the Python `StageEndpoint`; `route_endpoint`'s exact
 * hostname→endpoint match (Property 9 / R8.7) is realized by the Ingress
 * `host` rule below.
 */
export function stageEndpoint(stage: string, region: string): StageEndpoint {
  return {
    stage,
    namespace: workloadsNamespace(stage),
    port: STAGE_ENDPOINT_PORT,
    hostname: stageHostname(stage, region),
  };
}

/**
 * The container image tag the pipeline (task 18.1) injects per component.
 *
 * Until the pipeline supplies a resolved, immutable tag, this clearly-marked
 * placeholder is used so the stack synthesizes and type-checks. It is
 * intentionally obvious in the rendered manifest so a placeholder never
 * silently ships to a real environment.
 *
 * NOTE: The pipeline pushes `:latest` on every successful build, so `latest`
 * is the operational default. The TODO placeholder was never injected at
 * runtime — CDK Pipelines synthesizes templates BEFORE the build steps run,
 * making dynamic tag injection impossible without a parameter store or
 * post-synth mechanism. Using `latest` + `imagePullPolicy: Always` is the
 * correct approach for a continuously-deployed pipeline.
 */
export const PLACEHOLDER_IMAGE_TAG = 'latest';

/**
 * Data/DAX/secret resources the workloads wire to. Pass the sibling stacks'
 * exposed props (from `DataStack` / `AuthStack`).
 */
export interface WorkloadsDataRefs {
  readonly coreTable: dynamodb.ITable;
  readonly searchCacheTable: dynamodb.ITable;
  readonly sessionTable: dynamodb.ITable;
  /** DAX cluster discovery endpoint (host:port), from `DataStack.daxEndpoint`. */
  readonly daxEndpoint: string;
}

/** Secrets Manager entries the workloads wire to (from `AuthStack`). */
export interface WorkloadsSecretRefs {
  readonly discordBotToken: secretsmanager.ISecret;
  readonly tidalRefresh: secretsmanager.ISecret;
  readonly spotify: secretsmanager.ISecret;
  readonly ytCipher: secretsmanager.ISecret;
}

/** Properties for {@link WorkloadsStack}. */
export interface WorkloadsStackProps extends cdk.StackProps {
  /** The EKS cluster the workloads are deployed onto (from `EksStack.cluster`). */
  readonly cluster: eks.ICluster;

  /** The DynamoDB tables + DAX endpoint the components consume. */
  readonly data: WorkloadsDataRefs;

  /** The Secrets Manager entries the components consume. */
  readonly secrets: WorkloadsSecretRefs;

  /**
   * The keyless AI task role (Bedrock/Transcribe/Polly) from `AuthStack`,
   * assumed by the voice-pipeline component (design "Secrets": AI via IAM
   * task roles, no static keys).
   */
  readonly aiTaskRole: iam.IRole;

  /**
   * The ECR registry base URI images are pulled from, e.g.
   * `123456789012.dkr.ecr.us-east-1.amazonaws.com/hellodj`. Each component's
   * image is `${ecrRegistry}/<component>:<tag>`. When unset, an
   * account/region-derived registry is used and the image tag stays the
   * pipeline-injected placeholder.
   *
   * @default `${account}.dkr.ecr.${region}.amazonaws.com/hellodj`
   */
  readonly ecrRegistry?: string;

  /**
   * Per-component image tag override. The pipeline (task 18.1) injects the
   * resolved immutable tag per component here; any component not present falls
   * back to {@link PLACEHOLDER_IMAGE_TAG}.
   */
  readonly imageTags?: Record<string, string>;

  /**
   * The deployment stage name (`beta` / `staging` / `production`) used to
   * derive the per-stage namespace (`hellodj-<stage>`) and Ingress hostname
   * (`<stage>.<region>.hellodj.bot`) that isolate this stage on the single
   * shared GPU host (task 15.1, R8.2). Defaults to {@link DEFAULT_STAGE}.
   */
  readonly stage?: string;

  /**
   * The AWS region used to derive the stage's Ingress hostname
   * `<stage>.<region>.hellodj.bot`. Defaults to the stack's resolved region
   * (`this.region`) when unset, so the hostname mirrors `dns_naming`.
   */
  readonly region?: string;

  /**
   * The Cognito user-pool web-ui app client id (from `AuthStack`), injected
   * into the web-ui container so the admin/registration/recovery hosted-UI
   * flows work. When unset, the web-ui's Cognito buttons produce an empty
   * `client_id` and the hosted-UI redirect is broken (R8.2, R8.3, R8.5).
   */
  readonly cognitoClientId?: string;

  /**
   * The Discord OAuth application client id (from `AuthStack` secrets/config),
   * injected into the web-ui container so day-to-day Discord login works. When
   * unset, the Discord login button produces an empty `client_id` (R8.4).
   */
  readonly discordClientId?: string;

  /**
   * The shared Flask session signing key value (from an `AuthStack`-owned
   * Secrets Manager secret), placed into the per-stage `web-ui-flask-secret`
   * Kubernetes Secret so all web-ui replicas sign session cookies with the
   * SAME key. Without a shared key, an OAuth login started on one pod and its
   * callback landing on another pod can't validate the signed cookie and the
   * user is bounced back to /login. Passing the value from AuthStack (rather
   * than generating it here) avoids a cross-stack dependency cycle with the
   * shared cluster's eks stack.
   */
  readonly flaskSessionKey?: string;
}

/**
 * Composes every platform component into EKS Deployments/Services/HPAs with
 * IRSA/Pod-Identity data/secret/AI wiring and an ALB Ingress for the two
 * user-facing HTTP entry points.
 */
export class WorkloadsStack extends cdk.Stack {
  /** The namespace manifest all workloads share. */
  public readonly namespaceManifest: eks.KubernetesManifest;

  /** Per-component service-account IAM roles, keyed by component name. */
  public readonly serviceAccounts: Record<string, eks.ServiceAccount> = {};

  /** Per-component workload manifests (Deployment + Service + HPA), by name. */
  public readonly workloadManifests: Record<string, eks.KubernetesManifest> =
    {};

  /** The ALB Ingress manifest routing web-ui (`/`) and activity (`/activity/`). */
  public readonly ingressManifest: eks.KubernetesManifest;

  /**
   * The Kubernetes Secret holding the shared Flask session signing key for the
   * web-ui replicas (prevents OAuth-callback session loss across pods).
   */
  public readonly webUiFlaskSecretManifest: eks.KubernetesManifest;

  /** The deployment stage this stack's workloads belong to. */
  public readonly stage: string;

  /** The per-stage namespace (`hellodj-<stage>`) isolating this stage (R8.2). */
  public readonly namespace: string;

  /**
   * This stage's isolated {@link StageEndpoint} on the shared GPU host —
   * namespace `hellodj-<stage>`, port, hostname `<stage>.<region>.hellodj.bot`
   * (task 15.1, R8.2). A request to this hostname routes only to this stage's
   * namespace (R8.7).
   */
  public readonly stageEndpoint: StageEndpoint;

  private readonly cluster: eks.ICluster;
  private readonly props: WorkloadsStackProps;
  private readonly ecrRegistry: string;

  constructor(scope: Construct, id: string, props: WorkloadsStackProps) {
    super(scope, id, props);
    this.cluster = props.cluster;
    this.props = props;
    this.ecrRegistry =
      props.ecrRegistry ??
      `${this.account}.dkr.ecr.${this.region}.amazonaws.com/hellodj`;

    // Per-stage isolation on the single shared GPU host: derive the namespace
    // `hellodj-<stage>` and the Ingress hostname `<stage>.<region>.hellodj.bot`
    // that make up this stage's distinct StageEndpoint (task 15.1, R8.2). The
    // three stages differ only by namespace + hostname; they share one cluster,
    // one GPU AMI, and one time-sliced Karpenter GPU NodePool (R8.3, R8.4).
    this.stage = props.stage ?? DEFAULT_STAGE;
    this.namespace = workloadsNamespace(this.stage);
    this.stageEndpoint = stageEndpoint(this.stage, props.region ?? this.region);

    // The per-stage namespace for every HelloDJ workload in this stage.
    // All three stages add their manifests to the ONE shared cluster, so every
    // cluster-child construct id must be stage-unique (`<stage>-...`) to avoid
    // an id collision in the shared cluster's construct tree (R2.1, R1.7).
    this.namespaceManifest = this.cluster.addManifest(`${this.stage}-HelloDjNamespace`, {
      apiVersion: 'v1',
      kind: 'Namespace',
      metadata: {
        name: this.namespace,
        labels: {
          'app.kubernetes.io/part-of': 'hellodj',
          'hellodj.bot/stage': this.stage,
        },
      },
    });

    // Per-stage Flask session signing secret for the web-ui, as a Kubernetes
    // Secret shared by all web-ui replicas. A per-replica random key (the
    // app's fallback when FLASK_SECRET_KEY is unset) breaks the OAuth flow:
    // the login request and its callback can hit different pods, and a cookie
    // signed by one pod's key fails validation on another, dropping the CSRF
    // state and bouncing the user back to /login. A single shared, stable key
    // fixes it. The value comes from the AuthStack-owned Secrets Manager
    // secret (threaded via props) so referencing it here does NOT create a
    // cross-stack dependency cycle with the shared cluster's eks stack.
    this.webUiFlaskSecretManifest = this.cluster.addManifest(
      `${this.stage}-WebUiFlaskSecret`,
      {
        apiVersion: 'v1',
        kind: 'Secret',
        metadata: {
          name: 'web-ui-flask-secret',
          namespace: this.namespace,
        },
        type: 'Opaque',
        stringData: {
          FLASK_SECRET_KEY:
            this.props.flaskSessionKey ??
            'PLACEHOLDER-set-via-flaskSessionKey-prop',
        },
      },
    );
    this.webUiFlaskSecretManifest.node.addDependency(this.namespaceManifest);

    // Render each component's workload. Every component gets its own manifests
    // so a single component can be upgraded independently (R15.1, R15.2).
    //
    // SERIALIZE the applies: each component's ServiceAccount (IRSA) + workload
    // is a Kubernetes-manifest custom resource applied to the shared cluster
    // via the kubectl provider. CDK applies these in PARALLEL by default;
    // against a freshly-created EKS control plane, ~13 components × 3 stages
    // (~39 ServiceAccount manifests + ~39 workload manifests) applied at once
    // trips the EKS control-plane API rate limit
    // (`TooManyRequestsException: Rate Exceeded`) and fails the create. We
    // chain each component to the previous one so the applies are submitted
    // one at a time, staying under the throttle. (Ordering between components
    // is otherwise irrelevant, so a linear chain is safe.)
    let previous: eks.KubernetesManifest | undefined;
    for (const spec of COMPONENT_WORKLOADS) {
      const manifest = this.addComponent(spec, previous);
      manifest.node.addDependency(this.namespaceManifest);
      // The web-ui references the Flask session Secret via secretKeyRef, so it
      // must exist before the web-ui Deployment is applied.
      if (spec.name === 'web-ui') {
        manifest.node.addDependency(this.webUiFlaskSecretManifest);
      }
      this.workloadManifests[spec.name] = manifest;
      previous = manifest;
    }

    // Single ALB Ingress fronting the two user-facing HTTP entry points,
    // consistent with the CloudFront/edge routing (R18.4).
    this.ingressManifest = this.addIngress();
    this.ingressManifest.node.addDependency(this.namespaceManifest);
    for (const spec of COMPONENT_WORKLOADS) {
      if (spec.ingressPath && this.workloadManifests[spec.name]) {
        this.ingressManifest.node.addDependency(
          this.workloadManifests[spec.name],
        );
      }
    }

    new cdk.CfnOutput(this, 'WorkloadsNamespace', {
      value: this.namespace,
      description:
        `Per-stage Kubernetes namespace hosting the ${this.stage} HelloDJ ` +
        'workloads on the single shared GPU host (R8.2).',
    });
    new cdk.CfnOutput(this, 'StageEndpointHostname', {
      value: this.stageEndpoint.hostname,
      description:
        `Hostname routing only to the ${this.stage} stage's namespace ` +
        `(${this.namespace}) on the shared host (R8.7).`,
    });
    new cdk.CfnOutput(this, 'EcrRegistryBase', {
      value: this.ecrRegistry,
      description:
        'ECR registry base for the Nix-built component images ' +
        '(<registry>/<component>:<tag>; tag injected by the pipeline, task 18.1).',
    });
  }

  /**
   * Add a single component's ServiceAccount (IRSA), Deployment, optional
   * Service, and HPA, and grant its role least-privilege access to the
   * tables/secrets/AI it declares.
   */
  private addComponent(
    spec: ComponentWorkloadSpec,
    previous?: eks.KubernetesManifest,
  ): eks.KubernetesManifest {
    const sa = this.createServiceAccount(spec);
    this.grantDependencies(spec, sa);
    // Serialize against a fresh control plane: this component's ServiceAccount
    // manifest waits for the PREVIOUS component's workload to finish applying,
    // so the ~2 manifests per component are submitted one component at a time
    // rather than all at once (avoids the EKS API `Rate Exceeded` throttle).
    if (previous) {
      sa.node.addDependency(previous);
    }

    const docs: Record<string, unknown>[] = [
      this.deployment(spec, sa.serviceAccountName),
    ];
    if (spec.needsService && spec.port) {
      docs.push(this.service(spec));
    }
    docs.push(this.hpa(spec));

    const manifest = this.cluster.addManifest(
      `${this.stage}-Workload-${spec.name}`,
      ...docs,
    );
    // The ServiceAccount is created by an EKS `ServiceAccount` construct (its
    // own manifest); ensure the workload lands after it.
    manifest.node.addDependency(sa);
    return manifest;
  }

  /**
   * Create the component's Kubernetes ServiceAccount backed by an IAM role
   * (IRSA / EKS Pod Identity). The voice-pipeline reuses the shared keyless
   * AI task role; every other component gets a dedicated, least-privilege
   * role the grants below attach policies to (no static keys — design
   * "Secrets").
   */
  private createServiceAccount(spec: ComponentWorkloadSpec): eks.ServiceAccount {
    const sa = this.cluster.addServiceAccount(`${this.stage}-Sa-${spec.name}`, {
      name: `hellodj-${spec.name}`,
      namespace: this.namespace,
    });
    // The ServiceAccount depends on the namespace existing first.
    sa.node.addDependency(this.namespaceManifest);
    this.serviceAccounts[spec.name] = sa;
    return sa;
  }

  /**
   * Grant the component's IRSA role least-privilege access to exactly the
   * DynamoDB tables and Secrets Manager entries its dependencies declare, and
   * (for voice-pipeline) allow it to assume/serve the keyless Bedrock AI task
   * role. All access is role-based; no static credentials are injected.
   */
  private grantDependencies(
    spec: ComponentWorkloadSpec,
    sa: eks.ServiceAccount,
  ): void {
    const deps = spec.dependencies;
    if (deps.coreTable) {
      this.props.data.coreTable.grantReadWriteData(sa.role);
    }
    if (deps.searchCache) {
      this.props.data.searchCacheTable.grantReadWriteData(sa.role);
    }
    if (deps.sessionTable) {
      this.props.data.sessionTable.grantReadWriteData(sa.role);
    }
    if (deps.dax) {
      // DAX data-plane access is authorized by an IAM policy on the caller's
      // role (`dax:*` item/query/scan actions), keyless via IRSA.
      sa.role.addToPrincipalPolicy(
        new iam.PolicyStatement({
          sid: 'DaxDataPlane',
          effect: iam.Effect.ALLOW,
          actions: [
            'dax:GetItem',
            'dax:BatchGetItem',
            'dax:Query',
            'dax:Scan',
            'dax:PutItem',
            'dax:UpdateItem',
            'dax:DeleteItem',
            'dax:BatchWriteItem',
          ],
          resources: [
            `arn:aws:dax:${this.region}:${this.account}:cache/hellodj-dax`,
          ],
        }),
      );
    }
    if (deps.discordBotToken) {
      this.props.secrets.discordBotToken.grantRead(sa.role);
    }
    if (deps.tidalRefresh) {
      this.props.secrets.tidalRefresh.grantRead(sa.role);
    }
    if (deps.spotify) {
      this.props.secrets.spotify.grantRead(sa.role);
    }
    if (deps.ytCipher) {
      this.props.secrets.ytCipher.grantRead(sa.role);
    }
    if (deps.aiTaskRole) {
      // The voice-pipeline reaches Bedrock/Transcribe/Polly through the shared
      // keyless AI task role from the auth stack. Rather than duplicate the
      // Bedrock/Transcribe/Polly policy statements, let the component's IRSA
      // role assume that dedicated task role (no static keys either way).
      sa.role.addToPrincipalPolicy(
        new iam.PolicyStatement({
          sid: 'AssumeAiTaskRole',
          effect: iam.Effect.ALLOW,
          actions: ['sts:AssumeRole'],
          resources: [this.props.aiTaskRole.roleArn],
        }),
      );
    }
  }

  /** Resolve the Nix-built OCI image URI for a component. */
  private imageUri(spec: ComponentWorkloadSpec): string {
    const tag = this.props.imageTags?.[spec.name] ?? PLACEHOLDER_IMAGE_TAG;
    return `${this.ecrRegistry}/${spec.name}:${tag}`;
  }

  /** Standard pod labels for a component, stamped with the stage (R8.2). */
  private labels(spec: ComponentWorkloadSpec): Record<string, string> {
    return {
      'app.kubernetes.io/name': spec.name,
      'app.kubernetes.io/part-of': 'hellodj',
      'hellodj.bot/stage': this.stage,
    };
  }

  /**
   * The node placement fields (nodeSelector + tolerations) for a component.
   * App components target `workload=app`; the transcode component targets the
   * taint/label-isolated transcode node group and tolerates its taint.
   */
  private placement(spec: ComponentWorkloadSpec): {
    nodeSelector: Record<string, string>;
    tolerations?: Record<string, unknown>[];
  } {
    if (spec.placement === NodePlacement.Transcode) {
      return {
        nodeSelector: { [TRANSCODE_LABEL_KEY]: TRANSCODE_LABEL_VALUE },
        tolerations: [
          {
            key: TRANSCODE_TAINT_KEY,
            operator: 'Equal',
            value: TRANSCODE_TAINT_VALUE,
            effect: 'NoSchedule',
          },
        ],
      };
    }
    return { nodeSelector: { workload: 'app' } };
  }

  /**
   * Build the component's environment variables from its declared
   * dependencies: DynamoDB table names, the DAX endpoint, and the ARNs of the
   * secrets it may read (resolved from Secrets Manager at runtime by the
   * component using its IRSA role — the ARN, not the secret value, is in env).
   */
  private containerEnv(spec: ComponentWorkloadSpec): Record<string, unknown>[] {
    const deps = spec.dependencies;
    // Per-stage log level: beta/staging run with debug logging on, production
    // runs at INFO (debug off) to keep the Log_Store lean.
    const logLevel = stageLogLevel(this.stage);
    const env: Record<string, unknown>[] = [
      { name: 'HELLODJ_STAGE', value: this.stage },
      { name: 'HELLODJ_NAMESPACE', value: this.namespace },
      { name: 'HELLODJ_STAGE_HOSTNAME', value: this.stageEndpoint.hostname },
      { name: 'AWS_REGION', value: this.region },
      { name: 'LOG_LEVEL', value: logLevel },
      { name: 'HELLODJ_DEBUG', value: logLevel === 'DEBUG' ? 'true' : 'false' },
    ];
    if (deps.coreTable) {
      env.push({
        name: 'HELLODJ_CORE_TABLE',
        value: this.props.data.coreTable.tableName,
      });
    }
    if (deps.searchCache) {
      env.push({
        name: 'HELLODJ_SEARCH_CACHE_TABLE',
        value: this.props.data.searchCacheTable.tableName,
      });
    }
    if (deps.sessionTable) {
      env.push({
        name: 'HELLODJ_SESSION_TABLE',
        value: this.props.data.sessionTable.tableName,
      });
    }
    if (deps.dax) {
      env.push({
        name: 'HELLODJ_DAX_ENDPOINT',
        value: this.props.data.daxEndpoint,
      });
    }
    if (deps.discordBotToken) {
      env.push({
        name: 'HELLODJ_DISCORD_BOT_TOKEN_SECRET_ARN',
        value: this.props.secrets.discordBotToken.secretArn,
      });
    }
    if (deps.tidalRefresh) {
      env.push({
        name: 'HELLODJ_TIDAL_REFRESH_SECRET_ARN',
        value: this.props.secrets.tidalRefresh.secretArn,
      });
    }
    if (deps.spotify) {
      env.push({
        name: 'HELLODJ_SPOTIFY_SECRET_ARN',
        value: this.props.secrets.spotify.secretArn,
      });
    }
    if (deps.ytCipher) {
      env.push({
        name: 'HELLODJ_YT_CIPHER_SECRET_ARN',
        value: this.props.secrets.ytCipher.secretArn,
      });
    }
    if (deps.aiTaskRole) {
      env.push({
        name: 'HELLODJ_AI_TASK_ROLE_ARN',
        value: this.props.aiTaskRole.roleArn,
      });
    }

    // web-ui auth wiring: the Flask app reads these to build the Cognito
    // hosted-UI (admin/register/recover) and Discord OAuth (login) redirects.
    // Without them the "Administrator sign in" button redirects to a broken
    // `/login?client_id=` URL with an http:// (not https://) redirect URI
    // (R8.2-R8.5). The public base URL is this stage's https hostname so
    // redirect URIs are absolute + https; the Cognito domain follows the
    // AuthStack's deterministic `hellodj-<stage>-<account>` hosted-UI prefix.
    if (spec.name === 'web-ui') {
      env.push({
        name: 'HELLODJ_PUBLIC_BASE_URL',
        value: `https://${this.stageEndpoint.hostname}`,
      });
      env.push({
        name: 'HELLODJ_COOKIE_SECURE',
        value: '1',
      });
      // Flask session signing key MUST be shared across all web-ui replicas,
      // otherwise a login started on one pod and its OAuth callback landing on
      // another pod can't validate the signed session cookie (the state token
      // is lost) and the user is bounced back to /login. Sourced from a
      // per-stage Kubernetes Secret so the value is stable across restarts and
      // identical for every replica (see `webUiFlaskSecretManifest`).
      env.push({
        name: 'FLASK_SECRET_KEY',
        valueFrom: {
          secretKeyRef: {
            name: 'web-ui-flask-secret',
            key: 'FLASK_SECRET_KEY',
          },
        },
      });
      env.push({
        name: 'COGNITO_DOMAIN',
        value:
          `https://hellodj-${this.stage}-${this.account}` +
          `.auth.${this.region}.amazoncognito.com`,
      });
      if (this.props.cognitoClientId) {
        env.push({
          name: 'COGNITO_CLIENT_ID',
          value: this.props.cognitoClientId,
        });
      }
      if (this.props.discordClientId) {
        env.push({
          name: 'DISCORD_CLIENT_ID',
          value: this.props.discordClientId,
        });
      }
    }
    return env;
  }

  /** Build the Deployment manifest for a component. */
  private deployment(
    spec: ComponentWorkloadSpec,
    serviceAccountName: string,
  ): Record<string, unknown> {
    const labels = this.labels(spec);
    const placement = this.placement(spec);
    const usingPlaceholderTag =
      (this.props.imageTags?.[spec.name] ?? PLACEHOLDER_IMAGE_TAG) ===
      PLACEHOLDER_IMAGE_TAG;

    const container: Record<string, unknown> = {
      name: spec.name,
      image: this.imageUri(spec),
      imagePullPolicy: 'Always',
      env: this.containerEnv(spec),
      resources: {
        requests: {
          cpu: spec.resources.cpuRequest,
          memory: spec.resources.memoryRequest,
        },
        limits: {
          cpu: spec.resources.cpuLimit,
          memory: spec.resources.memoryLimit,
        },
      },
    };
    if (spec.port) {
      container.ports = [{ containerPort: spec.port, name: 'http' }];
    }

    return {
      apiVersion: 'apps/v1',
      kind: 'Deployment',
      metadata: {
        name: spec.name,
        namespace: this.namespace,
        labels,
        annotations: {
          'hellodj.bot/description': spec.description,
          // Surface the pipeline-injected-tag TODO on the workload so a
          // placeholder image never ships silently (task 18.1).
          'hellodj.bot/image-tag': usingPlaceholderTag
            ? `TODO-pipeline-injected:${this.imageUri(spec)}`
            : this.imageUri(spec),
        },
      },
      spec: {
        // The HPA owns the replica count; seed at the min replicas.
        replicas: spec.hpa.minReplicas,
        selector: { matchLabels: labels },
        template: {
          metadata: { labels },
          spec: {
            serviceAccountName,
            nodeSelector: placement.nodeSelector,
            ...(placement.tolerations
              ? { tolerations: placement.tolerations }
              : {}),
            containers: [container],
          },
        },
      },
    };
  }

  /** Build the ClusterIP Service manifest for a serving component. */
  private service(spec: ComponentWorkloadSpec): Record<string, unknown> {
    const labels = this.labels(spec);
    return {
      apiVersion: 'v1',
      kind: 'Service',
      metadata: {
        name: spec.name,
        namespace: this.namespace,
        labels,
      },
      spec: {
        type: 'ClusterIP',
        selector: labels,
        ports: [
          {
            name: 'http',
            port: spec.port,
            targetPort: spec.port,
            protocol: 'TCP',
          },
        ],
      },
    };
  }

  /**
   * Build the HorizontalPodAutoscaler for a component, keyed to the platform
   * scale-out threshold (70% average CPU, mirrored from `autoscale.py`) — so
   * pod-level scaling and the cluster autoscaler share one source of truth
   * (R16.1-R16.5).
   */
  private hpa(spec: ComponentWorkloadSpec): Record<string, unknown> {
    const targetCpu =
      spec.hpa.targetCpuPercent ?? DEFAULT_HPA_TARGET_CPU_PERCENT;
    return {
      apiVersion: 'autoscaling/v2',
      kind: 'HorizontalPodAutoscaler',
      metadata: {
        name: spec.name,
        namespace: this.namespace,
        labels: this.labels(spec),
      },
      spec: {
        scaleTargetRef: {
          apiVersion: 'apps/v1',
          kind: 'Deployment',
          name: spec.name,
        },
        minReplicas: spec.hpa.minReplicas,
        maxReplicas: spec.hpa.maxReplicas,
        metrics: [
          {
            type: 'Resource',
            resource: {
              name: 'cpu',
              target: {
                type: 'Utilization',
                averageUtilization: targetCpu,
              },
            },
          },
        ],
      },
    };
  }

  /**
   * Build the ALB Ingress that routes the two user-facing HTTP entry points
   * behind the load balancer, consistent with the CloudFront/edge routing:
   * `activity-backend` at `/activity/` and `web-ui` at `/` (R18.4).
   *
   * The AWS Load Balancer Controller reconciles this `Ingress` into an ALB.
   * The `/activity/` rule is listed before the `/` catch-all so the more
   * specific path wins. WebSocket upgrades for the Activity hub pass through
   * the ALB transparently.
   *
   * **Shared ALB (R1.5).** All three per-stage Ingresses declare the same
   * stage-independent {@link SHARED_ALB_GROUP_NAME} `group.name`, so the AWS
   * Load Balancer Controller merges them onto **one** ALB rather than one per
   * stage — the ALB is a foundation singleton.
   *
   * **No-match rejection (R5.3).** Each stage's host-scoped rule matches only
   * its own `<stage>.<region>.hellodj.bot` hostname. A request whose hostname
   * matches **no** provisioned {@link StageEndpoint} hostname falls through to
   * the shared ALB **default action**, which is wired here as a fixed-response
   * **404 (no matching host)** via an `alb.ingress.kubernetes.io/actions.*`
   * annotation bound to a **hostless** catch-all rule. Because the AWS Load
   * Balancer Controller orders host-scoped rules ahead of the hostless
   * catch-all, an unmatched host is routed to no namespace and explicitly
   * rejected with a 404 (R5.3), while matched hosts still reach their stage's
   * Services only (R5.1, R5.2).
   */
  private addIngress(): eks.KubernetesManifest {
    const routed = COMPONENT_WORKLOADS.filter((s) => s.ingressPath && s.port);
    // Order most-specific first: `/activity` before `/`.
    routed.sort(
      (a, b) => (b.ingressPath?.length ?? 0) - (a.ingressPath?.length ?? 0),
    );

    const paths = routed.map((s) => ({
      path: s.ingressPath,
      pathType: s.ingressPathType ?? 'Prefix',
      backend: {
        service: {
          name: s.name,
          port: { number: s.port },
        },
      },
    }));

    // The hostless catch-all path that binds the fixed-response 404 default
    // action (R5.3). It uses the AWS Load Balancer Controller's special
    // `use-annotation` service port, which resolves to the
    // `alb.ingress.kubernetes.io/actions.<name>` annotation below. Having NO
    // `host` makes this the ALB's default/fallback rule for any hostname that
    // matches none of the three provisioned stage hostnames.
    const default404Path = {
      path: '/',
      pathType: 'Prefix',
      backend: {
        service: {
          name: ALB_DEFAULT_404_ACTION,
          port: { name: 'use-annotation' },
        },
      },
    };

    const ingress = {
      apiVersion: 'networking.k8s.io/v1',
      kind: 'Ingress',
      metadata: {
        name: 'hellodj',
        namespace: this.namespace,
        labels: { 'hellodj.bot/stage': this.stage },
        annotations: {
          'kubernetes.io/ingress.class': 'alb',
          'alb.ingress.kubernetes.io/scheme': 'internet-facing',
          'alb.ingress.kubernetes.io/target-type': 'ip',
          // HTTP only — CloudFront terminates TLS (the edge stack has the ACM
          // cert). The ALB sits behind CloudFront and receives HTTP from it.
          'alb.ingress.kubernetes.io/listen-ports': '[{"HTTP":80}]',
          // Merge all three per-stage Ingresses onto the SINGLE shared ALB
          // (R1.5): a stage-independent group name means one ALB, not one per
          // stage. The ALB is a foundation singleton.
          'alb.ingress.kubernetes.io/group.name': SHARED_ALB_GROUP_NAME,
          // The fixed-response 404 default action for an unmatched host (R5.3):
          // a request whose hostname matches no provisioned Stage_Hostname hits
          // the hostless catch-all rule below, which returns 404 "no matching
          // host" and is routed to no namespace.
          [`alb.ingress.kubernetes.io/actions.${ALB_DEFAULT_404_ACTION}`]:
            JSON.stringify(ALB_DEFAULT_404_ACTION_CONFIG),
          'hellodj.bot/routing':
            'web-ui at / and activity-backend at /activity/ (R18.4)',
          // Cross-stage routing isolation (R8.7): this Ingress matches only
          // this stage's hostname and its backends are Services in this
          // stage's namespace, so a request to one Stage_Endpoint reaches only
          // that stage's workload — the CDK realization of `route_endpoint`.
          'hellodj.bot/stage-endpoint': this.stageEndpoint.hostname,
        },
      },
      spec: {
        rules: [
          {
            // Bind the rule to the stage's distinct hostname so a request to
            // `<stage>.<region>.hellodj.bot` routes only to this stage's
            // namespace Services and never to another stage's workload (R8.7,
            // R5.1, R5.2).
            host: this.stageEndpoint.hostname,
            http: { paths },
          },
          {
            // Hostless catch-all: any hostname matching no provisioned
            // Stage_Hostname falls through to the fixed-response 404 default
            // action, so an unmatched host reaches no namespace (R5.3).
            http: { paths: [default404Path] },
          },
        ],
      },
    };

    return this.cluster.addManifest(`${this.stage}-HelloDjIngress`, ingress);
  }
}

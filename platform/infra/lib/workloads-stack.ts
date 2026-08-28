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
import * as route53 from 'aws-cdk-lib/aws-route53';
import * as secretsmanager from 'aws-cdk-lib/aws-secretsmanager';
import * as ses from 'aws-cdk-lib/aws-ses';
import * as s3 from 'aws-cdk-lib/aws-s3';
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
 * The local-part of the branded invitation email's verified SES sender
 * identity. Invitation emails are sent from `<INVITE_LOCAL_PART>@<hostname>`
 * for the stage (R7.4), so the single-use registration link the invitee
 * receives originates from a HelloDJ-owned, stage-scoped identity.
 */
export const INVITE_SENDER_LOCAL_PART = 'invites';

/**
 * Derive the stage's verified SES sender identity for invitation emails:
 * `invites@<stage>.<region>.hellodj.bot`, mirroring {@link stageHostname} so
 * the sender, the public base URL, and the Ingress hostname all agree on the
 * one `dns_naming`-derived name (R7.4). This is the `INVITE_SENDER` the web-ui
 * sends branded invites from and the identity the web-ui role's `ses:SendEmail`
 * grant is scoped to (R7.1).
 */
export function stageInviteSender(stage: string, region: string): string {
  return `${INVITE_SENDER_LOCAL_PART}@${stageHostname(stage, region)}`;
}

/**
 * The default TTL (in seconds) of a single-use Invite_Token — 7 days — mirrored
 * from the web-ui's `invite_service.DEFAULT_INVITE_TTL_SECONDS`. Injected as
 * `INVITE_TOKEN_TTL` so IaC and the runtime agree on how long an invitation
 * link stays valid before it is treated as expired (R1.3).
 */
export const DEFAULT_INVITE_TOKEN_TTL_SECONDS = 7 * 24 * 60 * 60;

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
 * NOTE: `latest` is only the FALLBACK. The pipeline now injects an immutable
 * per-component tag (the source commit hash) via {@link imageTags}, resolved at
 * synth time from `CODEBUILD_RESOLVED_SOURCE_VERSION` in `bin/hellodj.ts` — the
 * Synth CodeBuild step runs on the SAME revision the ComponentBuilds tag their
 * images with, so the commit is available at synth and no parameter store /
 * post-synth mechanism is needed. A changing, immutable tag makes each pipeline
 * run alter the pod spec so Kubernetes rolls the workloads automatically (no
 * manual `kubectl rollout restart`). When no tag is injected (local synth), this
 * `latest` + `imagePullPolicy: Always` default keeps the stack synthesizable.
 */
export const PLACEHOLDER_IMAGE_TAG = 'latest';

/**
 * Components on the playback path that resolve a guild's own source OAuth
 * tokens at play time (R6.1). Each gets READ-ONLY access to the per-guild
 * secret prefix `hellodj/<stage>/guild/*` so a track played in one guild uses
 * that guild's Tidal/Spotify/YouTube auth, isolated from every other guild
 * (R6.3, R7.2). The web-ui is the writer (handled separately); these are
 * readers only.
 */
export const PER_GUILD_SOURCE_READERS: ReadonlySet<string> = new Set([
  'discord-bot-core',
  'tidal-stream',
  'spotify-stream',
  'lavalink',
]);

/**
 * Bot-path components that host the `UserEntitlementResolver`
 * (admin-entitlements-panel spec, task 6). The resolver reads a user's
 * entitlement + AI-pricing items on the `hellodj-core` single table and writes
 * (increments) the user's AI-cost tally item when metering an AI request
 * (R14.1, R10.1). `discord-bot-core` constructs the resolver once at startup
 * and the cogs enforce through it, so it is the component that needs this
 * fine-grained core-table access.
 *
 * These components already receive broad `grantReadWriteData` on the core
 * table via their `coreTable` dependency; this set drives an ADDITIONAL,
 * explicit, self-documenting least-privilege statement scoped by
 * `dynamodb:LeadingKeys` to exactly the entitlement/pricing/tally partitions,
 * so the entitlements access is auditable in its own right (R10.3, R14.1).
 */
export const ENTITLEMENT_RESOLVER_COMPONENTS: ReadonlySet<string> = new Set([
  'discord-bot-core',
]);

/**
 * The DynamoDB partition-key prefix every per-user entitlement item lives
 * under on the `hellodj-core` single table — the entitlement record
 * (`SK=ENTITLEMENT`), the AI-cost tally (`SK=AITALLY`), and the append-only
 * audit entries (`SK=AUDIT#...`) all share `PK=USER#<sub>`. DynamoDB's
 * `dynamodb:LeadingKeys` condition scopes item-level access to this partition
 * shape (the leading `USER#` value), mirroring the storage-key helpers in the
 * web-ui `entitlement_service.py`.
 */
export const ENTITLEMENT_USER_PK_PREFIX = 'USER#';

/**
 * The fixed partition key of the shared AI-pricing configuration item
 * (`PK=CONFIG#AIPRICING`, `SK=CONFIG`) on the `hellodj-core` table. The bot
 * resolver reads this item to apply the per-model Bedrock unit price + markup
 * when metering AI cost; ops update prices by editing this item, so pricing is
 * data, not code (R10.3).
 */
export const ENTITLEMENT_AIPRICING_PK = 'CONFIG#AIPRICING';

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
  /**
   * Per-guild bot-avatar assets bucket (from `DataStack.assetsBucket`). The
   * web-ui writes avatar bytes here (`guild/<gid>/bot-avatar/<hash>.<ext>`) and
   * the discord-bot-core reads them; wired into components declaring the
   * `assetsBucket` dependency as env `HELLODJ_ASSETS_BUCKET` + an IRSA grant.
   */
  readonly assetsBucket: s3.IBucket;
}

/** Secrets Manager entries the workloads wire to (from `AuthStack`). */
export interface WorkloadsSecretRefs {
  readonly discordBotToken: secretsmanager.ISecret;
  readonly tidalRefresh: secretsmanager.ISecret;
  readonly spotify: secretsmanager.ISecret;
  readonly ytCipher: secretsmanager.ISecret;
  /**
   * Google/YouTube OAuth client credentials ({client_id, client_secret}) the
   * web-ui reads to complete the per-guild YouTube code→refresh-token
   * exchange. Optional so foundations imported without it still synthesize.
   */
  readonly googleOauth?: secretsmanager.ISecret;
  /**
   * Discord OAuth client credentials ({client_id, client_secret}) the web-ui
   * reads for the Discord-login callback token exchange. Optional so
   * foundations imported without it still synthesize.
   */
  readonly discordOauth?: secretsmanager.ISecret;
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
   * The Cognito user pool id (from `AuthStack`), injected into the web-ui
   * container so the admin panel can manage all accounts via the Cognito admin
   * APIs (list users, promote/demote admins, enable/disable). When unset the
   * admin panel renders an empty directory (degraded mode).
   */
  readonly cognitoUserPoolId?: string;

  /**
   * The Discord OAuth application client id (from `AuthStack` secrets/config),
   * injected into the web-ui container so day-to-day Discord login works. When
   * unset, the Discord login button produces an empty `client_id` (R8.4).
   */
  readonly discordClientId?: string;

  /**
   * The Spotify OAuth application client id, injected into the web-ui
   * container as `SPOTIFY_CLIENT_ID` so a per-guild Spotify connect builds a
   * valid authorize URL instead of `source_authorize_url` returning `None`
   * (the silent no-op, R2.6). Client *ids* are not sensitive, so this is a
   * plain env value threaded via props (mirrors `discordClientId`); the client
   * *secret* is injected via the `web-ui-oauth-secret` Kubernetes Secret.
   * Additive, defaults to `""` when unset.
   */
  readonly spotifyClientId?: string;

  /**
   * The Google/YouTube OAuth application client id, injected into the web-ui
   * container as `GOOGLE_CLIENT_ID` so a per-guild YouTube / YouTube Music
   * connect builds a valid authorize URL (R2.6). Plain env value threaded via
   * props; additive, defaults to `""` when unset.
   */
  readonly googleClientId?: string;

  /**
   * The Tidal OAuth application client id, injected into the web-ui container
   * as `TIDAL_CLIENT_ID` so a per-guild Tidal connect keeps building a valid
   * authorize URL (R2.6, preserve 3.1). Plain env value threaded via props;
   * additive, defaults to `""` when unset.
   */
  readonly tidalClientId?: string;

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

  /**
   * The Google/YouTube OAuth client *secret* value, placed into the per-stage
   * `web-ui-oauth-secret` Kubernetes Secret and referenced by the web-ui
   * container via `secretKeyRef` (mirrors the `web-ui-flask-secret` pattern).
   * Threading the value here (rather than resolving it inline in the container
   * env) keeps the secret out of any CloudFormation env literal on the
   * Deployment manifest. Additive; when unset the Secret carries an empty
   * value and `GOOGLE_CLIENT_SECRET` resolves to "".
   */
  readonly googleClientSecret?: string;

  /**
   * The Discord OAuth client *secret* value, placed into the per-stage
   * `web-ui-oauth-secret` Kubernetes Secret and referenced by the web-ui
   * container via `secretKeyRef` for the Discord-login callback token
   * exchange. Additive; when unset the Secret carries an empty value and
   * `DISCORD_CLIENT_SECRET` resolves to "".
   */
  readonly discordClientSecret?: string;

  /**
   * Override for the verified SES sender identity the web-ui sends branded
   * invitation emails from (`INVITE_SENDER`). When unset it defaults to
   * `invites@<stage>.<region>.hellodj.bot` (see {@link stageInviteSender}) so
   * the sender agrees with the stage's Ingress hostname + public base URL. A
   * stage-scoped SES `EmailIdentity` is provisioned for whichever value is
   * used, and the web-ui role's `ses:SendEmail`/`ses:SendRawEmail` grant is
   * scoped to that identity's ARN (R7.1, R7.4).
   */
  readonly inviteSender?: string;

  /**
   * Override for the single-use Invite_Token TTL (seconds) injected as
   * `INVITE_TOKEN_TTL`. Defaults to {@link DEFAULT_INVITE_TOKEN_TTL_SECONDS}
   * (7 days), mirroring the web-ui's `invite_service` default (R1.3).
   */
  readonly inviteTokenTtlSeconds?: number;
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

  /**
   * The Kubernetes Secret holding the web-ui's OAuth client *secrets*
   * (`GOOGLE_CLIENT_SECRET`, `DISCORD_CLIENT_SECRET`), referenced by the
   * web-ui container via `secretKeyRef` so the per-guild YouTube exchange and
   * the Discord-login callback can complete without a secret value landing in
   * a CloudFormation env literal on the Deployment (mirrors
   * {@link webUiFlaskSecretManifest}).
   */
  public readonly webUiOauthSecretManifest: eks.KubernetesManifest;

  /**
   * The verified SES sender identity (`invites@<stage>.<region>.hellodj.bot`
   * by default) branded invitation emails are sent from for this stage. The
   * web-ui role's `ses:SendEmail`/`ses:SendRawEmail` grant is scoped to this
   * identity's ARN (R7.1, R7.4).
   */
  public readonly inviteSender: string;

  /**
   * The stage's SES sender {@link ses.EmailIdentity} for invitation emails.
   * Provisioning it here (a foundation-shared but stage-scoped identity) makes
   * the verified sender part of the declarative infra rather than a manual
   * console step. It is a DOMAIN identity for {@link inviteSenderDomain} with
   * Easy-DKIM CNAMEs published into the delegated `hellodj.bot` zone, so SES
   * self-verifies it (no manual mailbox-confirmation click). The account must
   * still leave the SES sandbox to email arbitrary (unverified) recipients —
   * that production-access request has no CloudFormation resource and is done
   * once out-of-band.
   */
  public readonly inviteSenderIdentity: ses.EmailIdentity;

  /**
   * The verified stage DOMAIN the {@link inviteSender} address belongs to
   * (`<stage>.<region>.hellodj.bot`). The web-ui role's `ses:SendEmail` grant
   * is scoped to this domain identity's ARN.
   */
  public readonly inviteSenderDomain: string;

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

    // Verified SES sender identity for the branded, single-use invitation
    // emails the web-ui sends (R7.4). The sender is
    // `invites@<stage>.<region>.hellodj.bot` so it agrees with the stage's
    // Ingress hostname + public base URL (one dns_naming-derived name).
    //
    // We verify the whole STAGE DOMAIN (`<stage>.<region>.hellodj.bot`) via
    // DKIM rather than the single `invites@` email address. A domain identity
    // lets ANY mailbox on that domain send (so `invites@` works, and future
    // senders like `noreply@` do too), and — crucially — it self-verifies:
    // the Easy-DKIM CNAMEs are written into the delegated `hellodj.bot` public
    // hosted zone (the same zone the EdgeStack uses for ACM DNS validation),
    // so SES completes verification automatically with no manual mailbox
    // confirmation. (An email identity, by contrast, sits in `Pending` forever
    // unless a human clicks a link mailed to an address that may have no
    // mailbox — that was the beta bug: `invites@...` stuck Pending.)
    //
    // The identity is the stage subdomain (`beta.us-east-1.hellodj.bot`), not
    // the apex, so DKIM is strictly DMARC-aligned with the `invites@<stage>`
    // From address and each stage stays isolated. `Identity.publicHostedZone`
    // would verify the ZONE's name (the apex `hellodj.bot`), so instead we use
    // `Identity.domain(<subdomain>)` and create its DKIM CNAMEs ourselves into
    // the looked-up apex zone (the subdomain is a strict child of `hellodj.bot`
    // and has no zone of its own). The web-ui role's `ses:SendEmail`/
    // `ses:SendRawEmail` grant below is scoped to THIS identity's ARN
    // (least privilege, R7.1).
    this.inviteSender =
      props.inviteSender ??
      stageInviteSender(this.stage, props.region ?? this.region);
    this.inviteSenderDomain = stageHostname(
      this.stage,
      props.region ?? this.region,
    );
    const inviteZone = route53.HostedZone.fromLookup(
      this,
      `${this.stage}-InviteSenderZone`,
      { domainName: HELLODJ_ZONE },
    );
    this.inviteSenderIdentity = new ses.EmailIdentity(
      this,
      `${this.stage}-InviteSenderIdentity`,
      {
        identity: ses.Identity.domain(this.inviteSenderDomain),
      },
    );
    // Easy-DKIM self-verification: publish the three CNAME tokens SES issues
    // for the domain identity into the delegated `hellodj.bot` zone. Once they
    // resolve, SES flips the identity to Verified with no manual step.
    //
    // `record.name` is the FULLY-QUALIFIED DKIM host
    // (`<token>._domainkey.<stage>.<region>.hellodj.bot`). CnameRecord treats a
    // `recordName` that does NOT end in "." as relative and appends the zone
    // name — which would double the suffix
    // (`...hellodj.bot.hellodj.bot`, SES then reports HOST_NOT_FOUND). Ending
    // it with a "." tells CDK to use it verbatim as the absolute name.
    this.inviteSenderIdentity.dkimRecords.forEach((record, index) => {
      const absoluteName = record.name.endsWith('.')
        ? record.name
        : `${record.name}.`;
      new route53.CnameRecord(
        this,
        `${this.stage}-InviteSenderDkim${index}`,
        {
          zone: inviteZone,
          recordName: absoluteName,
          domainName: record.value,
        },
      );
    });

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

    // Per-stage OAuth client-secret Secret for the web-ui, shared by all
    // replicas. Holds the Google/YouTube + Discord OAuth client *secrets* the
    // web-ui uses to complete the per-guild YouTube code→refresh-token
    // exchange and the Discord-login callback token exchange. Referenced by
    // the web-ui container via `secretKeyRef` (mirrors web-ui-flask-secret) so
    // no client-secret value lands in a CloudFormation env literal on the
    // Deployment manifest. Values are threaded via props from the
    // AuthStack-owned Secrets Manager entries (created empty, populated
    // out-of-band); an unset value renders an empty string so the env var
    // resolves to "" until the secret is populated.
    this.webUiOauthSecretManifest = this.cluster.addManifest(
      `${this.stage}-WebUiOauthSecret`,
      {
        apiVersion: 'v1',
        kind: 'Secret',
        metadata: {
          name: 'web-ui-oauth-secret',
          namespace: this.namespace,
        },
        type: 'Opaque',
        stringData: {
          GOOGLE_CLIENT_SECRET: this.props.googleClientSecret ?? '',
          DISCORD_CLIENT_SECRET: this.props.discordClientSecret ?? '',
        },
      },
    );
    this.webUiOauthSecretManifest.node.addDependency(this.namespaceManifest);

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
        // The web-ui also references the OAuth client-secret Secret via
        // secretKeyRef, so it must exist before the web-ui Deployment applies.
        manifest.node.addDependency(this.webUiOauthSecretManifest);
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
    if (deps.assetsBucket) {
      // Per-guild bot-avatar assets bucket. Least privilege by role: the
      // web-ui UPLOADS avatars (write), the discord-bot-core READS them back.
      // `grantDependencies` only has the spec, so branch on the component name.
      if (spec.name === 'web-ui') {
        this.props.data.assetsBucket.grantReadWrite(sa.role);
      } else {
        this.props.data.assetsBucket.grantRead(sa.role);
      }
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

    // The web-ui admin panel manages every account through the Cognito admin
    // APIs (list users, read/modify group membership, enable/disable). Grant
    // its IRSA role least-privilege access to the shared user pool only. The
    // admin panel itself is still gated to `admins`-group sessions in-app;
    // this grants the pod the AWS permission to perform those actions once an
    // admin requests them (R8.2 — admin administers all accounts).
    if (spec.name === 'web-ui' && this.props.cognitoUserPoolId) {
      sa.role.addToPrincipalPolicy(
        new iam.PolicyStatement({
          sid: 'CognitoAdminUserDirectory',
          effect: iam.Effect.ALLOW,
          actions: [
            'cognito-idp:ListUsers',
            'cognito-idp:AdminListGroupsForUser',
            'cognito-idp:AdminAddUserToGroup',
            'cognito-idp:AdminRemoveUserFromGroup',
            'cognito-idp:AdminEnableUser',
            'cognito-idp:AdminDisableUser',
            // Tokenized invite flow (R2.2): register() creates a CONFIRMED
            // account with admin_create_user (MessageAction=SUPPRESS) then
            // admin_set_user_password(Permanent=True) so Cognito sends no
            // temp-password email — the branded SES invite is the only email.
            'cognito-idp:AdminCreateUser',
            'cognito-idp:AdminSetUserPassword',
          ],
          resources: [
            `arn:aws:cognito-idp:${this.region}:${this.account}:userpool/` +
              this.props.cognitoUserPoolId,
          ],
        }),
      );
      // Branded invitation email delivery (R1.1, R7.1, R7.4): the web-ui sends
      // the single-use registration link from `invites@<stage>.<region>...`.
      //
      // SES evaluates `ses:SendEmail` against MULTIPLE identity ARNs per call:
      // the From domain, the From email-address, AND — in the SES sandbox —
      // each RECIPIENT identity. Recipients are arbitrary, so an ARN allow-list
      // can't enumerate them (that's what caused AccessDenied on
      // `identity/<recipient>`). The correct least-privilege shape is to allow
      // the action on any identity in THIS account/region (`identity/*`) and
      // constrain the SENDER via an `ses:FromAddress` condition — the web-ui
      // can still only send AS the invite sender, never as any other From.
      sa.role.addToPrincipalPolicy(
        new iam.PolicyStatement({
          sid: 'InviteEmailSend',
          effect: iam.Effect.ALLOW,
          actions: ['ses:SendEmail', 'ses:SendRawEmail'],
          resources: [
            `arn:aws:ses:${this.region}:${this.account}:identity/*`,
          ],
          conditions: {
            'ForAllValues:StringEquals': {
              'ses:FromAddress': this.inviteSender,
            },
          },
        }),
      );
      // Per-guild source OAuth tokens: the web-ui creates/updates/reads/deletes
      // one isolated Secrets Manager secret per guild+provider under the
      // `hellodj/<stage>/guild/*` prefix. Scoping the grant to that prefix is
      // the IAM half of the per-guild isolation (R5.1, R5.2, R7.1) — the
      // web-ui can never touch a non-guild secret.
      sa.role.addToPrincipalPolicy(
        new iam.PolicyStatement({
          sid: 'PerGuildSourceSecrets',
          effect: iam.Effect.ALLOW,
          actions: [
            'secretsmanager:CreateSecret',
            'secretsmanager:PutSecretValue',
            'secretsmanager:GetSecretValue',
            'secretsmanager:DescribeSecret',
            'secretsmanager:DeleteSecret',
          ],
          resources: [
            `arn:aws:secretsmanager:${this.region}:${this.account}:secret:` +
              `hellodj/${this.stage}/guild/*`,
          ],
        }),
      );

      // Source OAuth client credentials (R2.6): the web-ui resolves the
      // Google/YouTube + Discord OAuth client id/secret at runtime to complete
      // the per-guild code→token exchange (YouTube) and the Discord-login
      // callback. Grant READ on exactly those AuthStack secrets, scoped to
      // their ARNs (least privilege). Spotify is included so the web-ui can
      // read the Spotify client id/secret for the per-guild Spotify connect
      // flow. The grants are guarded so a foundation imported without these
      // optional secret handles still synthesizes.
      if (this.props.secrets.googleOauth) {
        this.props.secrets.googleOauth.grantRead(sa.role);
      }
      if (this.props.secrets.discordOauth) {
        this.props.secrets.discordOauth.grantRead(sa.role);
      }
      this.props.secrets.spotify.grantRead(sa.role);
    }

    // Bot-side per-guild source resolution (R6.1, R7.2): the playback path
    // components resolve a guild's own OAuth tokens at play time. They get
    // READ-ONLY access to the same `hellodj/<stage>/guild/*` prefix — enough to
    // load a guild's credentials, never to write or reach another guild's.
    if (PER_GUILD_SOURCE_READERS.has(spec.name)) {
      sa.role.addToPrincipalPolicy(
        new iam.PolicyStatement({
          sid: 'PerGuildSourceSecretsRead',
          effect: iam.Effect.ALLOW,
          actions: [
            'secretsmanager:GetSecretValue',
            'secretsmanager:DescribeSecret',
          ],
          resources: [
            `arn:aws:secretsmanager:${this.region}:${this.account}:secret:` +
              `hellodj/${this.stage}/guild/*`,
          ],
        }),
      );
    }

    // Bot-side user-entitlement resolution + AI-cost metering
    // (admin-entitlements-panel, tasks 6/14; R14.1, R10.1, R10.3). The
    // `discord-bot-core` resolver READS a user's entitlement item + the shared
    // AI-pricing item to decide what a user may do and how much an AI call
    // costs, and WRITES (increments) the user's AI-cost tally item when
    // metering a permitted AI request. It already holds broad
    // `grantReadWriteData` via its `coreTable` dependency, so this ADDITIONAL
    // statement is an explicit, auditable least-privilege declaration scoped by
    // `dynamodb:LeadingKeys` to exactly the entitlement/tally partitions
    // (`USER#*`) and the pricing partition (`CONFIG#AIPRICING`) — it documents
    // and pins the entitlements access in its own right rather than relying on
    // the blanket table grant.
    if (ENTITLEMENT_RESOLVER_COMPONENTS.has(spec.name)) {
      const coreTableArn = this.props.data.coreTable.tableArn;
      // READ on the per-user entitlement/tally partitions and the shared
      // pricing item. `LeadingKeys` restricts item access to rows whose
      // partition key begins with `USER#` OR equals `CONFIG#AIPRICING`.
      sa.role.addToPrincipalPolicy(
        new iam.PolicyStatement({
          sid: 'EntitlementResolverRead',
          effect: iam.Effect.ALLOW,
          actions: [
            'dynamodb:GetItem',
            'dynamodb:BatchGetItem',
            'dynamodb:Query',
          ],
          resources: [coreTableArn],
          conditions: {
            'ForAllValues:StringLike': {
              'dynamodb:LeadingKeys': [
                `${ENTITLEMENT_USER_PK_PREFIX}*`,
                ENTITLEMENT_AIPRICING_PK,
              ],
            },
          },
        }),
      );
      // WRITE on the per-user AI-cost tally item. DynamoDB item-level access is
      // scoped by partition key (`dynamodb:LeadingKeys`), so the write is
      // pinned to the `USER#*` partitions that hold the `AITALLY` item; the
      // resolver only ever updates that tally, never another user's data store.
      sa.role.addToPrincipalPolicy(
        new iam.PolicyStatement({
          sid: 'EntitlementResolverTallyWrite',
          effect: iam.Effect.ALLOW,
          actions: ['dynamodb:UpdateItem', 'dynamodb:PutItem'],
          resources: [coreTableArn],
          conditions: {
            'ForAllValues:StringLike': {
              'dynamodb:LeadingKeys': [`${ENTITLEMENT_USER_PK_PREFIX}*`],
            },
          },
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
    if (deps.assetsBucket) {
      // Both the web-ui (writer) and discord-bot-core (reader) resolve the
      // per-guild bot-avatar bucket from this env var. Placed in the general
      // (non-web-ui-specific) section so every component with the dep gets it.
      env.push({
        name: 'HELLODJ_ASSETS_BUCKET',
        value: this.props.data.assetsBucket.bucketName,
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
      if (this.props.cognitoUserPoolId) {
        // The admin panel manages all accounts via the Cognito admin APIs; it
        // needs the user pool id to list/enable/disable users and manage the
        // admins group.
        env.push({
          name: 'HELLODJ_COGNITO_USER_POOL_ID',
          value: this.props.cognitoUserPoolId,
        });
      }
      if (this.props.discordClientId) {
        env.push({
          name: 'DISCORD_CLIENT_ID',
          value: this.props.discordClientId,
        });
      }
      // Per-guild source OAuth client ids (R2.6). `app.py` reads these to build
      // the Spotify / YouTube / Tidal authorize URLs; when empty,
      // `source_authorize_url` returns None and the connect button silently
      // no-ops (Defect 1, root cause 1a). Client *ids* are not sensitive, so
      // they are plain env values threaded via props (mirroring
      // `discordClientId`). They are pushed unconditionally with an empty-string
      // default so the env var is always present (only its value is empty until
      // the provider is configured), which is what lets the app distinguish a
      // configured provider from an unconfigured one.
      env.push({
        name: 'SPOTIFY_CLIENT_ID',
        value: this.props.spotifyClientId ?? '',
      });
      env.push({
        name: 'GOOGLE_CLIENT_ID',
        value: this.props.googleClientId ?? '',
      });
      env.push({
        name: 'TIDAL_CLIENT_ID',
        value: this.props.tidalClientId ?? '',
      });
      // OAuth client *secrets* injected via the per-stage `web-ui-oauth-secret`
      // Kubernetes Secret (see `webUiOauthSecretManifest`), referenced by
      // secretKeyRef so no secret value lands in a CloudFormation env literal
      // (mirrors the FLASK_SECRET_KEY pattern). `GOOGLE_CLIENT_SECRET` lets the
      // web-ui complete the per-guild YouTube code→refresh-token exchange;
      // `DISCORD_CLIENT_SECRET` lets the Discord-login callback exchange run.
      env.push({
        name: 'GOOGLE_CLIENT_SECRET',
        valueFrom: {
          secretKeyRef: {
            name: 'web-ui-oauth-secret',
            key: 'GOOGLE_CLIENT_SECRET',
          },
        },
      });
      env.push({
        name: 'DISCORD_CLIENT_SECRET',
        valueFrom: {
          secretKeyRef: {
            name: 'web-ui-oauth-secret',
            key: 'DISCORD_CLIENT_SECRET',
          },
        },
      });
      // Tokenized invite flow wiring (R1.1, R1.3, R7.4). `INVITE_SENDER` is the
      // stage's verified SES sender identity the branded invitation email is
      // sent from; `INVITE_TOKEN_TTL` is how long a single-use link stays
      // valid; `PUBLIC_BASE_URL` is the site origin used to build the
      // `/invite/<token>` link. The app also reads `HELLODJ_PUBLIC_BASE_URL`
      // (set above) for the same origin; `PUBLIC_BASE_URL` is injected as the
      // explicitly-named alias so both conventions resolve to the one
      // stage hostname.
      env.push({ name: 'INVITE_SENDER', value: this.inviteSender });
      env.push({
        name: 'INVITE_TOKEN_TTL',
        value: String(
          this.props.inviteTokenTtlSeconds ??
            DEFAULT_INVITE_TOKEN_TTL_SECONDS,
        ),
      });
      env.push({
        name: 'PUBLIC_BASE_URL',
        value: `https://${this.stageEndpoint.hostname}`,
      });
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

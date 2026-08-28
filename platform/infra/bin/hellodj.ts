#!/usr/bin/env node
/**
 * CDK application entry point for the HelloDJ AWS platform.
 *
 * All AWS infrastructure for the platform is defined through this CDK app
 * (Requirement 1.1). Individual stacks (network, EKS, edge, data, auth,
 * observability, analytics, pipeline) are wired in by later tasks in the
 * aws-saas-replatform implementation plan; this skeleton establishes the
 * app and its resolved environment (task 1.1).
 */
import * as cdk from 'aws-cdk-lib';
import { resolveConfig, deriveEnvName, apexAliasTarget } from '../lib/config';
import { EksStack } from '../lib/eks-stack';
import { assertFoundationSingleton } from '../lib/foundation';
import { AuthStack } from '../lib/auth-stack';
import { DataStack } from '../lib/data-stack';
import { EdgeStack } from '../lib/edge-stack';
import { NetworkStack } from '../lib/network-stack';
import { AnalyticsStack } from '../lib/analytics-stack';
import { ObservabilityStack } from '../lib/observability-stack';
import { PipelineStack } from '../lib/pipeline-stack';
import { SourceStack } from '../lib/source-stack';

const app = new cdk.App();

const config = resolveConfig();

// Derive the environment DNS name once, from the same single-source-of-truth
// logic mirrored from the Python `dns_naming` module, so every stack added by
// later tasks references a consistent `<stage>.<region>.hellodj.bot` name
// (Requirements 12, 18.3).
const envName = deriveEnvName(config.stage, config.region);

// The CDK environment for account/region-scoped stacks. Account may be
// undefined during synth without credentials; stacks that need a concrete
// account/region are added by later tasks.
const env: cdk.Environment = {
  account: config.account,
  region: config.region,
};

// Expose the resolved config and derived DNS names as app-level context so
// stacks added later can read a single, consistent stage/region/account/name.
app.node.setContext('hellodj:config', config);
app.node.setContext('hellodj:envName', envName);
app.node.setContext('hellodj:apexAliasTarget', apexAliasTarget());

// VPC + multi-AZ networking foundation (task 9.2). Exposes the shared VPC,
// ALB, and NLB that the EKS and edge stacks consume. The Shared_Foundation is
// provisioned exactly once with a stage-independent id (`hellodj-network`), so
// one VPC/ALB/NLB is shared across the beta/staging/production software stages
// (Requirements 1.1, 1.5, 1.6). The `stage` prop only fed stage name tags, so
// it is dropped now that this stack is a singleton.
const network = new NetworkStack(app, 'hellodj-network', {
  env,
});

// Route 53 + ACM + CloudFront edge stack (task 9.5). Provisions the
// `hellodj.bot` zone, the per-env `<stage>.<region>.hellodj.bot` record, the
// prod apex alias, an ACM cert, and the CloudFront edge cache for web static
// assets and HLS segments (Requirements 12.1-12.4, 18.2, 18.4). Instantiated
// once as the shared edge (`hellodj-edge`); `region` is retained because the
// edge derives DNS names from it, `stage` is retained because the edge still
// consumes it for the environment record.
const edge = new EdgeStack(app, 'hellodj-edge', {
  env,
  stage: config.stage,
  region: config.region,
  // The AWS Load Balancer Controller creates this ALB from the Kubernetes
  // Ingress. Its DNS name is stable for the group 'hellodj'.
  applicationLoadBalancerDnsName:
    'k8s-hellodj-15947bf6df-1852676627.us-east-1.elb.amazonaws.com',
});

// DynamoDB + DAX data stack (task 10.1). DAX runs inside the platform VPC, so
// the data stack takes the network stack's shared VPC. The `hellodj-core`
// single table (+ GSI1) and the DAX-fronted `hellodj-search-cache` and
// `hellodj-session` hot tables live here; no RDS/PostgreSQL/SQLite resources
// are provisioned (Requirements 7.1-7.6). Provisioned once as the shared DAX
// singleton (`hellodj-data`, Requirement 1.4).
const data = new DataStack(app, 'hellodj-data', {
  env,
  vpc: network.vpc,
});

// Cognito + OAuth secrets + keyless AI IAM roles (task 10.2). Provisions the
// Cognito user pool (admin/registration/recovery), the Secrets Manager entries
// for the Discord/Tidal/Spotify/yt-cipher secrets, and the IAM task role that
// grants keyless Bedrock/Transcribe/Polly access (Requirements 8.2, 8.3, 8.5,
// 8.6, 9.2, 19.1, 19.3). Shared across all stages (`hellodj-auth`); `stage` is
// retained because the auth stack still consumes it.
const auth = new AuthStack(app, 'hellodj-auth', {
  env,
  stage: config.stage,
});

// EKS cluster with Graviton node groups (task 9.3, Decision D1). Runs the
// containerized fleet on Amazon EKS with a Graviton (ARM64) application node
// group (on-demand + Spot) and a taint/label-isolated transcode node group,
// attached to the network stack's shared VPC. Cluster autoscaling mirrors the
// `autoscale.py` 70%/40% thresholds (Requirements 2.1, 2.2, 3.7, 3.8, 3.11,
// 4.1, 16.1-16.5). The one shared EKS control plane + CPU_Node_Fleet is
// provisioned once (`hellodj-eks`, Requirements 1.2, 1.3); the `stage` prop is
// dropped as the cluster/node-group names become stage-independent.
const eks = new EksStack(app, 'hellodj-eks', {
  env,
  vpc: network.vpc,
});

// CloudWatch observability stack (task 17.1). Provisions the shared platform
// CloudWatch Logs group, the metrics dashboard, threshold alarms, and the SNS
// topic those alarms notify on breach; no Prometheus resources (Requirements
// 10.2, 10.3, 10.4, 10.5, 10.9). Shared singleton (`hellodj-observability`);
// `stage` is retained because the stack still consumes it.
const observability = new ObservabilityStack(
  app,
  'hellodj-observability',
  {
    env,
    stage: config.stage,
  },
);

// Analytics stack (task 17.2): the S3 Hive-partitioned Log_Store plus the
// Glue database/crawler, Athena workgroup/named query, and QuickSight Athena
// data source that catalog and query it. Partition keys mirror the Python
// `hellodj_platform_logic.hive_partition` year/month/day/hour scheme
// (Requirements 10.1, 10.6, 10.7, 10.8). Shared singleton (`hellodj-analytics`);
// `stage` is retained because the stack still consumes it.
const analytics = new AnalyticsStack(app, 'hellodj-analytics', {
  env,
  stage: config.stage,
});

// CDK Pipelines Beta -> Gamma -> Prod deployment pipeline (task 18.1). Models
// the fixed promotion order mirrored from the Python
// `hellodj_platform_logic.promotion` module (PROMOTION_ORDER Beta->Gamma->Prod)
// with per-component build paths for independent promotion; CDK Pipelines'
// sequential stages + halt-on-failure realize promotion.promote() ordering
// (Requirements 11.1-11.4, 15.2). Build-stage gate hook points for tasks
// 18.2-18.4 are left in `pipeline-stack.ts`.
const pipeline = new PipelineStack(app, 'hellodj-pipeline', {
  env,
  vpc: network.vpc,
  foundation: {
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
    // web-ui Cognito hosted-UI client id so the admin/register/recover
    // buttons build a valid hosted-UI redirect (R8.2, R8.3, R8.5).
    cognitoClientId: auth.userPoolClient.userPoolClientId,
  },
});

// Private CodeCommit source stack (task 11.1, hellodj-private-source-and-toolchain
// R1). Provisions the five private Source_Repos (`hellodj`, `Lavalink`,
// `lavaplayer`, `LavaSrc`, `youtube-source`) that relocate the source of truth
// off public GitHub, with a resource policy granting read/pull access only to
// the platform build IAM roles (GHA-runner + EKS/Karpenter builder), so each
// repo is private and not readable without an authorized IAM principal (R1.1,
// R1.3, R1.7). Instantiated once as a stage-independent singleton
// (`hellodj-source`) — the source of truth is shared across all stages. The
// build-role ARNs are read from app context when available; absent them the
// repos remain private with no allowing principal (still R1.7-compliant).
const sourceBuildRoleArns = app.node.tryGetContext('hellodj:buildRoleArns') as
  | string[]
  | undefined;
const source = new SourceStack(app, 'hellodj-source', {
  env,
  buildRoleArns: sourceBuildRoleArns,
});

// Workloads are deployed through the pipeline stages (beta → staging →
// production). The pipeline stack's `HelloDjStage` creates a `WorkloadsStack`
// per stage, each referencing the shared foundation (cluster, data, auth).
// There are NO standalone workload stacks outside the pipeline — a single
// ownership path prevents construct-tree ID collisions on the shared cluster.

// Remaining stacks are instantiated by subsequent tasks.
void env;
void network;
void edge;
void data;
void auth;
void eks;
void analytics;
void observability;
void pipeline;
void source;

// Enforce the Foundation_Singleton_Invariant (R1.7/R1.8) immediately before
// synth: synthesize-and-count every foundation resource type across all
// templates and throw — failing synth and producing no deployable app, with an
// error naming the duplicated type — if any shared foundation resource (VPC,
// EKS control plane, DAX, NAT gateway, node group, ALB, NLB) appears more than
// once. `assertFoundationSingleton` internally invokes `app.synth()`, which
// returns the cached assembly, so the subsequent `app.synth()` call is a no-op.
assertFoundationSingleton(app);

app.synth();

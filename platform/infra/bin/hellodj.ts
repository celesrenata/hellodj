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
import { resolveConfig, deriveEnvName, apexAliasTarget, DeploymentStage } from '../lib/config';
import { EksStack } from '../lib/eks-stack';
import { assertFoundationSingleton } from '../lib/foundation';
import { AuthStack } from '../lib/auth-stack';
import { DataStack } from '../lib/data-stack';
import { EdgeStack } from '../lib/edge-stack';
import { NetworkStack } from '../lib/network-stack';
import { AnalyticsStack } from '../lib/analytics-stack';
import { ObservabilityStack } from '../lib/observability-stack';
import { PipelineStack } from '../lib/pipeline-stack';
import { WorkloadsStack } from '../lib/workloads-stack';

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
});

// Component workloads stack (task 20.1, Decision D1/D2). This is the
// end-to-end wiring layer: it composes the 12 independently deployable
// components (design "Component Decomposition") into EKS
// Deployments/Services/HPAs on the EksStack cluster, wires each component's
// DynamoDB table names, DAX endpoint, Secrets Manager ARNs, and (for
// voice-pipeline) the keyless Bedrock/Transcribe/Polly AI task role via
// IRSA/EKS Pod Identity (no static keys), and adds a single ALB Ingress
// routing web-ui (`/`) and activity-backend (`/activity/`) consistent with the
// CloudFront/edge routing (R18.4). Instantiating it here means one
// `cdk deploy` provisions the whole platform with no manual console steps
// (Requirements 1.2, 1.3, 1.4, 6.1-6.5, 15.1, 15.2, 18.4). Real image tags are
// injected by the pipeline (task 18.1); until then a clearly-marked
// placeholder tag is used.
//
// All three software stages are always modeled on the ONE shared foundation
// (Requirements 2.1, 2.4, 2.5, 1.7): one `WorkloadsStack` per stage, every
// instance sharing the SAME `eks.cluster`, `data.*`, and `auth.*` (secrets +
// `aiTaskRole`) references and differing only by `stage`/`region` — which in
// turn selects a distinct `hellodj-<stage>` namespace and a distinct
// `<stage>.<region>.hellodj.bot` Ingress host. Nothing in `WorkloadsStack`
// provisions foundation hardware, so three instances add three namespaces'
// worth of software onto one foundation.
const STAGES = [
  DeploymentStage.Beta,
  DeploymentStage.Staging,
  DeploymentStage.Production,
];

const workloads = STAGES.map((stage) => {
  const w = new WorkloadsStack(app, `hellodj-workloads-${stage}`, {
    env,
    stage, // → namespace hellodj-<stage> + hostname <stage>.<region>.hellodj.bot
    region: config.region,
    cluster: eks.cluster, // the ONE shared cluster
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
  // The workloads land on the EKS cluster and read the data/auth resources, so
  // they must deploy after those stacks (single `cdk deploy` orders them).
  w.addStackDependency(eks);
  w.addStackDependency(data);
  w.addStackDependency(auth);
  return w;
});

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
void workloads;

// Enforce the Foundation_Singleton_Invariant (R1.7/R1.8) immediately before
// synth: synthesize-and-count every foundation resource type across all
// templates and throw — failing synth and producing no deployable app, with an
// error naming the duplicated type — if any shared foundation resource (VPC,
// EKS control plane, DAX, NAT gateway, node group, ALB, NLB) appears more than
// once. `assertFoundationSingleton` internally invokes `app.synth()`, which
// returns the cached assembly, so the subsequent `app.synth()` call is a no-op.
assertFoundationSingleton(app);

app.synth();

/**
 * DynamoDB + DAX data stack for the HelloDJ AWS platform.
 *
 * Implements the Data_Layer (Requirement 7): DynamoDB is the sole primary
 * datastore, with a single-table `hellodj-core` design plus two DAX-fronted
 * "hot" tables (`hellodj-search-cache`, `hellodj-session`) that minimize read
 * latency for the search cache and session/queue state.
 *
 *   * R7.1 - DynamoDB is the primary persistent data store.
 *   * R7.2 / R7.3 - No PostgreSQL and no SQLite resources are created here.
 *   * R7.4 - The search cache is served from DynamoDB.
 *   * R7.5 - Session and queue state is served from DynamoDB.
 *   * R7.6 - A DAX cluster fronts the hot tables to minimize read latency.
 *
 * The stack mirrors the design's "Data Models" section: the core single table
 * carries `PK`/`SK` with a `GSI1` (`GSI1PK`/`GSI1SK`), the search-cache table
 * is keyed by `queryKey` with a `ttl` attribute for auto-expiry, and the
 * session table carries `PK`/`SK`. DAX runs inside the platform VPC, so this
 * stack accepts the VPC (provided by the network stack in task 9.2) via props.
 */
import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as dax from 'aws-cdk-lib/aws-dax';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as kms from 'aws-cdk-lib/aws-kms';
import * as s3 from 'aws-cdk-lib/aws-s3';

/** The default deployment stage used to derive the assets bucket name. */
export const DEFAULT_ASSETS_STAGE = 'beta';

/** Fixed table names used across the platform's runtime components. */
export const CORE_TABLE_NAME = 'hellodj-core';
export const SEARCH_CACHE_TABLE_NAME = 'hellodj-search-cache';
export const SESSION_TABLE_NAME = 'hellodj-session';

/** The GSI defined on the core single table (design "Core single-table"). */
export const CORE_GSI1_NAME = 'GSI1';

/**
 * Derive the dedicated source-credentials KMS CMK alias for a stage:
 * `alias/hellodj-source-creds-<stage>` (unified-oauth-and-token-watchdog R3.5).
 * The alias gives the key a stable, human-readable handle the workloads wire
 * as `HELLODJ_SOURCE_CREDS_KMS_KEY_ID` and grant against.
 */
export function sourceCredsKeyAlias(stage: string): string {
  return `alias/hellodj-source-creds-${stage}`;
}

/** Properties for {@link DataStack}. */
export interface DataStackProps extends cdk.StackProps {
  /**
   * The platform VPC that the DAX cluster runs in. DAX clusters can only run
   * inside an Amazon VPC, so the network stack's VPC is threaded in here.
   */
  readonly vpc: ec2.IVpc;

  /**
   * The DAX node type. Defaults to a small Graviton-based node
   * (`dax.t3.small`) consistent with the platform's ARM64 default.
   */
  readonly daxNodeType?: string;

  /**
   * Number of DAX nodes. Defaults to 1 (single-node) for non-production; a
   * multi-node replication factor can be supplied for higher availability.
   */
  readonly daxReplicationFactor?: number;

  /**
   * The deployment stage (`beta` / `staging` / `production`) used to derive
   * the per-guild bot-avatar assets bucket name
   * (`hellodj-assets-<stage>-<region>`), mirroring how `EdgeStack` names its
   * web-static / HLS buckets. Defaults to {@link DEFAULT_ASSETS_STAGE} when
   * unset so existing composers keep synthesizing.
   */
  readonly stage?: string;

  /**
   * The AWS region used to derive the assets bucket name. Defaults to the
   * stack's resolved region (`this.region`) when unset.
   */
  readonly region?: string;
}

/**
 * Provisions the DynamoDB tables and the DAX cluster that fronts the hot
 * tables. No RDS/PostgreSQL/SQLite resources are created (R7.2, R7.3).
 */
export class DataStack extends cdk.Stack {
  /** Core single-table (`hellodj-core`) with `GSI1` (R7.1). */
  public readonly coreTable: dynamodb.Table;

  /** DAX-fronted search cache table with TTL (R7.4). */
  public readonly searchCacheTable: dynamodb.Table;

  /** DAX-fronted session/queue state table (R7.5). */
  public readonly sessionTable: dynamodb.Table;

  /** DAX cluster fronting the hot tables to minimize read latency (R7.6). */
  public readonly daxCluster: dax.CfnCluster;

  /**
   * Dedicated customer-managed KMS key (CMK) for application-layer envelope
   * encryption of source-credential token blobs
   * (unified-oauth-and-token-watchdog R3.5). Distinct from the core table's
   * AWS_MANAGED at-rest encryption (R3.1, unchanged): the web-ui/watchdog wrap
   * a per-item data key with this CMK before the token JSON is stored, so a
   * broad table read or a PITR export never yields a plaintext refresh token
   * (R3.2). Key rotation is enabled. Grants are scoped in `WorkloadsStack` to
   * exactly the components that read/write tokens (R3.3, R9.x). Exported so the
   * workloads stack can grant + wire `HELLODJ_SOURCE_CREDS_KMS_KEY_ID`.
   */
  public readonly sourceCredsKey: kms.Key;

  /** Security group applied to the DAX cluster nodes. */
  public readonly daxSecurityGroup: ec2.SecurityGroup;

  /** Cluster discovery endpoint of the DAX cluster (host:port). */
  public readonly daxEndpoint: string;

  /**
   * Per-guild bot-avatar assets bucket (`hellodj-assets-<stage>-<region>`).
   * The web-ui `BotIdentityService` writes avatar bytes here at key
   * `guild/<gid>/bot-avatar/<hash>.<ext>` and the discord-bot-core reads them.
   * Retained on stack removal because it holds user data (unlike the ephemeral
   * HLS bucket, which auto-destroys).
   */
  public readonly assetsBucket: s3.Bucket;

  constructor(scope: Construct, id: string, props: DataStackProps) {
    super(scope, id, props);

    const { vpc } = props;

    // -----------------------------------------------------------------------
    // Core single-table: hellodj-core (PK/SK + GSI1). (R7.1)
    // -----------------------------------------------------------------------
    this.coreTable = new dynamodb.Table(this, 'CoreTable', {
      tableName: CORE_TABLE_NAME,
      partitionKey: { name: 'PK', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'SK', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecovery: true,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
    });

    // GSI1 maps secondary access patterns (e.g. Discord id -> user,
    // appointer -> appointees) per the design's Core single-table model.
    this.coreTable.addGlobalSecondaryIndex({
      indexName: CORE_GSI1_NAME,
      partitionKey: { name: 'GSI1PK', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'GSI1SK', type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    // -----------------------------------------------------------------------
    // Hot table: search cache (hellodj-search-cache), keyed by queryKey with
    // a DynamoDB TTL attribute for auto-expiry. (R7.4)
    // -----------------------------------------------------------------------
    this.searchCacheTable = new dynamodb.Table(this, 'SearchCacheTable', {
      tableName: SEARCH_CACHE_TABLE_NAME,
      partitionKey: { name: 'queryKey', type: dynamodb.AttributeType.STRING },
      timeToLiveAttribute: 'ttl',
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
    });

    // -----------------------------------------------------------------------
    // Hot table: session/queue state (hellodj-session), PK/SK. (R7.5)
    // -----------------------------------------------------------------------
    this.sessionTable = new dynamodb.Table(this, 'SessionTable', {
      tableName: SESSION_TABLE_NAME,
      partitionKey: { name: 'PK', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'SK', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecovery: true,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
    });

    // -----------------------------------------------------------------------
    // DAX cluster fronting the hot tables to minimize read latency. (R7.6)
    // DAX runs in the VPC; it needs an IAM role to access DynamoDB, a subnet
    // group listing the VPC subnets, and a security group.
    // -----------------------------------------------------------------------

    // IAM role DAX assumes at runtime to read/write DynamoDB on our behalf.
    const daxRole = new iam.Role(this, 'DaxServiceRole', {
      assumedBy: new iam.ServicePrincipal('dax.amazonaws.com'),
      description:
        'Role assumed by the HelloDJ DAX cluster to access the hot DynamoDB tables.',
    });

    // Grant DAX access to the two hot tables it fronts (and their indexes).
    this.searchCacheTable.grantReadWriteData(daxRole);
    this.sessionTable.grantReadWriteData(daxRole);

    // Security group for the DAX nodes. Ingress on the DAX port (8111 plain /
    // 9111 TLS) is added by the consuming component stacks once they exist;
    // here we only create the group so the cluster is VPC-scoped.
    this.daxSecurityGroup = new ec2.SecurityGroup(this, 'DaxSecurityGroup', {
      vpc,
      description: 'Security group for the HelloDJ DAX cluster nodes.',
      allowAllOutbound: true,
    });

    // Subnet group across the VPC's private subnets (fall back to all subnets
    // if no dedicated private subnets are configured).
    const subnetSelection = vpc.privateSubnets.length
      ? vpc.privateSubnets
      : vpc.isolatedSubnets.length
        ? vpc.isolatedSubnets
        : vpc.publicSubnets;
    const daxSubnetGroup = new dax.CfnSubnetGroup(this, 'DaxSubnetGroup', {
      subnetGroupName: 'hellodj-dax-subnets',
      description: 'Subnet group for the HelloDJ DAX cluster.',
      subnetIds: subnetSelection.map((subnet) => subnet.subnetId),
    });

    this.daxCluster = new dax.CfnCluster(this, 'DaxCluster', {
      clusterName: 'hellodj-dax',
      description:
        'DAX cluster fronting the hellodj-search-cache and hellodj-session hot tables (R7.6).',
      iamRoleArn: daxRole.roleArn,
      nodeType: props.daxNodeType ?? 'dax.t3.small',
      replicationFactor: props.daxReplicationFactor ?? 1,
      subnetGroupName: daxSubnetGroup.subnetGroupName,
      securityGroupIds: [this.daxSecurityGroup.securityGroupId],
      sseSpecification: { sseEnabled: true },
    });
    // The cluster references the subnet group by name, so enforce ordering.
    this.daxCluster.addDependency(daxSubnetGroup);

    this.daxEndpoint = this.daxCluster.attrClusterDiscoveryEndpoint;

    // -----------------------------------------------------------------------
    // Per-guild bot-avatar assets bucket. The web-ui BotIdentityService writes
    // avatar bytes to `guild/<gid>/bot-avatar/<hash>.<ext>` in the bucket named
    // by env `HELLODJ_ASSETS_BUCKET`; the discord-bot-core reads them back.
    //
    // Naming mirrors EdgeStack's `hellodj-<purpose>-<stage>-<region>` scheme.
    // Unlike the ephemeral HLS bucket, avatars are USER DATA, so the removal
    // policy is RETAIN (never auto-destroyed on stack removal).
    // -----------------------------------------------------------------------
    const stage = props.stage ?? DEFAULT_ASSETS_STAGE;
    const region = props.region ?? this.region;
    this.assetsBucket = new s3.Bucket(this, 'AssetsBucket', {
      bucketName: `hellodj-assets-${stage}-${region}`,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      // Avatars are user data — never auto-destroy (differs from HLS).
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    // -----------------------------------------------------------------------
    // Source-credentials CMK (unified-oauth-and-token-watchdog R3.5). A
    // dedicated customer-managed key used for application-layer ENVELOPE
    // encryption of source-credential token blobs stored on `hellodj-core`:
    // the web-ui/watchdog call `kms:GenerateDataKey` to mint a per-item data
    // key, AES-GCM-encrypt the token JSON with it, and store the CMK-wrapped
    // data key beside the ciphertext; readers `kms:Decrypt` the wrapped key to
    // recover the blob. This is a SECOND layer on top of the table's
    // AWS_MANAGED at-rest encryption (R3.1, unchanged above), so a broad table
    // read or a PITR export never exposes a plaintext refresh token (R3.2).
    //
    // Key rotation is enabled (annual automatic rotation). The CMK is RETAINed
    // on stack removal — destroying it would make every stored credential
    // permanently undecryptable. Grants are scoped to exactly the token
    // read/write components in `WorkloadsStack` (R3.3, R9.x); no broad key
    // policy is added here.
    this.sourceCredsKey = new kms.Key(this, 'SourceCredsKey', {
      alias: sourceCredsKeyAlias(stage),
      description:
        `HelloDJ source-credential envelope-encryption CMK (${stage}). ` +
        'Wraps per-item data keys for token blobs on hellodj-core.',
      enableKeyRotation: true,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    // -----------------------------------------------------------------------
    // Outputs so component stacks can wire table names + DAX endpoint.
    // -----------------------------------------------------------------------
    new cdk.CfnOutput(this, 'CoreTableNameOutput', {
      value: this.coreTable.tableName,
      description: 'Name of the hellodj-core single table.',
    });
    new cdk.CfnOutput(this, 'SearchCacheTableNameOutput', {
      value: this.searchCacheTable.tableName,
      description: 'Name of the hellodj-search-cache hot table.',
    });
    new cdk.CfnOutput(this, 'SessionTableNameOutput', {
      value: this.sessionTable.tableName,
      description: 'Name of the hellodj-session hot table.',
    });
    new cdk.CfnOutput(this, 'DaxEndpointOutput', {
      value: this.daxEndpoint,
      description: 'DAX cluster discovery endpoint fronting the hot tables.',
    });
    new cdk.CfnOutput(this, 'AssetsBucketNameOutput', {
      value: this.assetsBucket.bucketName,
      description:
        'Name of the per-guild bot-avatar assets bucket (HELLODJ_ASSETS_BUCKET).',
    });
    new cdk.CfnOutput(this, 'SourceCredsKmsKeyIdOutput', {
      value: this.sourceCredsKey.keyId,
      description:
        'Key id of the source-credential envelope-encryption CMK ' +
        '(HELLODJ_SOURCE_CREDS_KMS_KEY_ID).',
    });
  }
}

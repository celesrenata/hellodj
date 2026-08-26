/**
 * S3 Hive Log_Store + Glue + Athena + QuickSight analytics stack.
 *
 * Implements the analytics half of the Observability_Stack (design's
 * `CWL --> S3L --> GLUE --> ATH --> QS` flow), covering:
 *
 *   * R10.1 - An S3 `Log_Store` bucket that stores HelloDJ_Platform logs in
 *     Hive-partitioned format. Objects are written under keys derived by the
 *     Python `hellodj_platform_logic.hive_partition` module, whose scheme is
 *     `<prefix>/year=YYYY/month=MM/day=DD/hour=HH[/<suffix>]`. That module is
 *     the single source of truth for the actual key derivation; this stack
 *     catalogs those same `year/month/day/hour` partition keys so IaC and the
 *     runtime log shipper agree on one layout.
 *   * R10.6 - A Glue database + crawler that catalogs the Log_Store for
 *     analytics, declaring the `year/month/day/hour` partition keys.
 *   * R10.7 - An Athena workgroup (with an S3 results location) and a saved
 *     named query so the Analytics_Query_Service can run queries and jobs over
 *     the cataloged Log_Store.
 *   * R10.8 - A QuickSight Athena data source so the Analytics_Dashboard_Service
 *     can build dashboards/visualizations over the analytics data.
 *
 * The pure decision logic (the actual Hive key derivation) lives in
 * `components/hellodj_platform_logic/hive_partition.py`; this stack only
 * declares the matching catalog partition keys and does not re-derive keys.
 */
import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as glue from 'aws-cdk-lib/aws-glue';
import * as athena from 'aws-cdk-lib/aws-athena';
import * as quicksight from 'aws-cdk-lib/aws-quicksight';
import * as iam from 'aws-cdk-lib/aws-iam';
import { DeploymentStage } from './config';

/**
 * The Hive partition keys the Log_Store is written with and the Glue table is
 * cataloged by. This mirrors the `year=/month=/day=/hour=` scheme produced by
 * the Python `hellodj_platform_logic.hive_partition` module (R10.1). Kept as
 * an exported constant so the assertion tests (task 17.3) can assert the Glue
 * crawler declares exactly these partition keys.
 */
export const HIVE_PARTITION_KEYS: readonly string[] = [
  'year',
  'month',
  'day',
  'hour',
];

/** Properties for {@link AnalyticsStack}. */
export interface AnalyticsStackProps extends cdk.StackProps {
  /** The deployment stage this analytics stack belongs to (Beta/Staging/Production). */
  readonly stage: DeploymentStage;

  /**
   * Whether to provision the QuickSight Athena data source (R10.8).
   *
   * `AWS::QuickSight::DataSource` requires an ACTIVE QuickSight account
   * subscription in the target AWS account; QuickSight sign-up is a one-time
   * account-level action that cannot be performed through CloudFormation. On
   * an account without that subscription the resource fails to create
   * (`ResourceNotFound` for the account's QuickSight namespace) and rolls the
   * stack back. The S3 Log_Store, Glue catalog/crawler, and Athena
   * workgroup/named query do NOT depend on QuickSight, so the data source is
   * gated behind this flag (default `false`): the analytics pipeline deploys
   * cleanly on a fresh account, and the QuickSight anchor is enabled once a
   * subscription exists.
   *
   * @default false
   */
  readonly enableQuickSightDataSource?: boolean;
}

/**
 * Analytics stack: S3 Hive Log_Store, Glue database + crawler, Athena
 * workgroup + named query, and a QuickSight Athena data source.
 */
export class AnalyticsStack extends cdk.Stack {
  /** The Hive-partitioned S3 `Log_Store` bucket (R10.1). */
  public readonly logStoreBucket: s3.Bucket;
  /** The S3 bucket Athena writes query results to. */
  public readonly athenaResultsBucket: s3.Bucket;
  /** The Glue catalog database backing the Log_Store (R10.6). */
  public readonly glueDatabase: glue.CfnDatabase;
  /** The Glue crawler that catalogs the Log_Store (R10.6). */
  public readonly glueCrawler: glue.CfnCrawler;
  /** The Athena workgroup for analytics queries/jobs (R10.7). */
  public readonly athenaWorkGroup: athena.CfnWorkGroup;
  /**
   * The QuickSight Athena data source for dashboards (R10.8). Present only when
   * {@link AnalyticsStackProps.enableQuickSightDataSource} is `true` (requires
   * an active QuickSight account subscription); otherwise `undefined`.
   */
  public readonly quickSightDataSource?: quicksight.CfnDataSource;

  constructor(scope: Construct, id: string, props: AnalyticsStackProps) {
    super(scope, id, props);

    const { stage } = props;
    const region = this.region;
    const account = this.account;

    // --- S3 Hive-partitioned Log_Store (R10.1) ---------------------------
    // Logs are written under keys of the form
    //   <prefix>/year=YYYY/month=MM/day=DD/hour=HH[/<suffix>]
    // derived by the Python `hellodj_platform_logic.hive_partition` module
    // (the single source of truth). This bucket is the storage target; the
    // Glue crawler below catalogs it by the same year/month/day/hour keys.
    this.logStoreBucket = new s3.Bucket(this, 'LogStoreBucket', {
      bucketName: `hellodj-log-store-${stage}-${region}`,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      // Retain logs then transition/expire via lifecycle to keep the
      // Log_Store cost in line with the cost model's tiered retention.
      lifecycleRules: [
        {
          transitions: [
            {
              storageClass: s3.StorageClass.INFREQUENT_ACCESS,
              transitionAfter: cdk.Duration.days(30),
            },
          ],
          expiration: cdk.Duration.days(365),
        },
      ],
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
    });

    // Athena must write query results somewhere; keep them out of the raw
    // Log_Store so the crawler never catalogs Athena's own output.
    this.athenaResultsBucket = new s3.Bucket(this, 'AthenaResultsBucket', {
      bucketName: `hellodj-athena-results-${stage}-${region}`,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      lifecycleRules: [{ expiration: cdk.Duration.days(30) }],
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
    });

    // --- Glue catalog database (R10.6) -----------------------------------
    this.glueDatabase = new glue.CfnDatabase(this, 'LogStoreDatabase', {
      catalogId: account,
      databaseInput: {
        name: `hellodj_logs_${stage}`,
        description:
          'HelloDJ Log_Store catalog database (Hive-partitioned S3 logs).',
      },
    });

    // IAM role the Glue crawler assumes to read the Log_Store and write the
    // catalog. Scoped to the AWS-managed Glue service policy plus read access
    // to the Log_Store bucket.
    const crawlerRole = new iam.Role(this, 'LogStoreCrawlerRole', {
      assumedBy: new iam.ServicePrincipal('glue.amazonaws.com'),
      managedPolicies: [
        iam.ManagedPolicy.fromAwsManagedPolicyName(
          'service-role/AWSGlueServiceRole',
        ),
      ],
    });
    this.logStoreBucket.grantRead(crawlerRole);

    // --- Glue crawler cataloging the Hive-partitioned Log_Store (R10.6) ---
    // The crawler infers the schema of the log objects; the S3 target path
    // points at the bucket root so the crawler discovers the
    // year=/month=/day=/hour= partitions written per hive_partition.py.
    // `createNativeDeltaTable`/partition settings default to Hive-style
    // partition discovery, which recovers the year/month/day/hour keys.
    this.glueCrawler = new glue.CfnCrawler(this, 'LogStoreCrawler', {
      name: `hellodj-log-store-crawler-${stage}`,
      role: crawlerRole.roleArn,
      databaseName: `hellodj_logs_${stage}`,
      description:
        'Catalogs the Hive-partitioned Log_Store by year/month/day/hour ' +
        `partition keys (${HIVE_PARTITION_KEYS.join('/')}).`,
      targets: {
        s3Targets: [
          {
            path: `s3://${this.logStoreBucket.bucketName}/`,
          },
        ],
      },
      // Run daily so the catalog picks up the day's new partitions. The
      // Log_Store is still written with hour-granular Hive keys
      // (year/month/day/hour), but the crawler only needs to run once a day to
      // register the new folders — a daily crawl keeps Glue cost minimal while
      // catalog freshness (next-day) is sufficient for the analytics queries.
      schedule: {
        // Daily at 00:05 UTC.
        scheduleExpression: 'cron(5 0 * * ? *)',
      },
      schemaChangePolicy: {
        // AWS Glue constraint: when `recrawlBehavior` is
        // `CRAWL_NEW_FOLDERS_ONLY`, BOTH `updateBehavior` and `deleteBehavior`
        // MUST be `LOG` (the crawler only logs schema changes rather than
        // mutating the catalog, since it never re-reads existing partitions).
        // Pairing `UPDATE_IN_DATABASE` with new-folders-only crawling is
        // rejected with an InvalidRequest at create time.
        updateBehavior: 'LOG',
        deleteBehavior: 'LOG',
      },
      recrawlPolicy: {
        // Only crawl new folders (new hourly partitions) for efficiency.
        recrawlBehavior: 'CRAWL_NEW_FOLDERS_ONLY',
      },
    });
    this.glueCrawler.addDependency(this.glueDatabase);

    // --- Athena workgroup + named query (R10.7) --------------------------
    this.athenaWorkGroup = new athena.CfnWorkGroup(this, 'AnalyticsWorkGroup', {
      name: `hellodj-analytics-${stage}`,
      description:
        'Athena workgroup for HelloDJ analytics over the cataloged Log_Store.',
      state: 'ENABLED',
      recursiveDeleteOption: true,
      workGroupConfiguration: {
        enforceWorkGroupConfiguration: true,
        publishCloudWatchMetricsEnabled: true,
        resultConfiguration: {
          outputLocation: `s3://${this.athenaResultsBucket.bucketName}/results/`,
          encryptionConfiguration: {
            encryptionOption: 'SSE_S3',
          },
        },
      },
    });

    // A saved query the Analytics_Query_Service can run over the cataloged
    // Log_Store, partition-pruned by the Hive year/month/day/hour keys.
    const namedQuery = new athena.CfnNamedQuery(this, 'RecentLogsQuery', {
      database: `hellodj_logs_${stage}`,
      workGroup: this.athenaWorkGroup.name,
      name: `hellodj-recent-logs-${stage}`,
      description:
        'Sample partition-pruned query over the Hive-partitioned Log_Store.',
      queryString: [
        'SELECT *',
        'FROM log_store',
        "WHERE year = '2026' AND month = '08' AND day = '24'",
        'LIMIT 100;',
      ].join('\n'),
    });
    namedQuery.addDependency(this.athenaWorkGroup);

    // --- QuickSight Athena data source (R10.8) ---------------------------
    // The Analytics_Dashboard_Service (QuickSight) reads analytics data via
    // Athena. This creates the Athena-backed data source QuickSight builds
    // dashboards/visualizations on top of. QuickSight requires an active
    // account subscription in the target AWS account; the data source is the
    // catalog-level anchor for downstream CfnDataSet/CfnAnalysis/CfnDashboard
    // resources (added as dashboards are authored).
    if (props.enableQuickSightDataSource) {
      this.quickSightDataSource = new quicksight.CfnDataSource(
        this,
        'AnalyticsDataSource',
        {
          awsAccountId: account,
          dataSourceId: `hellodj-analytics-${stage}`,
          name: `HelloDJ Analytics (${stage})`,
          type: 'ATHENA',
          dataSourceParameters: {
            athenaParameters: {
              workGroup: this.athenaWorkGroup.name!,
            },
          },
        },
      );
      this.quickSightDataSource.addDependency(this.athenaWorkGroup);
    }

    // --- Outputs ---------------------------------------------------------
    new cdk.CfnOutput(this, 'LogStoreBucketName', {
      value: this.logStoreBucket.bucketName,
      description: 'Hive-partitioned S3 Log_Store bucket.',
    });
    new cdk.CfnOutput(this, 'GlueDatabaseName', {
      value: `hellodj_logs_${stage}`,
      description: 'Glue catalog database for the Log_Store.',
    });
    new cdk.CfnOutput(this, 'GlueCrawlerName', {
      value: this.glueCrawler.name!,
      description: 'Glue crawler cataloging the Log_Store.',
    });
    new cdk.CfnOutput(this, 'AthenaWorkGroupName', {
      value: this.athenaWorkGroup.name,
      description: 'Athena workgroup for analytics queries.',
    });
  }
}

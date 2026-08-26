import * as cdk from 'aws-cdk-lib';
import { Template, Match } from 'aws-cdk-lib/assertions';
import { AnalyticsStack, HIVE_PARTITION_KEYS } from '../lib/analytics-stack';
import { DeploymentStage } from '../lib/config';

/**
 * CDK assertion tests for the S3 Hive Log_Store + Glue + Athena + QuickSight
 * AnalyticsStack (task 17.3).
 *
 * These assert the synthesized CloudFormation template matches the design's
 * Observability_Stack analytics half (CWL --> S3L --> GLUE --> ATH --> QS):
 *   - An S3 Log_Store bucket exists (R10.1).
 *   - A Glue database + crawler catalog the Log_Store (R10.6).
 *   - An Athena workgroup + named query enable analytics queries (R10.7).
 *   - A QuickSight Athena data source backs dashboards (R10.8).
 *   - The catalog reflects the year/month/day/hour Hive partition keys.
 *
 * _Requirements: 10.6, 10.7, 10.8._
 */

/** Synthesize the AnalyticsStack for the Beta stage. */
function synthAnalyticsStack(): Template {
  const app = new cdk.App();
  const stack = new AnalyticsStack(app, 'TestAnalyticsStack', {
    env: { account: '123456789012', region: 'us-east-1' },
    stage: DeploymentStage.Beta,
  });
  return Template.fromStack(stack);
}

describe('AnalyticsStack (S3 + Glue + Athena + QuickSight)', () => {
  const template = synthAnalyticsStack();

  test('provisions an S3 Log_Store bucket (R10.1)', () => {
    const buckets = template.findResources('AWS::S3::Bucket');
    expect(Object.keys(buckets).length).toBeGreaterThanOrEqual(1);
    template.hasResourceProperties('AWS::S3::Bucket', {
      BucketName: 'hellodj-log-store-beta-us-east-1',
    });
  });

  test('catalogs the Log_Store with a Glue database (R10.6)', () => {
    template.resourceCountIs('AWS::Glue::Database', 1);
    template.hasResourceProperties('AWS::Glue::Database', {
      DatabaseInput: Match.objectLike({ Name: 'hellodj_logs_beta' }),
    });
  });

  test('runs a Glue crawler over the Hive-partitioned Log_Store (R10.6)', () => {
    template.resourceCountIs('AWS::Glue::Crawler', 1);
    // The S3 target Path is a CloudFormation Fn::Join over the (tokenized)
    // Log_Store bucket name, so assert an S3 target with a Path is present
    // rather than pinning the interpolated string.
    template.hasResourceProperties('AWS::Glue::Crawler', {
      DatabaseName: 'hellodj_logs_beta',
      Targets: Match.objectLike({
        S3Targets: Match.arrayWith([
          Match.objectLike({ Path: Match.anyValue() }),
        ]),
      }),
    });
  });

  test('Hive partition keys are exactly year/month/day/hour', () => {
    expect([...HIVE_PARTITION_KEYS]).toEqual(['year', 'month', 'day', 'hour']);
  });

  test('crawler runs on a daily schedule', () => {
    template.hasResourceProperties('AWS::Glue::Crawler', {
      Schedule: { ScheduleExpression: 'cron(5 0 * * ? *)' },
    });
  });

  test('crawler description reflects the year/month/day/hour partition keys', () => {
    // The crawler description embeds the HIVE_PARTITION_KEYS joined by "/",
    // so the catalog and the runtime log shipper agree on the layout.
    const partitionPath = HIVE_PARTITION_KEYS.join('/');
    template.hasResourceProperties('AWS::Glue::Crawler', {
      Description: Match.stringLikeRegexp(partitionPath),
    });
  });

  test('provides an Athena workgroup for analytics queries (R10.7)', () => {
    template.resourceCountIs('AWS::Athena::WorkGroup', 1);
    template.hasResourceProperties('AWS::Athena::WorkGroup', {
      Name: 'hellodj-analytics-beta',
    });
  });

  test('provides a saved Athena named query over the Log_Store (R10.7)', () => {
    template.resourceCountIs('AWS::Athena::NamedQuery', 1);
    template.hasResourceProperties('AWS::Athena::NamedQuery', {
      Database: 'hellodj_logs_beta',
    });
  });

  test('omits the QuickSight data source by default (no subscription required)', () => {
    // QuickSight::DataSource needs an active account subscription; gated off by
    // default so the analytics pipeline deploys on a fresh account (R10.8).
    template.resourceCountIs('AWS::QuickSight::DataSource', 0);
  });

  test('provides a QuickSight Athena data source when enabled (R10.8)', () => {
    const app = new cdk.App();
    const stack = new AnalyticsStack(app, 'TestAnalyticsStackQs', {
      env: { account: '123456789012', region: 'us-east-1' },
      stage: DeploymentStage.Beta,
      enableQuickSightDataSource: true,
    });
    const qsTemplate = Template.fromStack(stack);
    qsTemplate.resourceCountIs('AWS::QuickSight::DataSource', 1);
    qsTemplate.hasResourceProperties('AWS::QuickSight::DataSource', {
      Type: 'ATHENA',
    });
  });
});

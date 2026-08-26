import * as cdk from 'aws-cdk-lib';
import { Template, Match } from 'aws-cdk-lib/assertions';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import {
  DataStack,
  CORE_TABLE_NAME,
  SEARCH_CACHE_TABLE_NAME,
  SESSION_TABLE_NAME,
  CORE_GSI1_NAME,
} from '../lib/data-stack';

/**
 * CDK assertion tests for the DynamoDB + DAX DataStack (task 10.3).
 *
 * These assert the synthesized CloudFormation template matches the design's
 * Data_Layer intent:
 *   - DynamoDB is the sole primary datastore (R7.1) — three tables present.
 *   - No PostgreSQL / SQLite / RDS resources are created (R7.2, R7.3).
 *   - The core single table carries a GSI1 global secondary index.
 *   - The search-cache table has a DynamoDB TTL on the `ttl` attribute.
 *   - A DAX cluster fronts the hot tables (R7.6).
 *
 * _Requirements: 7.1, 7.2, 7.3._
 */

/**
 * Synthesize the DataStack against a VPC created in a separate stack within
 * the same app. The DAX cluster only needs the VPC's subnet ids, so a plain
 * `ec2.Vpc` (public + private NAT subnets by default) satisfies the props.
 */
function synthDataStack(): Template {
  const app = new cdk.App();
  const netStack = new cdk.Stack(app, 'TestNetworkStack', {
    env: { account: '123456789012', region: 'us-east-1' },
  });
  const vpc = new ec2.Vpc(netStack, 'TestVpc', { maxAzs: 2 });

  const dataStack = new DataStack(app, 'TestDataStack', {
    env: { account: '123456789012', region: 'us-east-1' },
    vpc,
  });
  return Template.fromStack(dataStack);
}

describe('DataStack (DynamoDB + DAX)', () => {
  const template = synthDataStack();

  test('creates exactly three DynamoDB tables (R7.1)', () => {
    template.resourceCountIs('AWS::DynamoDB::Table', 3);
  });

  test('core table is named hellodj-core and has a GSI1 global secondary index', () => {
    template.hasResourceProperties('AWS::DynamoDB::Table', {
      TableName: CORE_TABLE_NAME,
      GlobalSecondaryIndexes: Match.arrayWith([
        Match.objectLike({ IndexName: CORE_GSI1_NAME }),
      ]),
    });
  });

  test('search-cache table has a TTL on the `ttl` attribute', () => {
    template.hasResourceProperties('AWS::DynamoDB::Table', {
      TableName: SEARCH_CACHE_TABLE_NAME,
      TimeToLiveSpecification: {
        AttributeName: 'ttl',
        Enabled: true,
      },
    });
  });

  test('session table is present with PK/SK', () => {
    template.hasResourceProperties('AWS::DynamoDB::Table', {
      TableName: SESSION_TABLE_NAME,
      KeySchema: Match.arrayWith([
        Match.objectLike({ AttributeName: 'PK', KeyType: 'HASH' }),
        Match.objectLike({ AttributeName: 'SK', KeyType: 'RANGE' }),
      ]),
    });
  });

  test('provisions a DAX cluster fronting the hot tables (R7.6)', () => {
    template.resourceCountIs('AWS::DAX::Cluster', 1);
    template.hasResourceProperties('AWS::DAX::Cluster', {
      ClusterName: 'hellodj-dax',
    });
    // DAX runs in the VPC via a subnet group.
    template.resourceCountIs('AWS::DAX::SubnetGroup', 1);
  });

  // ---------------------------------------------------------------------------
  // Shared-foundation task 4.1: the DAX cluster is a single-node singleton.
  //
  // The shared-foundation refactor instantiates DataStack exactly once as a
  // stage-independent singleton, so the whole foundation has one shared DAX
  // cluster (R1.4). That single cluster stays a one-node `dax.t3.small`
  // (`replicationFactor: 1`) shared across all three Software_Stages (R4.8).
  // No change to `data-stack.ts` is required — these assertions lock the
  // existing single-node DAX topology so the refactor cannot silently grow it.
  //
  // _Requirements: 4.8, 1.4._
  // ---------------------------------------------------------------------------
  test('has exactly one shared DAX cluster (R1.4)', () => {
    template.resourceCountIs('AWS::DAX::Cluster', 1);
  });

  test('the single DAX cluster is a one-node dax.t3.small (R4.8)', () => {
    template.hasResourceProperties('AWS::DAX::Cluster', {
      NodeType: 'dax.t3.small',
      ReplicationFactor: 1,
    });
  });

  test('creates NO PostgreSQL / RDS resources (R7.2)', () => {
    template.resourceCountIs('AWS::RDS::DBInstance', 0);
    template.resourceCountIs('AWS::RDS::DBCluster', 0);
  });

  test('creates NO SQLite-style relational datastore (R7.3)', () => {
    // There is no CloudFormation SQLite resource; the platform must not fall
    // back to any relational engine. Assert the RDS families stay empty and
    // no ElastiCache/Neptune relational substitutes sneak in.
    template.resourceCountIs('AWS::RDS::DBInstance', 0);
    template.resourceCountIs('AWS::RDS::DBCluster', 0);
    template.resourceCountIs('AWS::Neptune::DBCluster', 0);
  });
});

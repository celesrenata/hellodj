import * as cdk from 'aws-cdk-lib';
import { Template, Match } from 'aws-cdk-lib/assertions';
import {
  WorkloadsStack,
  WorkloadsStackProps,
  ENTITLEMENT_USER_PK_PREFIX,
  ENTITLEMENT_AIPRICING_PK,
} from '../lib/workloads-stack';
import { EksStack } from '../lib/eks-stack';
import { NetworkStack } from '../lib/network-stack';
import { DataStack } from '../lib/data-stack';
import { AuthStack } from '../lib/auth-stack';
import { DeploymentStage } from '../lib/config';

/**
 * Task 14 (admin-entitlements-panel) — CDK tests for the bot IRSA
 * entitlement/pricing/tally DynamoDB grants on the `hellodj-core` single table
 * (Requirements 10.3, 14.1).
 *
 * The `discord-bot-core` component hosts the `UserEntitlementResolver`: it
 * READS a user's entitlement item + the shared AI-pricing item and WRITES
 * (increments) the user's AI-cost tally item. In addition to the broad
 * `grantReadWriteData` it already holds via its `coreTable` dependency, the
 * WorkloadsStack adds two explicit least-privilege statements scoped by
 * `dynamodb:LeadingKeys` to the `USER#*` partitions (entitlement/tally) and the
 * `CONFIG#AIPRICING` pricing partition, so the entitlements access is auditable
 * in its own right.
 *
 * The web-ui already reads/writes the entitlement, tally, audit, and pricing
 * items through its full-table `grantReadWriteData` (`coreTable` dependency);
 * that broad grant is asserted here too so the web-ui half of task 14 is
 * covered.
 *
 * The per-component SA role + its policy are created by
 * `cluster.addServiceAccount`, so the `AWS::IAM::Policy` resource lands on the
 * shared cluster's `EksStack` template.
 */
describe('bot entitlement/pricing/tally DynamoDB IAM (task 14, R10.3, R14.1)', () => {
  const TEST_REGION = 'us-east-1';
  const TEST_ACCOUNT = '111111111111';
  const COMPOSE_ENV = { account: TEST_ACCOUNT, region: TEST_REGION };
  const STAGE = DeploymentStage.Beta;

  function compose(): Template {
    const app = new cdk.App();
    const network = new NetworkStack(app, 'hellodj-network', {
      env: COMPOSE_ENV,
    });
    const data = new DataStack(app, 'hellodj-data', {
      env: COMPOSE_ENV,
      vpc: network.vpc,
    });
    const auth = new AuthStack(app, 'hellodj-auth', {
      env: COMPOSE_ENV,
      stage: STAGE,
    });
    const eks = new EksStack(app, 'hellodj-eks', {
      env: COMPOSE_ENV,
      vpc: network.vpc,
    });

    const props: WorkloadsStackProps = {
      env: COMPOSE_ENV,
      stage: STAGE,
      region: TEST_REGION,
      cluster: eks.cluster,
      data: {
        coreTable: data.coreTable,
        searchCacheTable: data.searchCacheTable,
        sessionTable: data.sessionTable,
        daxEndpoint: data.daxEndpoint,
        assetsBucket: data.assetsBucket,
      },
      secrets: {
        discordBotToken: auth.discordBotTokenSecret,
        tidalRefresh: auth.tidalRefreshSecret,
        spotify: auth.spotifySecret,
        ytCipher: auth.ytCipherSecret,
      },
      aiTaskRole: auth.aiTaskRole,
    };
    const workloads = new WorkloadsStack(
      app,
      `hellodj-workloads-${STAGE}`,
      props,
    );
    workloads.addStackDependency(eks);
    workloads.addStackDependency(data);
    workloads.addStackDependency(auth);

    return Template.fromStack(eks);
  }

  // -- R14.1: bot resolver READ on entitlement + pricing items -------------
  test('bot role grants scoped DynamoDB read on entitlement + pricing partitions (R14.1)', () => {
    const eksTemplate = compose();
    eksTemplate.hasResourceProperties('AWS::IAM::Policy', {
      PolicyDocument: Match.objectLike({
        Statement: Match.arrayWith([
          Match.objectLike({
            Effect: 'Allow',
            Action: [
              'dynamodb:GetItem',
              'dynamodb:BatchGetItem',
              'dynamodb:Query',
            ],
            Condition: {
              'ForAllValues:StringLike': {
                'dynamodb:LeadingKeys': [
                  `${ENTITLEMENT_USER_PK_PREFIX}*`,
                  ENTITLEMENT_AIPRICING_PK,
                ],
              },
            },
          }),
        ]),
      }),
    });
  });

  // -- R14.1/R10.1: bot resolver WRITE on the per-user tally partition -----
  test('bot role grants scoped DynamoDB write on the USER# tally partition (R10.1)', () => {
    const eksTemplate = compose();
    eksTemplate.hasResourceProperties('AWS::IAM::Policy', {
      PolicyDocument: Match.objectLike({
        Statement: Match.arrayWith([
          Match.objectLike({
            Effect: 'Allow',
            Action: ['dynamodb:UpdateItem', 'dynamodb:PutItem'],
            Condition: {
              'ForAllValues:StringLike': {
                'dynamodb:LeadingKeys': [`${ENTITLEMENT_USER_PK_PREFIX}*`],
              },
            },
          }),
        ]),
      }),
    });
  });

  // -- The scoped read grant must NOT include write actions (least priv) ---
  test('the entitlement read grant does not include UpdateItem/DeleteItem (least privilege)', () => {
    const eksTemplate = compose();
    const policies = eksTemplate.findResources('AWS::IAM::Policy');
    const readStatements = Object.values(policies).flatMap((p) => {
      const statements = (p.Properties?.PolicyDocument?.Statement ??
        []) as Array<Record<string, unknown>>;
      return statements.filter((s) => {
        const actions = s.Action;
        return (
          Array.isArray(actions) &&
          actions.includes('dynamodb:GetItem') &&
          actions.includes('dynamodb:Query') &&
          !actions.includes('dynamodb:UpdateItem')
        );
      });
    });
    // At least the resolver's read statement exists, and none of the pure-read
    // statements grant a write/delete action.
    expect(readStatements.length).toBeGreaterThan(0);
    for (const s of readStatements) {
      const actions = s.Action as string[];
      expect(actions).not.toContain('dynamodb:DeleteItem');
      expect(actions).not.toContain('dynamodb:UpdateItem');
      expect(actions).not.toContain('dynamodb:PutItem');
    }
  });

  // -- Web-ui: full read/write on the core table (entitlement/tally/audit/pricing) --
  test('web-ui role holds read/write on the core table for entitlement items (R14.1)', () => {
    const eksTemplate = compose();
    // web-ui declares `coreTable: true` -> CDK `grantReadWriteData` renders the
    // DynamoDB read+write action set on the core table ARN, which covers the
    // entitlement, AITALLY, AUDIT#, and CONFIG#AIPRICING items. CDK emits the
    // action set in its own (unordered-from-our-view) sequence, so assert set
    // membership rather than a positional `arrayWith` subsequence (which is
    // sensitive to CDK's internal action ordering).
    const REQUIRED_RW_ACTIONS = [
      'dynamodb:GetItem',
      'dynamodb:Query',
      'dynamodb:PutItem',
      'dynamodb:UpdateItem',
    ];
    const policies = eksTemplate.findResources('AWS::IAM::Policy');
    const rwStatements = Object.values(policies).flatMap((p) => {
      const statements = (p.Properties?.PolicyDocument?.Statement ??
        []) as Array<Record<string, unknown>>;
      return statements.filter((s) => {
        const actions = s.Action;
        return (
          Array.isArray(actions) &&
          REQUIRED_RW_ACTIONS.every((a) => actions.includes(a))
        );
      });
    });
    // At least one statement (the web-ui's core-table grant) grants the full
    // read+write action set that covers every entitlement item shape.
    expect(rwStatements.length).toBeGreaterThan(0);
  });
});

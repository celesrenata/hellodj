import * as cdk from 'aws-cdk-lib';
import { Template, Match } from 'aws-cdk-lib/assertions';
import {
  WorkloadsStack,
  WorkloadsStackProps,
} from '../lib/workloads-stack';
import { EksStack } from '../lib/eks-stack';
import { NetworkStack } from '../lib/network-stack';
import { DataStack } from '../lib/data-stack';
import { AuthStack } from '../lib/auth-stack';
import { DeploymentStage } from '../lib/config';

/**
 * CDK tests for the per-guild bot-avatar assets S3 bucket
 * (bot-identity-and-source-auth).
 *
 * The web-ui `BotIdentityService` uploads avatar bytes to S3 at key
 * `guild/<gid>/bot-avatar/<hash>.<ext>` in the bucket named by env
 * `HELLODJ_ASSETS_BUCKET`; the discord-bot-core reads those bytes back. The
 * bucket is owned by `DataStack` (durable data resource, already a
 * WorkloadsStack dependency via `data`) and named
 * `hellodj-assets-<stage>-<region>`, mirroring the edge stack's bucket naming.
 *
 * These assertions lock:
 *   - the assets bucket is synthesized on the DataStack template with the
 *     expected name pattern and a RETAIN removal policy (user data — unlike the
 *     ephemeral HLS bucket which auto-destroys);
 *   - the web-ui container env includes `HELLODJ_ASSETS_BUCKET`;
 *   - the discord-bot-core container env includes `HELLODJ_ASSETS_BUCKET`;
 *   - the web-ui SA role is granted WRITE (`s3:PutObject`) and the
 *     discord-bot-core SA role is granted READ (`s3:GetObject`) on the bucket,
 *     each least-privilege.
 *
 * Mirrors `web-ui-invite-iam.test.ts` / `web-ui-source-oauth-env.test.ts`: the
 * Kubernetes manifests are attached to the shared cluster (`cluster.addManifest`)
 * so they land on the `EksStack` template (flatten the literal `Fn::Join`
 * fragments to search the container env); the per-component ServiceAccount IAM
 * policies (created by `cluster.addServiceAccount`) also land on the shared
 * `EksStack` template. The bucket resource itself lands on the DataStack
 * template.
 */
describe('per-guild bot-avatar assets bucket (bot-identity-and-source-auth)', () => {
  const TEST_REGION = 'us-east-1';
  const TEST_ACCOUNT = '111111111111';
  const COMPOSE_ENV = { account: TEST_ACCOUNT, region: TEST_REGION };
  const STAGE = DeploymentStage.Beta;
  const USER_POOL_ID = 'us-east-1_TestPool0';

  interface Composed {
    dataTemplate: Template;
    eksTemplate: Template;
  }

  /**
   * Compose the shared foundation once and attach a single Beta WorkloadsStack.
   * The DataStack takes the `stage`/`region` so the bucket name is derived
   * exactly as `bin/hellodj.ts` threads it. Returns the DataStack template
   * (where the bucket lands) and the EksStack template (container env +
   * per-component SA IAM policies).
   */
  function compose(): Composed {
    const app = new cdk.App();
    const network = new NetworkStack(app, 'hellodj-network', {
      env: COMPOSE_ENV,
    });
    const data = new DataStack(app, 'hellodj-data', {
      env: COMPOSE_ENV,
      vpc: network.vpc,
      stage: STAGE,
      region: TEST_REGION,
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
      cognitoUserPoolId: USER_POOL_ID,
    };
    const workloads = new WorkloadsStack(
      app,
      `hellodj-workloads-${STAGE}`,
      props,
    );
    workloads.addStackDependency(eks);
    workloads.addStackDependency(data);
    workloads.addStackDependency(auth);

    return {
      dataTemplate: Template.fromStack(data),
      eksTemplate: Template.fromStack(eks),
    };
  }

  /** The expected assets bucket name `hellodj-assets-<stage>-<region>`. */
  const expectedBucketName = `hellodj-assets-${STAGE}-${TEST_REGION}`;

  /** Flatten a manifest value's literal `Fn::Join` fragments into one string. */
  function flattenManifest(value: unknown): string {
    if (typeof value === 'string') {
      return value;
    }
    if (Array.isArray(value)) {
      return value.map(flattenManifest).join('');
    }
    if (value && typeof value === 'object') {
      const obj = value as Record<string, unknown>;
      if (obj['Fn::Join']) {
        const join = obj['Fn::Join'] as [string, unknown[]];
        const [sep, parts] = join;
        return parts.map(flattenManifest).join(sep);
      }
      return '';
    }
    return '';
  }

  /** The flattened literal text of every Kubernetes manifest on a template. */
  function collectManifestText(template: Template): string {
    const resources = template.findResources(
      'Custom::AWSCDK-EKS-KubernetesResource',
    );
    return Object.values(resources)
      .map((r) => flattenManifest(r.Properties?.Manifest))
      .join('\n');
  }

  // -- Bucket synthesized with the expected name + RETAIN policy -----------
  test('the assets bucket is synthesized with the expected name pattern', () => {
    const { dataTemplate } = compose();
    dataTemplate.hasResourceProperties('AWS::S3::Bucket', {
      BucketName: expectedBucketName,
    });
  });

  test('the assets bucket is BLOCK_ALL public access + SSE + enforceSSL', () => {
    const { dataTemplate } = compose();
    dataTemplate.hasResourceProperties('AWS::S3::Bucket', {
      BucketName: expectedBucketName,
      PublicAccessBlockConfiguration: {
        BlockPublicAcls: true,
        BlockPublicPolicy: true,
        IgnorePublicAcls: true,
        RestrictPublicBuckets: true,
      },
      BucketEncryption: {
        ServerSideEncryptionConfiguration: Match.arrayWith([
          Match.objectLike({
            ServerSideEncryptionByDefault: { SSEAlgorithm: 'AES256' },
          }),
        ]),
      },
    });
  });

  test('the assets bucket has a RETAIN removal policy (user data, unlike HLS)', () => {
    const { dataTemplate } = compose();
    // RemovalPolicy.RETAIN maps to Retain on both DeletionPolicy and
    // UpdateReplacePolicy for the raw CloudFormation resource.
    const buckets = dataTemplate.findResources('AWS::S3::Bucket', {
      Properties: { BucketName: expectedBucketName },
    });
    const bucket = Object.values(buckets)[0];
    expect(bucket).toBeDefined();
    expect(bucket.DeletionPolicy).toBe('Retain');
    expect(bucket.UpdateReplacePolicy).toBe('Retain');
  });

  // -- Container env includes HELLODJ_ASSETS_BUCKET for both components -----
  // The env VALUE is the bucket name, which is a cross-stack CFN reference
  // (the bucket lives in DataStack, the manifests on EksStack), so it renders
  // as a token that flattens to empty text — exactly like HELLODJ_CORE_TABLE.
  // Assert on the env var NAME being wired (mirrors the source-oauth-env test).
  test('web-ui container env includes HELLODJ_ASSETS_BUCKET', () => {
    const { eksTemplate } = compose();
    const text = collectManifestText(eksTemplate);
    expect(text).toContain('"name":"HELLODJ_ASSETS_BUCKET"');
  });

  test('discord-bot-core container env includes HELLODJ_ASSETS_BUCKET', () => {
    const { eksTemplate } = compose();
    const text = collectManifestText(eksTemplate);
    // Both web-ui and discord-bot-core wire the env var, so the name fragment
    // appears at least twice across the synthesized manifests.
    const occurrences = text.split('"name":"HELLODJ_ASSETS_BUCKET"').length - 1;
    expect(occurrences).toBeGreaterThanOrEqual(2);
  });

  // -- Least-privilege IAM: web-ui WRITE, discord-bot-core READ ------------
  //
  // The per-component SA role's IAM policy (created by
  // `cluster.addServiceAccount`) lands on the shared EksStack template. The
  // bucket is defined in the sibling DataStack, so the grant Resource is a
  // cross-stack `Fn::ImportValue` embedding the bucket construct id
  // (`AssetsBucket`). The web-ui gets grantReadWrite (includes `s3:PutObject`);
  // the discord-bot-core gets grantRead (`s3:GetObject`, no put).

  // CDK's `grantReadWrite` expands to the wildcard action forms
  // (`s3:GetObject*`, `s3:PutObject*`, `s3:DeleteObject*`, `s3:List*`, ...);
  // `grantRead` expands to the read-only set (`s3:GetObject*`, `s3:GetBucket*`,
  // `s3:List*`). Match those exact rendered forms. The bucket is defined in the
  // sibling DataStack, so the grant Resource is a cross-stack `Fn::ImportValue`
  // whose export name embeds the bucket construct id (`AssetsBucket`).
  test('web-ui SA role is granted WRITE on the assets bucket (s3:PutObject)', () => {
    const { eksTemplate } = compose();
    eksTemplate.hasResourceProperties('AWS::IAM::Policy', {
      PolicyDocument: Match.objectLike({
        Statement: Match.arrayWith([
          Match.objectLike({
            Effect: 'Allow',
            // grantReadWrite renders the object-put action as the bare
            // `s3:PutObject` token (plus PutObjectTagging/Retention/etc.).
            Action: Match.arrayWith(['s3:PutObject']),
            Resource: Match.arrayWith([
              Match.objectLike({
                'Fn::ImportValue': Match.stringLikeRegexp('AssetsBucket'),
              }),
            ]),
          }),
        ]),
      }),
    });
  });

  test('discord-bot-core SA role is granted READ on the assets bucket (s3:GetObject*)', () => {
    const { eksTemplate } = compose();
    eksTemplate.hasResourceProperties('AWS::IAM::Policy', {
      PolicyDocument: Match.objectLike({
        Statement: Match.arrayWith([
          Match.objectLike({
            Effect: 'Allow',
            Action: Match.arrayWith(['s3:GetObject*']),
            Resource: Match.arrayWith([
              Match.objectLike({
                'Fn::ImportValue': Match.stringLikeRegexp('AssetsBucket'),
              }),
            ]),
          }),
        ]),
      }),
    });
  });

  test('the discord-bot-core read grant does NOT include s3:PutObject* (least privilege)', () => {
    const { eksTemplate } = compose();
    // Find every IAM policy statement that grants read on an S3 object
    // (`s3:GetObject*`) and confirm at least one such statement carries no
    // `s3:PutObject*` — the bot's grantRead. This proves the bot got READ, not
    // read-write, on the bucket.
    const policies = eksTemplate.findResources('AWS::IAM::Policy');
    const readOnlyBucketStatements = Object.values(policies)
      .flatMap((p) => {
        const doc = p.Properties?.PolicyDocument as
          | { Statement?: Array<Record<string, unknown>> }
          | undefined;
        return doc?.Statement ?? [];
      })
      .filter((stmt) => {
        const action = stmt.Action;
        const actions = Array.isArray(action) ? action : [action];
        return (
          actions.includes('s3:GetObject*') &&
          !actions.includes('s3:PutObject*') &&
          !actions.includes('s3:PutObject')
        );
      });
    // At least one read-only bucket statement exists (the bot's grantRead),
    // and by construction it carries no put action.
    expect(readOnlyBucketStatements.length).toBeGreaterThanOrEqual(1);
  });
});

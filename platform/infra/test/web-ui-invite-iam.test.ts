import * as cdk from 'aws-cdk-lib';
import { Template, Match } from 'aws-cdk-lib/assertions';
import {
  WorkloadsStack,
  WorkloadsStackProps,
  stageInviteSender,
  stageHostname,
  DEFAULT_INVITE_TOKEN_TTL_SECONDS,
} from '../lib/workloads-stack';
import { EksStack } from '../lib/eks-stack';
import { NetworkStack } from '../lib/network-stack';
import { DataStack } from '../lib/data-stack';
import { AuthStack } from '../lib/auth-stack';
import { DeploymentStage } from '../lib/config';

/**
 * Task 15 — CDK tests for the tokenized-invite IAM + env wiring on the web-ui
 * (Requirements 7.1, 7.4).
 *
 * Task 14 provisions, on the WorkloadsStack, a stage-scoped SES sender identity
 * `invites@<stage>.<region>.hellodj.bot` (`ses.EmailIdentity`) and grants the
 * web-ui IRSA role:
 *
 *   * `ses:SendEmail` + `ses:SendRawEmail` scoped to THAT identity's ARN only
 *     (least privilege — the web-ui can never send from another identity), and
 *   * `cognito-idp:AdminSetUserPassword` added to the existing Cognito admin
 *     action list (so `register()` creates a CONFIRMED account and Cognito
 *     sends no temp-password email — the branded SES invite is the only mail).
 *
 * It also injects `INVITE_SENDER`, `INVITE_TOKEN_TTL`, and `PUBLIC_BASE_URL`
 * into the web-ui container env.
 *
 * These grants are gated on `cognitoUserPoolId` being supplied, so the compose
 * below wires it (mirroring `bin/hellodj.ts` for the Beta stage). The web-ui
 * ServiceAccount role + its policy are created by `cluster.addServiceAccount`,
 * so the `AWS::IAM::Policy` resource lands on the shared cluster's `EksStack`
 * template; the SES identity and the Kubernetes manifests (container env) land
 * on the WorkloadsStack template.
 */
describe('web-ui tokenized-invite IAM + env wiring (task 15, R7.1, R7.4)', () => {
  const TEST_REGION = 'us-east-1';
  const TEST_ACCOUNT = '111111111111';
  const COMPOSE_ENV = { account: TEST_ACCOUNT, region: TEST_REGION };
  const STAGE = DeploymentStage.Beta;
  const USER_POOL_ID = 'us-east-1_TestPool0';

  interface Composed {
    eksTemplate: Template;
    workloadsTemplate: Template;
  }

  /**
   * Compose the shared foundation once and attach a single Beta WorkloadsStack
   * with `cognitoUserPoolId` set (so the Cognito + SES web-ui grants fire).
   * Returns the EksStack template (where the IRSA role's IAM policy lands) and
   * the WorkloadsStack template (SES identity + Kubernetes manifests).
   */
  function compose(): Composed {
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
      // Gate: the Cognito + SES web-ui grants only fire when the user pool id
      // is supplied (otherwise the admin panel runs in degraded mode).
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
      eksTemplate: Template.fromStack(eks),
      workloadsTemplate: Template.fromStack(workloads),
    };
  }

  /** The expected stage sender identity `invites@<stage>.<region>.hellodj.bot`. */
  const expectedSender = stageInviteSender(STAGE, TEST_REGION);
  /** The expected stage hostname `<stage>.<region>.hellodj.bot`. */
  const expectedHostname = stageHostname(STAGE, TEST_REGION);

  /**
   * Concatenate the literal string fragments of a manifest value into one
   * searchable string. `cluster.addManifest` serializes each manifest as an
   * `Fn::Join` mixing literal JSON fragments with CFN tokens (table names,
   * secret ARNs). Env var names and their literal values (INVITE_SENDER,
   * PUBLIC_BASE_URL, INVITE_TOKEN_TTL) are literals, so flattening the literal
   * fragments lets us assert on the container env without resolving tokens.
   * (Mirrors the `flattenManifest` helper in `beta-smoke.test.ts`.)
   */
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
      // Tokens like Fn::GetAtt / Ref contribute no literal text.
      return '';
    }
    return '';
  }

  /**
   * The flattened literal text of every Kubernetes manifest on a synthesized
   * template (the `Custom::AWSCDK-EKS-KubernetesResource` resources
   * `cluster.addManifest` produces). The web-ui Deployment manifest carries
   * token-bearing fields (secret ARNs / table names) so it renders as a single
   * `Fn::Join`, not a plain JSON string — flattening yields its static content.
   */
  function collectManifestText(template: Template): string {
    const resources = template.findResources(
      'Custom::AWSCDK-EKS-KubernetesResource',
    );
    return Object.values(resources)
      .map((r) => flattenManifest(r.Properties?.Manifest))
      .join('\n');
  }

  // -- R7.1: SES send scoped to the sender identity ARN --------------------
  test('web-ui role grants ses:SendEmail/SendRawEmail scoped to the sender identity (R7.1)', () => {
    const { eksTemplate } = compose();
    eksTemplate.hasResourceProperties('AWS::IAM::Policy', {
      PolicyDocument: Match.objectLike({
        Statement: Match.arrayWith([
          Match.objectLike({
            Effect: 'Allow',
            Action: ['ses:SendEmail', 'ses:SendRawEmail'],
            // Scoped to the stage's verified sender DOMAIN identity ARN
            // (`identity/<stage>.<region>.hellodj.bot`), with the actual From
            // pinned to `invites@<domain>` via an ses:FromAddress condition.
            Resource: `arn:aws:ses:${TEST_REGION}:${TEST_ACCOUNT}:identity/${expectedHostname}`,
            Condition: {
              'ForAllValues:StringEquals': {
                'ses:FromAddress': expectedSender,
              },
            },
          }),
        ]),
      }),
    });
  });

  test('the SES send grant resource matches the domain identity ARN suffix (R7.1)', () => {
    const { eksTemplate } = compose();
    eksTemplate.hasResourceProperties('AWS::IAM::Policy', {
      PolicyDocument: Match.objectLike({
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: ['ses:SendEmail', 'ses:SendRawEmail'],
            Resource: Match.stringLikeRegexp(
              `identity/${expectedHostname.replace(/\./g, '\\.')}$`,
            ),
          }),
        ]),
      }),
    });
  });

  // -- R7.4: AdminSetUserPassword on the Cognito admin statement -----------
  test('web-ui Cognito admin statement includes AdminSetUserPassword alongside AdminCreateUser (R7.4)', () => {
    const { eksTemplate } = compose();
    eksTemplate.hasResourceProperties('AWS::IAM::Policy', {
      PolicyDocument: Match.objectLike({
        Statement: Match.arrayWith([
          Match.objectLike({
            Effect: 'Allow',
            Action: Match.arrayWith([
              'cognito-idp:AdminCreateUser',
              'cognito-idp:AdminSetUserPassword',
            ]),
          }),
        ]),
      }),
    });
  });

  // -- Task 14 SES identity provisioning -----------------------------------
  test('a stage SES domain EmailIdentity is provisioned for the sender domain (task 14)', () => {
    const { workloadsTemplate } = compose();
    // A DOMAIN identity for `<stage>.<region>.hellodj.bot` (verifies any
    // mailbox on the domain, including `invites@`), not the bare email — an
    // email identity sits Pending until a human clicks a mailed link.
    workloadsTemplate.resourceCountIs('AWS::SES::EmailIdentity', 1);
    workloadsTemplate.hasResourceProperties('AWS::SES::EmailIdentity', {
      EmailIdentity: expectedHostname,
    });
  });

  test('Easy-DKIM CNAME records self-verify the domain identity (task 14)', () => {
    const { workloadsTemplate } = compose();
    // Easy DKIM issues three CNAME tokens; publishing them into the delegated
    // hellodj.bot zone lets SES self-verify with no manual confirmation.
    workloadsTemplate.resourceCountIs('AWS::Route53::RecordSet', 3);
    workloadsTemplate.hasResourceProperties('AWS::Route53::RecordSet', {
      Type: 'CNAME',
    });
  });

  // -- R7.4 / R1.1 / R1.3: new web-ui env vars are wired -------------------
  // The container env is serialized inside the Deployment manifest JSON; each
  // var appears as a `{"name":"X","value":"Y"}` fragment. The values below are
  // literals, so they survive flattening even though the same manifest carries
  // token-bearing secret ARNs.
  test('web-ui container env wires INVITE_SENDER to the stage sender identity (R7.4)', () => {
    // The Kubernetes manifests are attached to the shared cluster, so they
    // land on the `EksStack` template, not the WorkloadsStack template.
    const { eksTemplate } = compose();
    const text = collectManifestText(eksTemplate);
    expect(text).toContain(
      `{"name":"INVITE_SENDER","value":"${expectedSender}"}`,
    );
  });

  test('web-ui container env wires PUBLIC_BASE_URL to the stage https origin', () => {
    const { eksTemplate } = compose();
    const text = collectManifestText(eksTemplate);
    expect(text).toContain(
      `{"name":"PUBLIC_BASE_URL","value":"https://${expectedHostname}"}`,
    );
  });

  test('web-ui container env wires INVITE_TOKEN_TTL to the default 7-day TTL (R1.3)', () => {
    const { eksTemplate } = compose();
    const text = collectManifestText(eksTemplate);
    expect(DEFAULT_INVITE_TOKEN_TTL_SECONDS).toBe(604800);
    expect(text).toContain(
      `{"name":"INVITE_TOKEN_TTL","value":"${DEFAULT_INVITE_TOKEN_TTL_SECONDS}"}`,
    );
  });
});

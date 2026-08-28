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
 * Task 1 → Task 3.2 (bot-identity-and-source-auth) — per-guild source OAuth
 * env + IAM wiring on the web-ui (root cause 1a fix).
 *
 * Task 1 wrote the FIRST block below as a bug-condition EXPLORATION test that
 * asserted the EXPECTED POST-FIX state (the client-id env vars PRESENT); it was
 * deliberately expected to FAIL on the unfixed workloads-stack. Task 3.1 wired
 * the fix (`SPOTIFY_CLIENT_ID` / `GOOGLE_CLIENT_ID` / `TIDAL_CLIENT_ID` plain
 * env; `GOOGLE_CLIENT_SECRET` / `DISCORD_CLIENT_SECRET` via `secretKeyRef` to
 * the per-stage `web-ui-oauth-secret`; `grantRead` on the new google-oauth +
 * discord-oauth secrets for the web-ui IRSA role), so those exploration
 * assertions now PASS (their fixed counterpart).
 *
 * Task 3.2 EXTENDS this with the remaining fix assertions:
 *   - the client *secrets* are wired via `secretKeyRef` to `web-ui-oauth-secret`
 *     (never as a plain CloudFormation env literal — mirrors FLASK_SECRET_KEY);
 *   - the web-ui IRSA role is granted READ on the new google-oauth and
 *     discord-oauth Secrets Manager secrets.
 *
 * Mirrors `web-ui-invite-iam.test.ts`: the Kubernetes manifests are attached to
 * the shared cluster (`cluster.addManifest`), so they land on the `EksStack`
 * template (flatten the literal `Fn::Join` fragments to search the container
 * env); the web-ui ServiceAccount role's `AWS::IAM::Policy` (where `grantRead`
 * statements land) is created by `cluster.addServiceAccount`, so it ALSO lands
 * on the shared `EksStack` template.
 *
 * The compose threads the OAuth client-id/secret props and the google-oauth /
 * discord-oauth secret handles so both the env wiring and the least-privilege
 * grants fire (Task 3.1 gates the grants on those optional secret handles being
 * supplied, mirroring `bin/hellodj.ts`).
 *
 * Validates: Requirements 1.1, 1.2, 1.3 (root cause 1a) and 2.6 (Task 3.2)
 */
describe('web-ui source OAuth env + IAM wiring (task 3.2 fix, root cause 1a)', () => {
  const TEST_REGION = 'us-east-1';
  const TEST_ACCOUNT = '111111111111';
  const COMPOSE_ENV = { account: TEST_ACCOUNT, region: TEST_REGION };
  const STAGE = DeploymentStage.Beta;
  const USER_POOL_ID = 'us-east-1_TestPool0';

  /** Compose the shared foundation + a Beta WorkloadsStack (mirrors the IAM test). */
  function composeEksTemplate(): Template {
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
        // Task 3.1 threads the new source-OAuth client-credential secrets; the
        // web-ui `grantRead` on google-oauth + discord-oauth is gated on these
        // optional handles being supplied (mirrors `bin/hellodj.ts`).
        googleOauth: auth.googleOauthSecret,
        discordOauth: auth.discordOauthSecret,
      },
      aiTaskRole: auth.aiTaskRole,
      cognitoUserPoolId: USER_POOL_ID,
      // Plain client-id env values (Task 3.1) — non-sensitive, mirror
      // `discordClientId`. Threaded so the env assertions exercise real values.
      spotifyClientId: 'spotify-client-id-test',
      googleClientId: 'google-client-id-test',
      tidalClientId: 'tidal-client-id-test',
      // Client *secrets* land in the `web-ui-oauth-secret` k8s Secret and are
      // referenced via secretKeyRef (never a CFN env literal).
      googleClientSecret: 'google-client-secret-test',
      discordClientSecret: 'discord-client-secret-test',
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

  // -- FIXED (Task 3.1): the source client-id env vars are wired ------------
  //
  // Task 1 wrote these expecting them to FAIL on the unfixed stack (the names
  // never appeared in the web-ui container env). Task 3.1 pushes each client id
  // unconditionally (empty-string default) into the web-ui `containerEnv`, so
  // the names now appear as `{"name":"...CLIENT_ID","value":"..."}` fragments
  // and the assertions PASS (root cause 1a fixed).

  test('web-ui container env includes SPOTIFY_CLIENT_ID (1.1, 1.2)', () => {
    const text = collectManifestText(composeEksTemplate());
    expect(text).toContain('"name":"SPOTIFY_CLIENT_ID"');
  });

  test('web-ui container env includes GOOGLE_CLIENT_ID (1.3)', () => {
    const text = collectManifestText(composeEksTemplate());
    expect(text).toContain('"name":"GOOGLE_CLIENT_ID"');
  });

  test('web-ui container env includes TIDAL_CLIENT_ID', () => {
    const text = collectManifestText(composeEksTemplate());
    expect(text).toContain('"name":"TIDAL_CLIENT_ID"');
  });

  // -- Task 3.2: client-id env values carry the configured props ------------
  //
  // The client *ids* are non-sensitive plain env values, so their configured
  // values appear literally in the manifest (they survive `flattenManifest`).
  // This confirms the props threaded in `bin/hellodj.ts` actually reach the
  // web-ui container env (not just an empty-string default).

  test('web-ui SPOTIFY_CLIENT_ID env carries the configured value (2.6)', () => {
    const text = collectManifestText(composeEksTemplate());
    expect(text).toContain(
      '{"name":"SPOTIFY_CLIENT_ID","value":"spotify-client-id-test"}',
    );
  });

  test('web-ui GOOGLE_CLIENT_ID env carries the configured value (2.6)', () => {
    const text = collectManifestText(composeEksTemplate());
    expect(text).toContain(
      '{"name":"GOOGLE_CLIENT_ID","value":"google-client-id-test"}',
    );
  });

  test('web-ui TIDAL_CLIENT_ID env carries the configured value (2.6)', () => {
    const text = collectManifestText(composeEksTemplate());
    expect(text).toContain(
      '{"name":"TIDAL_CLIENT_ID","value":"tidal-client-id-test"}',
    );
  });

  // -- Task 3.2: client *secrets* wired via secretKeyRef, not env literals --
  //
  // `GOOGLE_CLIENT_SECRET` / `DISCORD_CLIENT_SECRET` are injected via a
  // `secretKeyRef` into the per-stage `web-ui-oauth-secret` Kubernetes Secret
  // (mirrors FLASK_SECRET_KEY), so no secret value lands in a CloudFormation
  // env literal on the Deployment manifest. The env entry is a
  // `{"name":"...","valueFrom":{"secretKeyRef":{"name":"web-ui-oauth-secret",
  // "key":"..."}}}` fragment — all literal, so it survives flattening.

  test('web-ui GOOGLE_CLIENT_SECRET is wired via secretKeyRef to web-ui-oauth-secret (2.6)', () => {
    const text = collectManifestText(composeEksTemplate());
    expect(text).toContain(
      '{"name":"GOOGLE_CLIENT_SECRET","valueFrom":' +
        '{"secretKeyRef":{"name":"web-ui-oauth-secret","key":"GOOGLE_CLIENT_SECRET"}}}',
    );
  });

  test('web-ui DISCORD_CLIENT_SECRET is wired via secretKeyRef to web-ui-oauth-secret (2.6)', () => {
    const text = collectManifestText(composeEksTemplate());
    expect(text).toContain(
      '{"name":"DISCORD_CLIENT_SECRET","valueFrom":' +
        '{"secretKeyRef":{"name":"web-ui-oauth-secret","key":"DISCORD_CLIENT_SECRET"}}}',
    );
  });

  test('the client secrets never appear as plain env values (kept out of CFN literals) (2.6)', () => {
    const text = collectManifestText(composeEksTemplate());
    // The literal secret values must NOT appear as a plain `"value":"..."`
    // env entry anywhere in the manifest — they are only reachable via the
    // secretKeyRef into the web-ui-oauth-secret k8s Secret.
    expect(text).not.toContain('"value":"google-client-secret-test"');
    expect(text).not.toContain('"value":"discord-client-secret-test"');
  });

  // -- Task 3.2: web-ui IRSA role granted READ on the new OAuth secrets ------
  //
  // The web-ui ServiceAccount role's IAM policy (created by
  // `cluster.addServiceAccount`) lands on the shared EksStack template. A
  // `grantRead` on a secret owned by the sibling AuthStack emits a statement
  // allowing the read actions (`secretsmanager:GetSecretValue` +
  // `secretsmanager:DescribeSecret`) scoped to the secret's ARN. Because the
  // secret is defined in another stack, CDK exports its ARN from `hellodj-auth`
  // and the grant `Resource` is a single cross-stack `Fn::ImportValue` object
  // (NOT an array, NOT a same-stack `Ref`) whose export name embeds the
  // secret's construct id (`GoogleOauthSecret` / `DiscordOauthSecret`). Match on
  // the read action + that import-value shape rather than a literal ARN string.

  test('web-ui role is granted read on the google-oauth secret (2.6)', () => {
    const template = composeEksTemplate();
    template.hasResourceProperties('AWS::IAM::Policy', {
      PolicyDocument: Match.objectLike({
        Statement: Match.arrayWith([
          Match.objectLike({
            Effect: 'Allow',
            Action: Match.arrayWith(['secretsmanager:GetSecretValue']),
            Resource: {
              'Fn::ImportValue': Match.stringLikeRegexp('GoogleOauthSecret'),
            },
          }),
        ]),
      }),
    });
  });

  test('web-ui role is granted read on the discord-oauth secret (2.6)', () => {
    const template = composeEksTemplate();
    template.hasResourceProperties('AWS::IAM::Policy', {
      PolicyDocument: Match.objectLike({
        Statement: Match.arrayWith([
          Match.objectLike({
            Effect: 'Allow',
            Action: Match.arrayWith(['secretsmanager:GetSecretValue']),
            Resource: {
              'Fn::ImportValue': Match.stringLikeRegexp('DiscordOauthSecret'),
            },
          }),
        ]),
      }),
    });
  });
});

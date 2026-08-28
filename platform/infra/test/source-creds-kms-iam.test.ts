import * as cdk from 'aws-cdk-lib';
import { Template, Match } from 'aws-cdk-lib/assertions';
import {
  WorkloadsStack,
  WorkloadsStackProps,
  SOURCE_CREDENTIAL_KMS_COMPONENTS,
  SOURCE_CREDENTIAL_READERS,
  SOURCE_CREDENTIAL_WRITER,
  SOURCE_CREDENTIAL_WATCHDOG,
} from '../lib/workloads-stack';
import { EksStack } from '../lib/eks-stack';
import { NetworkStack } from '../lib/network-stack';
import { DataStack } from '../lib/data-stack';
import { AuthStack } from '../lib/auth-stack';
import { COMPONENT_WORKLOADS } from '../lib/component-workloads';
import { DeploymentStage } from '../lib/config';

/**
 * unified-oauth-and-token-watchdog Task 10 — source-credentials CMK grants +
 * env wiring on the workloads (R3.5, R9.1-R9.4).
 *
 * The source-credential token blobs on `hellodj-core` are envelope-encrypted
 * with a dedicated CMK (`DataStack.sourceCredsKey`). This asserts the
 * least-privilege matrix documented by {@link SOURCE_CREDENTIAL_KMS_COMPONENTS}
 * is realized in the synthesized IAM policies + container env:
 *   - web-ui + playback-orchestrator hold the full envelope path (Encrypt +
 *     Decrypt + GenerateDataKey); readers hold Decrypt only.
 *   - Only components in the documented set carry a CMK grant / the key-id env
 *     (Correctness Property 9 — least privilege, R9.4).
 *
 * Like `web-ui-source-oauth-env.test.ts`, the Kubernetes manifests + the
 * per-component ServiceAccount IAM policies land on the shared `EksStack`
 * template (`cluster.addManifest` / `cluster.addServiceAccount`).
 *
 * Validates: Requirements 3.1, 3.5, 9.1, 9.2, 9.3, 9.4
 */
describe('source-credentials CMK grants + env (task 10, R9.x)', () => {
  const TEST_REGION = 'us-east-1';
  const TEST_ACCOUNT = '111111111111';
  const COMPOSE_ENV = { account: TEST_ACCOUNT, region: TEST_REGION };
  const STAGE = DeploymentStage.Beta;

  function composeEksTemplate(): Template {
    const app = new cdk.App();
    const network = new NetworkStack(app, 'hellodj-network', {
      env: COMPOSE_ENV,
    });
    const data = new DataStack(app, 'hellodj-data', {
      env: COMPOSE_ENV,
      vpc: network.vpc,
      stage: STAGE,
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
        // The CMK under test — threaded so the grants + env fire.
        sourceCredsKey: data.sourceCredsKey,
      },
      secrets: {
        discordBotToken: auth.discordBotTokenSecret,
        tidalRefresh: auth.tidalRefreshSecret,
        spotify: auth.spotifySecret,
        ytCipher: auth.ytCipherSecret,
        googleOauth: auth.googleOauthSecret,
        discordOauth: auth.discordOauthSecret,
      },
      aiTaskRole: auth.aiTaskRole,
    };
    const workloads = new WorkloadsStack(app, `hellodj-workloads-${STAGE}`, props);
    workloads.addStackDependency(eks);
    workloads.addStackDependency(data);
    workloads.addStackDependency(auth);
    return Template.fromStack(eks);
  }

  /** Flatten a manifest value's literal `Fn::Join` fragments into one string. */
  function flattenManifest(value: unknown): string {
    if (typeof value === 'string') return value;
    if (Array.isArray(value)) return value.map(flattenManifest).join('');
    if (value && typeof value === 'object') {
      const obj = value as Record<string, unknown>;
      if (obj['Fn::Join']) {
        const [sep, parts] = obj['Fn::Join'] as [string, unknown[]];
        return parts.map(flattenManifest).join(sep);
      }
      return '';
    }
    return '';
  }

  function collectManifestText(template: Template): string {
    const resources = template.findResources(
      'Custom::AWSCDK-EKS-KubernetesResource',
    );
    return Object.values(resources)
      .map((r) => flattenManifest(r.Properties?.Manifest))
      .join('\n');
  }

  // -- Documented least-privilege matrix (R9.4) -----------------------------

  test('the documented KMS component set is exactly writer + watchdog + readers', () => {
    expect([...SOURCE_CREDENTIAL_KMS_COMPONENTS].sort()).toEqual(
      [
        SOURCE_CREDENTIAL_WRITER,
        SOURCE_CREDENTIAL_WATCHDOG,
        ...SOURCE_CREDENTIAL_READERS,
      ].sort(),
    );
  });

  test('lavalink is NOT in the KMS decrypt component set (Property 9)', () => {
    // lavalink is a per-guild source reader for the LEGACY secret path but it
    // never decrypts a token blob itself (config is pre-rendered), so it must
    // not hold the CMK grant.
    expect(SOURCE_CREDENTIAL_KMS_COMPONENTS.has('lavalink')).toBe(false);
  });

  // -- Grants (R9.1, R9.2, R9.3) --------------------------------------------

  test('writer + watchdog roles are granted kms:GenerateDataKey (write path, R9.1/R9.2)', () => {
    const template = composeEksTemplate();
    // grantEncryptDecrypt emits GenerateDataKey* among the allowed actions.
    template.hasResourceProperties('AWS::IAM::Policy', {
      PolicyDocument: Match.objectLike({
        Statement: Match.arrayWith([
          Match.objectLike({
            Effect: 'Allow',
            Action: Match.arrayWith(['kms:GenerateDataKey*']),
          }),
        ]),
      }),
    });
  });

  test('a decrypt grant (kms:Decrypt) is present for the CMK readers (R9.3)', () => {
    const template = composeEksTemplate();
    template.hasResourceProperties('AWS::IAM::Policy', {
      PolicyDocument: Match.objectLike({
        Statement: Match.arrayWith([
          Match.objectLike({
            Effect: 'Allow',
            Action: Match.arrayWith(['kms:Decrypt']),
          }),
        ]),
      }),
    });
  });

  test('exactly the documented component count carries kms:Decrypt (Property 9, R9.4)', () => {
    const template = composeEksTemplate();
    // Count IAM policy statements that allow kms:Decrypt. Each granted
    // component's ServiceAccount role gets its own AWS::IAM::Policy; the number
    // of policies bearing a Decrypt statement must equal the documented set.
    const policies = template.findResources('AWS::IAM::Policy');
    let decryptStatementRoles = 0;
    for (const policy of Object.values(policies)) {
      const statements =
        (policy.Properties?.PolicyDocument?.Statement as unknown[]) ?? [];
      const hasDecrypt = statements.some((s) => {
        const action = (s as { Action?: unknown }).Action;
        const actions = Array.isArray(action) ? action : [action];
        return actions.includes('kms:Decrypt');
      });
      if (hasDecrypt) decryptStatementRoles += 1;
    }
    expect(decryptStatementRoles).toBe(SOURCE_CREDENTIAL_KMS_COMPONENTS.size);
  });

  // -- Env wiring (R3.5) ----------------------------------------------------

  test('HELLODJ_SOURCE_CREDS_KMS_KEY_ID env is present for granted components', () => {
    const text = collectManifestText(composeEksTemplate());
    expect(text).toContain('"name":"HELLODJ_SOURCE_CREDS_KMS_KEY_ID"');
  });

  test('the KMS key-id env appears exactly once per documented component', () => {
    const text = collectManifestText(composeEksTemplate());
    const matches = text.match(/"name":"HELLODJ_SOURCE_CREDS_KMS_KEY_ID"/g) ?? [];
    expect(matches.length).toBe(SOURCE_CREDENTIAL_KMS_COMPONENTS.size);
  });

  test('watchdog carries TOKEN_WATCHDOG_INTERVAL + threshold env (R5.2)', () => {
    const text = collectManifestText(composeEksTemplate());
    expect(text).toContain('"name":"TOKEN_WATCHDOG_INTERVAL"');
    expect(text).toContain('"name":"TOKEN_WATCHDOG_THRESHOLD"');
  });

  test('web-ui carries HELLODJ_GOOGLE_OAUTH_SECRET_ARN + POTOKEN_SERVER_URL env (task 10)', () => {
    const text = collectManifestText(composeEksTemplate());
    expect(text).toContain('"name":"HELLODJ_GOOGLE_OAUTH_SECRET_ARN"');
    expect(text).toContain('"name":"POTOKEN_SERVER_URL"');
  });

  // -- Discord OAuth wiring intact (verify, do not duplicate) ---------------

  test('web-ui Discord OAuth wiring is intact (DISCORD_CLIENT_ID + secret ARN)', () => {
    const app = new cdk.App();
    const network = new NetworkStack(app, 'hellodj-network', { env: COMPOSE_ENV });
    const data = new DataStack(app, 'hellodj-data', {
      env: COMPOSE_ENV,
      vpc: network.vpc,
      stage: STAGE,
    });
    const auth = new AuthStack(app, 'hellodj-auth', { env: COMPOSE_ENV, stage: STAGE });
    const eks = new EksStack(app, 'hellodj-eks', { env: COMPOSE_ENV, vpc: network.vpc });
    const workloads = new WorkloadsStack(app, `hellodj-workloads-${STAGE}`, {
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
        sourceCredsKey: data.sourceCredsKey,
      },
      secrets: {
        discordBotToken: auth.discordBotTokenSecret,
        tidalRefresh: auth.tidalRefreshSecret,
        spotify: auth.spotifySecret,
        ytCipher: auth.ytCipherSecret,
        googleOauth: auth.googleOauthSecret,
        discordOauth: auth.discordOauthSecret,
      },
      aiTaskRole: auth.aiTaskRole,
      // The Discord client id threaded exactly as bin/hellodj.ts does.
      discordClientId: 'discord-app-id-test',
    });
    workloads.addStackDependency(eks);
    const text = collectManifestText(Template.fromStack(eks));
    expect(text).toContain(
      '{"name":"DISCORD_CLIENT_ID","value":"discord-app-id-test"}',
    );
    expect(text).toContain('"name":"HELLODJ_DISCORD_OAUTH_SECRET_ARN"');
  });

  // -- Sanity: the component catalog still contains the granted components ---

  test('every documented KMS component exists in the workload catalog', () => {
    const names = new Set(COMPONENT_WORKLOADS.map((c) => c.name));
    for (const comp of SOURCE_CREDENTIAL_KMS_COMPONENTS) {
      expect(names.has(comp)).toBe(true);
    }
  });
});

import * as cdk from 'aws-cdk-lib';
import { Template, Match } from 'aws-cdk-lib/assertions';
import {
  AuthStack,
  COGNITO_INVITATION_SUBJECT,
  COGNITO_VERIFICATION_SUBJECT,
} from '../lib/auth-stack';
import { DeploymentStage } from '../lib/config';

/**
 * CDK assertion tests for the Cognito + Secrets Manager + IAM AuthStack
 * (task 10.3).
 *
 * These assert the synthesized CloudFormation template matches the design's
 * managed-auth intent:
 *   - A Cognito user pool authenticates admin / registration / recovery
 *     (R8.2), with an `admins` group and a web-ui client.
 *   - The four service secrets live in Secrets Manager (R8.6) — Discord bot
 *     token, Tidal refresh, Spotify, yt-cipher — not in the datastore.
 *   - A keyless IAM task role grants Bedrock/Transcribe/Polly access with NO
 *     static access keys.
 *
 * _Requirements: 8.2, 8.6._
 */

function synthAuthStack(): Template {
  const app = new cdk.App();
  const stack = new AuthStack(app, 'TestAuthStack', {
    env: { account: '123456789012', region: 'us-east-1' },
    stage: DeploymentStage.Beta,
  });
  return Template.fromStack(stack);
}

describe('AuthStack (Cognito + Secrets + IAM)', () => {
  const template = synthAuthStack();

  test('provisions a Cognito user pool (R8.2)', () => {
    template.resourceCountIs('AWS::Cognito::UserPool', 1);
    template.hasResourceProperties('AWS::Cognito::UserPool', {
      UserPoolName: 'hellodj-beta',
    });
  });

  test('provisions an `admins` user pool group', () => {
    template.resourceCountIs('AWS::Cognito::UserPoolGroup', 1);
    template.hasResourceProperties('AWS::Cognito::UserPoolGroup', {
      GroupName: 'admins',
    });
  });

  test('provisions a user pool client for the web-ui', () => {
    template.resourceCountIs('AWS::Cognito::UserPoolClient', 1);
    template.hasResourceProperties('AWS::Cognito::UserPoolClient', {
      ClientName: 'hellodj-web-ui-beta',
    });
  });

  test('enables USER_PASSWORD_AUTH for the first-party login form', () => {
    // The web-ui login form calls InitiateAuth(USER_PASSWORD_AUTH)
    // server-side; the app client must allow that flow (custom-auth-forms R6.1).
    template.hasResourceProperties('AWS::Cognito::UserPoolClient', {
      ExplicitAuthFlows: Match.arrayWith(['ALLOW_USER_PASSWORD_AUTH']),
    });
  });

  test('sends Cognito emails via SES from the verified stage domain', () => {
    // EmailSendingAccount DEVELOPER + a from address on the DKIM-verified
    // stage domain (invites@<stage>.<region>.hellodj.bot) — not COGNITO_DEFAULT.
    template.hasResourceProperties('AWS::Cognito::UserPool', {
      EmailConfiguration: Match.objectLike({
        EmailSendingAccount: 'DEVELOPER',
        From: Match.stringLikeRegexp('invites@beta\\.us-east-1\\.hellodj\\.bot'),
        SourceArn: Match.anyValue(),
      }),
    });
  });

  test('brands the Cognito verification email as HelloDJ HTML', () => {
    // Replaces the plain "The verification code to your new account is {####}"
    // default with an inline-styled HelloDJ dark-glass template.
    template.hasResourceProperties('AWS::Cognito::UserPool', {
      VerificationMessageTemplate: Match.objectLike({
        EmailSubject: COGNITO_VERIFICATION_SUBJECT,
        EmailMessage: Match.stringLikeRegexp('HelloDJ'),
        DefaultEmailOption: 'CONFIRM_WITH_CODE',
      }),
    });
  });

  test('brands the admin-invitation email as HelloDJ HTML (no plaintext default)', () => {
    // The reported bug: an unbranded "Your username is X and temporary
    // password is Y" email. Branding the invitation template guarantees any
    // account created outside the SES invite flow still gets themed HTML.
    template.hasResourceProperties('AWS::Cognito::UserPool', {
      AdminCreateUserConfig: Match.objectLike({
        InviteMessageTemplate: Match.objectLike({
          EmailSubject: COGNITO_INVITATION_SUBJECT,
          // Contains the brand shell and both Cognito placeholders.
          EmailMessage: Match.stringLikeRegexp('HelloDJ'),
        }),
      }),
    });
  });

  test('stores the service + session secrets in Secrets Manager (R8.6)', () => {
    // Four external service secrets, the two source-OAuth client-credential
    // secrets (Google/YouTube + Discord OAuth, used by the web-ui to complete
    // the per-guild YouTube exchange + Discord-login callback, R2.6), plus the
    // auto-generated web-ui Flask session signing key (shared across replicas
    // to prevent OAuth-callback session loss).
    template.resourceCountIs('AWS::SecretsManager::Secret', 7);

    for (const leaf of [
      'discord-bot-token',
      'tidal-refresh',
      'spotify',
      'yt-cipher-secret',
      'google-oauth',
      'discord-oauth',
      'web-ui-flask-session',
    ]) {
      template.hasResourceProperties('AWS::SecretsManager::Secret', {
        Name: `hellodj/beta/${leaf}`,
      });
    }
  });

  test('provisions a keyless IAM task role allowing bedrock:InvokeModel', () => {
    // At least one IAM role exists (the AI task role).
    const roles = template.findResources('AWS::IAM::Role');
    expect(Object.keys(roles).length).toBeGreaterThanOrEqual(1);

    // The role is named for the AI task and trusted by EKS pod identity.
    template.hasResourceProperties('AWS::IAM::Role', {
      RoleName: 'hellodj-ai-task-beta',
      AssumeRolePolicyDocument: Match.objectLike({
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: 'sts:AssumeRole',
            Principal: Match.objectLike({
              Service: 'pods.eks.amazonaws.com',
            }),
          }),
        ]),
      }),
    });

    // A policy attached to the role allows bedrock:InvokeModel.
    template.hasResourceProperties('AWS::IAM::Policy', {
      PolicyDocument: Match.objectLike({
        Statement: Match.arrayWith([
          Match.objectLike({
            Effect: 'Allow',
            Action: Match.arrayWith(['bedrock:InvokeModel']),
          }),
        ]),
      }),
    });
  });

  test('creates NO static IAM access keys (keyless role assumption)', () => {
    template.resourceCountIs('AWS::IAM::AccessKey', 0);
  });
});

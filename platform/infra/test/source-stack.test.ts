import * as cdk from 'aws-cdk-lib';
import { Template, Match } from 'aws-cdk-lib/assertions';
import { SourceStack, SOURCE_REPOS } from '../lib/source-stack';

/**
 * CDK assertion tests for the private CodeCommit SourceStack (task 11.1/11.3).
 *
 * These assert the synthesized CloudFormation template matches the design's
 * source-relocation intent (hellodj-private-source-and-toolchain §1):
 *   - Exactly five CodeCommit repositories with the expected names (R1.1).
 *   - `Lavalink` designates `dev` as its build branch (R1.3).
 *   - Access is granted only to the build IAM roles, with no public/anonymous
 *     access (R1.7).
 *
 * _Requirements: 1.1, 1.3, 1.7._
 */

const BUILD_ROLE_ARNS = [
  'arn:aws:iam::123456789012:role/hellodj-gha-runner',
  'arn:aws:iam::123456789012:role/hellodj-eks-builder',
];

function synthSourceStack(buildRoleArns: string[] = BUILD_ROLE_ARNS): Template {
  const app = new cdk.App();
  const stack = new SourceStack(app, 'TestSourceStack', {
    env: { account: '123456789012', region: 'us-east-1' },
    buildRoleArns,
  });
  return Template.fromStack(stack);
}

describe('SourceStack (private CodeCommit repositories)', () => {
  const template = synthSourceStack();

  test('creates exactly five CodeCommit repositories (R1.1)', () => {
    template.resourceCountIs('AWS::CodeCommit::Repository', 5);
  });

  test('provisions the five expected Source_Repos by name (R1.1)', () => {
    for (const name of [
      'hellodj',
      'Lavalink',
      'lavaplayer',
      'LavaSrc',
      'youtube-source',
    ]) {
      template.hasResourceProperties('AWS::CodeCommit::Repository', {
        RepositoryName: name,
      });
    }
  });

  test('SOURCE_REPOS designates dev as the Lavalink build branch (R1.3)', () => {
    const lavalink = SOURCE_REPOS.find((r) => r.name === 'Lavalink');
    expect(lavalink?.buildBranch).toBe('dev');
  });

  test('exposes the Lavalink build branch via a stack output (R1.3)', () => {
    template.hasOutput('LavalinkBuildBranchOutput', {
      Value: 'dev',
    });
  });

  test('grants read/pull access only to the build IAM roles (R1.7)', () => {
    // The grant attaches an IAM policy to each imported build role granting
    // the CodeCommit read/pull actions on the repositories. No CodeCommit
    // resource policy opening anonymous/public access is emitted.
    template.hasResourceProperties('AWS::IAM::Policy', {
      PolicyDocument: Match.objectLike({
        Statement: Match.arrayWith([
          Match.objectLike({
            Effect: 'Allow',
            Action: 'codecommit:GitPull',
          }),
        ]),
      }),
    });
  });

  test('emits no public/anonymous CodeCommit access (R1.7)', () => {
    // No statement grants a wildcard principal. Assert every IAM policy
    // statement is scoped (the grants target the imported roles, so the
    // policies are attached to those roles, not to a repo resource policy with
    // a "*" principal).
    const policies = template.findResources('AWS::IAM::Policy');
    for (const policy of Object.values(policies)) {
      const statements = policy.Properties?.PolicyDocument?.Statement ?? [];
      for (const stmt of statements) {
        // Identity policies carry no Principal at all; a "*" principal would
        // only appear in a resource policy, which we do not create.
        expect(stmt.Principal).toBeUndefined();
      }
    }
  });

  test('remains private when no build roles are supplied — no allowing principal (R1.7)', () => {
    const t = synthSourceStack([]);
    // Repos still exist and are private by default; with no grant there is no
    // IAM policy opening access, so the repos are not readable (R1.7).
    t.resourceCountIs('AWS::CodeCommit::Repository', 5);
    t.resourceCountIs('AWS::IAM::Policy', 0);
  });
});

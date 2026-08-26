import * as cdk from 'aws-cdk-lib';
import { Template, Match } from 'aws-cdk-lib/assertions';
import { EdgeStack } from '../lib/edge-stack';
import { DeploymentStage } from '../lib/config';

/**
 * CDK assertion tests for {@link EdgeStack} (task 9.6).
 *
 * Verifies the synthesized CloudFormation template contains the edge/DNS
 * resources the design's Route 53 naming model and edge-cache subgraph
 * require:
 *   * the `hellodj.bot` public hosted zone,
 *   * the per-environment `<stage>.<region>.hellodj.bot` alias record
 *     (Requirements 12.2, 12.4),
 *   * the production apex alias `hellodj.bot` (Requirement 12.3), present
 *     only for production,
 *   * an ACM certificate, and
 *   * a CloudFront distribution with a default behavior plus an `hls/*`
 *     additional behavior (Requirement 18.2).
 *
 * Route 53 record names in the synthesized template are fully-qualified
 * domain names ending in a trailing dot, so assertions match accordingly
 * (e.g. `beta.us-east-1.hellodj.bot.`).
 *
 * Validates: Requirements 12.2, 12.3, 12.4, 18.2
 */

const REGION = 'us-east-1';
// CloudFront + DNS-validated ACM require a concrete env at synth time.
const ENV = { account: '123456789012', region: REGION };

function synth(stage: DeploymentStage): Template {
  const app = new cdk.App();
  const stack = new EdgeStack(app, `Edge-${stage}`, {
    stage,
    region: REGION,
    env: ENV,
  });
  return Template.fromStack(stack);
}

describe('EdgeStack — non-prod (Beta, us-east-1)', () => {
  let template: Template;

  beforeAll(() => {
    template = synth(DeploymentStage.Beta);
  });

  test('uses the pre-existing delegated hellodj.bot zone (no new zone created)', () => {
    // The registrar-delegated `hellodj.bot` zone is looked up, not created, so
    // ACM DNS validation records land in the publicly-delegated zone. A second
    // PublicHostedZone for the same apex would be undelegated and would hang
    // certificate validation, so the stack must synthesize ZERO Route53 zones.
    template.resourceCountIs('AWS::Route53::HostedZone', 0);
  });

  test('creates the per-env <stage>.<region>.hellodj.bot alias record', () => {
    template.hasResourceProperties('AWS::Route53::RecordSet', {
      Name: 'beta.us-east-1.hellodj.bot.',
      Type: 'A',
    });
  });

  test('does NOT create an apex record for a non-prod stage', () => {
    // The bare-zone apex record only exists for prod (Requirement 12.3).
    const apexRecords = template.findResources('AWS::Route53::RecordSet', {
      Properties: { Name: 'hellodj.bot.' },
    });
    expect(Object.keys(apexRecords)).toHaveLength(0);
  });

  test('creates an ACM certificate', () => {
    template.resourceCountIs('AWS::CertificateManager::Certificate', 1);
  });

  test('creates a CloudFront distribution with default + hls/* behaviors', () => {
    // Default behavior must be present, and an additional cache behavior
    // must serve the `hls/*` path pattern (Requirement 18.2).
    template.hasResourceProperties('AWS::CloudFront::Distribution', {
      DistributionConfig: Match.objectLike({
        DefaultCacheBehavior: Match.objectLike({
          ViewerProtocolPolicy: Match.anyValue(),
        }),
        CacheBehaviors: Match.arrayWith([
          Match.objectLike({ PathPattern: 'hls/*' }),
        ]),
      }),
    });
  });
});

describe('EdgeStack — production (us-east-1)', () => {
  let template: Template;

  beforeAll(() => {
    template = synth(DeploymentStage.Production);
  });

  test('creates the per-env production.<region>.hellodj.bot alias record', () => {
    template.hasResourceProperties('AWS::Route53::RecordSet', {
      Name: 'production.us-east-1.hellodj.bot.',
      Type: 'A',
    });
  });

  test('creates the apex hellodj.bot alias record (Requirement 12.3)', () => {
    template.hasResourceProperties('AWS::Route53::RecordSet', {
      Name: 'hellodj.bot.',
      Type: 'A',
    });
  });

  test('has exactly two A records: the env record and the apex alias', () => {
    const records = template.findResources('AWS::Route53::RecordSet');
    const names = Object.values(records).map(
      (r: any) => r.Properties?.Name,
    );
    expect(names).toEqual(
      expect.arrayContaining([
        'production.us-east-1.hellodj.bot.',
        'hellodj.bot.',
      ]),
    );
    expect(names).toHaveLength(2);
  });
});

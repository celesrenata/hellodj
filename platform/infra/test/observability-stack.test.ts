import * as cdk from 'aws-cdk-lib';
import { Template, Match } from 'aws-cdk-lib/assertions';
import {
  ObservabilityStack,
  PLATFORM_METRICS_NAMESPACE,
} from '../lib/observability-stack';
import { DeploymentStage } from '../lib/config';

/**
 * CDK assertion tests for the CloudWatch ObservabilityStack (task 17.3).
 *
 * These assert the synthesized CloudFormation template matches the design's
 * Observability_Stack (CloudWatch half) intent:
 *   - A CloudWatch Logs group collects platform logs (R10.2).
 *   - A CloudWatch dashboard is provided for the platform (R10.4).
 *   - Threshold alarms (>=3) fan out to an SNS topic on breach (R10.5).
 *   - Prometheus / self-hosted metrics resources are excluded (R10.9).
 *
 * _Requirements: 10.4, 10.5, 10.9._
 */

/** Synthesize the ObservabilityStack for the Beta stage. */
function synthObservabilityStack(): Template {
  const app = new cdk.App();
  const stack = new ObservabilityStack(app, 'TestObservabilityStack', {
    env: { account: '123456789012', region: 'us-east-1' },
    stage: DeploymentStage.Beta,
  });
  return Template.fromStack(stack);
}

describe('ObservabilityStack (CloudWatch + SNS)', () => {
  const template = synthObservabilityStack();

  test('publishes the platform custom metrics namespace', () => {
    expect(PLATFORM_METRICS_NAMESPACE).toBe('HelloDJ/Platform');
  });

  test('collects platform logs in a CloudWatch Logs group (R10.2)', () => {
    template.resourceCountIs('AWS::Logs::LogGroup', 1);
    template.hasResourceProperties('AWS::Logs::LogGroup', {
      LogGroupName: '/hellodj/beta/platform',
    });
  });

  test('provides a CloudWatch dashboard for the platform (R10.4)', () => {
    template.resourceCountIs('AWS::CloudWatch::Dashboard', 1);
    template.hasResourceProperties('AWS::CloudWatch::Dashboard', {
      DashboardName: 'hellodj-beta-platform',
    });
  });

  test('raises at least three CloudWatch alarms (R10.5)', () => {
    const alarms = template.findResources('AWS::CloudWatch::Alarm');
    expect(Object.keys(alarms).length).toBeGreaterThanOrEqual(3);
  });

  test('provisions an SNS topic for alarm notifications (R10.5)', () => {
    template.resourceCountIs('AWS::SNS::Topic', 1);
    template.hasResourceProperties('AWS::SNS::Topic', {
      TopicName: 'hellodj-beta-alarms',
    });
  });

  test('every alarm notifies the SNS topic on breach (R10.5)', () => {
    const alarms = template.findResources('AWS::CloudWatch::Alarm');
    const alarmEntries = Object.values(alarms);
    expect(alarmEntries.length).toBeGreaterThanOrEqual(3);
    for (const alarm of alarmEntries) {
      // Each alarm must carry a non-empty AlarmActions list (the SNS action).
      const actions = alarm.Properties?.AlarmActions;
      expect(Array.isArray(actions)).toBe(true);
      expect(actions.length).toBeGreaterThanOrEqual(1);
    }
    // And at least one alarm's action references the provisioned SNS topic.
    template.hasResourceProperties('AWS::CloudWatch::Alarm', {
      AlarmActions: Match.arrayWith([
        Match.objectLike({ Ref: Match.stringLikeRegexp('AlarmTopic') }),
      ]),
    });
  });

  test('subscribes the owner email and SMS to the alarm topic (R10.5)', () => {
    template.hasResourceProperties('AWS::SNS::Subscription', {
      Protocol: 'email',
      Endpoint: 'celes+hellodj@celestium.life',
    });
    template.hasResourceProperties('AWS::SNS::Subscription', {
      Protocol: 'sms',
      Endpoint: '+14257853431',
    });
  });

  test('every alarm name starts with the HelloDJ: subject prefix (R10.5)', () => {
    const alarms = template.findResources('AWS::CloudWatch::Alarm');
    const names = Object.values(alarms).map(
      (a: any) => a.Properties?.AlarmName as string,
    );
    expect(names.length).toBeGreaterThanOrEqual(3);
    for (const name of names) {
      expect(typeof name).toBe('string');
      expect(name.startsWith('HelloDJ:')).toBe(true);
    }
  });

  test('excludes Prometheus / self-hosted metrics resources (R10.9)', () => {
    // No managed Prometheus workspace or rule groups (AWS::APS is the
    // Amazon Managed Service for Prometheus CFN namespace).
    template.resourceCountIs('AWS::APS::Workspace', 0);
    template.resourceCountIs('AWS::APS::RuleGroupsNamespace', 0);
    // No resource of any type whose name contains "Prometheus".
    const allResources = template.toJSON().Resources ?? {};
    const prometheusResources = Object.values(allResources).filter(
      (r: any) => typeof r.Type === 'string' && r.Type.includes('Prometheus'),
    );
    expect(prometheusResources).toHaveLength(0);
  });
});

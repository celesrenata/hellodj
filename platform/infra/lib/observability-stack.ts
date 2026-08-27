/**
 * CloudWatch observability stack for the HelloDJ AWS platform.
 *
 * Implements the CloudWatch half of the Observability_Stack (Requirement 10):
 *
 *   * R10.2 - The Log_Service collects platform logs through Amazon
 *             CloudWatch Logs (a shared `logs.LogGroup`).
 *   * R10.3 - The Metrics_Service publishes platform metrics to Amazon
 *             CloudWatch (metrics are referenced from the platform's custom
 *             `HelloDJ/Platform` namespace, e.g. the CPU/GPU transcode
 *             pressure signals published by `hls-transcode`).
 *   * R10.4 - The Metrics_Service provides a CloudWatch dashboard for the
 *             platform (`cloudwatch.Dashboard` with widgets over the platform
 *             metrics and the log group).
 *   * R10.5 - When a metric crosses a defined alarm threshold, the
 *             Metrics_Service raises a CloudWatch alarm and sends a
 *             notification: every alarm here fans out to an SNS topic via
 *             `alarm.addAlarmAction(new cloudwatchActions.SnsAction(topic))`.
 *   * R10.9 - The platform excludes Prometheus from the Observability_Stack;
 *             this stack provisions only native CloudWatch + SNS resources and
 *             creates no Prometheus / self-hosted metrics resources.
 *
 * The S3 Hive Log_Store + Glue + Athena + QuickSight analytics half of
 * Requirement 10 (R10.1, R10.6-R10.8) is provisioned by the companion
 * analytics stack (task 17.2); this stack owns CloudWatch Logs, metrics,
 * dashboards, alarms, and the SNS notification topic only.
 */
import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';
import * as cloudwatchActions from 'aws-cdk-lib/aws-cloudwatch-actions';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as sns from 'aws-cdk-lib/aws-sns';
import * as sqs from 'aws-cdk-lib/aws-sqs';
import * as subscriptions from 'aws-cdk-lib/aws-sns-subscriptions';

/**
 * Subject prefix applied to every HelloDJ alarm email so the Platform_Owner
 * can filter alarm notifications into a folder. CloudWatch's default
 * SNS-to-email path derives the email subject from the alarm name, so every
 * alarm here is named with this prefix (see {@link ObservabilityStack}). Kept
 * as a constant so tests can assert every alarm name carries it.
 */
export const ALARM_SUBJECT_PREFIX = 'HelloDJ:';

/** Default alarm-notification email (Platform_Owner). */
export const DEFAULT_ALARM_EMAIL = 'celes+hellodj@celestium.life';

/** Default alarm-notification SMS number (Platform_Owner, US E.164). */
export const DEFAULT_ALARM_SMS = '+14257853431';

/**
 * The custom CloudWatch metrics namespace the platform publishes into.
 *
 * `hls-transcode` publishes CPU/GPU transcode pressure to CloudWatch for the
 * Autoscaler (design: hls-transcode interfaces); the dashboard and alarms in
 * this stack read from the same namespace so IaC and runtime agree.
 */
export const PLATFORM_METRICS_NAMESPACE = 'HelloDJ/Platform';

/** Properties for {@link ObservabilityStack}. */
export interface ObservabilityStackProps extends cdk.StackProps {
  /**
   * The deployment stage (Beta/Staging/Production). Used to name the log group,
   * dashboard, and topic so the three stages get isolated observability
   * resources under one account.
   */
  readonly stage: string;

  /**
   * Retention for the shared platform log group. Defaults to two weeks, in
   * line with the cost model's minimal-retention CloudWatch Logs baseline
   * (design Cost Model, Observability_Stack row).
   */
  readonly logRetention?: logs.RetentionDays;

  /**
   * Email address alarm notifications are sent to. Defaults to
   * {@link DEFAULT_ALARM_EMAIL}. Every alarm name is prefixed with
   * {@link ALARM_SUBJECT_PREFIX} so the notification email subject starts with
   * `HelloDJ:` and can be filtered into a folder.
   */
  readonly alarmEmail?: string;

  /**
   * SMS number (E.164) alarm notifications are sent to. Defaults to
   * {@link DEFAULT_ALARM_SMS}.
   */
  readonly alarmSms?: string;

  /**
   * When true, alarm emails are routed through a Subject_Rewriter Lambda that
   * rewrites the subject to start with `HelloDJ:` and enriches the body with
   * alarm details before delivering via SES. When false (default), the
   * existing SNS-to-email subscription delivers directly. The SMS subscription
   * is always present regardless of this toggle. (R7.1, R7.4, R7.5)
   */
  readonly subjectRewriterEnabled?: boolean;
}

/**
 * Provisions the CloudWatch Logs, metrics dashboard, threshold alarms, and the
 * SNS notification topic those alarms publish to. No Prometheus / self-hosted
 * metrics resources are created (R10.9).
 */
export class ObservabilityStack extends cdk.Stack {
  /** Shared CloudWatch Logs group for platform logs (R10.2). */
  public readonly logGroup: logs.LogGroup;

  /** CloudWatch dashboard over platform metrics and logs (R10.4). */
  public readonly dashboard: cloudwatch.Dashboard;

  /** SNS topic that every alarm notifies on threshold breach (R10.5). */
  public readonly alarmTopic: sns.Topic;

  /** The threshold alarms wired to {@link alarmTopic}. */
  public readonly alarms: cloudwatch.Alarm[];

  /**
   * The Subject_Rewriter Lambda, created only when
   * {@link ObservabilityStackProps.subjectRewriterEnabled} is true.
   */
  public readonly subjectRewriterLambda?: lambda.Function;

  constructor(scope: Construct, id: string, props: ObservabilityStackProps) {
    super(scope, id, props);

    const { stage } = props;

    // -----------------------------------------------------------------------
    // Log_Service: shared CloudWatch Logs group for platform logs. (R10.2)
    // The CloudWatch agent on the nodes and the containerized components ship
    // logs here; the analytics stack (task 17.2) subscribes/exports this on to
    // the S3 Hive Log_Store.
    // -----------------------------------------------------------------------
    this.logGroup = new logs.LogGroup(this, 'PlatformLogGroup', {
      logGroupName: `/hellodj/${stage}/platform`,
      retention: props.logRetention ?? logs.RetentionDays.TWO_WEEKS,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    // -----------------------------------------------------------------------
    // R10.5: single SNS topic every alarm notifies on threshold breach.
    // -----------------------------------------------------------------------
    this.alarmTopic = new sns.Topic(this, 'AlarmTopic', {
      topicName: `hellodj-${stage}-alarms`,
      // SNS "displayName" becomes the From-name and the SMS sender label; kept
      // short and prefixed so SMS alerts are recognizably HelloDJ.
      displayName: ALARM_SUBJECT_PREFIX,
    });

    // Fan every alarm out to the Platform_Owner's email and cellphone (R10.5).
    // Email: CloudWatch derives the notification subject from the alarm name,
    // and every alarm below is named with the `HelloDJ:` prefix, so alarm
    // emails arrive with a subject starting `HelloDJ:` for folder filtering.
    // SMS: delivered to the US number in E.164 form.

    const alarmEmail = props.alarmEmail ?? DEFAULT_ALARM_EMAIL;

    // SMS subscription is ALWAYS present regardless of the rewriter toggle.
    this.alarmTopic.addSubscription(
      new subscriptions.SmsSubscription(props.alarmSms ?? DEFAULT_ALARM_SMS),
    );

    if (props.subjectRewriterEnabled) {
      // -----------------------------------------------------------------------
      // R7.1, R7.4, R7.5: Subject_Rewriter Lambda path.
      // The Lambda subscribes to the alarm SNS topic, rewrites the subject to
      // start with `HelloDJ:` and enriches the body with alarm details, then
      // delivers via SES. A Dead Letter Queue catches Lambda failures and a
      // DLQ alarm re-notifies the same topic with a direct email subscription
      // as failsafe.
      // -----------------------------------------------------------------------

      // Dead Letter Queue for Lambda invocation failures.
      const dlq = new sqs.Queue(this, 'SubjectRewriterDLQ', {
        queueName: `hellodj-${stage}-subject-rewriter-dlq`,
        retentionPeriod: cdk.Duration.days(14),
      });

      // Subject_Rewriter Lambda function.
      this.subjectRewriterLambda = new lambda.Function(this, 'SubjectRewriterLambda', {
        functionName: `hellodj-${stage}-alarm-subject-rewriter`,
        description:
          'Rewrites CloudWatch alarm notification emails with HelloDJ: prefix and alarm details, delivers via SES. (R7.1, R7.4)',
        runtime: lambda.Runtime.PYTHON_3_13,
        handler: 'handler.lambda_handler',
        code: lambda.Code.fromAsset('platform/components/alarm_subject_rewriter'),
        timeout: cdk.Duration.seconds(30),
        deadLetterQueue: dlq,
        environment: {
          RECIPIENT_EMAIL: alarmEmail,
          SENDER_EMAIL: alarmEmail,
          SUBJECT_PREFIX: ALARM_SUBJECT_PREFIX,
        },
      });

      // Grant SES:SendEmail permission to the Lambda.
      this.subjectRewriterLambda.addToRolePolicy(
        new iam.PolicyStatement({
          actions: ['ses:SendEmail', 'ses:SendRawEmail'],
          resources: ['*'],
        }),
      );

      // Subscribe the Lambda to the alarm SNS topic.
      this.alarmTopic.addSubscription(
        new subscriptions.LambdaSubscription(this.subjectRewriterLambda),
      );

      // Failsafe: alarm on DLQ message count > 0 so Lambda failures still
      // reach the Platform_Owner's email via a direct subscription on the
      // same topic.
      const dlqAlarm = new cloudwatch.Alarm(this, 'SubjectRewriterDLQAlarm', {
        alarmName: `${ALARM_SUBJECT_PREFIX} hellodj-${stage}-subject-rewriter-dlq`,
        alarmDescription:
          'Subject_Rewriter Lambda is failing — alarm messages are landing in the DLQ. Direct email failsafe activated. (R7.5)',
        metric: dlq.metricApproximateNumberOfMessagesVisible({
          period: cdk.Duration.minutes(1),
        }),
        threshold: 0,
        evaluationPeriods: 1,
        comparisonOperator:
          cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
        treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
      });

      // The DLQ alarm notifies the SAME topic — but we add a direct email
      // subscription as a failsafe so the Platform_Owner still gets notified
      // even when the Lambda is broken.
      dlqAlarm.addAlarmAction(new cloudwatchActions.SnsAction(this.alarmTopic));

      // Direct email subscription as failure sink (activates when DLQ alarm
      // fires, but since it's a topic-level subscription it always receives
      // the DLQ alarm notification directly).
      this.alarmTopic.addSubscription(
        new subscriptions.EmailSubscription(alarmEmail, {
          filterPolicy: {
            // Only deliver to this subscription when the message comes from
            // the DLQ alarm (AlarmName contains "subject-rewriter-dlq").
            // Normal alarm messages go through the Lambda instead.
            AlarmName: sns.SubscriptionFilter.stringFilter({
              allowlist: [
                `${ALARM_SUBJECT_PREFIX} hellodj-${stage}-subject-rewriter-dlq`,
              ],
            }),
          },
        }),
      );
    } else {
      // Default path: direct SNS-to-email subscription (no Lambda).
      this.alarmTopic.addSubscription(
        new subscriptions.EmailSubscription(alarmEmail),
      );
    }

    const alarmAction = new cloudwatchActions.SnsAction(this.alarmTopic);

    // -----------------------------------------------------------------------
    // Metrics_Service: metrics the platform publishes to CloudWatch. (R10.3)
    // These reference the custom platform namespace (published by the runtime
    // components, e.g. hls-transcode's CPU/GPU transcode pressure signals that
    // the Autoscaler consumes). Alarms below watch these same metrics.
    // -----------------------------------------------------------------------
    const cpuTranscodePressure = new cloudwatch.Metric({
      namespace: PLATFORM_METRICS_NAMESPACE,
      metricName: 'CpuTranscodePressure',
      statistic: cloudwatch.Stats.AVERAGE,
      period: cdk.Duration.minutes(1),
    });
    const gpuTranscodePressure = new cloudwatch.Metric({
      namespace: PLATFORM_METRICS_NAMESPACE,
      metricName: 'GpuTranscodePressure',
      statistic: cloudwatch.Stats.AVERAGE,
      period: cdk.Duration.minutes(1),
    });
    const componentErrors = new cloudwatch.Metric({
      namespace: PLATFORM_METRICS_NAMESPACE,
      metricName: 'ComponentErrors',
      statistic: cloudwatch.Stats.SUM,
      period: cdk.Duration.minutes(5),
    });

    // -----------------------------------------------------------------------
    // R10.5: threshold alarms -> SNS notification on breach. Each alarm calls
    // addAlarmAction(SnsAction(topic)) so a breach publishes to the topic.
    // -----------------------------------------------------------------------
    const cpuPressureAlarm = new cloudwatch.Alarm(this, 'CpuTranscodePressureAlarm', {
      alarmName: `${ALARM_SUBJECT_PREFIX} hellodj-${stage}-cpu-transcode-pressure`,
      alarmDescription:
        'CPU transcode pressure sustained above scale-out threshold (design D3 / R16).',
      metric: cpuTranscodePressure,
      threshold: 70,
      evaluationPeriods: 3,
      comparisonOperator:
        cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });

    const gpuPressureAlarm = new cloudwatch.Alarm(this, 'GpuTranscodePressureAlarm', {
      alarmName: `${ALARM_SUBJECT_PREFIX} hellodj-${stage}-gpu-transcode-pressure`,
      alarmDescription:
        'GPU transcode pressure sustained above scale-out threshold (design D3 / R16).',
      metric: gpuTranscodePressure,
      threshold: 70,
      evaluationPeriods: 3,
      comparisonOperator:
        cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });

    const errorAlarm = new cloudwatch.Alarm(this, 'ComponentErrorAlarm', {
      alarmName: `${ALARM_SUBJECT_PREFIX} hellodj-${stage}-component-errors`,
      alarmDescription:
        'Platform component error rate crossed the alarm threshold (R10.5).',
      metric: componentErrors,
      threshold: 10,
      evaluationPeriods: 1,
      comparisonOperator:
        cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });

    this.alarms = [cpuPressureAlarm, gpuPressureAlarm, errorAlarm];
    for (const alarm of this.alarms) {
      // R10.5: raise a CloudWatch alarm AND send a notification on breach.
      alarm.addAlarmAction(alarmAction);
      // Also notify when the alarm clears, so operators see recovery.
      alarm.addOkAction(alarmAction);
    }

    // -----------------------------------------------------------------------
    // Metrics_Service: CloudWatch dashboard over the platform metrics + logs.
    // (R10.4)
    // -----------------------------------------------------------------------
    this.dashboard = new cloudwatch.Dashboard(this, 'PlatformDashboard', {
      dashboardName: `hellodj-${stage}-platform`,
    });

    this.dashboard.addWidgets(
      new cloudwatch.GraphWidget({
        title: 'Transcode pressure (CPU vs GPU)',
        left: [cpuTranscodePressure, gpuTranscodePressure],
        width: 12,
      }),
      new cloudwatch.GraphWidget({
        title: 'Component errors',
        left: [componentErrors],
        width: 12,
      }),
    );
    this.dashboard.addWidgets(
      new cloudwatch.AlarmStatusWidget({
        title: 'Platform alarms',
        alarms: this.alarms,
        width: 12,
      }),
      new cloudwatch.LogQueryWidget({
        title: 'Recent platform logs',
        logGroupNames: [this.logGroup.logGroupName],
        queryLines: [
          'fields @timestamp, @message',
          'sort @timestamp desc',
          'limit 50',
        ],
        width: 12,
      }),
    );

    // -----------------------------------------------------------------------
    // Outputs so component/pipeline stacks can wire the log group, topic, and
    // dashboard.
    // -----------------------------------------------------------------------
    new cdk.CfnOutput(this, 'PlatformLogGroupNameOutput', {
      value: this.logGroup.logGroupName,
      description: 'Name of the shared platform CloudWatch Logs group (R10.2).',
    });
    new cdk.CfnOutput(this, 'AlarmTopicArnOutput', {
      value: this.alarmTopic.topicArn,
      description: 'ARN of the SNS topic alarms notify on breach (R10.5).',
    });
    new cdk.CfnOutput(this, 'DashboardNameOutput', {
      value: this.dashboard.dashboardName ?? `hellodj-${stage}-platform`,
      description: 'Name of the platform CloudWatch dashboard (R10.4).',
    });
  }
}

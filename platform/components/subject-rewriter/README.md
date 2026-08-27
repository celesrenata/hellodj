# Subject_Rewriter Lambda (optional)

Thin IO wrapper around the pure `hellodj_platform_logic.alarm_subject` decision
functions. Inserted (optionally) between the alarm SNS topic and email delivery
so alarm emails literally begin with `HelloDJ:` (R7.2), the body reproduces the
alarm name and the previous/new alarm state verbatim (R7.3), and a rewriter
failure **never drops an alarm** — the original notification is delivered
instead (fail-open, R7.5).

## What this component does (and does not) do

This package is **IO only**:

1. Parse the incoming SNS record into an `AlarmNotification`
   (`parse_sns_record`).
2. Call the pure `hellodj_platform_logic.alarm_subject.rewriter_outcome(...)`
   (which uses `rewrite_subject` / `rewrite_body`) to decide the delivered
   email.
3. Deliver the resulting `EmailDelivery` via SES `SendEmail` — SES lets the
   sender fully control the subject, unlike the default SNS-to-email subject.

Every decision (prefixing, body preservation, fail-open) lives in
`hellodj_platform_logic`. The handler additionally guarantees fail-open even for
a *parse* failure that occurs before `rewriter_outcome` can run: the original
SNS subject/body is delivered.

CDK wiring into `observability-stack.ts` behind a `subjectRewriterEnabled`
toggle is a separate task (17.2); this component provides the handler only.

## Entry point

`subject_rewriter.handler.handler(event, context)` — the Lambda handler.

## Environment

| Variable                   | Purpose                                             |
|----------------------------|-----------------------------------------------------|
| `HELLODJ_ALARM_EMAIL_TO`   | Recipient (Platform_Owner) address. Required.       |
| `HELLODJ_ALARM_EMAIL_FROM` | Verified SES sender identity. Required.             |
| `AWS_REGION` / `AWS_DEFAULT_REGION` | Region for the SES client. Optional.       |

## Testing

The SES client is injectable (`handler(event, context, client=fake)`), and
`boto3` is imported lazily, so the handler is unit-testable with a fake client
and imports without AWS libraries present. The pure rewrite logic is covered by
Property 7 in `hellodj_platform_logic/tests/test_alarm_subject_property.py`.

Requirements: 7.2, 7.3, 7.5

"""Subject_Rewriter Lambda handler — thin IO over the pure rewriter (R7).

This module is the *only* IO in the Subject_Rewriter path. Its job is narrow
and every decision is delegated to
:mod:`hellodj_platform_logic.alarm_subject`:

1. **Parse** each incoming SNS record into an
   :class:`~hellodj_platform_logic.types.AlarmNotification` — pulling the alarm
   name and the previous/new alarm state out of the CloudWatch alarm message
   JSON, and the original subject/body off the SNS envelope (:func:`parse_sns_record`).
2. **Decide** the delivered email by calling the pure
   :func:`~hellodj_platform_logic.alarm_subject.rewriter_outcome` (which itself
   uses ``rewrite_subject`` / ``rewrite_body``). On success the subject begins
   ``HelloDJ:`` (R7.2) and the body reproduces the alarm name + prev/new state
   verbatim (R7.3).
3. **Deliver** the resulting :class:`~hellodj_platform_logic.types.EmailDelivery`
   via SES ``SendEmail`` (whose subject is fully controllable, unlike the
   default SNS-to-email subject).

**Fail-open (R7.5).** Every processing step for a single record is wrapped so
that *any* error — a malformed SNS payload, a JSON parse failure, an
unexpected shape — results in delivering the **original** notification instead
of dropping it. The pure ``rewriter_outcome`` already models the "rewriter
processing failed → deliver the original" decision; this handler additionally
guarantees that even a parse failure (before ``rewriter_outcome`` can run) still
delivers the original SNS subject/body. An alarm is never silently dropped.

The SES client is injected (a lazily-imported ``boto3`` ``ses`` client by
default) so the handler is unit-testable with a fake client and the module
imports without AWS libraries present — mirroring the ``migration`` component's
injectable-client convention.

Environment:
    HELLODJ_ALARM_EMAIL_TO      Recipient address for the rewritten alarm email
                                (the Platform_Owner). Required at runtime.
    HELLODJ_ALARM_EMAIL_FROM    Verified SES sender/identity address. Required
                                at runtime.
    AWS_REGION / AWS_DEFAULT_REGION  Region for the SES client (optional).

Requirements: 7.2, 7.3, 7.5
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Protocol

from hellodj_platform_logic.alarm_subject import rewriter_outcome
from hellodj_platform_logic.types import AlarmNotification, EmailDelivery

__all__ = [
    "SesClient",
    "build_ses_client",
    "parse_sns_record",
    "deliver",
    "process_record",
    "handler",
    "ENV_EMAIL_TO",
    "ENV_EMAIL_FROM",
]

log = logging.getLogger("subject_rewriter")

#: Recipient of the (possibly rewritten) alarm email.
ENV_EMAIL_TO = "HELLODJ_ALARM_EMAIL_TO"
#: Verified SES sender identity the email is sent from.
ENV_EMAIL_FROM = "HELLODJ_ALARM_EMAIL_FROM"
ENV_REGION = "AWS_REGION"
ENV_REGION_FALLBACK = "AWS_DEFAULT_REGION"


class SesClient(Protocol):
    """Minimal subset of the boto3 ``ses`` client interface used here."""

    def send_email(self, **kwargs: Any) -> dict[str, Any]:
        """Send an email; SES lets the caller fully control the subject."""
        ...


def build_ses_client(region_name: str | None = None) -> SesClient:
    """Create a real boto3 ``ses`` client (imported lazily).

    Imported inside the factory so the module imports for tests / ``py_compile``
    without ``boto3`` present, matching the other components' convention.
    """
    import boto3

    return boto3.client("ses", region_name=region_name)


def _resolve_region() -> str | None:
    """Return the configured AWS region, if any."""
    return os.environ.get(ENV_REGION) or os.environ.get(ENV_REGION_FALLBACK)


def parse_sns_record(record: dict[str, Any]) -> AlarmNotification:
    """Parse one SNS event record into an :class:`AlarmNotification`.

    The CloudWatch alarm state-change notification arrives as a JSON string in
    ``record["Sns"]["Message"]`` with (at least) ``AlarmName``,
    ``NewStateValue`` and ``OldStateValue`` fields; the human-facing subject is
    on the SNS envelope at ``record["Sns"]["Subject"]``. The original message
    string is preserved verbatim as the notification body so the fail-open path
    (and ``rewrite_body``) can reproduce it unaltered.

    Args:
        record: One element of the Lambda SNS event's ``Records`` list.

    Returns:
        The parsed :class:`~hellodj_platform_logic.types.AlarmNotification`.

    Raises:
        Exception: On any malformed/unexpected payload. Callers
            (:func:`process_record`) treat a raised parse error as the
            fail-open trigger, so a parse failure never drops the alarm.
    """
    sns = record["Sns"]
    original_subject = sns.get("Subject") or ""
    original_body = sns.get("Message") or ""

    alarm_name = ""
    previous_state = ""
    new_state = ""
    # The alarm message is JSON for CloudWatch alarm notifications; a
    # non-JSON/plain message simply yields empty alarm fields and the original
    # body is still preserved for delivery.
    try:
        message = json.loads(original_body)
    except (TypeError, ValueError):
        message = None
    if isinstance(message, dict):
        alarm_name = str(message.get("AlarmName", "") or "")
        previous_state = str(message.get("OldStateValue", "") or "")
        new_state = str(message.get("NewStateValue", "") or "")

    return AlarmNotification(
        alarm_name=alarm_name,
        previous_state=previous_state,
        new_state=new_state,
        original_subject=original_subject,
        original_body=original_body,
    )


def deliver(
    client: SesClient,
    delivery: EmailDelivery,
    *,
    source: str,
    to_address: str,
) -> None:
    """Send one :class:`EmailDelivery` via SES ``SendEmail``.

    SES lets the caller fully control the subject, so the delivered email's
    subject is ``delivery.subject`` verbatim — which begins with ``HelloDJ:``
    when the notification was rewritten (R7.2).

    Args:
        client: The SES client to send through.
        delivery: The subject/body to deliver.
        source: The verified SES sender identity.
        to_address: The recipient address.
    """
    client.send_email(
        Source=source,
        Destination={"ToAddresses": [to_address]},
        Message={
            "Subject": {"Data": delivery.subject, "Charset": "UTF-8"},
            "Body": {"Text": {"Data": delivery.body, "Charset": "UTF-8"}},
        },
    )


def process_record(
    record: dict[str, Any],
    client: SesClient,
    *,
    source: str,
    to_address: str,
) -> EmailDelivery:
    """Process and deliver a single SNS record, failing open (R7.5).

    Parses the record, asks the pure ``rewriter_outcome`` for the delivered
    email, and sends it via SES. If *anything before the SES send* raises — a
    malformed payload, a parse error — the original notification is delivered
    instead (fail-open), so no alarm is ever dropped. The pure
    ``rewriter_outcome`` is called with ``process_succeeded=True`` when parsing
    succeeded and ``False`` when it did not, so the fail-open decision itself
    stays in the pure layer.

    Args:
        record: One SNS event record.
        client: The SES client to deliver through.
        source: The verified SES sender identity.
        to_address: The recipient address.

    Returns:
        The :class:`EmailDelivery` that was sent (rewritten on success, the
        original on the fail-open path).
    """
    try:
        notification = parse_sns_record(record)
        process_succeeded = True
    except Exception:  # noqa: BLE001 - fail-open: never drop an alarm (R7.5)
        log.exception("failed to parse SNS alarm record; failing open")
        notification = _fallback_notification(record)
        process_succeeded = False

    delivery = rewriter_outcome(process_succeeded, notification)
    deliver(client, delivery, source=source, to_address=to_address)
    return delivery


def _fallback_notification(record: dict[str, Any]) -> AlarmNotification:
    """Best-effort original notification when parsing failed (R7.5).

    Pulls whatever subject/body can be recovered from the raw record so the
    fail-open delivery still carries the original alarm content. Every field is
    accessed defensively so this never raises.
    """
    sns = record.get("Sns", {}) if isinstance(record, dict) else {}
    subject = ""
    body = ""
    if isinstance(sns, dict):
        subject = sns.get("Subject") or ""
        body = sns.get("Message") or ""
    return AlarmNotification(
        alarm_name="",
        previous_state="",
        new_state="",
        original_subject=subject,
        original_body=body,
    )


def handler(
    event: dict[str, Any],
    context: Any = None,
    *,
    client: SesClient | None = None,
) -> dict[str, Any]:
    """Lambda entry point: rewrite + deliver every alarm SNS record (R7).

    Iterates the SNS event's ``Records``, delivering each through
    :func:`process_record`. Each record is handled independently and fail-open:
    a failure delivering one record neither drops that alarm (the original is
    delivered) nor prevents the remaining records from being processed.

    Args:
        event: The Lambda SNS event (``{"Records": [...]}``).
        context: The Lambda context (unused).
        client: An injected SES client (defaults to a real boto3 client). Kept
            as a keyword arg so tests can supply a fake client.

    Returns:
        A small summary dict (``{"delivered": n, "rewritten": r}``) for logging
        / test assertions.
    """
    source = os.environ.get(ENV_EMAIL_FROM, "").strip()
    to_address = os.environ.get(ENV_EMAIL_TO, "").strip()
    if not source or not to_address:
        raise RuntimeError(
            f"{ENV_EMAIL_FROM} and {ENV_EMAIL_TO} environment variables are "
            "required"
        )

    ses = client or build_ses_client(_resolve_region())

    records = event.get("Records", []) if isinstance(event, dict) else []
    delivered = 0
    rewritten = 0
    for record in records:
        try:
            result = process_record(
                record, ses, source=source, to_address=to_address
            )
            delivered += 1
            if result.rewritten:
                rewritten += 1
        except Exception:  # noqa: BLE001 - one record's failure never blocks the rest
            log.exception("failed to deliver alarm record; continuing")

    return {"delivered": delivered, "rewritten": rewritten}

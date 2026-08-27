"""Alarm-email subject rewrite decision logic (R7.2/R7.3/R7.5).

This module holds the pure functions behind the optional ``Subject_Rewriter``
Lambda in the observability stack. Per the design (Components -- "Optional
alarm-email subject prefixed with HelloDJ" and the "Alarm-notification path with
the optional Subject_Rewriter" architecture section), the rewriter is inserted
between the alarm SNS topic and the email delivery so the delivered email
subject literally begins with ``HelloDJ:`` (R7.2), the body reproduces the
original alarm name and both the previous and new alarm state verbatim (R7.3),
and -- crucially -- a rewriter failure never drops an alarm: the original
notification is delivered unchanged instead (fail-open, R7.5).

The three functions here are the pure core the Lambda handler wraps:

* :func:`rewrite_subject` -- prefixes the subject with ``HelloDJ:``,
  idempotently (it never double-prefixes an already-prefixed subject) (R7.2).
* :func:`rewrite_body` -- builds a body that contains the alarm name and both
  alarm states verbatim, preserving the original body too (R7.3).
* :func:`rewriter_outcome` -- the fail-open decision: on rewriter success return
  the rewritten :class:`~hellodj_platform_logic.types.EmailDelivery`; on
  rewriter failure return a fail-open delivery of the *original* notification
  (subject and body unaltered), never dropping the alarm (R7.5).

Like the other decision modules in this package (``pinning``, ``stale_pins``,
``python_migration``, ``binary_cache``), everything here is pure: no SNS/SES
call, no live AWS access. The IO -- parsing the SNS message into an
:class:`~hellodj_platform_logic.types.AlarmNotification` and calling SES
``SendEmail`` -- lives in the Lambda handler (task 17), so the correctness
property (P7) can exercise these functions directly.

Design references:
    * Components -- "Subject literally begins with ``HelloDJ:`` (R7.2)":
      ``rewrite_subject(original) -> str`` guarantees the prefix and does not
      double-prefix if the source already starts with it.
    * Components -- "Body preserves original name + both states verbatim
      (R7.3)": ``rewrite_body(alarm_name, prev_state, new_state, original) ->
      str`` asserts each field appears unaltered in the output.
    * Components -- fail-open (R7.5):
      ``rewriter_outcome(process_succeeded, notification) -> EmailDelivery`` --
      on error deliver the original; never drop.
    * Architecture -- "Alarm-notification path with the optional
      Subject_Rewriter".
    * Data Models -- ``AlarmNotification``, ``EmailDelivery``.
    * Correctness Property 7: an enabled subject rewriter prefixes the subject,
      preserves the body, and never drops on failure.

Requirements: 7.2, 7.3, 7.5
"""

from __future__ import annotations

from hellodj_platform_logic.types import AlarmNotification, EmailDelivery

__all__ = ["SUBJECT_PREFIX", "rewrite_subject", "rewrite_body", "rewriter_outcome"]

#: The literal prefix the delivered email subject must begin with (R7.2). The
#: text ``HelloDJ:`` is followed by a single space before the original subject
#: so the result reads ``HelloDJ: <original>``.
SUBJECT_PREFIX = "HelloDJ:"


def rewrite_subject(original_subject: str) -> str:
    """Prefix the subject with ``HelloDJ:``, idempotently (R7.2).

    Implements the subject half of Property 7 / R7.2. The returned subject's
    first characters are exactly :data:`SUBJECT_PREFIX` (``HelloDJ:``). The
    transform is idempotent: a subject that already begins with the prefix is
    returned unchanged, so routing a notification through the rewriter more than
    once never yields a double prefix (``HelloDJ: HelloDJ: ...``).

    Args:
        original_subject: The subject of the original alarm notification (which
            CloudWatch may itself have prepended text to).

    Returns:
        A subject string that literally begins with ``HelloDJ:``. When the
        original already begins with the prefix it is returned verbatim;
        otherwise the prefix and a single separating space are prepended.

    Requirements: 7.2
    """
    if original_subject.startswith(SUBJECT_PREFIX):
        return original_subject
    return f"{SUBJECT_PREFIX} {original_subject}"


def rewrite_body(
    alarm_name: str,
    previous_state: str,
    new_state: str,
    original_body: str,
) -> str:
    """Build a body reproducing the alarm name and both states verbatim (R7.3).

    Implements the body half of Property 7 / R7.3. The returned body contains,
    reproduced verbatim (as exact substrings, unaltered), the original alarm
    name and both the previous and the new alarm state from the original
    notification, and preserves the original body so no information is lost.

    Args:
        alarm_name: The original alarm name; appears verbatim in the output.
        previous_state: The previous alarm state; appears verbatim in the output.
        new_state: The new alarm state; appears verbatim in the output.
        original_body: The original notification body; preserved verbatim in the
            output.

    Returns:
        A body string that contains ``alarm_name``, ``previous_state``,
        ``new_state`` and ``original_body`` each as an exact substring.

    Requirements: 7.3
    """
    return (
        f"Alarm: {alarm_name}\n"
        f"State transition: {previous_state} -> {new_state}\n"
        f"\n"
        f"{original_body}"
    )


def rewriter_outcome(
    process_succeeded: bool,
    notification: AlarmNotification,
) -> EmailDelivery:
    """Decide the delivered email, failing open on rewriter error (R7.5).

    Implements the fail-open half of Property 7 / R7.5.

    * On rewriter **success** (``process_succeeded`` True), returns the
      *rewritten* delivery: the subject is prefixed via :func:`rewrite_subject`
      (begins with ``HelloDJ:``, R7.2) and the body is rebuilt via
      :func:`rewrite_body` (reproduces the alarm name and both states verbatim,
      R7.3). ``EmailDelivery.rewritten`` is ``True``.
    * On rewriter **failure** (``process_succeeded`` False), returns a
      *fail-open* delivery of the **original** notification: the
      ``original_subject`` and ``original_body`` are delivered unaltered so the
      alarm is still delivered and never silently dropped (R7.5).
      ``EmailDelivery.rewritten`` is ``False``.

    Either way an :class:`~hellodj_platform_logic.types.EmailDelivery` is
    returned -- the function never signals "drop this alarm".

    Args:
        process_succeeded: Whether the rewriter processed the notification
            without error. ``False`` triggers the fail-open path.
        notification: The original alarm notification to rewrite or fall back to.

    Returns:
        The :class:`~hellodj_platform_logic.types.EmailDelivery` to send: the
        rewritten email on success, or a fail-open delivery of the original
        notification on failure. Never ``None`` -- the alarm is always delivered.

    Requirements: 7.2, 7.3, 7.5
    """
    if process_succeeded:
        return EmailDelivery(
            subject=rewrite_subject(notification.original_subject),
            body=rewrite_body(
                notification.alarm_name,
                notification.previous_state,
                notification.new_state,
                notification.original_body,
            ),
            rewritten=True,
        )
    # Fail-open (R7.5): deliver the original notification unaltered so no alarm
    # is silently dropped.
    return EmailDelivery(
        subject=notification.original_subject,
        body=notification.original_body,
        rewritten=False,
    )

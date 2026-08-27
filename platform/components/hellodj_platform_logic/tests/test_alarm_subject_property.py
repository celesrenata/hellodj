"""Property-based test for the alarm-subject rewriter decision functions (task 7.2).

Feature: hellodj-private-source-and-toolchain, Property 7: an enabled subject
rewriter prefixes the subject, preserves the body, and never drops on failure

Property 7 (R7.2/R7.3/R7.5): *for any* generated
:class:`~hellodj_platform_logic.types.AlarmNotification`
(``alarm_name``/``previous_state``/``new_state``/``original_subject``/
``original_body``) and *any* ``process_succeeded`` boolean, the pure rewriter
functions SHALL satisfy, over the full input space:

* :func:`~hellodj_platform_logic.alarm_subject.rewrite_subject` -- the result
  literally begins with ``HelloDJ:`` as its first characters (R7.2), and the
  transform is idempotent: a subject already beginning with the prefix is
  returned unchanged (no double-prefix);
* :func:`~hellodj_platform_logic.alarm_subject.rewrite_body` -- the output
  contains ``alarm_name``, ``previous_state``, ``new_state`` and
  ``original_body`` each reproduced verbatim, as exact substrings (R7.3);
* :func:`~hellodj_platform_logic.alarm_subject.rewriter_outcome` -- an
  :class:`~hellodj_platform_logic.types.EmailDelivery` is *always* returned
  (the alarm is never dropped); on ``process_succeeded`` True the delivery is
  the rewritten email (subject begins ``HelloDJ:``, ``rewritten`` True) and on
  ``process_succeeded`` False it is the fail-open delivery carrying the
  **original** subject and body verbatim with ``rewritten`` False (R7.5).

Validates: Requirements 7.2, 7.3, 7.5
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from hellodj_platform_logic.alarm_subject import (
    SUBJECT_PREFIX,
    rewrite_body,
    rewrite_subject,
    rewriter_outcome,
)
from hellodj_platform_logic.types import AlarmNotification, EmailDelivery

# Free text for subjects/bodies/names/states. Includes whitespace and a few
# punctuation classes so the verbatim-substring guarantees are exercised against
# realistic alarm text; the decision reasons over prefixing and substring
# containment, never over the specific characters.
_TEXT = st.text(
    alphabet=st.characters(
        min_codepoint=ord(" "),
        max_codepoint=ord("~"),
    ),
    min_size=0,
    max_size=40,
)

# Alarm states as short non-empty tokens (e.g. OK / ALARM / INSUFFICIENT_DATA)
# plus arbitrary text, so both realistic and adversarial state strings appear.
_STATE = st.one_of(
    st.sampled_from(["OK", "ALARM", "INSUFFICIENT_DATA"]),
    _TEXT,
)


@st.composite
def alarm_notifications(draw: st.DrawFn) -> AlarmNotification:
    """Generate an arbitrary :class:`AlarmNotification` across the full field space."""
    return AlarmNotification(
        alarm_name=draw(_TEXT),
        previous_state=draw(_STATE),
        new_state=draw(_STATE),
        original_subject=draw(_TEXT),
        original_body=draw(_TEXT),
    )


@settings(max_examples=200)
@given(subject=_TEXT)
def test_rewrite_subject_prefixes_and_is_idempotent(subject: str) -> None:
    """Feature: hellodj-private-source-and-toolchain, Property 7 (subject half).

    The rewritten subject literally begins with ``HelloDJ:`` and rewriting an
    already-prefixed subject is a no-op (idempotent, no double-prefix).

    Validates: Requirements 7.2
    """
    once = rewrite_subject(subject)

    # Begins with the literal prefix as its very first characters (R7.2).
    assert once.startswith(SUBJECT_PREFIX)
    assert once[: len(SUBJECT_PREFIX)] == SUBJECT_PREFIX

    # Idempotent: an already-prefixed subject is returned unchanged, so a second
    # pass never yields "HelloDJ: HelloDJ: ...".
    twice = rewrite_subject(once)
    assert twice == once

    # A subject that already begins with the prefix is returned verbatim.
    if subject.startswith(SUBJECT_PREFIX):
        assert once == subject


@settings(max_examples=200)
@given(
    alarm_name=_TEXT,
    previous_state=_STATE,
    new_state=_STATE,
    original_body=_TEXT,
)
def test_rewrite_body_preserves_fields_verbatim(
    alarm_name: str,
    previous_state: str,
    new_state: str,
    original_body: str,
) -> None:
    """Feature: hellodj-private-source-and-toolchain, Property 7 (body half).

    The rewritten body contains the alarm name, both states, and the original
    body each as an exact verbatim substring.

    Validates: Requirements 7.3
    """
    body = rewrite_body(alarm_name, previous_state, new_state, original_body)

    assert alarm_name in body
    assert previous_state in body
    assert new_state in body
    assert original_body in body


@settings(max_examples=200)
@given(notification=alarm_notifications(), process_succeeded=st.booleans())
def test_rewriter_outcome_rewrites_on_success_fails_open_on_failure(
    notification: AlarmNotification,
    process_succeeded: bool,
) -> None:
    """Feature: hellodj-private-source-and-toolchain, Property 7 (fail-open).

    ``rewriter_outcome`` always returns an :class:`EmailDelivery` (never drops):
    on success the rewritten email (subject begins ``HelloDJ:``, ``rewritten``
    True, body preserves the fields verbatim); on failure the fail-open delivery
    of the ORIGINAL subject and body verbatim with ``rewritten`` False.

    Validates: Requirements 7.2, 7.3, 7.5
    """
    delivery = rewriter_outcome(process_succeeded, notification)

    # Never dropped: an EmailDelivery is always produced (R7.5).
    assert isinstance(delivery, EmailDelivery)

    if process_succeeded:
        # Rewritten: subject begins with the prefix (R7.2) and is flagged.
        assert delivery.rewritten is True
        assert delivery.subject.startswith(SUBJECT_PREFIX)
        # Body reproduces the alarm name and both states verbatim (R7.3), and
        # preserves the original body too.
        assert notification.alarm_name in delivery.body
        assert notification.previous_state in delivery.body
        assert notification.new_state in delivery.body
        assert notification.original_body in delivery.body
    else:
        # Fail-open (R7.5): the ORIGINAL subject and body are delivered
        # verbatim, unaltered, and the delivery is flagged not-rewritten.
        assert delivery.rewritten is False
        assert delivery.subject == notification.original_subject
        assert delivery.body == notification.original_body

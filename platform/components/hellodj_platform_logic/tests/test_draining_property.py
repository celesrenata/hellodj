"""Property test for the connection-draining state machine (task 5.4).

Property 8 (design "Connection-draining state machine"): for any host /
container / GPU_Node carrying a set of in-flight tasks with arbitrary remaining
durations and a defined drain timeout, once draining begins the state machine
(:func:`hellodj_platform_logic.draining.drain`) SHALL:

* accept no new connections -- :attr:`DrainOutcome.accepts_new_connections` is
  ``False``, and :func:`accepts_new_connections` is ``False`` for ``DRAINING``
  and ``DRAINED`` but ``True`` for ``ACTIVE`` (R17.2);
* allow every task whose ``remaining_seconds`` is within the drain timeout to
  complete normally -- it appears in :attr:`DrainOutcome.completed` (R17.3);
* for every task still running at the timeout (``remaining_seconds`` strictly
  greater than the timeout) terminate it and record *exactly one* termination
  event, aligned one-to-one with the terminated task by ``task_id`` (R17.5);
* partition the input exactly into ``completed`` U ``terminated`` (no loss, no
  duplication) preserving input order; and
* reach the terminal :attr:`DrainState.DRAINED` state.

The decision function is pure, so the property is exercised directly over lists
of :class:`InFlightTask` (unique-ish task ids, non-negative remaining seconds)
and a non-negative drain timeout generated with Hypothesis (>=100 iterations).
The default-timeout path (``DEFAULT_DRAIN_TIMEOUT_SECONDS`` = 120 s) is
exercised too.

Feature: aws-saas-replatform, Property 8

Validates: Requirements 17.1, 17.2, 17.3, 17.5
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from hellodj_platform_logic.draining import (
    DrainOutcome,
    accepts_new_connections,
    drain,
)
from hellodj_platform_logic.types import (
    DEFAULT_DRAIN_TIMEOUT_SECONDS,
    DrainState,
    InFlightTask,
)

# Remaining durations and drain timeouts are non-negative, finite seconds.
_seconds = st.floats(
    min_value=0.0,
    max_value=10_000.0,
    allow_nan=False,
    allow_infinity=False,
)


@st.composite
def _tasks(draw: st.DrawFn) -> list[InFlightTask]:
    """Draw a list of in-flight tasks with unique ids and non-negative work.

    Task ids are drawn as a set (then materialised into a list) so each task is
    uniquely identifiable, which lets the property check the one-to-one mapping
    between terminated tasks and their termination events. Each task is given an
    arbitrary non-negative ``remaining_seconds``.
    """
    ids = draw(
        st.lists(
            st.text(
                alphabet=st.characters(min_codepoint=48, max_codepoint=122),
                min_size=1,
                max_size=12,
            ),
            min_size=0,
            max_size=20,
            unique=True,
        )
    )
    return [InFlightTask(task_id=task_id, remaining_seconds=draw(_seconds)) for task_id in ids]


def _assert_property_8(outcome: DrainOutcome, tasks: list[InFlightTask], timeout: float) -> None:
    """Assert the full Property 8 invariant over a drain outcome."""
    # Terminal state is always DRAINED.
    assert outcome.state is DrainState.DRAINED

    # No new connections are accepted once draining has begun.
    assert outcome.accepts_new_connections is False
    assert accepts_new_connections(DrainState.DRAINING) is False
    assert accepts_new_connections(DrainState.DRAINED) is False
    assert accepts_new_connections(DrainState.ACTIVE) is True

    # In-window tasks complete; strictly-over-timeout tasks are terminated.
    expected_completed = [t for t in tasks if t.remaining_seconds <= timeout]
    expected_terminated = [t for t in tasks if t.remaining_seconds > timeout]

    # Input order is preserved in both output collections.
    assert list(outcome.completed) == expected_completed
    assert list(outcome.terminated) == expected_terminated

    # completed U terminated partitions the input exactly (no loss/duplication).
    assert len(outcome.completed) + len(outcome.terminated) == len(tasks)

    # Exactly one termination event per terminated task, one-to-one by task_id.
    assert len(outcome.termination_events) == len(outcome.terminated)
    assert [e.task_id for e in outcome.termination_events] == [
        t.task_id for t in outcome.terminated
    ]

    # Every completed task is within the window; every terminated task exceeds
    # the timeout and its event carries the timeout that was breached.
    for task in outcome.completed:
        assert task.remaining_seconds <= timeout
    for task, event in zip(
        outcome.terminated, outcome.termination_events, strict=True
    ):
        assert task.remaining_seconds > timeout
        assert event.remaining_seconds == task.remaining_seconds
        assert event.drain_timeout_seconds == timeout


@settings(max_examples=300)
@given(tasks=_tasks(), timeout=_seconds)
def test_draining_state_machine(tasks: list[InFlightTask], timeout: float) -> None:
    """Draining partitions tasks, terminates over-timeout ones exactly once.

    Feature: aws-saas-replatform, Property 8

    Validates: Requirements 17.1, 17.2, 17.3, 17.5
    """
    outcome = drain(tasks, drain_timeout=timeout)
    _assert_property_8(outcome, tasks, timeout)


@settings(max_examples=200)
@given(tasks=_tasks())
def test_draining_default_timeout(tasks: list[InFlightTask]) -> None:
    """The default-timeout path (120 s) obeys the same Property 8 rules.

    Feature: aws-saas-replatform, Property 8

    Validates: Requirements 17.1, 17.2, 17.3, 17.5
    """
    outcome = drain(tasks)
    _assert_property_8(outcome, tasks, DEFAULT_DRAIN_TIMEOUT_SECONDS)

"""Connection-draining state machine for graceful shutdown.

This module holds the pure decision logic that governs how a host, container or
GPU_Node drains its in-flight work before termination. It is imported by both
the CDK infrastructure layer (which configures the drain timeout on node
groups / target groups) and the runtime workloads (which enforce the same
semantics at shutdown) so infrastructure-as-code and runtime share a single
source of truth. It makes no live AWS calls, so the correctness property
(Property 8) can exercise it directly.

The draining lifecycle is modelled as a small state machine over the
:class:`~hellodj_platform_logic.types.DrainState` enum:

* ``ACTIVE`` — normal operation, new connections are accepted.
* ``DRAINING`` — draining has begun: no new connections are accepted and every
  in-flight task is given up to the drain timeout to finish.
* ``DRAINED`` — every task has either completed within the timeout or been
  force-terminated at the timeout.

:func:`drain` is the pure entry point. Given the set of in-flight tasks carried
at the moment draining begins and a drain timeout, it deterministically derives
which tasks complete in-window, which are terminated at the timeout (recording
exactly one termination event each), whether new connections are accepted, and
the resulting :class:`DrainState`.

Design references:
    * State machine: ACTIVE -> DRAINING -> DRAINED
    * Correctness Property 8: Connection-draining state machine
    * Error handling: tasks exceeding the drain timeout are force-terminated
      and a termination event is recorded (R17.5)

Requirements: 17.1, 17.2, 17.3, 17.4, 17.5
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from hellodj_platform_logic.types import (
    DEFAULT_DRAIN_TIMEOUT_SECONDS,
    DrainState,
    InFlightTask,
)

__all__ = [
    "TerminationEvent",
    "DrainOutcome",
    "accepts_new_connections",
    "drain",
]


@dataclass(frozen=True)
class TerminationEvent:
    """A record of a single forced task termination at the drain timeout.

    Exactly one :class:`TerminationEvent` is produced for each in-flight task
    that is still running when the drain timeout elapses (R17.5). The runtime
    emits these to CloudWatch; the pure logic here only derives them.

    Attributes:
        task_id: Identifier of the task that was force-terminated.
        remaining_seconds: How much work the task still had outstanding at the
            moment the drain timeout elapsed (always strictly greater than the
            drain timeout for a terminated task).
        drain_timeout_seconds: The drain timeout that was exceeded.
    """

    task_id: str
    remaining_seconds: float
    drain_timeout_seconds: float


@dataclass(frozen=True)
class DrainOutcome:
    """The result of draining a host/container/GPU_Node (Property 8).

    Attributes:
        state: The resulting drain state. Draining always terminates in
            :attr:`DrainState.DRAINED` once every task has been resolved.
        accepts_new_connections: Whether the draining target accepts new
            connections. Always ``False`` once draining has begun (R17.2).
        completed: The tasks that finished normally within the drain timeout,
            in their input order (R17.3).
        terminated: The tasks that were still running at the drain timeout and
            were force-terminated, in their input order (R17.5).
        termination_events: Exactly one :class:`TerminationEvent` per terminated
            task, aligned one-to-one with :attr:`terminated` (R17.5).
    """

    state: DrainState
    accepts_new_connections: bool
    completed: tuple[InFlightTask, ...] = ()
    terminated: tuple[InFlightTask, ...] = ()
    termination_events: tuple[TerminationEvent, ...] = field(default_factory=tuple)


def accepts_new_connections(state: DrainState) -> bool:
    """Return whether a target in the given state accepts new connections.

    New connections are accepted only while :attr:`DrainState.ACTIVE`. As soon
    as draining begins (``DRAINING``) and after it finishes (``DRAINED``) the
    target stops routing new connections to itself (R17.2).

    Args:
        state: The current drain state.

    Returns:
        ``True`` only when ``state`` is :attr:`DrainState.ACTIVE`.
    """
    return state is DrainState.ACTIVE


def _completes_within_timeout(task: InFlightTask, drain_timeout: float) -> bool:
    """Return whether ``task`` finishes within ``drain_timeout``.

    A task completes in-window when the work it still has outstanding at the
    moment draining begins is no greater than the drain timeout (R17.3). A task
    whose remaining work exactly equals the timeout is treated as completing
    (the boundary is inclusive), so only tasks that strictly exceed the timeout
    are force-terminated.
    """
    return task.remaining_seconds <= drain_timeout


def drain(
    tasks: Iterable[InFlightTask],
    drain_timeout: float = DEFAULT_DRAIN_TIMEOUT_SECONDS,
) -> DrainOutcome:
    """Drain a host/container/GPU_Node's in-flight tasks (Property 8).

    Models the ``ACTIVE -> DRAINING -> DRAINED`` transition as a pure function.
    At the moment draining begins the target stops accepting new connections
    (R17.2). Every in-flight task is then classified against the drain timeout:

    * Tasks whose ``remaining_seconds`` is within ``drain_timeout`` are allowed
      to complete normally (R17.3) and appear in :attr:`DrainOutcome.completed`.
    * Tasks still running once ``drain_timeout`` elapses are force-terminated
      (R17.5); each appears in :attr:`DrainOutcome.terminated` and produces
      exactly one :class:`TerminationEvent` in
      :attr:`DrainOutcome.termination_events`.

    Once every task has been resolved the target reaches
    :attr:`DrainState.DRAINED`. Input ordering is preserved in every output
    collection so the result is fully deterministic.

    Args:
        tasks: The in-flight tasks carried at the moment draining begins.
        drain_timeout: The drain timeout in seconds. Defaults to
            :data:`DEFAULT_DRAIN_TIMEOUT_SECONDS` (120 s per R17.3). Must be
            non-negative.

    Returns:
        A :class:`DrainOutcome` describing which tasks completed, which were
        terminated (with exactly one termination event each), whether new
        connections are accepted, and the resulting :class:`DrainState`.

    Raises:
        ValueError: If ``drain_timeout`` is negative.
    """
    if drain_timeout < 0:
        raise ValueError("drain_timeout must be non-negative")

    completed: list[InFlightTask] = []
    terminated: list[InFlightTask] = []
    termination_events: list[TerminationEvent] = []

    for task in tasks:
        if _completes_within_timeout(task, drain_timeout):
            completed.append(task)
        else:
            terminated.append(task)
            termination_events.append(
                TerminationEvent(
                    task_id=task.task_id,
                    remaining_seconds=task.remaining_seconds,
                    drain_timeout_seconds=drain_timeout,
                )
            )

    return DrainOutcome(
        state=DrainState.DRAINED,
        accepts_new_connections=accepts_new_connections(DrainState.DRAINING),
        completed=tuple(completed),
        terminated=tuple(terminated),
        termination_events=tuple(termination_events),
    )

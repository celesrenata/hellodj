"""Python 3.11 -> 3.14 component migration-readiness decision (R5.3/R5.4).

This module holds the pure decision function that gates whether a Python
component may be marked migrated to Python 3.14. Per the design (Components
"Migrate Python components to Python 3.14"), before a component is marked
migrated the platform must verify, for every runtime dependency (cryptography,
onnxruntime, torch, discord.py, wavelink, flask, ... where present), that the
dependency imports without error under Python 3.14, *and* that the component's
existing test suite passes under Python 3.14. If any dependency fails to import
or the test suite does not pass, the platform records the name of the specific
blocking dependency (or the fact that the test suite failed) and does not mark
the component migrated (R5.4).

Like the other decision modules in this package (``migration``, ``pinning``,
``binary_cache``), this function is pure: it consumes the already-collected
per-dependency import results and the test-suite outcome and performs no live
imports, subprocess calls, or network/AWS calls, so the correctness property
(P5) can exercise it directly.

Design references:
    * Components -- "Dependency + test verification before 'migrated'
      (R5.3/R5.4)": ready iff every dependency imports under 3.14 AND the test
      suite passes; otherwise not ready with the first failing dependency named.
    * Data Models -- ``DependencyCheck`` / ``PythonComponentMigration``.
    * Correctness Property 5: A Python component is migration-ready iff every
      dependency imports and its tests pass.

Requirements: 5.3, 5.4
"""

from __future__ import annotations

from collections.abc import Iterable

from hellodj_platform_logic.types import DependencyCheck

__all__ = [
    "TEST_SUITE_BLOCKER",
    "python_migration_ready",
]

#: The blocking-dependency identifier recorded when every runtime dependency
#: imported under Python 3.14 but the component's test suite did not pass
#: (R5.4). Using a sentinel here lets callers distinguish "a named dependency
#: failed to import" from "the test suite failed" while still surfacing a single
#: blocker string.
TEST_SUITE_BLOCKER = "test-suite"


def python_migration_ready(
    checks: Iterable[DependencyCheck],
    test_suite_passed: bool,
) -> tuple[bool, str | None]:
    """Decide whether a Python component may be marked migrated to Python 3.14.

    Implements Property 5 / R5.3, R5.4. A component is migration-ready **iff**
    every one of its runtime dependencies imported without error under Python
    3.14 *and* its existing test suite passed under Python 3.14:

    * **Ready.** When every :class:`~hellodj_platform_logic.types.DependencyCheck`
      reports ``imports_ok`` True and ``test_suite_passed`` is True, returns
      ``(True, None)`` -- the component may be marked migrated (R5.3).
    * **Blocked by a dependency.** When any dependency failed to import, returns
      ``(False, <name>)`` naming the **first** dependency (in the given order)
      whose ``imports_ok`` is False. A failing dependency blocks the migration
      regardless of the test-suite outcome, and the first such dependency is
      reported so the recorded blocker is deterministic (R5.4).
    * **Blocked by the test suite.** When every dependency imported but
      ``test_suite_passed`` is False, returns ``(False, TEST_SUITE_BLOCKER)`` --
      the component is not migrated and the recorded blocker names the failed
      test suite rather than any dependency (R5.4).

    The check order is significant only for which blocker is named: dependency
    import failures take precedence over a failed test suite, and among
    dependency failures the earliest in iteration order is named. The input is
    consumed exactly once.

    Args:
        checks: The per-dependency Python 3.14 import results for the component
            (cryptography, onnxruntime, torch, discord.py, wavelink, flask, ...
            where present). Iterated once, in order; may be empty (a component
            with no runtime dependencies is ready iff its test suite passes).
        test_suite_passed: Whether the component's existing test suite passed
            under Python 3.14.

    Returns:
        ``(ready, blocking_dependency)``: ``(True, None)`` when the component is
        migration-ready; otherwise ``(False, <blocker>)`` where ``<blocker>`` is
        the name of the first dependency that failed to import, or
        :data:`TEST_SUITE_BLOCKER` when every dependency imported but the test
        suite failed.

    Requirements: 5.3, 5.4
    """
    for check in checks:
        if not check.imports_ok:
            return (False, check.name)

    if not test_suite_passed:
        return (False, TEST_SUITE_BLOCKER)

    return (True, None)

"""Property-based test for the Python migration-readiness decision (task 5.2).

Feature: hellodj-private-source-and-toolchain, Property 5

Property 5 (A Python component is migration-ready iff every dependency imports
and its tests pass): *for any* ordered list of per-dependency Python 3.14 import
results and any test-suite outcome,
:func:`hellodj_platform_logic.python_migration.python_migration_ready` SHALL

* return ``(True, None)`` **iff** every ``DependencyCheck.imports_ok`` is True
  *and* the test suite passed (R5.3); otherwise
* return ``(False, blocker)`` where ``blocker`` names the **first** dependency
  whose ``imports_ok`` is False (dependency failures take precedence over the
  test suite), or :data:`TEST_SUITE_BLOCKER` (``"test-suite"``) when every
  dependency imported but the suite failed (R5.4).

Validates: Requirements 5.3, 5.4
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from hellodj_platform_logic.python_migration import (
    TEST_SUITE_BLOCKER,
    python_migration_ready,
)
from hellodj_platform_logic.types import DependencyCheck

# Dependency names. Kept to a small charset/size so datasets are cheap to
# generate; the decision function reasons purely over order + each check's
# ``imports_ok`` flag, never over the name text, so any non-empty label
# exercises the logic. A dedicated small pool of realistic names plus arbitrary
# short labels ensures both representative (cryptography, torch, …) and
# adversarial (collisions, odd characters) datasets are covered.
_DEP_NAMES = st.one_of(
    st.sampled_from(
        [
            "cryptography",
            "onnxruntime",
            "torch",
            "discord.py",
            "wavelink",
            "flask",
            "boto3",
            "aiohttp",
            "numpy",
            "gunicorn",
        ]
    ),
    st.text(
        alphabet=st.characters(min_codepoint=ord("a"), max_codepoint=ord("z")),
        min_size=1,
        max_size=8,
    ),
)

# One dependency check: a name plus whether it imported under Python 3.14. The
# empty-list case is included (min_size=0): a component with no runtime
# dependencies is ready iff its test suite passes.
_DEP_CHECKS = st.lists(
    st.builds(DependencyCheck, name=_DEP_NAMES, imports_ok=st.booleans()),
    min_size=0,
    max_size=12,
)


@settings(max_examples=200)
@given(checks=_DEP_CHECKS, test_suite_passed=st.booleans())
def test_migration_ready_iff_all_import_and_tests_pass(
    checks: list[DependencyCheck],
    test_suite_passed: bool,
) -> None:
    """Feature: hellodj-private-source-and-toolchain, Property 5.

    Validates: Requirements 5.3, 5.4
    """
    ready, blocker = python_migration_ready(checks, test_suite_passed)

    all_import = all(check.imports_ok for check in checks)
    # The first dependency (in order) that failed to import, if any.
    first_failing = next(
        (check.name for check in checks if not check.imports_ok),
        None,
    )

    # --- Migration-ready iff every dependency imports AND tests pass -------
    assert ready is (all_import and test_suite_passed)

    if ready:
        # Ready ⇒ (True, None): no blocker recorded, the component may be
        # marked migrated (R5.3).
        assert blocker is None
        return

    # --- Not ready: a single blocker string is recorded (R5.4) ------------
    assert blocker is not None

    if not all_import:
        # A failing dependency blocks the migration regardless of the
        # test-suite outcome, and the FIRST such dependency is named so the
        # recorded blocker is deterministic (dependency failures take
        # precedence over the suite).
        assert blocker == first_failing
    else:
        # Every dependency imported but the test suite failed: the recorded
        # blocker names the failed test suite rather than any dependency.
        assert test_suite_passed is False
        assert blocker == TEST_SUITE_BLOCKER

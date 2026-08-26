"""Property-based test for the fork-migration decision function (task 3.2).

Feature: hellodj-nix-native-delivery, Property 1

Property 1 (fork migration halts on first failure and leaves prior repos
unchanged): *for any* ordered fork list and any chosen failure point, the
migration SHALL process forks in order, halt at the first fork that cannot be
created or whose ``upstream`` remote cannot be established, record an error
naming exactly that fork, mark every prior fork migrated-and-unchanged (created
and upstream both ok, no error), and process no fork after the failure -- so the
returned list is a prefix of the input ending at the failing fork.

Validates: Requirements 1.6
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from hellodj_platform_logic.migration import ForkOutcome, migrate_forks
from hellodj_platform_logic.types import ForkMigration

# Fork repository names. Kept to a small charset/size so datasets are cheap to
# generate; the decision function reasons purely over order + per-fork outcome,
# never over the name text, so any non-empty label exercises the logic.
_FORK_NAMES = st.text(
    alphabet=st.characters(
        min_codepoint=ord("a"),
        max_codepoint=ord("z"),
    ),
    min_size=1,
    max_size=8,
)

# The two distinct failure modes a fork can hit: it could not be created
# (created=False) or its upstream remote could not be established
# (created=True, upstream_remote_ok=False). Both must halt migration (R1.6).
_FAILURE_OUTCOMES: list[ForkOutcome] = [(False, True), (True, False), (False, False)]


@st.composite
def fork_scenarios(
    draw: st.DrawFn,
) -> tuple[list[str], int | None, ForkOutcome]:
    """Generate a fork list plus an optional failure index and failure mode.

    Returns ``(forks, failure_index, failure_outcome)`` where:

    * ``forks`` is a non-empty ordered list of fork names,
    * ``failure_index`` is either ``None`` (every fork migrates successfully) or
      a valid index into ``forks`` at which the attempt fails,
    * ``failure_outcome`` is the ``(created, upstream_remote_ok)`` returned at
      the failing index (only meaningful when ``failure_index`` is not None).

    This spans the full space the property quantifies over: all-success runs and
    runs that fail at any position under any of the distinct failure modes.
    """
    forks = draw(st.lists(_FORK_NAMES, min_size=1, max_size=8))
    failure_index = draw(
        st.one_of(
            st.none(),
            st.integers(min_value=0, max_value=len(forks) - 1),
        )
    )
    failure_outcome = draw(st.sampled_from(_FAILURE_OUTCOMES))
    return forks, failure_index, failure_outcome


@settings(max_examples=200)
@given(scenario=fork_scenarios())
def test_migration_halts_on_first_failure(
    scenario: tuple[list[str], int | None, ForkOutcome],
) -> None:
    """Feature: hellodj-nix-native-delivery, Property 1.

    Validates: Requirements 1.6
    """
    forks, failure_index, failure_outcome = scenario

    # Track which forks the callback was invoked for, in order, so we can assert
    # no fork after the failure is ever processed.
    processed: list[str] = []

    def attempt(repo: str) -> ForkOutcome:
        processed.append(repo)
        # The failing index (if any) returns the drawn failure outcome; every
        # other fork migrates cleanly.
        current_index = len(processed) - 1
        if failure_index is not None and current_index == failure_index:
            return failure_outcome
        return (True, True)

    result = migrate_forks(forks, attempt)

    # --- The result is a prefix of the input forks ------------------------
    # On full success it has one record per fork; on failure it ends at the
    # failing fork. In every case the repos appear in input order.
    assert [migration.repo for migration in result] == forks[: len(result)]
    assert all(isinstance(migration, ForkMigration) for migration in result)

    if failure_index is None:
        # --- All forks migrate successfully -------------------------------
        # Every fork is processed once, in order, and recorded as
        # migrated-and-unchanged with no error.
        assert len(result) == len(forks)
        assert processed == forks
        for migration in result:
            assert migration.created is True
            assert migration.upstream_remote_ok is True
            assert migration.error == ""
        return

    # --- Migration fails at failure_index ---------------------------------
    # The returned list ends exactly at the failing fork: prior forks + the one
    # that failed, and nothing after it (R1.6).
    assert len(result) == failure_index + 1

    # No fork after the failure is processed: the callback was invoked only for
    # the prefix up to and including the failing fork.
    assert processed == forks[: failure_index + 1]

    # --- Prior forks are migrated-and-unchanged ---------------------------
    for migration in result[:failure_index]:
        assert migration.created is True
        assert migration.upstream_remote_ok is True
        assert migration.error == ""

    # --- The failing fork carries an error naming exactly it --------------
    failing = result[failure_index]
    created, upstream_remote_ok = failure_outcome
    assert failing.repo == forks[failure_index]
    assert failing.created is created
    assert failing.upstream_remote_ok is upstream_remote_ok
    assert failing.error != ""
    # The error names exactly the failing fork and no other fork before it (the
    # repr of the fork name appears; names are drawn from a small alphabet).
    assert repr(forks[failure_index]) in failing.error

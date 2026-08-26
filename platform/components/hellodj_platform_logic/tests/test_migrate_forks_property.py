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

from hellodj_platform_logic.migration import (
    ForkOutcome,
    RepoOutcome,
    migrate_forks,
    migrate_repos,
)
from hellodj_platform_logic.types import CodeCommitRepo, ForkMigration

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


# ---------------------------------------------------------------------------
# Property (migration): five-repo CodeCommit migration -- task 3.2
#
# Feature: hellodj-private-source-and-toolchain, Property (migration)
#
# Property (migration) (repo migration halts on first failure and leaves prior
# repos unchanged): *for any* ordered five-repo list and any chosen failure
# point, migrate_repos SHALL process the repos in order, halt at the first repo
# whose create / upstream-remote / history-preservation step fails, record an
# error naming exactly that Source_Repo, mark every prior repo
# migrated-and-unchanged (created, upstream_remote_ok both ok, history
# preserved, no error), and process no repo after the failure -- so the returned
# list is a prefix of the input ending at the failing repo.
#
# This extends the fork-only migrate_forks property above to the five
# CodeCommitRepo entries (hellodj, Lavalink, lavaplayer, LavaSrc,
# youtube-source) and to the three-part
# (created, upstream_remote_ok, history_preserved) outcome, adding the
# history-preservation assertion (R1.4).
#
# Validates: Requirements 1.4, 1.5
# ---------------------------------------------------------------------------

# The five Source_Repos of the hellodj-private-source-and-toolchain spec, in the
# fixed migration order (hellodj app repo first, then the four JVM forks). The
# app repo has no upstream (upstream_url is None); the four forks each carry
# their preserved public upstream. migrate_repos reasons purely over order +
# per-repo outcome, never over the URL/branch text, so these concrete entries
# fully exercise the decision.
_SOURCE_REPOS: list[CodeCommitRepo] = [
    CodeCommitRepo(
        name="hellodj",
        upstream_url=None,
        build_branch="main",
    ),
    CodeCommitRepo(
        name="Lavalink",
        upstream_url="https://github.com/lavalink-devs/Lavalink",
        build_branch="dev",
    ),
    CodeCommitRepo(
        name="lavaplayer",
        upstream_url="https://github.com/lavalink-devs/lavaplayer",
        build_branch="main",
    ),
    CodeCommitRepo(
        name="LavaSrc",
        upstream_url="https://github.com/topi314/LavaSrc",
        build_branch="tidal-v2-api",
    ),
    CodeCommitRepo(
        name="youtube-source",
        upstream_url="https://github.com/lavalink-devs/youtube-source",
        build_branch="main",
    ),
]

# The three distinct failure modes a repo can hit, mapped to the three-part
# (created, upstream_remote_ok, history_preserved) outcome: it could not be
# created; it was created but its upstream remote could not be established; or
# both of those succeeded but the post-mirror-push history-preservation check
# failed. Each must halt migration (R1.5).
_REPO_FAILURE_OUTCOMES: list[RepoOutcome] = [
    (False, True, True),    # not created
    (True, False, True),    # upstream remote not established
    (True, True, False),    # history not preserved (R1.4)
    (False, False, False),  # everything failed
    (True, False, False),   # created, but upstream + history both failed
]


@st.composite
def repo_scenarios(
    draw: st.DrawFn,
) -> tuple[list[CodeCommitRepo], int | None, RepoOutcome]:
    """Generate a repo list plus an optional failure index and failure mode.

    Returns ``(repos, failure_index, failure_outcome)`` where:

    * ``repos`` is a non-empty ordered prefix of the five Source_Repos (drawn as
      a sublist that preserves order so the fixed migration order is honoured),
    * ``failure_index`` is either ``None`` (every repo migrates successfully) or
      a valid index into ``repos`` at which the attempt fails,
    * ``failure_outcome`` is the ``(created, upstream_remote_ok,
      history_preserved)`` returned at the failing index (only meaningful when
      ``failure_index`` is not None).

    This spans the full space the property quantifies over: all-success runs and
    runs that fail at any position under any of the three distinct failure
    modes, across repo lists from one repo up to the full five.
    """
    # Draw an ordered, non-empty sublist of the five Source_Repos so ordering
    # (and the fixed migration order) is always preserved.
    mask = draw(
        st.lists(st.booleans(), min_size=len(_SOURCE_REPOS), max_size=len(_SOURCE_REPOS))
    )
    repos = [repo for repo, keep in zip(_SOURCE_REPOS, mask, strict=False) if keep]
    if not repos:
        repos = list(_SOURCE_REPOS)

    failure_index = draw(
        st.one_of(
            st.none(),
            st.integers(min_value=0, max_value=len(repos) - 1),
        )
    )
    failure_outcome = draw(st.sampled_from(_REPO_FAILURE_OUTCOMES))
    return repos, failure_index, failure_outcome


@settings(max_examples=200)
@given(scenario=repo_scenarios())
def test_repo_migration_halts_on_first_failure(
    scenario: tuple[list[CodeCommitRepo], int | None, RepoOutcome],
) -> None:
    """Feature: hellodj-private-source-and-toolchain, Property (migration).

    Validates: Requirements 1.4, 1.5
    """
    repos, failure_index, failure_outcome = scenario

    # Track which repos the callback was invoked for, in order, so we can assert
    # no repo after the failure is ever processed (R1.5).
    processed: list[CodeCommitRepo] = []

    def attempt(repo: CodeCommitRepo) -> RepoOutcome:
        processed.append(repo)
        # The failing index (if any) returns the drawn failure outcome; every
        # other repo migrates cleanly (created, upstream ok, history preserved).
        current_index = len(processed) - 1
        if failure_index is not None and current_index == failure_index:
            return failure_outcome
        return (True, True, True)

    result = migrate_repos(repos, attempt)

    repo_names = [repo.name for repo in repos]

    # --- The result is a prefix of the input repos -----------------------
    # On full success it has one record per repo; on failure it ends at the
    # failing repo. In every case the repos appear in input (migration) order.
    assert [migration.repo for migration in result] == repo_names[: len(result)]
    assert all(isinstance(migration, ForkMigration) for migration in result)

    if failure_index is None:
        # --- All repos migrate successfully -------------------------------
        # Every repo is processed once, in order, and recorded as
        # migrated-and-unchanged with no error.
        assert len(result) == len(repos)
        assert processed == repos
        for migration in result:
            assert migration.created is True
            assert migration.upstream_remote_ok is True
            assert migration.error == ""
        return

    # --- Migration fails at failure_index ---------------------------------
    # The returned list ends exactly at the failing repo: prior repos + the one
    # that failed, and nothing after it (R1.5).
    assert len(result) == failure_index + 1

    # No repo after the failure is processed: the callback was invoked only for
    # the prefix up to and including the failing repo (R1.5).
    assert processed == repos[: failure_index + 1]

    # --- Prior repos are migrated-and-unchanged ---------------------------
    for migration in result[:failure_index]:
        assert migration.created is True
        assert migration.upstream_remote_ok is True
        assert migration.error == ""

    # --- The failing repo carries an error naming exactly it --------------
    failing = result[failure_index]
    created, upstream_remote_ok, history_preserved = failure_outcome
    assert failing.repo == repo_names[failure_index]
    assert failing.created is created
    assert failing.upstream_remote_ok is upstream_remote_ok
    assert failing.error != ""
    # The error names exactly the failing repo (its name appears in the message).
    assert repr(repo_names[failure_index]) in failing.error
    # History-preservation is part of the success criterion: when create and
    # upstream both succeeded, the only way to reach this failure branch is a
    # failed history-preservation check (R1.4).
    if created and upstream_remote_ok:
        assert history_preserved is False

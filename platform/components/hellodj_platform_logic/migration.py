"""Clean-slate migration filter for the AWS re-platform.

This module holds the pure decision function that the one-time legacy-to-AWS
migration invokes to decide which legacy records are carried forward. Per the
clean-slate policy (R19), the AWS platform begins fresh: the *only* data
migrated from the legacy platform is the ``Admin_Bootstrap_Credential`` that
lets the Platform_Owner log in as the administrator for the first time through
Cognito. All legacy playback, session, playlist and configuration data is
excluded and initialized anew on AWS.

It is imported by both the CDK infrastructure layer (the migration step) and any
runtime migration tooling so they share a single source of truth, and it makes
no live AWS calls so the correctness property can exercise it directly.

Implemented here:

* :func:`filter_legacy` -- Property 12 / R19.1, R19.2, R19.4. Given an arbitrary
  mix of legacy records, return only the admin bootstrap credential record(s)
  present in the input and exclude every playback, session, playlist and
  configuration record.

* :func:`migrate_forks` -- Property 1 / R1.6. Given the ordered list of forks to
  migrate into the ``hellodj`` account, process them in order and halt at the
  first fork that cannot be created or whose ``upstream`` remote cannot be
  established, recording an error naming exactly that fork, marking every prior
  fork migrated-and-unchanged, and processing no fork after the failure. This is
  the same "process in order, halt on first failure, leave prior state
  untouched" shape as the promotion controller (:mod:`hellodj_platform_logic.
  promotion`); the per-fork create/remote outcome is injected so the pure
  decision can be exercised directly by the correctness property.

Design references:
    * Auth/identity mapping: the ``Admin_Bootstrap_Credential`` seeds the first
      admin in the Cognito user pool (R19.3).
    * Correctness Property 12: Clean-slate migration filter.
    * Components -- Fork repositories: "Migration procedure (transactional,
      R1.6)" -- process the four repos in a fixed list, halt at the first repo
      that cannot be created or whose ``upstream`` remote cannot be established,
      report an error naming the affected ``Fork_Repo``, and leave the
      already-migrated repos unchanged.
    * Correctness Property 1: Fork migration halts on first failure and leaves
      prior repos unchanged.

Requirements: 1.6, 19.1, 19.2, 19.4
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence

from hellodj_platform_logic.types import (
    CodeCommitRepo,
    ForkMigration,
    LegacyRecord,
    LegacyRecordType,
)

__all__ = [
    "EXCLUDED_LEGACY_RECORD_TYPES",
    "MIGRATED_LEGACY_RECORD_TYPE",
    "ForkOutcome",
    "RepoOutcome",
    "filter_legacy",
    "migrate_forks",
    "migrate_repos",
]

#: A per-fork migration attempt outcome: ``(created, upstream_remote_ok)``.
#:
#: ``created`` is whether the repository was created under the ``hellodj``
#: account; ``upstream_remote_ok`` is whether its ``upstream`` remote was
#: established and resolved. A fork migrates successfully only when *both* are
#: ``True`` (R1.2/R1.6). The outcome is injected via the ``attempt`` callback so
#: :func:`migrate_forks` stays pure and directly testable -- it performs no git
#: or network calls itself.
ForkOutcome = tuple[bool, bool]

#: A per-repo migration attempt outcome for the five-repo CodeCommit migration:
#: ``(created, upstream_remote_ok, history_preserved)``.
#:
#: This extends :data:`ForkOutcome` with the history-preservation assertion
#: (R1.4): ``created`` is whether the CodeCommit repository was created;
#: ``upstream_remote_ok`` is whether its ``upstream`` remote was established and
#: ``git fetch upstream`` succeeded (for the ``hellodj`` app repo, which has no
#: upstream, the injected callback reports ``True``); ``history_preserved`` is
#: whether the post-``git push --mirror`` verification confirmed that each
#: branch tip SHA, the set of ancestor SHAs reachable from each tip, and the set
#: of branch and tag names all equal the pre-migration source. A repo migrates
#: successfully only when *all three* are ``True`` (R1.4). The outcome is
#: injected via the ``attempt`` callback so :func:`migrate_repos` stays pure and
#: directly testable -- it performs no git or network calls itself.
RepoOutcome = tuple[bool, bool, bool]

#: The single legacy record type carried forward by the clean-slate migration
#: (R19.1): the admin bootstrap credential that seeds the Platform_Owner's first
#: administrator login through Cognito.
MIGRATED_LEGACY_RECORD_TYPE: LegacyRecordType = (
    LegacyRecordType.ADMIN_BOOTSTRAP_CREDENTIAL
)

#: Every legacy record type explicitly excluded from the migration (R19.2,
#: R19.4). These are initialized fresh on AWS rather than carried over. This is
#: exactly the set of all legacy record types other than the migrated one, so
#: the filter's include/exclude partition is total over
#: :class:`~hellodj_platform_logic.types.LegacyRecordType`.
EXCLUDED_LEGACY_RECORD_TYPES: frozenset[LegacyRecordType] = frozenset(
    record_type
    for record_type in LegacyRecordType
    if record_type is not MIGRATED_LEGACY_RECORD_TYPE
)


def filter_legacy(records: Iterable[LegacyRecord]) -> list[LegacyRecord]:
    """Return only the admin bootstrap credential records from a legacy dataset.

    Implements Property 12 / R19. The AWS platform starts clean: only the
    ``Admin_Bootstrap_Credential`` is migrated (R19.1) and all other data --
    legacy playback, session, playlist and configuration records -- is excluded
    so it can be initialized fresh on AWS (R19.2, R19.4).

    The input order of the surviving records is preserved, and any admin
    bootstrap credential present in the input is retained (including the case of
    more than one). If the input contains no admin bootstrap credential the
    result is empty.

    Args:
        records: An arbitrary iterable of legacy records mixing all record
            types. The iterable is consumed exactly once.

    Returns:
        A list containing exactly the input records whose ``record_type`` is
        :data:`MIGRATED_LEGACY_RECORD_TYPE`
        (:attr:`~hellodj_platform_logic.types.LegacyRecordType.ADMIN_BOOTSTRAP_CREDENTIAL`),
        in their original relative order, with every excluded record type
        (:data:`EXCLUDED_LEGACY_RECORD_TYPES`) omitted.
    """
    return [
        record
        for record in records
        if record.record_type is MIGRATED_LEGACY_RECORD_TYPE
    ]


def migrate_forks(
    forks: Sequence[str],
    attempt: Callable[[str], ForkOutcome],
) -> list[ForkMigration]:
    """Migrate the forks into the ``hellodj`` account, halting on first failure.

    Implements Property 1 / R1.6. The forks are processed in the given order.
    For each fork the ``attempt`` callback is invoked to (attempt to) create the
    repository under the ``hellodj`` account and establish its ``upstream``
    remote, returning ``(created, upstream_remote_ok)``. A fork is considered
    migrated only when it was both created *and* its ``upstream`` remote was
    established (R1.2).

    Processing follows the same transactional shape as the promotion controller
    (process in order, halt on first failure, leave prior state untouched):

    * **Prior forks unchanged.** Every fork before the first failure is recorded
      as migrated (``created`` and ``upstream_remote_ok`` both ``True``) with no
      error, and nothing about those already-migrated repos is altered.
    * **Halt at the first failure.** The first fork that cannot be created or
      whose ``upstream`` remote cannot be established is recorded with an
      ``error`` naming exactly that fork, and promotion of the remaining forks
      stops immediately.
    * **No fork processed after the failure.** Forks after the failing one are
      never attempted and do not appear in the result, so the returned list ends
      at the failing fork.

    Consequently the returned list is a prefix of ``forks``: on full success it
    has one :class:`~hellodj_platform_logic.types.ForkMigration` per input fork,
    all successful; on failure it contains the successful prefix followed by the
    single failing fork and nothing more. The ``attempt`` callback is invoked at
    most once per fork and is never invoked for any fork after the failure.

    Args:
        forks: The ordered fork repository names to migrate (for example
            ``["Lavalink", "lavaplayer", "LavaSrc", "youtube-source"]``). The
            sequence is processed left to right and may be empty (yielding an
            empty result).
        attempt: A callback that, given a fork name, attempts its migration and
            returns ``(created, upstream_remote_ok)``. It is the injection point
            for the git/GitHub side effects so this function stays pure; it is
            called at most once per fork, in order, and not at all once a failure
            has occurred.

    Returns:
        A list of :class:`~hellodj_platform_logic.types.ForkMigration` records, a
        prefix of ``forks``: the migrated-and-unchanged forks in order, then
        (if any) the single fork that failed with a populated ``error`` naming
        it, and no records for forks after the failure.

    Requirements: 1.6
    """
    results: list[ForkMigration] = []

    for repo in forks:
        created, upstream_remote_ok = attempt(repo)

        if created and upstream_remote_ok:
            # Migrated successfully: recorded as-is, left unchanged (R1.6).
            results.append(
                ForkMigration(
                    repo=repo,
                    created=True,
                    upstream_remote_ok=True,
                )
            )
            continue

        # First failure: record it with an error naming exactly this fork and
        # stop -- no later fork is processed and prior forks are untouched
        # (R1.6).
        results.append(
            ForkMigration(
                repo=repo,
                created=created,
                upstream_remote_ok=upstream_remote_ok,
                error=_migration_error(repo, created, upstream_remote_ok),
            )
        )
        break

    return results


def _migration_error(repo: str, created: bool, upstream_remote_ok: bool) -> str:
    """Build the failure message naming exactly the affected fork (R1.6).

    Args:
        repo: The fork repository that failed to migrate.
        created: Whether the repository was created under the ``hellodj``
            account.
        upstream_remote_ok: Whether the ``upstream`` remote was established and
            resolved.

    Returns:
        A non-empty message identifying ``repo`` and the specific failure (the
        repo could not be created, or its ``upstream`` remote could not be
        established).
    """
    if not created:
        return (
            f"fork {repo!r} could not be created under the hellodj account; "
            "migration halted, already-migrated forks left unchanged"
        )
    return (
        f"fork {repo!r} upstream remote could not be established; "
        "migration halted, already-migrated forks left unchanged"
    )


def migrate_repos(
    repos: Sequence[CodeCommitRepo],
    attempt: Callable[[CodeCommitRepo], RepoOutcome],
) -> list[ForkMigration]:
    """Migrate the five Source_Repos into CodeCommit, halting on first failure.

    Implements Property (migration) / R1.4, R1.5. This is the five-repo
    CodeCommit extension of :func:`migrate_forks`: it shares the same "process
    in order, halt on first failure, leave prior state untouched" transactional
    shape, extended from the fork-only ``(created, upstream_remote_ok)`` outcome
    to the full ``(created, upstream_remote_ok, history_preserved)`` outcome so
    the post-``git push --mirror`` history-preservation assertion (R1.4) is part
    of the per-repo success criterion.

    The repos are processed in the given fixed order (the spec's order is
    ``hellodj``, ``Lavalink``, ``lavaplayer``, ``LavaSrc``, ``youtube-source``).
    For each repo the ``attempt`` callback is invoked to (attempt to) create the
    CodeCommit repository, establish its ``upstream`` remote (verifying
    ``git fetch upstream`` for the four forks; the ``hellodj`` app repo has no
    upstream so the callback reports it satisfied), mirror-push the full history,
    and verify preservation, returning
    ``(created, upstream_remote_ok, history_preserved)``. A repo is considered
    migrated only when all three are ``True`` (R1.4).

    Processing follows the same transactional shape as :func:`migrate_forks`:

    * **Prior repos unchanged.** Every repo before the first failure is recorded
      as migrated (``created`` and ``upstream_remote_ok`` both ``True``, and its
      history preserved) with no error, and nothing about those already-migrated
      repos is altered (R1.5).
    * **Halt at the first failure.** The first repo that cannot be created, whose
      ``upstream`` remote cannot be established, or whose history-preservation
      check fails is recorded with an ``error`` naming exactly that Source_Repo,
      and migration of the remaining repos stops immediately (R1.5). Because the
      mirror push is all-or-nothing per repo, a failed push leaves no partial ref
      set on CodeCommit.
    * **No repo processed after the failure.** Repos after the failing one are
      never attempted and do not appear in the result, so the returned list ends
      at the failing repo (R1.5).

    Consequently the returned list is a prefix of ``repos``: on full success it
    has one :class:`~hellodj_platform_logic.types.ForkMigration` per input repo,
    all successful; on failure it contains the successful prefix followed by the
    single failing repo and nothing more. The ``attempt`` callback is invoked at
    most once per repo and is never invoked for any repo after the failure.

    The result reuses :class:`~hellodj_platform_logic.types.ForkMigration` (its
    ``repo``/``created``/``upstream_remote_ok``/``error`` fields carry over
    unchanged); the history-preservation outcome contributes to the
    success/failure decision and the failure message but is not a new result
    field.

    Args:
        repos: The ordered :class:`~hellodj_platform_logic.types.CodeCommitRepo`
            entries to migrate. The sequence is processed left to right and may
            be empty (yielding an empty result).
        attempt: A callback that, given a
            :class:`~hellodj_platform_logic.types.CodeCommitRepo`, attempts its
            migration and returns
            ``(created, upstream_remote_ok, history_preserved)``. It is the
            injection point for the git/CodeCommit side effects so this function
            stays pure; it is called at most once per repo, in order, and not at
            all once a failure has occurred.

    Returns:
        A list of :class:`~hellodj_platform_logic.types.ForkMigration` records, a
        prefix of ``repos``: the migrated-and-unchanged repos in order, then
        (if any) the single repo that failed with a populated ``error`` naming
        it, and no records for repos after the failure.

    Requirements: 1.4, 1.5
    """
    results: list[ForkMigration] = []

    for repo in repos:
        created, upstream_remote_ok, history_preserved = attempt(repo)

        if created and upstream_remote_ok and history_preserved:
            # Migrated successfully: recorded as-is, left unchanged (R1.5).
            results.append(
                ForkMigration(
                    repo=repo.name,
                    created=True,
                    upstream_remote_ok=True,
                )
            )
            continue

        # First failure: record it with an error naming exactly this repo and
        # stop -- no later repo is processed and prior repos are untouched
        # (R1.5).
        results.append(
            ForkMigration(
                repo=repo.name,
                created=created,
                upstream_remote_ok=upstream_remote_ok,
                error=_repo_migration_error(
                    repo.name,
                    created,
                    upstream_remote_ok,
                    history_preserved,
                ),
            )
        )
        break

    return results


def _repo_migration_error(
    repo: str,
    created: bool,
    upstream_remote_ok: bool,
    history_preserved: bool,
) -> str:
    """Build the failure message naming exactly the affected Source_Repo (R1.5).

    The three failure modes are checked in the same order the migration
    procedure performs them -- create, establish/verify ``upstream``, then
    mirror-push and verify history preservation -- so the message names the
    earliest step that failed.

    Args:
        repo: The Source_Repo that failed to migrate.
        created: Whether the CodeCommit repository was created.
        upstream_remote_ok: Whether the ``upstream`` remote was established and
            ``git fetch upstream`` succeeded.
        history_preserved: Whether the post-mirror-push history-preservation
            check confirmed the branch tips, ancestor SHA sets, and branch/tag
            names all equal the pre-migration source (R1.4).

    Returns:
        A non-empty message identifying ``repo`` and the specific failure (the
        repo could not be created, its ``upstream`` remote could not be
        established, or its history was not preserved).
    """
    if not created:
        return (
            f"repo {repo!r} could not be created on CodeCommit; "
            "migration halted, already-migrated repos left unchanged"
        )
    if not upstream_remote_ok:
        return (
            f"repo {repo!r} upstream remote could not be established; "
            "migration halted, already-migrated repos left unchanged"
        )
    return (
        f"repo {repo!r} history could not be preserved on mirror push; "
        "migration halted, already-migrated repos left unchanged"
    )

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
    ForkMigration,
    LegacyRecord,
    LegacyRecordType,
)

__all__ = [
    "EXCLUDED_LEGACY_RECORD_TYPES",
    "MIGRATED_LEGACY_RECORD_TYPE",
    "ForkOutcome",
    "filter_legacy",
    "migrate_forks",
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

"""Migration types: legacy records, fork migration, CodeCommit repos, Python readiness.

Types for clean-slate migration filtering (R19), fork migration outcomes (R1),
CodeCommit repo identity (R1/R2), GPU idle config (R8), and Python component
migration readiness (R5).

Requirements: 1.x, 5.x, 8.x, 19.x
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

__all__ = [
    "LegacyRecordType",
    "LegacyRecord",
    "ForkMigration",
    "CodeCommitRepo",
    "GpuIdleConfig",
    "DependencyCheck",
    "PythonComponentMigration",
]


# ---------------------------------------------------------------------------
# Clean-slate migration filter (Property 12, R19)
# ---------------------------------------------------------------------------


class LegacyRecordType(Enum):
    """Kind discriminator for a record in the legacy platform dataset.

    The clean-slate migration (R19) carries exactly one kind of record forward:
    the :attr:`ADMIN_BOOTSTRAP_CREDENTIAL`, which seeds the Platform_Owner's
    first administrator login through Cognito (R19.1, R19.3). Every other kind
    -- legacy playback, session, playlist and configuration data -- is excluded
    from the migration and initialized fresh on AWS (R19.2, R19.4).
    """

    ADMIN_BOOTSTRAP_CREDENTIAL = "admin_bootstrap_credential"
    PLAYBACK = "playback"
    SESSION = "session"
    PLAYLIST = "playlist"
    CONFIGURATION = "configuration"


@dataclass(frozen=True)
class LegacyRecord:
    """A single record drawn from the legacy platform dataset (Property 12).

    ``record_type`` is the kind discriminator the migration filter keys on;
    ``record_id`` identifies the record within its kind and ``payload`` carries
    the opaque legacy data. The migration filter reasons purely over
    ``record_type`` -- only :attr:`LegacyRecordType.ADMIN_BOOTSTRAP_CREDENTIAL`
    records survive the migration (R19.1, R19.4).
    """

    record_type: LegacyRecordType
    record_id: str = ""
    payload: str = ""


# ---------------------------------------------------------------------------
# Fork migration (Property 1, R1)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ForkMigration:
    """One fork's migration outcome (R1.6)."""

    repo: str                        # Lavalink / lavaplayer / LavaSrc / youtube-source
    created: bool
    upstream_remote_ok: bool
    error: str = ""                  # names the affected fork on failure (R1.6)


@dataclass(frozen=True)
class CodeCommitRepo:
    """One migrated Source_Repo's identity + preserved upstream.

    Models one of the five private CodeCommit Source_Repos the
    ``hellodj-private-source-and-toolchain`` spec relocates off public GitHub
    (R1.1/R1.2/R1.3): the ``hellodj`` application repo plus the four JVM forks
    (``Lavalink``, ``lavaplayer``, ``LavaSrc``, ``youtube-source``). It carries
    the repository's name, its preserved public ``upstream`` remote URL (``None``
    for the app repo, which has no upstream), and its designated build branch
    (for example ``Lavalink`` -> ``"dev"``).

    Consumed by :func:`hellodj_platform_logic.migration.migrate_repos` as the
    ordered unit of the transactional five-repo migration.
    """

    name: str                        # hellodj / Lavalink / lavaplayer / LavaSrc / youtube-source
    upstream_url: str | None         # public upstream for the 4 forks; None for the app repo
    build_branch: str                # e.g. Lavalink -> "dev"


# ---------------------------------------------------------------------------
# GPU idle decision (Property 8, R8)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GpuIdleConfig:
    """Idle-window config for GPU scale-to-zero (R8.5)."""

    idle_window_seconds: float = 300.0   # default; valid range [60, 900]

    def __post_init__(self) -> None:
        if not (60.0 <= self.idle_window_seconds <= 900.0):
            raise ValueError("idle window must be within 60-900 seconds (R8.5)")


# ---------------------------------------------------------------------------
# Python component migration readiness (Property 5, R5)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DependencyCheck:
    """One runtime dependency's Python 3.14 verification result (R5.3/R5.4).

    ``imports_ok`` records whether the named runtime dependency (for example
    ``cryptography``, ``onnxruntime``, ``torch``, ``discord.py``, ``wavelink``
    or ``flask``) imported without error under Python 3.14. A component is only
    migration-ready when every one of its dependency checks reports
    ``imports_ok`` (and its test suite passes); the first check with
    ``imports_ok`` False is the blocking dependency named in the verdict
    (R5.4).
    """

    name: str            # cryptography / onnxruntime / torch / discord.py / …
    imports_ok: bool     # imported without error under Python 3.14


@dataclass(frozen=True)
class PythonComponentMigration:
    """A component's migration-readiness inputs and verdict (R5.2-R5.6).

    ``dependency_checks`` are the per-dependency Python 3.14 import results and
    ``test_suite_passed`` is whether the component's existing test suite passed
    under Python 3.14. ``migrated`` is True only when the component is ready --
    every dependency imports *and* the test suite passes -- as derived by
    :func:`hellodj_platform_logic.python_migration.python_migration_ready`. When
    not ready, ``blocking_dependency`` names the first failing dependency, or
    the test suite when every dependency imported but the suite did not pass
    (R5.4).
    """

    component: str
    dependency_checks: tuple[DependencyCheck, ...]
    test_suite_passed: bool
    migrated: bool = False
    blocking_dependency: str | None = None

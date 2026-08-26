"""Orchestration of the one-time clean-slate admin-bootstrap migration.

Wires the injectable pieces into the end-to-end migration flow (R19):

1. Load the legacy export from the injected :class:`LegacySource`.
2. Run the shared pure decision function
   :func:`hellodj_platform_logic.migration.filter_legacy` to keep **only** the
   ``Admin_Bootstrap_Credential`` records (R19.1, R19.2, R19.4).
3. Seed that single credential into the Cognito user pool via
   :class:`CognitoAdminSeeder` so the Platform_Owner can log in as admin for the
   first time (R19.3).
4. Run the :class:`FreshDataInitializer` fresh-start step, which writes **no**
   legacy playback/session/playlist/config data (R19.4).

The class holds no AWS dependencies of its own — every collaborator is injected
— so the whole flow is unit-testable without live AWS.

Requirements: 19.1, 19.2, 19.3, 19.4
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from hellodj_platform_logic.migration import filter_legacy

from .cognito_seeder import AdminBootstrapCredential, CognitoAdminSeeder
from .fresh_init import FreshDataInitializer
from .legacy_source import LegacySource

__all__ = ["MigrationJob", "MigrationResult"]

log = logging.getLogger("migration_job")


@dataclass(frozen=True)
class MigrationResult:
    """Summary of a completed migration run.

    Attributes:
        legacy_record_count: Total records read from the legacy export.
        seeded_usernames: Usernames of admin bootstrap credentials seeded into
            Cognito (normally exactly one).
        fresh_tables_verified: Fresh DynamoDB tables verified reachable (empty
            when the fresh-init step ran as a no-op).
    """

    legacy_record_count: int
    seeded_usernames: tuple[str, ...] = ()
    fresh_tables_verified: tuple[str, ...] = field(default_factory=tuple)


class MigrationJob:
    """Runs the clean-slate admin-bootstrap migration end to end.

    Args:
        legacy_source: Source of the legacy export records.
        seeder: Cognito seeder for the admin bootstrap credential.
        fresh_initializer: Fresh-start initializer for all non-migrated data.
            Defaults to a documented no-op :class:`FreshDataInitializer`.
    """

    def __init__(
        self,
        legacy_source: LegacySource,
        seeder: CognitoAdminSeeder,
        *,
        fresh_initializer: FreshDataInitializer | None = None,
    ) -> None:
        self._legacy_source = legacy_source
        self._seeder = seeder
        self._fresh_initializer = fresh_initializer or FreshDataInitializer()

    def run(self) -> MigrationResult:
        """Execute the migration and return a summary result.

        Returns:
            A :class:`MigrationResult` describing what was migrated and verified.
        """
        records = self._legacy_source.load()
        log.info("loaded %d legacy record(s) from export", len(records))

        # Clean-slate filter: only the admin bootstrap credential survives
        # (R19.1, R19.2, R19.4).
        migrated = filter_legacy(records)
        log.info(
            "migration filter kept %d admin bootstrap credential record(s); "
            "excluded %d other legacy record(s)",
            len(migrated),
            len(records) - len(migrated),
        )

        seeded: list[str] = []
        for record in migrated:
            credential = AdminBootstrapCredential.from_record(record)
            self._seeder.seed(credential)
            seeded.append(credential.username)
            log.info("seeded admin bootstrap credential %r into Cognito", credential.username)

        if not seeded:
            log.warning(
                "no admin bootstrap credential present in the legacy export; "
                "no Cognito user was seeded"
            )

        # Fresh start: all other data begins new on AWS (R19.2, R19.4).
        verified = self._fresh_initializer.initialize()
        if verified:
            log.info("verified fresh DynamoDB tables: %s", ", ".join(verified))
        else:
            log.info(
                "fresh-init: no legacy playback/session/playlist/config data "
                "migrated; all data starts fresh on AWS"
            )

        return MigrationResult(
            legacy_record_count=len(records),
            seeded_usernames=tuple(seeded),
            fresh_tables_verified=tuple(verified),
        )

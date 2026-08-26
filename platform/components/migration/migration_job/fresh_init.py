"""Fresh initialization of all non-migrated data on AWS.

Under the clean-slate policy (R19.2, R19.4) every kind of legacy data other than
the ``Admin_Bootstrap_Credential`` — playback, session, playlist and
configuration — is **not** carried forward. The AWS DynamoDB tables therefore
start empty and each entity is created anew as the platform runs.

Concretely this means the migration Job does **not** write any legacy
playback/session/playlist/config rows into DynamoDB. There is no data to seed;
"fresh init" is the deliberate *absence* of a legacy playback. This module makes
that intent explicit and auditable: it verifies (best-effort) that the target
DynamoDB tables exist and are reachable so the freshly-provisioned platform is
ready to accept new data, and it records which legacy record types were
intentionally excluded.

The DynamoDB resource is injectable and used only for a lightweight existence
probe; if none is supplied the step is a documented no-op. ``boto3`` is imported
lazily so the module imports without AWS libraries present.

Requirements: 19.2, 19.4
"""

from __future__ import annotations

from typing import Any, Protocol

from hellodj_platform_logic.migration import EXCLUDED_LEGACY_RECORD_TYPES
from hellodj_platform_logic.types import LegacyRecordType

__all__ = [
    "FreshDataInitializer",
    "DynamoResource",
    "build_dynamo_resource",
    "DEFAULT_FRESH_TABLES",
]

#: The DynamoDB tables that must exist for the platform to accept fresh data.
#: These mirror the design's data model (single core table + hot tables) and
#: intentionally hold *no* migrated legacy data.
DEFAULT_FRESH_TABLES: tuple[str, ...] = (
    "hellodj-core",
    "hellodj-session",
    "hellodj-search-cache",
)


class DynamoResource(Protocol):
    """Minimal subset of the boto3 DynamoDB resource used for the probe."""

    def Table(self, name: str) -> Any:  # noqa: N802 - boto3 resource API name
        """Return a Table handle exposing a ``load()`` existence check."""
        ...


def build_dynamo_resource(region_name: str | None = None) -> DynamoResource:
    """Create a real boto3 DynamoDB resource (imported lazily)."""
    import boto3

    return boto3.resource("dynamodb", region_name=region_name)


class FreshDataInitializer:
    """Documents and (best-effort) verifies the clean-slate fresh start.

    The migration writes no legacy playback/session/playlist/config data; this
    step exists to make that explicit and to confirm the fresh tables are
    reachable so the platform can begin accepting new data (R19.2, R19.4).

    Args:
        resource: An injected DynamoDB resource used only to probe table
            existence. When ``None`` the verification is skipped (documented
            no-op) and the excluded-type record is still returned.
        table_names: The tables expected to exist for fresh data
            (default :data:`DEFAULT_FRESH_TABLES`).
    """

    def __init__(
        self,
        *,
        resource: DynamoResource | None = None,
        table_names: tuple[str, ...] = DEFAULT_FRESH_TABLES,
    ) -> None:
        self._resource = resource
        self._table_names = table_names

    @property
    def excluded_record_types(self) -> frozenset[LegacyRecordType]:
        """The legacy record types intentionally excluded from migration."""
        return EXCLUDED_LEGACY_RECORD_TYPES

    def verify_tables(self) -> list[str]:
        """Probe that each fresh table exists and is reachable.

        Returns:
            The list of verified table names. Empty when no DynamoDB resource
            was injected (the step is then a documented no-op).

        Raises:
            RuntimeError: If a resource was injected but a table cannot be
                loaded, so a misconfigured target fails the Job loudly.
        """
        if self._resource is None:
            return []

        verified: list[str] = []
        for name in self._table_names:
            table = self._resource.Table(name)
            try:
                table.load()
            except Exception as error:  # noqa: BLE001 - surface as clean failure
                raise RuntimeError(
                    f"fresh-init table {name!r} is not reachable: {error}"
                ) from error
            verified.append(name)
        return verified

    def initialize(self) -> list[str]:
        """Run the fresh-init step: verify tables, seed no legacy data.

        Returns:
            The verified fresh table names (empty on the no-op path).
        """
        return self.verify_tables()

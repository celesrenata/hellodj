"""Single-table (``hellodj-core``) data access with optimistic locking.

Implements the repository for the ``hellodj-core`` single-table design
(R7.1-R7.3):

======================  ====  ===================================================
Attribute               Type  Notes
======================  ====  ===================================================
``PK``                  S     Partition key (``GUILD#<id>``, ``USER#<id>`` ...)
``SK``                  S     Sort key (``META``, ``CONFIG``, ``TRACK#<n>`` ...)
``entityType``          S     ``Guild`` | ``User`` | ``Playlist`` | ``Config`` |
                              ``Appointment``
``data``                M     Entity payload
``version``             N     Optimistic-lock version
``updatedAt``           N     Epoch ms
``GSI1PK`` / ``GSI1SK`` S     GSI1 index keys (Discord-id -> user, etc.)
======================  ====  ===================================================

The repository exposes the write/read primitives plus an optimistic-lock
read-modify-write (:meth:`CoreTable.update_with_lock`) guarded by a ``version``
``ConditionExpression`` that retries the read-modify-write on a version
conflict and surfaces :class:`~.errors.OptimisticLockError` when it cannot be
committed. Nothing here imports ``boto3``; the DynamoDB (and optional DAX)
tables are injected, so the module is import-safe and testable against
moto/DynamoDB Local.

Design references:
    * Data Models: Core single-table (``hellodj-core``) + GSI1
    * Error handling: optimistic-lock (``version``) conflicts retry the
      read-modify-write; DAX-fronted reads fall through to DynamoDB.

Requirements: 7.1, 7.2, 7.3, 7.4, 7.5, 7.6
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator, Mapping
from typing import Any

from .clients import BackoffConfig, ReadThroughTable, TableLike
from .errors import (
    ConditionalCheckFailedError,
    ItemNotFoundError,
    OptimisticLockError,
    is_conditional_check_failure,
)

__all__ = [
    "CORE_TABLE_NAME",
    "DEFAULT_OPTIMISTIC_LOCK_RETRIES",
    "core_key",
    "CoreTable",
]

#: The single-table name for entity data.
CORE_TABLE_NAME = "hellodj-core"

#: How many times :meth:`CoreTable.update_with_lock` re-reads and retries after
#: a version conflict before surfacing :class:`OptimisticLockError`.
DEFAULT_OPTIMISTIC_LOCK_RETRIES = 5


def core_key(pk: str, sk: str) -> dict[str, str]:
    """Return the primary-key mapping for a ``hellodj-core`` item."""
    return {"PK": pk, "SK": sk}


def _now_ms() -> int:
    """Return the current time in epoch milliseconds."""
    return int(time.time() * 1000)


class CoreTable:
    """Repository over the ``hellodj-core`` single table.

    Args:
        ddb_table: The authoritative DynamoDB resource ``Table`` for
            ``hellodj-core``.
        dax_table: Optional DAX table fronting the read path.
        backoff: Optional shared backoff configuration.
        lock_retries: Number of read-modify-write retries on version conflict.
        clock_ms: Injectable clock returning epoch milliseconds (for tests).
    """

    def __init__(
        self,
        ddb_table: TableLike,
        dax_table: TableLike | None = None,
        *,
        backoff: BackoffConfig | None = None,
        lock_retries: int = DEFAULT_OPTIMISTIC_LOCK_RETRIES,
        clock_ms: Callable[[], int] = _now_ms,
    ) -> None:
        if lock_retries < 0:
            raise ValueError("lock_retries must be >= 0")
        self._table = ReadThroughTable(ddb_table, dax_table, backoff=backoff)
        self._lock_retries = lock_retries
        self._clock_ms = clock_ms

    # -- reads --------------------------------------------------------------

    def get(self, pk: str, sk: str) -> dict[str, Any] | None:
        """Return the item at (``pk``, ``sk``) or ``None`` if absent.

        Served through the DAX read path with fall-through to DynamoDB.
        """
        response = self._table.get_item(Key=core_key(pk, sk))
        item = response.get("Item")
        return dict(item) if item is not None else None

    def require(self, pk: str, sk: str) -> dict[str, Any]:
        """Return the item at (``pk``, ``sk``) or raise :class:`ItemNotFoundError`."""
        item = self.get(pk, sk)
        if item is None:
            raise ItemNotFoundError(f"no hellodj-core item at PK={pk!r} SK={sk!r}")
        return item

    def query_gsi1(
        self,
        gsi1pk: str,
        *,
        sk_prefix: str | None = None,
    ) -> list[dict[str, Any]]:
        """Query GSI1 by partition key, optionally filtering by ``GSI1SK`` prefix.

        Used to resolve secondary access patterns such as Discord-id -> user or
        appointer -> appointees. Served through the DAX read path with
        fall-through to DynamoDB.
        """
        kwargs: dict[str, Any] = {
            "IndexName": "GSI1",
            "KeyConditionExpression": "GSI1PK = :pk",
            "ExpressionAttributeValues": {":pk": gsi1pk},
        }
        if sk_prefix is not None:
            kwargs["KeyConditionExpression"] = (
                "GSI1PK = :pk AND begins_with(GSI1SK, :skp)"
            )
            kwargs["ExpressionAttributeValues"][":skp"] = sk_prefix
        response = self._table.query(**kwargs)
        return [dict(item) for item in response.get("Items", [])]

    def query_pk_prefix(
        self,
        pk: str,
        *,
        sk_prefix: str | None = None,
    ) -> list[dict[str, Any]]:
        """Query the base table by partition key, optionally by ``SK`` prefix.

        Used to enumerate an item collection under one partition (e.g. all
        ``ADMIN#<discordId>`` edges or ``SOURCE#<provider>`` items for a guild).
        Served through the DAX read path with fall-through to DynamoDB.
        """
        kwargs: dict[str, Any] = {
            "KeyConditionExpression": "PK = :pk",
            "ExpressionAttributeValues": {":pk": pk},
        }
        if sk_prefix is not None:
            kwargs["KeyConditionExpression"] = (
                "PK = :pk AND begins_with(SK, :skp)"
            )
            kwargs["ExpressionAttributeValues"][":skp"] = sk_prefix
        response = self._table.query(**kwargs)
        return [dict(item) for item in response.get("Items", [])]

    def scan_entity(self, entity_type: str) -> Iterator[dict[str, Any]]:
        """Yield every item of ``entity_type``, key-projected, across all pages.

        Enumerates the base table with a ``entityType = :et`` filter and a
        projection restricted to the primary key, the ``entityType``, and the
        two plaintext status fields the watchdog needs (``data.expires_at`` and
        ``data.refresh_status``). The projection deliberately **excludes**
        ``data.enc_blob`` (and every other token field) so an enumeration never
        pulls ciphertext or forces a KMS decrypt — only the subsequent refresh
        path loads the blob.

        Pagination is handled transparently via ``LastEvaluatedKey``: each page
        is requested with the prior page's ``LastEvaluatedKey`` as
        ``ExclusiveStartKey`` until DynamoDB stops returning one, and items are
        yielded lazily as an :class:`~collections.abc.Iterator` so a large
        result set is never fully materialized in memory.

        Args:
            entity_type: The ``entityType`` to enumerate (e.g.
                ``"SourceCredential"``).

        Yields:
            One key-projected item dict per matching table item.
        """
        kwargs: dict[str, Any] = {
            "FilterExpression": "#et = :et",
            "ProjectionExpression": (
                "PK, SK, #et, #d.expires_at, #d.refresh_status"
            ),
            "ExpressionAttributeNames": {"#et": "entityType", "#d": "data"},
            "ExpressionAttributeValues": {":et": entity_type},
        }
        while True:
            response = self._table.scan(**kwargs)
            for item in response.get("Items", []):
                yield dict(item)
            last_key = response.get("LastEvaluatedKey")
            if not last_key:
                return
            kwargs["ExclusiveStartKey"] = last_key

    # -- writes -------------------------------------------------------------

    def put_new(
        self,
        pk: str,
        sk: str,
        entity_type: str,
        data: Mapping[str, Any],
        *,
        gsi1pk: str | None = None,
        gsi1sk: str | None = None,
    ) -> dict[str, Any]:
        """Create a new item at version 1, failing if one already exists.

        Guards the create with ``attribute_not_exists(PK)`` so a concurrent
        create surfaces :class:`ConditionalCheckFailedError` rather than
        silently overwriting.
        """
        item: dict[str, Any] = {
            **core_key(pk, sk),
            "entityType": entity_type,
            "data": dict(data),
            "version": 1,
            "updatedAt": self._clock_ms(),
        }
        if gsi1pk is not None:
            item["GSI1PK"] = gsi1pk
        if gsi1sk is not None:
            item["GSI1SK"] = gsi1sk
        try:
            self._table.put_item(
                Item=item,
                ConditionExpression="attribute_not_exists(PK)",
            )
        except Exception as error:  # noqa: BLE001 - classify then re-raise typed
            if is_conditional_check_failure(error):
                raise ConditionalCheckFailedError(
                    f"hellodj-core item already exists at PK={pk!r} SK={sk!r}",
                    error_code="ConditionalCheckFailedException",
                ) from error
            raise
        return item

    def update_with_lock(
        self,
        pk: str,
        sk: str,
        mutator: Callable[[dict[str, Any]], Mapping[str, Any]],
        *,
        entity_type: str | None = None,
    ) -> dict[str, Any]:
        """Optimistic-lock read-modify-write of an item's ``data`` payload.

        Reads the current item (creating an implicit version-0 baseline when
        absent), applies ``mutator`` to a copy of its ``data`` payload, and
        writes the result back guarded by a ``version`` ``ConditionExpression``.
        On a version conflict (a concurrent writer advanced ``version``) the
        cycle re-reads and retries up to ``lock_retries`` times; if it still
        conflicts, :class:`OptimisticLockError` is raised.

        Args:
            pk: Partition key of the item to update.
            sk: Sort key of the item to update.
            mutator: Pure function mapping the current ``data`` payload to the
                new ``data`` payload. It must not mutate its argument.
            entity_type: ``entityType`` to set when creating the item for the
                first time; required if the item may not yet exist.

        Returns:
            The committed item, including its incremented ``version`` and new
            ``updatedAt``.

        Raises:
            OptimisticLockError: If the write cannot be committed within the
                configured retries.
            ValueError: If the item does not exist and no ``entity_type`` was
                supplied to create it.
        """
        for _ in range(self._lock_retries + 1):
            current = self.get(pk, sk)
            expected_version = int(current["version"]) if current else 0
            current_data = dict(current["data"]) if current else {}
            resolved_type = (
                current.get("entityType") if current else None
            ) or entity_type
            if resolved_type is None:
                raise ValueError(
                    "entity_type is required to create a new hellodj-core item"
                )

            new_data = dict(mutator(current_data))
            new_item: dict[str, Any] = {
                **core_key(pk, sk),
                "entityType": resolved_type,
                "data": new_data,
                "version": expected_version + 1,
                "updatedAt": self._clock_ms(),
            }
            # Preserve GSI1 keys across the update when present.
            if current is not None:
                for gsi_attr in ("GSI1PK", "GSI1SK"):
                    if gsi_attr in current:
                        new_item[gsi_attr] = current[gsi_attr]

            if expected_version == 0:
                condition = "attribute_not_exists(version)"
                values: dict[str, Any] = {}
            else:
                condition = "version = :expected"
                values = {":expected": expected_version}

            try:
                kwargs: dict[str, Any] = {
                    "Item": new_item,
                    "ConditionExpression": condition,
                }
                if values:
                    kwargs["ExpressionAttributeValues"] = values
                self._table.put_item(**kwargs)
                return new_item
            except Exception as error:  # noqa: BLE001 - classify then retry/raise
                if is_conditional_check_failure(error):
                    continue  # version moved under us; re-read and retry.
                raise

        raise OptimisticLockError(
            f"optimistic lock failed for PK={pk!r} SK={sk!r} "
            f"after {self._lock_retries + 1} attempts",
            error_code="ConditionalCheckFailedException",
        )

    def delete(self, pk: str, sk: str) -> None:
        """Delete the item at (``pk``, ``sk``); a no-op if it does not exist."""
        self._table.delete_item(Key=core_key(pk, sk))

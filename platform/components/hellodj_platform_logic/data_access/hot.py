"""Hot-table repositories (search cache and session/queue), DAX-fronted.

Implements the two DAX-fronted hot tables from the Data Models section:

``hellodj-search-cache`` (TTL, idempotent):

========  ====  ================================================
Attribute Type  Notes
========  ====  ================================================
``queryKey`` S   Hash of normalized query + source (partition key)
``results``  M   Cached resolved tracks
``ttl``      N   DynamoDB TTL (auto-expire)
========  ====  ================================================

``hellodj-session`` (optimistic-locked, single-writer = orchestrator):

========  ====  ================================================
Attribute Type  Notes
========  ====  ================================================
``PK``     S    ``GUILD#<id>`` (partition key)
``SK``     S    ``SESSION`` | ``QUEUE#<seq>`` (sort key)
``state``  M    voice/text channel, current track, repeat, filters
``version`` N   Optimistic-lock version (single writer)
========  ====  ================================================

Both repositories read through the DAX path with fall-through to DynamoDB. The
search-cache write is **idempotent**: writing the same ``queryKey`` +
``results`` (+ ``ttl``) more than once leaves the stored value identical to
writing it once (Property 10). The session repository provides an
optimistic-lock read-modify-write guarded by the ``version`` attribute.

Nothing here imports ``boto3``; tables are injected, so the module is
import-safe and testable against moto/DynamoDB Local.

Design references:
    * Hot table: search cache (``hellodj-search-cache``, DAX-fronted, TTL)
    * Hot table: session/queue (``hellodj-session``, DAX-fronted)
    * Correctness Property 10 (round-trip + search-cache idempotence)

Requirements: 7.4, 7.5, 7.6
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from typing import Any

from .clients import BackoffConfig, ReadThroughTable, TableLike
from .errors import OptimisticLockError, is_conditional_check_failure

__all__ = [
    "SEARCH_CACHE_TABLE_NAME",
    "SESSION_TABLE_NAME",
    "DEFAULT_SEARCH_CACHE_TTL_SECONDS",
    "session_key",
    "SearchCacheTable",
    "SessionTable",
]

#: Table names for the two hot tables.
SEARCH_CACHE_TABLE_NAME = "hellodj-search-cache"
SESSION_TABLE_NAME = "hellodj-session"

#: Default time-to-live (seconds) applied to a search-cache entry when the
#: caller does not supply an explicit absolute ``ttl``.
DEFAULT_SEARCH_CACHE_TTL_SECONDS = 3600

#: Retries for the session optimistic-lock read-modify-write on version
#: conflict before surfacing :class:`OptimisticLockError`.
DEFAULT_SESSION_LOCK_RETRIES = 5


def _now_s() -> int:
    """Return the current time in epoch seconds."""
    return int(time.time())


def session_key(pk: str, sk: str) -> dict[str, str]:
    """Return the primary-key mapping for a ``hellodj-session`` item."""
    return {"PK": pk, "SK": sk}


class SearchCacheTable:
    """Repository over the ``hellodj-search-cache`` hot table.

    The write is deterministic and idempotent for a given ``query_key`` +
    ``results``: repeated writes with the same inputs and TTL policy leave the
    stored item identical. Reads go through the DAX path with fall-through to
    DynamoDB.

    Args:
        ddb_table: The authoritative DynamoDB resource ``Table``.
        dax_table: Optional DAX table fronting the read path.
        backoff: Optional shared backoff configuration.
        ttl_seconds: Relative TTL applied when a caller passes ``ttl=None``.
        clock_s: Injectable clock returning epoch seconds (for tests).
    """

    def __init__(
        self,
        ddb_table: TableLike,
        dax_table: TableLike | None = None,
        *,
        backoff: BackoffConfig | None = None,
        ttl_seconds: int = DEFAULT_SEARCH_CACHE_TTL_SECONDS,
        clock_s: Callable[[], int] = _now_s,
    ) -> None:
        self._table = ReadThroughTable(ddb_table, dax_table, backoff=backoff)
        self._ttl_seconds = ttl_seconds
        self._clock_s = clock_s

    def get(self, query_key: str) -> dict[str, Any] | None:
        """Return the cached entry for ``query_key`` or ``None`` if absent."""
        response = self._table.get_item(Key={"queryKey": query_key})
        item = response.get("Item")
        return dict(item) if item is not None else None

    def get_results(self, query_key: str) -> dict[str, Any] | None:
        """Return just the cached ``results`` payload, or ``None`` if absent."""
        item = self.get(query_key)
        if item is None:
            return None
        results = item.get("results")
        return dict(results) if isinstance(results, Mapping) else results

    def put(
        self,
        query_key: str,
        results: Mapping[str, Any],
        *,
        ttl: int | None = None,
    ) -> dict[str, Any]:
        """Idempotently write a search-cache entry.

        The stored item is a pure function of (``query_key``, ``results``,
        resolved ``ttl``), so writing the same inputs twice yields the same
        stored value (Property 10 idempotence). Pass an explicit absolute
        ``ttl`` (epoch seconds) to keep repeated writes byte-identical; when
        ``ttl`` is ``None`` a relative TTL is derived from the injected clock.
        """
        resolved_ttl = ttl if ttl is not None else self._clock_s() + self._ttl_seconds
        item: dict[str, Any] = {
            "queryKey": query_key,
            "results": dict(results),
            "ttl": resolved_ttl,
        }
        self._table.put_item(Item=item)
        return item


class SessionTable:
    """Repository over the ``hellodj-session`` hot table.

    The orchestrator is the single writer; writes use an optimistic-lock
    read-modify-write guarded by the ``version`` attribute. Reads go through the
    DAX path with fall-through to DynamoDB.

    Args:
        ddb_table: The authoritative DynamoDB resource ``Table``.
        dax_table: Optional DAX table fronting the read path.
        backoff: Optional shared backoff configuration.
        lock_retries: Read-modify-write retries on version conflict.
    """

    def __init__(
        self,
        ddb_table: TableLike,
        dax_table: TableLike | None = None,
        *,
        backoff: BackoffConfig | None = None,
        lock_retries: int = DEFAULT_SESSION_LOCK_RETRIES,
    ) -> None:
        if lock_retries < 0:
            raise ValueError("lock_retries must be >= 0")
        self._table = ReadThroughTable(ddb_table, dax_table, backoff=backoff)
        self._lock_retries = lock_retries

    def get(self, pk: str, sk: str) -> dict[str, Any] | None:
        """Return the session/queue item at (``pk``, ``sk``) or ``None``."""
        response = self._table.get_item(Key=session_key(pk, sk))
        item = response.get("Item")
        return dict(item) if item is not None else None

    def put_state(
        self,
        pk: str,
        sk: str,
        mutator: Callable[[dict[str, Any]], Mapping[str, Any]],
    ) -> dict[str, Any]:
        """Optimistic-lock read-modify-write of a session/queue ``state``.

        Reads the current item (version 0 when absent), applies ``mutator`` to a
        copy of its ``state`` payload, and writes it back guarded by a
        ``version`` ``ConditionExpression``. On a version conflict the cycle
        re-reads and retries up to ``lock_retries`` times before surfacing
        :class:`OptimisticLockError`.

        Args:
            pk: Partition key (for example ``GUILD#<id>``).
            sk: Sort key (``SESSION`` or ``QUEUE#<seq>``).
            mutator: Pure function mapping the current ``state`` to the new
                ``state``. It must not mutate its argument.

        Returns:
            The committed item including its incremented ``version``.

        Raises:
            OptimisticLockError: If the write cannot be committed within the
                configured retries.
        """
        for _ in range(self._lock_retries + 1):
            current = self.get(pk, sk)
            expected_version = int(current["version"]) if current else 0
            current_state = dict(current["state"]) if current else {}

            new_item: dict[str, Any] = {
                **session_key(pk, sk),
                "state": dict(mutator(current_state)),
                "version": expected_version + 1,
            }

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
                    continue
                raise

        raise OptimisticLockError(
            f"optimistic lock failed for session PK={pk!r} SK={sk!r} "
            f"after {self._lock_retries + 1} attempts",
            error_code="ConditionalCheckFailedException",
        )

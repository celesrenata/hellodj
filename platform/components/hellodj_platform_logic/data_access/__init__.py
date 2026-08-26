"""DynamoDB data-access layer for the HelloDJ AWS platform.

This package is the single source of truth for how the platform persists and
reads entity, search-cache, and session/queue state on DynamoDB (R7.1-R7.6):

* :class:`~.core.CoreTable` — the ``hellodj-core`` single table
  (``PK``/``SK``/``entityType``/``data``/``version``/``updatedAt`` + GSI1) with
  optimistic-lock read-modify-write.
* :class:`~.hot.SearchCacheTable` — the ``hellodj-search-cache`` hot table
  (``queryKey``/``results``/``ttl``) with idempotent writes.
* :class:`~.hot.SessionTable` — the ``hellodj-session`` hot table
  (``PK``/``SK``/``state``/``version``) with optimistic locking.
* :class:`~.clients.ReadThroughTable` — a DAX-fronted read path with
  fall-through to DynamoDB, wrapping every call in exponential-backoff retry.
* Typed errors in :mod:`~.errors` (``OptimisticLockError``, ``ThrottledError``,
  ...).

The layer is split across small modules so each stays well under the 500-line
per-file ceiling (R13.3). It never imports ``boto3`` at module load: the
DynamoDB and optional DAX resource ``Table`` objects are **injected**, so the
package is import-safe in any environment and testable against
moto / DynamoDB Local.

Requirements: 7.1, 7.2, 7.3, 7.4, 7.5, 7.6
"""

from __future__ import annotations

from .clients import (
    DEFAULT_BASE_DELAY_SECONDS,
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_MAX_DELAY_SECONDS,
    BackoffConfig,
    ReadThroughTable,
    TableLike,
    with_backoff,
)
from .core import (
    CORE_TABLE_NAME,
    DEFAULT_OPTIMISTIC_LOCK_RETRIES,
    CoreTable,
    core_key,
)
from .errors import (
    ConditionalCheckFailedError,
    DataAccessError,
    ItemNotFoundError,
    OptimisticLockError,
    ThrottledError,
    extract_error_code,
    is_conditional_check_failure,
    is_throttling_error,
)
from .hot import (
    DEFAULT_SEARCH_CACHE_TTL_SECONDS,
    SEARCH_CACHE_TABLE_NAME,
    SESSION_TABLE_NAME,
    SearchCacheTable,
    SessionTable,
    session_key,
)

__all__ = [
    # errors
    "DataAccessError",
    "ThrottledError",
    "OptimisticLockError",
    "ConditionalCheckFailedError",
    "ItemNotFoundError",
    "extract_error_code",
    "is_throttling_error",
    "is_conditional_check_failure",
    # clients
    "TableLike",
    "BackoffConfig",
    "ReadThroughTable",
    "with_backoff",
    "DEFAULT_MAX_ATTEMPTS",
    "DEFAULT_BASE_DELAY_SECONDS",
    "DEFAULT_MAX_DELAY_SECONDS",
    # core single-table
    "CoreTable",
    "core_key",
    "CORE_TABLE_NAME",
    "DEFAULT_OPTIMISTIC_LOCK_RETRIES",
    # hot tables
    "SearchCacheTable",
    "SessionTable",
    "session_key",
    "SEARCH_CACHE_TABLE_NAME",
    "SESSION_TABLE_NAME",
    "DEFAULT_SEARCH_CACHE_TTL_SECONDS",
]

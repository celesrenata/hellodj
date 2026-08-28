"""Client abstractions for the DynamoDB data-access layer.

This module provides two things the higher-level table repositories build on:

* :func:`with_backoff` — a retry helper that re-invokes an operation with
  exponential backoff and jitter when it fails with a throttling error, then
  surfaces a typed :class:`~.errors.ThrottledError` once attempts are exhausted.
* :class:`ReadThroughTable` — a DAX-fronted read path with fall-through to the
  base DynamoDB table. Reads (``get_item``/``query``) are served from the DAX
  table when one is injected; if DAX errors (miss/unavailable), the read falls
  through to the underlying DynamoDB table. Writes always go to DynamoDB so DAX
  never holds unacknowledged state.

The clients are **injected** (boto3 resource ``Table`` objects, or any object
implementing the same ``get_item``/``put_item``/``update_item``/``query``
surface). Nothing here imports ``boto3``; the module is import-safe everywhere
and testable with fakes or moto/DynamoDB Local.

Design references:
    * Hot table: search cache / session-queue fronted by DAX (R7.6)
    * Error handling: DAX miss falls through to DynamoDB; throttles retry with
      exponential backoff and jitter, then surface a typed error.

Requirements: 7.4, 7.5, 7.6
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from typing import Any, Protocol, TypeVar, runtime_checkable

from .errors import ThrottledError, is_throttling_error

__all__ = [
    "TableLike",
    "BackoffConfig",
    "with_backoff",
    "ReadThroughTable",
]

T = TypeVar("T")

#: Default maximum number of attempts (initial try plus retries) for a
#: throttled request before a :class:`ThrottledError` is surfaced.
DEFAULT_MAX_ATTEMPTS = 5

#: Default base delay (seconds) for exponential backoff. Attempt ``n`` waits up
#: to ``base * 2**(n-1)`` seconds (full jitter), capped at ``max_delay``.
DEFAULT_BASE_DELAY_SECONDS = 0.05

#: Default cap (seconds) on any single backoff sleep.
DEFAULT_MAX_DELAY_SECONDS = 2.0


@runtime_checkable
class TableLike(Protocol):
    """Minimal subset of the boto3 DynamoDB resource ``Table`` interface.

    Any object providing these methods (a boto3 resource ``Table``, an
    ``amazon-dax-client`` resource table, or a test fake) can be injected. All
    methods take and return native Python types (the resource layer performs
    DynamoDB (de)serialization), keeping the repositories free of low-level
    attribute-value maps.
    """

    def get_item(self, **kwargs: Any) -> dict[str, Any]:
        """Return a response dict, optionally containing an ``Item`` key."""
        ...

    def put_item(self, **kwargs: Any) -> dict[str, Any]:
        """Write a single item, honoring any ``ConditionExpression``."""
        ...

    def update_item(self, **kwargs: Any) -> dict[str, Any]:
        """Update a single item, honoring any ``ConditionExpression``."""
        ...

    def query(self, **kwargs: Any) -> dict[str, Any]:
        """Return a response dict containing an ``Items`` list."""
        ...

    def scan(self, **kwargs: Any) -> dict[str, Any]:
        """Return a response dict containing an ``Items`` list (table scan)."""
        ...

    def delete_item(self, **kwargs: Any) -> dict[str, Any]:
        """Delete a single item by key, honoring any ``ConditionExpression``."""
        ...


class BackoffConfig:
    """Configuration for :func:`with_backoff` exponential-backoff retries."""

    def __init__(
        self,
        *,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        base_delay_seconds: float = DEFAULT_BASE_DELAY_SECONDS,
        max_delay_seconds: float = DEFAULT_MAX_DELAY_SECONDS,
        sleep: Callable[[float], None] = time.sleep,
        rng: Callable[[], float] = random.random,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if base_delay_seconds < 0 or max_delay_seconds < 0:
            raise ValueError("backoff delays must be non-negative")
        self.max_attempts = max_attempts
        self.base_delay_seconds = base_delay_seconds
        self.max_delay_seconds = max_delay_seconds
        self._sleep = sleep
        self._rng = rng

    def delay_for_attempt(self, attempt: int) -> float:
        """Return the full-jitter backoff delay before ``attempt`` (1-based)."""
        # attempt 1 is the first retry (after the initial try failed).
        exponential = self.base_delay_seconds * (2 ** (attempt - 1))
        capped = min(exponential, self.max_delay_seconds)
        # Full jitter: sleep a random duration in [0, capped].
        return capped * self._rng()

    def sleep_before_retry(self, attempt: int) -> None:
        """Sleep the jittered backoff duration before the next attempt."""
        self._sleep(self.delay_for_attempt(attempt))


def with_backoff[T](
    operation: Callable[[], T],
    *,
    config: BackoffConfig | None = None,
    description: str = "dynamodb operation",
) -> T:
    """Invoke ``operation``, retrying throttling failures with backoff.

    The operation is attempted up to ``config.max_attempts`` times. A failure
    classified as a throttling error (see
    :func:`~.errors.is_throttling_error`) triggers an exponential-backoff sleep
    with full jitter and another attempt; any other exception propagates
    immediately. When attempts are exhausted while still throttled, a
    :class:`ThrottledError` is raised.

    Args:
        operation: A zero-argument callable performing the DynamoDB/DAX call.
        config: Backoff configuration; a default is used when omitted.
        description: Human-readable label used in the surfaced error message.

    Returns:
        Whatever ``operation`` returns on its first successful attempt.

    Raises:
        ThrottledError: If the operation remains throttled after the configured
            maximum number of attempts.
    """
    cfg = config or BackoffConfig()
    last_error: BaseException | None = None

    for attempt in range(1, cfg.max_attempts + 1):
        try:
            return operation()
        except Exception as error:  # noqa: BLE001 - re-raised or wrapped below
            if not is_throttling_error(error):
                raise
            last_error = error
            if attempt < cfg.max_attempts:
                cfg.sleep_before_retry(attempt)

    raise ThrottledError(
        f"{description} throttled after {cfg.max_attempts} attempts",
        error_code=(
            None if last_error is None else getattr(last_error, "error_code", None)
        ),
    ) from last_error


class ReadThroughTable:
    """A DAX-fronted read path with fall-through to a DynamoDB table.

    Reads are served from the injected DAX table when present; if the DAX call
    raises (a miss, a cold cache, or DAX being unavailable), the read falls
    through to the underlying DynamoDB table so correctness never depends on
    DAX. Writes always target DynamoDB directly. All calls are wrapped in
    :func:`with_backoff` so throttling is retried and then surfaced as a typed
    :class:`ThrottledError`.

    Args:
        ddb_table: The authoritative DynamoDB resource ``Table``.
        dax_table: Optional DAX resource table fronting the same DynamoDB table.
        backoff: Optional backoff configuration shared by every call.
    """

    def __init__(
        self,
        ddb_table: TableLike,
        dax_table: TableLike | None = None,
        *,
        backoff: BackoffConfig | None = None,
    ) -> None:
        self._ddb = ddb_table
        self._dax = dax_table
        self._backoff = backoff

    @property
    def has_dax(self) -> bool:
        """Whether a DAX accelerator table is fronting this table."""
        return self._dax is not None

    def get_item(self, **kwargs: Any) -> dict[str, Any]:
        """Read one item via DAX, falling through to DynamoDB on any DAX error."""
        if self._dax is not None:
            try:
                return with_backoff(
                    lambda: self._dax.get_item(**kwargs),
                    config=self._backoff,
                    description="dax get_item",
                )
            except ThrottledError:
                raise
            except Exception:  # noqa: BLE001 - DAX miss/unavailable -> fall through
                pass
        return with_backoff(
            lambda: self._ddb.get_item(**kwargs),
            config=self._backoff,
            description="ddb get_item",
        )

    def query(self, **kwargs: Any) -> dict[str, Any]:
        """Query via DAX, falling through to DynamoDB on any DAX error."""
        if self._dax is not None:
            try:
                return with_backoff(
                    lambda: self._dax.query(**kwargs),
                    config=self._backoff,
                    description="dax query",
                )
            except ThrottledError:
                raise
            except Exception:  # noqa: BLE001 - DAX miss/unavailable -> fall through
                pass
        return with_backoff(
            lambda: self._ddb.query(**kwargs),
            config=self._backoff,
            description="ddb query",
        )

    def scan(self, **kwargs: Any) -> dict[str, Any]:
        """Scan the DynamoDB table directly (never DAX).

        A scan enumerates the base table; it is served from DynamoDB rather
        than DAX so an enumeration always reflects the authoritative store
        (DAX only fronts point reads/queries). Wrapped in backoff so a
        throttled scan page is retried and then surfaced as a typed error.
        """
        return with_backoff(
            lambda: self._ddb.scan(**kwargs),
            config=self._backoff,
            description="ddb scan",
        )

    def put_item(self, **kwargs: Any) -> dict[str, Any]:
        """Write one item to DynamoDB (the authoritative store)."""
        return with_backoff(
            lambda: self._ddb.put_item(**kwargs),
            config=self._backoff,
            description="ddb put_item",
        )

    def update_item(self, **kwargs: Any) -> dict[str, Any]:
        """Update one item on DynamoDB (the authoritative store)."""
        return with_backoff(
            lambda: self._ddb.update_item(**kwargs),
            config=self._backoff,
            description="ddb update_item",
        )

    def delete_item(self, **kwargs: Any) -> dict[str, Any]:
        """Delete one item on DynamoDB (the authoritative store)."""
        return with_backoff(
            lambda: self._ddb.delete_item(**kwargs),
            config=self._backoff,
            description="ddb delete_item",
        )

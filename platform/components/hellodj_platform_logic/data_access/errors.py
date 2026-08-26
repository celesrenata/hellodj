"""Typed errors and error classification for the DynamoDB data-access layer.

This module is import-safe without ``boto3``/``botocore`` installed: it never
imports either at module load. Instead it classifies exceptions raised by the
injected DynamoDB/DAX clients by inspecting the ``response`` payload that
``botocore.exceptions.ClientError`` exposes (``error.response["Error"]["Code"]``)
using duck typing. This keeps the pure decision layer importable in every
environment while still giving callers precise, typed failures.

Design references:
    * Error handling: DynamoDB throttling / DAX miss, optimistic-lock conflicts
      retry the read-modify-write, and the layer surfaces typed errors to the
      caller.

Requirements: 7.1, 7.2, 7.3, 7.4, 7.5, 7.6
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "DataAccessError",
    "ThrottledError",
    "OptimisticLockError",
    "ConditionalCheckFailedError",
    "ItemNotFoundError",
    "extract_error_code",
    "is_throttling_error",
    "is_conditional_check_failure",
]

#: DynamoDB/DAX error codes that indicate the request was throttled and should
#: be retried with exponential backoff and jitter.
THROTTLING_ERROR_CODES: frozenset[str] = frozenset(
    {
        "ProvisionedThroughputExceededException",
        "ThrottlingException",
        "Throttling",
        "RequestLimitExceeded",
        "TransactionInProgressException",
        "LimitExceededException",
        "RequestThrottled",
        "SlowDown",
    }
)

#: The error code DynamoDB returns when a ``ConditionExpression`` fails; used by
#: the optimistic-lock read-modify-write to detect a version conflict.
CONDITIONAL_CHECK_FAILED_CODE = "ConditionalCheckFailedException"


class DataAccessError(Exception):
    """Base error for the DynamoDB data-access layer.

    All typed errors raised by the data layer derive from this class so callers
    can catch the whole family with a single ``except``. ``error_code`` carries
    the underlying DynamoDB/DAX error code when one was available.
    """

    def __init__(self, message: str, *, error_code: str | None = None) -> None:
        super().__init__(message)
        self.error_code = error_code


class ThrottledError(DataAccessError):
    """Raised when a request remains throttled after exhausting retries.

    The layer retries throttled requests with exponential backoff and jitter;
    if the request is still throttled after the configured maximum attempts,
    this error is surfaced to the caller.
    """


class OptimisticLockError(DataAccessError):
    """Raised when an optimistic-lock read-modify-write cannot be committed.

    The write is guarded by a ``version`` ``ConditionExpression``; if a
    concurrent writer advances the version and the read-modify-write still
    conflicts after its configured retries, this error is raised so the caller
    can decide whether to retry at a higher level.
    """


class ConditionalCheckFailedError(DataAccessError):
    """Raised when a caller-supplied condition on a write is not satisfied.

    Distinct from :class:`OptimisticLockError`: this surfaces a failed
    ``ConditionExpression`` that the data layer does not itself retry (for
    example an ``attribute_not_exists`` create guard).
    """


class ItemNotFoundError(DataAccessError):
    """Raised when a required item does not exist in the table."""


def extract_error_code(error: BaseException) -> str | None:
    """Return the DynamoDB/DAX error code for ``error`` if present.

    Works by duck typing on the ``response`` attribute that
    ``botocore.exceptions.ClientError`` exposes, so no ``botocore`` import is
    required. Returns ``None`` when the exception is not a client error or
    carries no recognizable error code.
    """
    response: Any = getattr(error, "response", None)
    if isinstance(response, dict):
        error_block = response.get("Error")
        if isinstance(error_block, dict):
            code = error_block.get("Code")
            if isinstance(code, str) and code:
                return code
    return None


def is_throttling_error(error: BaseException) -> bool:
    """Return whether ``error`` represents a retryable throttling condition."""
    return extract_error_code(error) in THROTTLING_ERROR_CODES


def is_conditional_check_failure(error: BaseException) -> bool:
    """Return whether ``error`` is a DynamoDB conditional-check failure."""
    return extract_error_code(error) == CONDITIONAL_CHECK_FAILED_CODE

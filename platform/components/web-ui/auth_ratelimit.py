"""Best-effort in-process rate limiter for the auth POST routes.

Throttles repeated failed login / confirm / recover attempts from the same
source within a short window so brute-force and code-guessing are impractical
(R5.1). This is a per-pod, in-memory fixed-window counter — it does NOT
coordinate across the (typically 2) web-ui replicas, and is documented as
best-effort: Cognito itself enforces the authoritative throttling/lockout, and
CloudFront/WAF sit in front. The limiter raises the cost of a naive attack per
pod without adding a datastore dependency.

The limiter keys on ``(client_ip, route)`` and only counts attempts the caller
marks as failures, so a legitimate user who succeeds is never throttled. State
is a plain dict guarded by a lock; stale windows are lazily discarded.

Requirements: 5.1
"""

from __future__ import annotations

import threading
import time

__all__ = ["RateLimiter", "RateLimited"]

#: Max failed attempts per key within the window before throttling.
_DEFAULT_MAX_ATTEMPTS = 8
#: Sliding-reset window length (seconds).
_DEFAULT_WINDOW_SECONDS = 300


class RateLimited(Exception):  # noqa: N818 - a control-flow signal, not an error
    """Raised when a key exceeds the allowed failed attempts in the window."""


class RateLimiter:
    """A thread-safe, per-pod fixed-window failed-attempt limiter.

    Args:
        max_attempts: Failed attempts allowed per key per window.
        window_seconds: Window length; the count resets once it elapses.
    """

    def __init__(
        self,
        *,
        max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
        window_seconds: int = _DEFAULT_WINDOW_SECONDS,
    ) -> None:
        self._max = max_attempts
        self._window = window_seconds
        self._lock = threading.Lock()
        # key -> (count, window_start_epoch)
        self._hits: dict[str, tuple[int, float]] = {}

    def check(self, key: str, *, now: float | None = None) -> None:
        """Raise :class:`RateLimited` if ``key`` is currently throttled.

        Call BEFORE attempting the guarded operation. A key whose window has
        elapsed is treated as fresh (not throttled).
        """
        ts = time.time() if now is None else now
        with self._lock:
            entry = self._hits.get(key)
            if entry is None:
                return
            count, start = entry
            if ts - start >= self._window:
                # Window elapsed → stale, drop it.
                del self._hits[key]
                return
            if count >= self._max:
                raise RateLimited(key)

    def record_failure(self, key: str, *, now: float | None = None) -> None:
        """Record one failed attempt for ``key`` (starts/extends the window)."""
        ts = time.time() if now is None else now
        with self._lock:
            entry = self._hits.get(key)
            if entry is None or ts - entry[1] >= self._window:
                self._hits[key] = (1, ts)
            else:
                self._hits[key] = (entry[0] + 1, entry[1])

    def reset(self, key: str) -> None:
        """Clear a key's counter (call on a successful attempt)."""
        with self._lock:
            self._hits.pop(key, None)

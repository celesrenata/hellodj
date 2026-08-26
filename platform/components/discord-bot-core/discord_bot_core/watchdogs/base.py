"""Base class for periodic background watchdogs.

A :class:`PeriodicWatchdog` runs an async tick coroutine on a fixed interval in
its own :class:`asyncio.Task`. Concrete watchdogs override :meth:`tick`. The
base handles start/stop lifecycle, cancellation, and swallowing per-tick errors
so a single failing tick never tears down the loop (a failed refresh or health
check must not crash the bot — mirrors the legacy watchdog resilience).
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod

log = logging.getLogger(__name__)

__all__ = ["PeriodicWatchdog"]


class PeriodicWatchdog(ABC):
    """An async background task that invokes :meth:`tick` on a fixed interval."""

    def __init__(self, interval_s: float, *, name: str | None = None) -> None:
        """Initialise the watchdog.

        Args:
            interval_s: Seconds between ticks. Must be positive.
            name: Optional task name for diagnostics; defaults to the class name.

        Raises:
            ValueError: If ``interval_s`` is not positive.
        """
        if interval_s <= 0:
            raise ValueError("interval_s must be positive")
        self._interval_s = float(interval_s)
        self._name = name or type(self).__name__
        self._task: asyncio.Task[None] | None = None
        self._running = False

    @property
    def name(self) -> str:
        """The watchdog's task name."""
        return self._name

    @property
    def interval_s(self) -> float:
        """The tick interval in seconds."""
        return self._interval_s

    @property
    def is_running(self) -> bool:
        """Whether the watchdog loop is active."""
        return self._running

    @abstractmethod
    async def tick(self) -> None:
        """Perform one unit of watchdog work. Overridden by subclasses."""
        raise NotImplementedError

    async def start(self) -> None:
        """Start the periodic loop (idempotent)."""
        if self._running:
            log.warning("%s already running", self._name)
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(), name=self._name)
        log.info("%s started (interval=%.1fs)", self._name, self._interval_s)

    async def stop(self) -> None:
        """Stop the periodic loop and await task teardown (idempotent)."""
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        log.info("%s stopped", self._name)

    async def _loop(self) -> None:
        """Internal loop: sleep, then tick, forever until stopped."""
        while self._running:
            try:
                await asyncio.sleep(self._interval_s)
                if self._running:
                    await self.tick()
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001 - resilience: never die on a tick
                log.warning("%s tick failed: %s", self._name, exc)

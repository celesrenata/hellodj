"""Gateway-health watchdog.

Detects a stalled Discord gateway (the READY/heartbeat pipeline going quiet
behind NAT or a half-open socket) and forces a reconnect. Mirrors the legacy
``_gateway_health_watchdog``: it checks how long since the last gateway
heartbeat and, if that exceeds the configured stall timeout, triggers a
reconnect through an injected probe.

The probe is abstracted behind :class:`GatewayProbe` so the watchdog is testable
without a live discord.py client.
"""

from __future__ import annotations

import logging
from typing import Protocol

from .base import PeriodicWatchdog

log = logging.getLogger(__name__)

__all__ = ["GatewayProbe", "GatewayHealthWatchdog"]


class GatewayProbe(Protocol):
    """Structural type for the gateway health surface the watchdog needs.

    A discord.py client adapter satisfies this in production; tests provide a
    fake that reports a controllable heartbeat age and records reconnects.
    """

    def seconds_since_last_heartbeat(self) -> float | None:
        """Return seconds since the last gateway heartbeat, or ``None``.

        ``None`` means "no heartbeat observed yet" (e.g. still connecting),
        which the watchdog treats as healthy so it does not thrash during boot.
        """
        ...

    async def force_reconnect(self) -> None:
        """Force the gateway to reconnect."""
        ...


class GatewayHealthWatchdog(PeriodicWatchdog):
    """Forces a gateway reconnect when heartbeats stall past a timeout."""

    def __init__(
        self,
        probe: GatewayProbe,
        interval_s: float,
        stall_timeout_s: float,
    ) -> None:
        """Initialise the watchdog.

        Args:
            probe: Injected gateway health probe (mockable in tests).
            interval_s: Seconds between health checks.
            stall_timeout_s: Heartbeat age beyond which the gateway is deemed
                stalled and a reconnect is forced. Must be positive.

        Raises:
            ValueError: If ``stall_timeout_s`` is not positive.
        """
        super().__init__(interval_s, name="gateway-health-watchdog")
        if stall_timeout_s <= 0:
            raise ValueError("stall_timeout_s must be positive")
        self._probe = probe
        self._stall_timeout_s = float(stall_timeout_s)

    @property
    def stall_timeout_s(self) -> float:
        """The heartbeat-age threshold that triggers a reconnect."""
        return self._stall_timeout_s

    def is_stalled(self, heartbeat_age_s: float | None) -> bool:
        """Return whether ``heartbeat_age_s`` indicates a stalled gateway.

        A ``None`` age (no heartbeat yet) is not a stall.
        """
        return heartbeat_age_s is not None and heartbeat_age_s > self._stall_timeout_s

    async def tick(self) -> None:
        """Check the gateway heartbeat age and reconnect if stalled."""
        age = self._probe.seconds_since_last_heartbeat()
        if self.is_stalled(age):
            log.warning(
                "gateway-health-watchdog: gateway stalled (%.1fs since last "
                "heartbeat > %.1fs); forcing reconnect",
                age,
                self._stall_timeout_s,
            )
            await self._probe.force_reconnect()

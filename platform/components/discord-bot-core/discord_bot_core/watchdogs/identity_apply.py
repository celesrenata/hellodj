"""Identity-apply watchdog.

Periodically applies every pending per-guild bot-identity change to Discord by
polling the injected :class:`~discord_bot_core.identity.applier.IdentityApplier`.
The applier reads the persisted ``BOTIDENTITY`` items (written by the web-ui),
diffs each against what has already been applied, and applies only the changes —
so a periodic poll is idempotent and eventually consistent even if a guild was
not in the gateway cache when the change was requested.
"""

from __future__ import annotations

import logging
from typing import Any

from .base import PeriodicWatchdog

log = logging.getLogger(__name__)

__all__ = ["IdentityApplyWatchdog"]


class IdentityApplyWatchdog(PeriodicWatchdog):
    """Applies pending per-guild identity changes on a fixed interval."""

    def __init__(self, applier: Any, interval_s: float) -> None:
        """Initialise the watchdog.

        Args:
            applier: The injected :class:`IdentityApplier` whose
                ``apply_all_pending`` is invoked each tick.
            interval_s: Seconds between apply passes.
        """
        super().__init__(interval_s, name="identity-apply-watchdog")
        self._applier = applier

    async def tick(self) -> None:
        """Apply every guild with a pending identity change (change-only)."""
        results = await self._applier.apply_all_pending()
        if results:
            log.debug(
                "identity-apply-watchdog: applied pass over %d guild(s)",
                len(results),
            )

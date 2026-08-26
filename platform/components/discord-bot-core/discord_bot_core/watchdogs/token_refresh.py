"""Token-refresh watchdog.

Periodically re-reads the Discord bot token from AWS Secrets Manager through the
injected :class:`~discord_bot_core.secrets.TokenProvider`, so a rotated secret is
picked up without a redeploy. On change, an optional callback is invoked so the
gateway layer can decide how to apply the new token.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from ..secrets import TokenProvider
from .base import PeriodicWatchdog

log = logging.getLogger(__name__)

__all__ = ["TokenRefreshWatchdog"]

#: Callback invoked with the new token when it changes; may be sync or async.
OnTokenChange = Callable[[str], Awaitable[None] | None]


class TokenRefreshWatchdog(PeriodicWatchdog):
    """Refreshes the Discord token from Secrets Manager on a fixed interval."""

    def __init__(
        self,
        provider: TokenProvider,
        interval_s: float,
        *,
        on_change: OnTokenChange | None = None,
    ) -> None:
        """Initialise the watchdog.

        Args:
            provider: The injected token provider (reads Secrets Manager).
            interval_s: Seconds between refresh attempts.
            on_change: Optional callback invoked with the new token when it
                differs from the previously observed value.
        """
        super().__init__(interval_s, name="token-refresh-watchdog")
        self._provider = provider
        self._on_change = on_change
        self._last: str | None = None

    async def tick(self) -> None:
        """Re-read the token; invoke ``on_change`` if it changed."""
        new_token = self._provider.refresh()
        if new_token == self._last:
            return
        self._last = new_token
        log.info("token-refresh-watchdog: Discord token updated from Secrets Manager")
        if self._on_change is not None:
            result = self._on_change(new_token)
            if result is not None:
                await result

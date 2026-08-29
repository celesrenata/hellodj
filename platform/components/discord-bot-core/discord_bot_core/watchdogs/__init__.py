"""Background watchdogs for discord-bot-core.

Each watchdog is a periodic background task:

* :class:`~discord_bot_core.watchdogs.token_refresh.TokenRefreshWatchdog` —
  periodically re-reads the Discord bot token from Secrets Manager.
* :class:`~discord_bot_core.watchdogs.gateway_health.GatewayHealthWatchdog` —
  detects gateway READY stalls and forces a reconnect.

They share the :class:`~discord_bot_core.watchdogs.base.PeriodicWatchdog` base.
"""

from __future__ import annotations

from .base import PeriodicWatchdog
from .gateway_health import GatewayHealthWatchdog, GatewayProbe
from .token_refresh import TokenRefreshWatchdog

__all__ = [
    "GatewayHealthWatchdog",
    "GatewayProbe",
    "PeriodicWatchdog",
    "TokenRefreshWatchdog",
]

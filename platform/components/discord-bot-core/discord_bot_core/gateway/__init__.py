"""Discord gateway bootstrap for discord-bot-core.

:mod:`discord_bot_core.gateway.client` builds and runs the discord.py ``Bot``
that owns the outbound gateway (WSS) connection. The gateway is sharded and
scales by shard count (design: bot-core scaling).
"""

from __future__ import annotations

from .client import BotClient, build_intents

__all__ = ["BotClient", "build_intents"]

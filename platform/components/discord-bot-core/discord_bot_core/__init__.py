"""HelloDJ ``discord-bot-core`` component.

This component owns the Discord gateway connection, cog/command registration,
guild authorization policy, and the background watchdogs (Discord token refresh
and gateway health). It reads the Discord bot token from AWS Secrets Manager and
delegates all playback to the ``playback-orchestrator`` over HTTP/JSON — it
contains no playback logic itself.

It is packaged as an independently deployable, independently versioned component
(Requirements 15.1, 15.3): its own Nix-built image, its own semantic version,
and its own CI/CD path.

Public surface:
    * :class:`~discord_bot_core.config.BotConfig` — runtime settings.
    * :func:`~discord_bot_core.secrets.get_discord_token` — token provider.
    * :class:`~discord_bot_core.gateway.client.BotClient` — gateway bootstrap.
    * :class:`~discord_bot_core.playback.client.PlaybackClient` — orchestrator client.
"""

from __future__ import annotations

__all__ = ["__version__"]

#: Independent semantic version for the discord-bot-core component (R15.3).
__version__ = "0.1.0"

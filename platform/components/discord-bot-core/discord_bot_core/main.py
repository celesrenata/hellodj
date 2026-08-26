"""Entry point that wires the discord-bot-core component together.

Composition root: build the config, the Secrets Manager token provider, the
playback client, the guild policy, the cog registry, the gateway client, and the
background watchdogs — then run the gateway until shutdown.

This module wires optional runtime dependencies (boto3, discord.py, aiohttp)
lazily inside :func:`run`/factory functions so importing the module for tests or
syntax checks does not require them to be installed.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from .commands.playback_cog import build_playback_cog
from .commands.registry import CommandRegistry
from .config import BotConfig
from .gateway.client import BotClient
from .playback.client import PlaybackClient
from .policy.guild_policy import GuildPolicy
from .secrets import TokenProvider
from .watchdogs.gateway_health import GatewayHealthWatchdog
from .watchdogs.token_refresh import TokenRefreshWatchdog

log = logging.getLogger(__name__)

__all__ = ["Dependencies", "build_secrets_client", "build_transport", "run"]


@dataclass
class Dependencies:
    """Shared dependency container handed to cog factories.

    Satisfies the ``CogDependencies`` protocol used by
    :class:`~discord_bot_core.commands.registry.CommandRegistry`.
    """

    playback: PlaybackClient
    guild_policy: GuildPolicy


def build_secrets_client(region: str | None) -> Any:
    """Create a boto3 Secrets Manager client (lazy boto3 import)."""
    import boto3

    if region:
        return boto3.client("secretsmanager", region_name=region)
    return boto3.client("secretsmanager")


def build_transport(base_url: str) -> Any:
    """Create the aiohttp-backed playback transport (lazy aiohttp import).

    Returns an object satisfying
    :class:`~discord_bot_core.playback.client.Transport`.
    """
    import aiohttp

    class _AioHttpTransport:
        """aiohttp adapter implementing the playback ``Transport`` protocol."""

        async def post_json(
            self, url: str, payload: dict[str, Any]
        ) -> dict[str, Any]:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload) as resp:
                    resp.raise_for_status()
                    return await resp.json()

    del base_url  # base_url is applied by PlaybackClient, kept for symmetry
    return _AioHttpTransport()


def _build_watchdogs(
    config: BotConfig,
    token_provider: TokenProvider,
    gateway: BotClient,
) -> tuple[TokenRefreshWatchdog, GatewayHealthWatchdog]:
    """Construct the token-refresh and gateway-health watchdogs."""
    token_watchdog = TokenRefreshWatchdog(
        token_provider, config.token_refresh_interval_s
    )
    health_watchdog = GatewayHealthWatchdog(
        gateway,
        config.gateway_health_interval_s,
        config.gateway_stall_timeout_s,
    )
    return token_watchdog, health_watchdog


async def run(config: BotConfig | None = None) -> None:
    """Compose and run the bot until the gateway connection ends.

    Args:
        config: Optional pre-built config; defaults to :meth:`BotConfig.from_env`.
    """
    cfg = config or BotConfig.from_env()

    secrets_client = build_secrets_client(cfg.aws_region)
    token_provider = TokenProvider(secrets_client, cfg.discord_token_secret_id)

    transport = build_transport(cfg.orchestrator_base_url)
    playback = PlaybackClient(cfg.orchestrator_base_url, transport)
    guild_policy = GuildPolicy()

    registry = CommandRegistry()
    registry.register(
        lambda deps: build_playback_cog(deps.playback, deps.guild_policy)
    )

    deps = Dependencies(playback=playback, guild_policy=guild_policy)
    gateway = BotClient(cfg, registry, guild_policy, deps)
    gateway.build()

    token_watchdog, health_watchdog = _build_watchdogs(
        cfg, token_provider, gateway
    )

    token = token_provider.get()
    await token_watchdog.start()
    await health_watchdog.start()
    try:
        await gateway.start(token)
    finally:
        await token_watchdog.stop()
        await health_watchdog.stop()
        await gateway.close()


def main() -> None:
    """Console entry point: configure logging and run the event loop."""
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run())


if __name__ == "__main__":
    main()

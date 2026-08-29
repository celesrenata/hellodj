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

from .commands.activation_cog import build_activation_cog
from .commands.playback_cog import build_playback_cog
from .commands.registry import CommandRegistry
from .config import BotConfig
from .gateway.client import BotClient
from .identity.applier import IdentityApplier
from .identity.store import build_identity_store
from .playback.client import PlaybackClient
from .policy.activation import GuildActivation, build_activation_store
from .policy.guild_policy import GuildPolicy
from .secrets import TokenProvider
from .watchdogs.gateway_health import GatewayHealthWatchdog
from .watchdogs.identity_apply import IdentityApplyWatchdog
from .watchdogs.token_refresh import TokenRefreshWatchdog

log = logging.getLogger(__name__)

__all__ = [
    "Dependencies",
    "build_identity_applier",
    "build_s3_client",
    "build_secrets_client",
    "build_transport",
    "run",
]


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


def build_s3_client(region: str | None) -> Any:
    """Create a boto3 S3 client for reading per-guild avatar bytes (lazy boto3)."""
    import boto3

    if region:
        return boto3.client("s3", region_name=region)
    return boto3.client("s3")


def build_identity_applier(
    config: BotConfig, gateway: BotClient
) -> IdentityApplier | None:
    """Build the per-guild identity applier when it is fully configured.

    Returns ``None`` (identity apply disabled) unless both the core table name
    and the assets bucket are set and the backing DynamoDB store can be built.
    The applier reads persisted ``BOTIDENTITY`` items and applies them to
    Discord via the gateway's underlying ``bot``.
    """
    if not config.core_table_name or not config.assets_bucket:
        return None
    store = build_identity_store(config.core_table_name, config.aws_region)
    if store is None:
        return None
    s3 = build_s3_client(config.aws_region)
    return IdentityApplier(
        gateway.bot, store, s3, avatar_bucket=config.assets_bucket
    )


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
            async with (
                aiohttp.ClientSession() as session,
                session.post(url, json=payload) as resp,
            ):
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
    bot = gateway.build()

    # Per-guild activation gate (on-prem /activate parity): a guild is locked
    # until an admin runs /activate <key>. The cog owns the command and installs
    # a global command check on the bot. When no core table is configured the
    # store is None and the gate treats every guild as locked (secure default),
    # so the bot still runs but refuses commands until activation is wired.
    activation_store = build_activation_store(cfg.core_table_name, cfg.aws_region)
    if activation_store is not None:
        activation = GuildActivation(activation_store)
        # The gateway uses the activation reader to decide which commands are
        # VISIBLE per guild (unactivated -> only activate/help), and re-syncs a
        # guild the moment /activate succeeds (activate disappears, the rest
        # appear) via the on_activated callback.
        gateway.set_activation(activation)
        cog = build_activation_cog(
            bot, activation, on_activated=gateway.resync_guild
        )
        result = bot.add_cog(cog)
        if result is not None:
            await result

    # Optional per-guild bot-identity apply: only when the core table + assets
    # bucket are configured. The applier reads persisted BOTIDENTITY items and
    # applies nickname/avatar changes to Discord; the watchdog polls it and the
    # gateway runs it on ready / on guild join.
    identity_applier = build_identity_applier(cfg, gateway)
    identity_watchdog: IdentityApplyWatchdog | None = None
    if identity_applier is not None:
        gateway.set_identity_applier(identity_applier)
        identity_watchdog = IdentityApplyWatchdog(
            identity_applier, cfg.identity_apply_interval_s
        )

    token_watchdog, health_watchdog = _build_watchdogs(
        cfg, token_provider, gateway
    )

    token = token_provider.get()
    await token_watchdog.start()
    await health_watchdog.start()
    if identity_watchdog is not None:
        await identity_watchdog.start()
    try:
        await gateway.start(token)
    finally:
        if identity_watchdog is not None:
            await identity_watchdog.stop()
        await token_watchdog.stop()
        await health_watchdog.stop()
        await gateway.close()


def main() -> None:
    """Console entry point: configure logging and run the event loop."""
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run())


if __name__ == "__main__":
    main()

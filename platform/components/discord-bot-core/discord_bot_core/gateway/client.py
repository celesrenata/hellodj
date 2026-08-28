"""Discord gateway client bootstrap.

Owns the discord.py ``Bot`` lifecycle: it wires up intents, registers cogs via
the :class:`~discord_bot_core.commands.registry.CommandRegistry`, applies the
guild policy on join/remove, and exposes a :class:`GatewayProbe`-compatible
health surface for the gateway-health watchdog.

discord.py is imported lazily inside the methods that need it so this module can
be imported (and syntax-checked / partially unit-tested) without the runtime
dependency present.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from ..commands.registry import CommandRegistry
from ..config import BotConfig
from ..policy.guild_policy import GuildPolicy

log = logging.getLogger(__name__)

__all__ = ["build_intents", "BotClient"]


def build_intents() -> Any:
    """Build the Discord gateway intents the bot requires.

    Enables the guild, voice-state, and message-content intents needed for
    music commands and guild policy. Imports discord lazily.
    """
    import discord

    intents = discord.Intents.default()
    intents.message_content = True
    intents.voice_states = True
    intents.guilds = True
    return intents


class BotClient:
    """Wraps a discord.py ``Bot`` with HelloDJ wiring and a health surface.

    The underlying ``Bot`` is created on :meth:`build` (lazy discord import).
    ``deps`` is the shared dependency container handed to cog factories.
    """

    def __init__(
        self,
        config: BotConfig,
        registry: CommandRegistry,
        guild_policy: GuildPolicy,
        deps: Any,
        identity_applier: Any | None = None,
    ) -> None:
        """Initialise the client wrapper.

        Args:
            config: Runtime settings (command prefix, endpoints).
            registry: Cog registry attached during setup.
            guild_policy: Policy applied on guild join/remove events.
            deps: Shared dependency container passed to cog factories.
            identity_applier: Optional per-guild bot-identity applier. When
                present, ``on_ready`` runs an initial ``apply_all_pending`` and
                ``on_guild_join`` applies that guild — so a fresh join/reconnect
                picks up any pending identity change without waiting for the
                periodic watchdog. ``None`` disables the event-driven apply.
        """
        self._config = config
        self._registry = registry
        self._policy = guild_policy
        self._deps = deps
        self._identity_applier = identity_applier
        self._bot: Any = None
        self._last_heartbeat_monotonic: float | None = None

    @property
    def bot(self) -> Any:
        """The underlying discord.py ``Bot`` (``None`` until :meth:`build`)."""
        return self._bot

    def build(self) -> Any:
        """Create and configure the discord.py ``Bot`` instance.

        Returns:
            The configured ``Bot``.
        """
        from discord.ext import commands

        bot = commands.Bot(
            command_prefix=self._config.command_prefix,
            intents=build_intents(),
        )
        self._register_events(bot)
        self._bot = bot
        return bot

    def _register_events(self, bot: Any) -> None:
        """Attach lifecycle and guild-policy event handlers to ``bot``."""

        @bot.event
        async def setup_hook() -> None:  # pragma: no cover - discord runtime
            count = await self._registry.attach_all(bot, self._deps)
            log.info("gateway: attached %d cogs", count)

        @bot.event
        async def on_ready() -> None:  # pragma: no cover - discord runtime
            self._last_heartbeat_monotonic = time.monotonic()
            log.info("gateway: READY as %s", getattr(bot.user, "name", "?"))
            if self._identity_applier is not None:
                try:
                    await self._identity_applier.apply_all_pending()
                except Exception as exc:  # noqa: BLE001 - never crash on_ready
                    log.warning("gateway: initial identity apply failed: %s", exc)

        @bot.event
        async def on_resumed() -> None:  # pragma: no cover - discord runtime
            self._last_heartbeat_monotonic = time.monotonic()

        @bot.event
        async def on_guild_join(guild: Any) -> None:  # pragma: no cover
            status = self._policy.check_on_join(
                int(guild.id), getattr(guild, "name", "")
            )
            log.info("gateway: joined guild %s -> %s", guild.id, status.value)
            if self._identity_applier is not None:
                try:
                    await self._identity_applier.apply_guild(int(guild.id))
                except Exception as exc:  # noqa: BLE001 - never crash the event
                    log.warning(
                        "gateway: identity apply on join %s failed: %s",
                        guild.id,
                        exc,
                    )

        @bot.event
        async def on_guild_remove(guild: Any) -> None:  # pragma: no cover
            self._policy.clear(int(guild.id))

    def note_heartbeat(self) -> None:
        """Record that a gateway heartbeat/READY was observed just now.

        Called by the discord event handlers; also usable by tests to simulate
        heartbeat activity for the health watchdog.
        """
        self._last_heartbeat_monotonic = time.monotonic()

    def seconds_since_last_heartbeat(self) -> float | None:
        """Return seconds since the last observed heartbeat, or ``None``.

        Satisfies :class:`~discord_bot_core.watchdogs.gateway_health.GatewayProbe`.
        """
        if self._last_heartbeat_monotonic is None:
            return None
        return time.monotonic() - self._last_heartbeat_monotonic

    async def force_reconnect(self) -> None:
        """Force the gateway to reconnect by closing the current connection.

        discord.py's reconnect logic re-establishes the session after close.
        Satisfies :class:`~discord_bot_core.watchdogs.gateway_health.GatewayProbe`.
        """
        if self._bot is None:  # pragma: no cover - defensive
            return
        log.warning("gateway: forcing reconnect")
        await self._bot.close()

    async def start(self, token: str) -> None:
        """Start the gateway connection with ``token`` (builds the bot if needed)."""
        if self._bot is None:
            self.build()
        await self._bot.start(token)

    def set_identity_applier(self, applier: Any) -> None:
        """Attach the per-guild identity applier after the bot is built.

        The applier needs :attr:`bot` (available only after :meth:`build`), while
        the gateway's event handlers read ``self._identity_applier`` when they
        fire (after :meth:`start`). Setting it here — before ``start`` — keeps the
        event-driven initial apply / on-join apply wired without a construction
        ordering problem.
        """
        self._identity_applier = applier

    async def close(self) -> None:
        """Close the gateway connection."""
        if self._bot is not None:
            await self._bot.close()

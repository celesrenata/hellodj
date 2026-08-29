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

__all__ = ["BotClient", "build_intents"]


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
        """Record that a gateway heartbeat/READY/resume was observed just now.

        Called by the ``on_ready``/``on_resumed`` handlers as a coarse fallback
        liveness signal. The authoritative signal is discord.py's own heartbeat
        ACK tracking, read in :meth:`seconds_since_last_heartbeat`; this only
        matters before the first ACK lands or when the websocket is momentarily
        unavailable. Also usable by tests to simulate heartbeat activity.
        """
        self._last_heartbeat_monotonic = time.monotonic()

    def _ws_heartbeat_age(self) -> float | None:
        """Seconds since discord.py last received a HEARTBEAT_ACK, or ``None``.

        Reads discord.py's live keepalive handler (``bot.ws._keep_alive``), which
        stamps ``_last_ack`` (monotonic) on every ACK — the ONLY signal that
        actually tracks the ongoing heartbeat pipeline. ``on_ready``/``on_resumed``
        fire once per connection, so a manual timestamp goes stale on a perfectly
        healthy gateway and would trigger a spurious reconnect every ~90s. Returns
        ``None`` when the websocket or its keepalive isn't available yet, so the
        caller falls back to the coarse ``on_ready`` timestamp.
        """
        bot = self._bot
        if bot is None:  # pragma: no cover - defensive
            return None
        ws = getattr(bot, "ws", None)
        if ws is None:
            return None
        keep_alive = getattr(ws, "_keep_alive", None)
        last_ack = getattr(keep_alive, "_last_ack", None) if keep_alive else None
        if last_ack is None:
            return None
        # discord.py's KeepAliveHandler uses time.perf_counter() for _last_ack.
        return time.perf_counter() - float(last_ack)

    def seconds_since_last_heartbeat(self) -> float | None:
        """Return seconds since the last gateway heartbeat, or ``None``.

        Prefers discord.py's real HEARTBEAT_ACK timestamp (updates every ~41s on
        a healthy connection). Falls back to the coarse ``on_ready``/``on_resumed``
        timestamp only before the first ACK is observed. ``None`` means "no
        liveness signal yet" (still connecting), which the watchdog treats as
        healthy so it does not thrash during boot.

        Satisfies :class:`~discord_bot_core.watchdogs.gateway_health.GatewayProbe`.
        """
        # A permanently-closed client is not a "stalled heartbeat" case — the run
        # loop is exiting; don't force a reconnect on a closing client.
        if self._bot is not None and self._bot.is_closed():
            return None
        ws_age = self._ws_heartbeat_age()
        if ws_age is not None:
            return ws_age
        if self._last_heartbeat_monotonic is None:
            return None
        return time.monotonic() - self._last_heartbeat_monotonic

    async def force_reconnect(self) -> None:
        """Force the gateway to reconnect WITHOUT terminating the client.

        Closes the underlying *websocket* with a non-1000 code (4000). discord.py's
        internal ``connect()`` loop catches the resulting ``ConnectionClosed`` and
        transparently RESUMEs/reconnects — the ``bot.start()`` coroutine keeps
        running. This is the critical difference from ``bot.close()``, which is a
        terminal shutdown: calling it here previously ended ``bot.start()``, the
        run loop then double-closed and raised ``CancelledError``, and the process
        crashed (crash-loop). We must never call ``bot.close()`` to "reconnect".

        Satisfies :class:`~discord_bot_core.watchdogs.gateway_health.GatewayProbe`.
        """
        bot = self._bot
        if bot is None:  # pragma: no cover - defensive
            return
        if bot.is_closed():  # pragma: no cover - defensive
            return
        ws = getattr(bot, "ws", None)
        if ws is None:
            log.warning("gateway: reconnect requested but no live websocket")
            return
        log.warning("gateway: forcing websocket reconnect (resume)")
        try:
            # code=4000 (non-1000) => discord.py treats it as resumable and its
            # connect() loop reconnects instead of exiting.
            await ws.close(code=4000)
        except Exception as exc:  # noqa: BLE001 - never crash the watchdog tick
            log.warning("gateway: force_reconnect ws close failed: %s", exc)

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

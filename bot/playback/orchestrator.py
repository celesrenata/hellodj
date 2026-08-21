"""Instance orchestrator for multi-channel music playback.

Manages multiple bot application connections (discord.Client instances) so
that HelloDJ can play music in several voice channels within the same guild
simultaneously — working around Discord's one-voice-connection-per-bot limit.

All instances share the same Lavalink sidecar via wavelink's multi-session
support.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    import discord
    import wavelink
    from discord.ext import commands

log = logging.getLogger(__name__)

__all__ = ["BotInstance", "InstanceOrchestrator"]

# Limits defined by requirements
_MIN_INSTANCES = 2
_MAX_INSTANCES = 10
_HEALTH_CHECK_TIMEOUT_S = 10.0
_RELEASE_DEADLINE_S = 5.0


@dataclass
class BotInstance:
    """A secondary bot application used for multi-channel music.

    Each instance holds its own discord.Client (voice-only, no command tree)
    and connects to the shared Lavalink sidecar.
    """

    index: int
    client: discord.Client
    token: str
    application_id: int
    status: Literal["available", "connected", "unhealthy"]
    channel_id: int | None = None
    guild_id: int | None = None
    last_health_check: float = field(default_factory=time.time)
    display_name: str = ""


class InstanceOrchestrator:
    """Manages multiple bot instances for multi-channel music.

    The orchestrator loads credentials from the encrypted credential store,
    creates lightweight discord.Client instances, and assigns them to voice
    channels on demand. Health checks run periodically to detect stale
    connections.
    """

    def __init__(self, primary_bot: commands.Bot, registry: object) -> None:
        """Initialise the orchestrator.

        Parameters
        ----------
        primary_bot:
            The main HelloDJ bot (already running, owns slash commands).
        registry:
            The SessionRegistry used to track playback sessions.
        """
        self._primary = primary_bot
        self._registry = registry
        self._instances: list[BotInstance] = []
        self._lavalink_node: wavelink.Node | None = None
        self._initialized: bool = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        """Load instance credentials from credential store, connect clients.

        Reads `playback.instance_count` and per-instance keys:
        - ``instance.<N>.token``
        - ``instance.<N>.app_id``
        - ``instance.<N>.name``

        Instances with missing or invalid credentials are logged and skipped.
        """
        try:
            from config import cfg
        except ImportError:
            from bot.config import cfg

        count_raw = cfg("playback.instance_count")
        if count_raw is None:
            log.info("No secondary instances configured (playback.instance_count not set)")
            self._initialized = True
            return

        try:
            count = int(count_raw)
        except (ValueError, TypeError):
            log.error("Invalid playback.instance_count value: %r", count_raw)
            self._initialized = True
            return

        count = max(_MIN_INSTANCES, min(count, _MAX_INSTANCES))
        log.info("Initializing %d secondary bot instances", count)

        import discord

        for i in range(count):
            token = cfg(f"instance.{i}.token")
            app_id_raw = cfg(f"instance.{i}.app_id")
            name = cfg(f"instance.{i}.name", f"Instance #{i + 1}")

            if not token or not app_id_raw:
                log.error(
                    "Missing credentials for instance.%d (token=%s, app_id=%s) — skipping",
                    i,
                    "present" if token else "MISSING",
                    "present" if app_id_raw else "MISSING",
                )
                continue

            try:
                app_id = int(app_id_raw)
            except (ValueError, TypeError):
                log.error("Invalid app_id for instance.%d: %r — skipping", i, app_id_raw)
                continue

            # Secondary instances only need voice — minimal intents
            intents = discord.Intents.none()
            intents.guilds = True
            intents.voice_states = True

            client = discord.Client(intents=intents)

            instance = BotInstance(
                index=i,
                client=client,
                token=token,
                application_id=app_id,
                status="available",
                display_name=name or f"Instance #{i + 1}",
            )
            self._instances.append(instance)

        # Connect all instances in parallel
        login_tasks = [
            self._connect_instance(inst) for inst in self._instances
        ]
        if login_tasks:
            results = await asyncio.gather(*login_tasks, return_exceptions=True)
            for inst, result in zip(self._instances, results):
                if isinstance(result, Exception):
                    log.error(
                        "Failed to connect instance %d (%s): %s",
                        inst.index,
                        inst.display_name,
                        result,
                    )
                    inst.status = "unhealthy"

        available = sum(1 for inst in self._instances if inst.status == "available")
        log.info(
            "Instance orchestrator ready: %d/%d instances available",
            available,
            len(self._instances),
        )
        self._initialized = True

    async def assign_instance(
        self, guild_id: int, channel_id: int
    ) -> BotInstance | None:
        """Find and assign an available instance to a channel.

        Returns the assigned BotInstance, or None if all instances are busy.
        Does not reassign if an instance is already serving this channel.
        """
        # Check if an instance is already connected to this channel
        existing = self.get_instance_for_channel(guild_id, channel_id)
        if existing is not None:
            return existing

        # Pick the first available instance
        instance = self._get_available_instance()
        if instance is None:
            return None

        instance.status = "connected"
        instance.guild_id = guild_id
        instance.channel_id = channel_id
        log.info(
            "Assigned instance %d (%s) to guild=%d channel=%d",
            instance.index,
            instance.display_name,
            guild_id,
            channel_id,
        )
        return instance

    async def release_instance(self, guild_id: int, channel_id: int) -> None:
        """Release a bot instance when playback ends.

        Sets the instance status back to 'available' within the 5s deadline.
        """
        instance = self.get_instance_for_channel(guild_id, channel_id)
        if instance is None:
            log.debug(
                "release_instance called but no instance found for guild=%d channel=%d",
                guild_id,
                channel_id,
            )
            return

        # Release within the 5-second deadline
        try:
            await asyncio.wait_for(
                self._do_release(instance), timeout=_RELEASE_DEADLINE_S
            )
        except asyncio.TimeoutError:
            log.warning(
                "Release of instance %d timed out after %.1fs — forcing available",
                instance.index,
                _RELEASE_DEADLINE_S,
            )
            self._force_release(instance)

    def get_instance_for_channel(
        self, guild_id: int, channel_id: int
    ) -> BotInstance | None:
        """Get the instance currently serving a channel."""
        for instance in self._instances:
            if (
                instance.guild_id == guild_id
                and instance.channel_id == channel_id
                and instance.status == "connected"
            ):
                return instance
        return None

    async def health_check(self) -> None:
        """Periodic health check of all instances.

        If a client doesn't respond to `latency` check within 10 seconds,
        the instance is marked 'unhealthy' and skipped for assignment.
        Unhealthy instances that recover are set back to 'available'.
        """
        for instance in self._instances:
            try:
                healthy = await asyncio.wait_for(
                    self._check_instance_health(instance),
                    timeout=_HEALTH_CHECK_TIMEOUT_S,
                )
            except asyncio.TimeoutError:
                healthy = False
            except Exception as exc:
                log.warning(
                    "Health check error for instance %d: %s", instance.index, exc
                )
                healthy = False

            if healthy:
                instance.last_health_check = time.time()
                # Recover previously unhealthy instances that are not connected
                if instance.status == "unhealthy" and instance.channel_id is None:
                    log.info(
                        "Instance %d (%s) recovered — marking available",
                        instance.index,
                        instance.display_name,
                    )
                    instance.status = "available"
            else:
                if instance.status != "unhealthy":
                    log.warning(
                        "Instance %d (%s) failed health check — marking unhealthy",
                        instance.index,
                        instance.display_name,
                    )
                instance.status = "unhealthy"
                # Clear channel assignment for unhealthy instances
                instance.channel_id = None
                instance.guild_id = None

    @property
    def instances(self) -> list[BotInstance]:
        """Return the list of managed instances (read-only view)."""
        return list(self._instances)

    @property
    def available_count(self) -> int:
        """Number of instances currently available for assignment."""
        return sum(1 for inst in self._instances if inst.status == "available")

    @property
    def connected_instances(self) -> list[BotInstance]:
        """Instances currently connected to voice channels."""
        return [inst for inst in self._instances if inst.status == "connected"]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_available_instance(self) -> BotInstance | None:
        """Return the first instance with status 'available'."""
        for instance in self._instances:
            if instance.status == "available":
                return instance
        return None

    async def _connect_instance(self, instance: BotInstance) -> None:
        """Log in a secondary bot client (non-blocking start).

        We use asyncio.create_task so the client runs its gateway loop
        without blocking the orchestrator's initialization.
        """
        try:
            # Start the client in the background — it maintains its own
            # gateway connection independently.
            asyncio.create_task(
                instance.client.start(instance.token),
                name=f"instance-{instance.index}-gateway",
            )
            # Give it a moment to establish the connection
            await asyncio.sleep(2.0)

            if instance.client.is_ready():
                log.info(
                    "Instance %d (%s) connected successfully",
                    instance.index,
                    instance.display_name,
                )
            else:
                # Not ready yet but may still be connecting — not fatal
                log.debug(
                    "Instance %d (%s) started but not yet ready",
                    instance.index,
                    instance.display_name,
                )
        except Exception as exc:
            log.error(
                "Failed to start instance %d (%s): %s",
                instance.index,
                instance.display_name,
                exc,
            )
            raise

    async def _do_release(self, instance: BotInstance) -> None:
        """Perform the actual release of an instance."""
        # Disconnect from voice if connected
        if instance.client.voice_clients:
            for vc in instance.client.voice_clients:
                try:
                    await vc.disconnect(force=True)
                except Exception as exc:
                    log.debug(
                        "Error disconnecting voice for instance %d: %s",
                        instance.index,
                        exc,
                    )

        self._force_release(instance)
        log.info(
            "Released instance %d (%s) from guild=%d channel=%d",
            instance.index,
            instance.display_name,
            instance.guild_id or 0,
            instance.channel_id or 0,
        )

    def _force_release(self, instance: BotInstance) -> None:
        """Reset instance state to available without async operations."""
        instance.status = "available"
        instance.channel_id = None
        instance.guild_id = None

    async def _check_instance_health(self, instance: BotInstance) -> bool:
        """Check if an instance's client is responsive.

        Returns True if the client is connected and responding.
        """
        client = instance.client
        # A closed or non-running client is unhealthy
        if client.is_closed():
            return False
        # Check if the websocket is connected
        if not client.is_ready():
            return False
        # Verify latency is reasonable (not stale)
        latency = client.latency
        if latency == float("inf") or latency < 0:
            return False
        return True

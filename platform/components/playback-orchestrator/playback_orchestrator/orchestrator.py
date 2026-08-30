"""Instance orchestrator base class for the ``playback-orchestrator`` component.

Owns the assign / release / health / quota logic for secondary bot instances.
Originated as a port of the (now-removed) on-prem
``bot/playback/orchestrator.py``; with on-prem retired (AWS 100%) this file is
the standalone source of truth. The AWS subclass
(:class:`~playback_orchestrator.instance_runtime.AwsInstanceOrchestrator`)
overrides ONLY :meth:`initialize` (the credential source) plus the entitlement
resolver seam (:meth:`_resolve_effective`), inheriting the assignment logic
unchanged.

The quota helpers source their pure decision functions and default entitlements
from the SHARED ``entitlements_core`` module, so the web-ui and this runtime
agree exactly. The base :meth:`_resolve_effective` returns the restrictive
default (no resolver seam of its own); subclasses inject a resolver.

Requirements: 2.1, 2.5, 3.1, 3.2, 3.3, 3.4, 5.1, 5.2, 5.3
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:  # pragma: no cover - typing only
    import discord
    import wavelink
    from discord.ext import commands

log = logging.getLogger(__name__)

__all__ = ["BotInstance", "InstanceOrchestrator", "QuotaExceededError"]


class QuotaExceededError(Exception):
    """Adding/activating a bot instance would exceed a user quota (R11.2/R12.3).

    Carries a clear, user-facing message stating the entitlement limit reached.
    """


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
    # Owning-user Discord id, recorded at assignment so the orchestrator can
    # count a user's per-guild bot instances and distinct guilds for
    # entitlement-quota enforcement (R11/R12). None for legacy (userless) calls.
    user_id: int | None = None


class InstanceOrchestrator:
    """Manages multiple bot instances for multi-channel music.

    The orchestrator loads credentials from the encrypted credential store,
    creates lightweight discord.Client instances, and assigns them to voice
    channels on demand. Health checks run periodically to detect stale
    connections.
    """

    #: Post-start gateway readiness grace (2.0, identical to on-prem). Exposed
    #: as a class attribute (vendored-port-only) so tests can zero it without
    #: patching ``asyncio.sleep`` globally; subclasses/tests may lower it.
    _connect_grace_seconds: float = 2.0

    def __init__(self, primary_bot: commands.Bot, registry: object) -> None:
        """Initialise the orchestrator (primary bot + session registry)."""
        self._primary = primary_bot
        self._registry = registry
        self._instances: list[BotInstance] = []
        self._lavalink_node: wavelink.Node | None = None
        self._initialized: bool = False

    # -- Public API -------------------------------------------------------

    async def initialize(self) -> None:
        """Load instance credentials from the store and connect clients.

        Reads ``playback.instance_count`` and per-instance
        ``instance.<N>.token`` / ``instance.<N>.app_id`` / ``instance.<N>.name``.
        Instances with missing or invalid credentials are logged and skipped.
        """
        try:
            from config import cfg
        except ImportError:
            # No standalone ``config`` in this component; the AWS runtime
            # overrides ``initialize`` (its creds come from the bot-app pool).
            # Degrade to "no secondary instances" rather than reference the
            # removed on-prem ``bot.config``.
            log.info("No config source for base initialize() — no instances.")
            self._initialized = True
            return

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
            for inst, result in zip(self._instances, results, strict=False):
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
        self,
        guild_id: int,
        channel_id: int,
        user_id: int | None = None,
    ) -> BotInstance | None:
        """Find and assign an available instance to a channel.

        With an owning ``user_id``, per-user entitlement quotas are enforced
        first (R11/R12): the per-guild bot limit and, for a guild the user is not
        already active in, the ``max_guilds`` limit; a rejection raises
        :class:`QuotaExceededError`. No ``user_id`` (legacy callers) → no quota.
        Returns the assigned BotInstance, or None if all instances are busy; does
        not reassign if an instance is already serving this channel.
        """
        # Reuse the instance already serving this channel, if any.
        existing = self.get_instance_for_channel(guild_id, channel_id)
        if existing is not None:
            return existing

        # Enforce per-user entitlement quotas before consuming an instance.
        if user_id is not None:
            self._enforce_quotas(guild_id, user_id)

        instance = self._get_available_instance()
        if instance is None:
            return None

        instance.status = "connected"
        instance.guild_id = guild_id
        instance.channel_id = channel_id
        instance.user_id = user_id
        log.info(
            "Assigned instance %d (%s) to guild=%d channel=%d user=%s",
            instance.index,
            instance.display_name,
            guild_id,
            channel_id,
            user_id,
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
        except TimeoutError:
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
            except TimeoutError:
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
                # Clear assignment for unhealthy instances
                instance.channel_id = None
                instance.guild_id = None
                instance.user_id = None

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

    # -- Internal helpers -------------------------------------------------

    def _get_available_instance(self) -> BotInstance | None:
        """Return the first instance with status 'available'."""
        for instance in self._instances:
            if instance.status == "available":
                return instance
        return None

    # -- entitlement quota enforcement (R11/R12) --
    def _user_bot_count_in_guild(self, guild_id: int, user_id: int) -> int:
        """Count the user's *connected* bot instances in one guild (R11.2)."""
        return sum(
            1
            for inst in self._instances
            if inst.status == "connected"
            and inst.guild_id == guild_id
            and inst.user_id == user_id
        )

    def _user_active_guilds(self, user_id: int) -> set[int]:
        """Distinct guilds the user has connected instances in (R12.4)."""
        return {
            inst.guild_id
            for inst in self._instances
            if inst.status == "connected"
            and inst.user_id == user_id
            and inst.guild_id is not None
        }

    def _enforce_quotas(self, guild_id: int, user_id: int) -> None:
        """Reject an assignment that would exceed the user's quotas (R11/R12).

        Resolves effective entitlements (fail-safe defaults, R14.3) and applies
        the shared pure helpers, raising :class:`QuotaExceededError` when reached.
        """
        effective = self._resolve_effective(user_id)
        from entitlements_core import (
            effective_max_bots_per_guild,
            quota_reached,
        )

        # Guild limit (R12.3/R12.4): only a NEW guild grows the distinct count.
        active_guilds = self._user_active_guilds(user_id)
        if guild_id not in active_guilds:
            max_guilds = int(effective.get("max_guilds", 1))
            if quota_reached(len(active_guilds), max_guilds):
                raise QuotaExceededError(
                    f"Guild limit reached: you may operate in at most "
                    f"{max_guilds} guild(s)."
                )

        # Per-guild bot limit (add-instance path, R11.2/R11.4).
        per_guild_limit = effective_max_bots_per_guild(effective)
        current = self._user_bot_count_in_guild(guild_id, user_id)
        if quota_reached(current, per_guild_limit):
            raise QuotaExceededError(
                f"Per-guild bot limit reached: you may run at most "
                f"{per_guild_limit} bot instance(s) in this guild."
            )

    def _resolve_effective(self, user_id: int) -> dict:
        """Resolve the owning user's effective entitlements (fail-safe, R14.3).

        The base class has no entitlement-resolver seam of its own, so it returns
        the restrictive shared ``DEFAULT_ENTITLEMENTS`` (limits = 1) — the
        secure default. Subclasses that carry a resolver (the AWS
        :class:`AwsInstanceOrchestrator`, which injects one) override this to
        consult it, falling back to the same restrictive default on any failure.
        Both source the map from the SHARED ``entitlements_core`` so every path
        agrees exactly.
        """
        from entitlements_core import DEFAULT_ENTITLEMENTS, merge_effective

        return merge_effective(dict(DEFAULT_ENTITLEMENTS))

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
            await asyncio.sleep(self._connect_grace_seconds)

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
        instance.user_id = None

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

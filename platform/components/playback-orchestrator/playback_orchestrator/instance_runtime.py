"""AWS multi-bot instance runtime — credential source (pool ∩ claims).

The AWS platform can already *provision* multiple Discord bots per guild (the
global ``hellodj/<stage>/bot-app-pool`` secret + per-guild ``BotAppClaim``
items written by the web-ui). This module supplies the *runtime* half's
credential source: it resolves which pool applications a guild may actually
connect, by intersecting the global pool with the guild's claims and keeping
only entries that hold a bot token.

:class:`PoolCredentialSource` is the AWS replacement for the on-prem SQLite
``instance.<index>.token`` / ``instance.<index>.app_id`` credential model
(Requirement 1.4): the on-prem orchestrator read per-instance keys from the
encrypted SQLite store, whereas on AWS the bot credentials live in Secrets
Manager (the pool) and the per-guild assignment lives in DynamoDB (the claims).

Design (design.md "PoolCredentialSource"):

* :meth:`pool` reads the ``hellodj/<stage>/bot-app-pool`` secret via the
  injected Secrets Manager client and parses it with the shared
  :func:`~hellodj_platform_logic.bot_app_pool.parse_pool` — the SAME parser the
  web-ui uses, so the two never drift (Requirement 1.1).
* :meth:`claimed_client_ids` reads the guild's ``GUILD#<gid>``/``BOTAPP#*``
  claim items via :meth:`CoreTable.query_pk_prefix` and returns the set of
  claimed application ids (Requirement 1.2).
* :meth:`instances_for_guild` returns the pool entries that are BOTH claimed by
  the guild AND hold a bot token (pool ∩ claims ∩ has-token) — the exact set of
  applications the runtime may open a gateway for. A claimed-but-tokenless entry
  is skipped and logged (Requirement 1.3); a token is never logged (R1.5).

Everything here is dependency-injected (secrets client + ``CoreTable`` + stage)
so it is unit-testable with fakes, mirroring the token watchdog's approach. It
holds no boto3 import of its own — the bootstrap builds the concrete clients.

Task 4 (entitlement quotas): the base enforces per-user quotas in
``assign_instance`` via ``_enforce_quotas`` / ``_resolve_effective``, both now
sourced from the SHARED ``entitlements_core`` (the base returns the restrictive
``DEFAULT_ENTITLEMENTS`` as it has no resolver seam of its own).
:class:`AwsInstanceOrchestrator` takes an INJECTED ``entitlements_resolver`` and
overrides ``_resolve_effective`` (consult the resolver; restrictive
``DEFAULT_ENTITLEMENTS`` on any failure, R4.3) + ``_enforce_quotas`` (same
decision, same shared ``entitlements_core`` helpers so web-ui and this runtime
agree exactly, R4.4).

Requirements: 1.1, 1.2, 1.3, 1.5, 4.1, 4.2, 4.3, 4.4
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Protocol

from hellodj_platform_logic.bot_app_pool import PoolApp

from .instance_identity import apply_instance_identities
from .instance_pool_source import (
    BOTAPP_SK_PREFIX,
    PoolCredentialSource,
    SecretsClient,
    bot_app_pool_secret_name,
    guild_pk,
)
from .orchestrator import BotInstance, InstanceOrchestrator, QuotaExceededError

_LOG = logging.getLogger("playback_orchestrator.instance_runtime")

# Re-exported from :mod:`instance_pool_source` (extracted to keep this file under
# the 500-line ceiling) so existing imports from ``instance_runtime`` still work.
__all__ = [
    "BOTAPP_SK_PREFIX",
    "AwsInstanceOrchestrator",
    "EntitlementsResolver",
    "PoolCredentialSource",
    "SecretsClient",
    "bot_app_pool_secret_name",
    "guild_pk",
]


class EntitlementsResolver(Protocol):
    """Resolve a Discord user id → that user's effective entitlements dict.

    The AWS path's injectable quota seam (Requirement 4.1/4.3). Mirrors the
    on-prem ``UserEntitlementResolver.effective_for_discord`` contract so the
    same resolver implementation (Discord id → Cognito sub → merged effective
    entitlements, restrictive default on failure) can back it. Implementations
    MUST return an already-effective entitlements map (stored merged over
    :data:`DEFAULT_ENTITLEMENTS`). A fake satisfies this in tests.
    """

    def effective_for_discord(self, discord_id: str | int) -> dict[str, Any]:
        """Return the user's effective entitlements (never raises to caller)."""
        ...


class AwsInstanceOrchestrator(InstanceOrchestrator):
    """AWS multi-bot orchestrator: pool ∩ claims credential source.

    The AWS port of the on-prem
    :class:`~playback_orchestrator.orchestrator.InstanceOrchestrator`. It
    overrides ONLY :meth:`initialize` — the credential source — and inherits
    ``assign_instance`` / ``release_instance`` / ``get_instance_for_channel`` /
    ``health_check`` and the quota helpers UNCHANGED (design.md: the base class
    is credential-source-agnostic below ``initialize()``; do not fork the
    assignment logic).

    On-prem, ``initialize()`` read ``playback.instance_count`` and per-instance
    ``instance.<N>.token`` / ``instance.<N>.app_id`` / ``instance.<N>.name`` from
    the encrypted SQLite store. On AWS those keys do not exist (Requirement 1.4);
    instead the connectable applications come from
    :meth:`PoolCredentialSource.instances_for_guild` — the pool ∩ the guild's
    claims ∩ has-token (Requirements 1.2, 3.5). Each surviving :class:`PoolApp`
    becomes one voice-only :class:`BotInstance` (``application_id = client_id``,
    R-context), connected in parallel with per-instance isolation exactly as the
    on-prem ``_connect_instance`` (Requirement 2.2): a single instance's connect
    failure marks THAT instance ``unhealthy`` and never crashes the loop or the
    other instances.

    All instances share the primary's single Lavalink node/session (Requirement
    2.5) — this class opens no second node; wavelink's multi-session support
    routes each secondary client through the one node the primary established,
    mirroring the on-prem orchestrator.

    Entitlement quotas (Requirement 4): the inherited ``assign_instance`` calls
    ``_enforce_quotas`` when an owning ``user_id`` is supplied. This subclass
    supplies the AWS resolver seam — :meth:`_resolve_effective` consults the
    injected ``entitlements_resolver`` and falls back to the restrictive
    ``DEFAULT_ENTITLEMENTS`` on any failure (R4.3) — and overrides
    :meth:`_enforce_quotas` to source the pure decision helpers from the shared
    ``entitlements_core`` module (R4.4), keeping the decision identical to the
    base.

    Args:
        primary_bot: The primary process handle (health/loop owner) — passed
            through to the base initializer. On AWS the "primary" is the
            standing orchestrator process rather than a discord.py bot; only the
            base's stored reference is used.
        registry: The session registry, passed through to the base.
        source: The :class:`PoolCredentialSource` resolving connectable pool
            apps per guild.
        entitlements_resolver: Optional resolver mapping a Discord user id to
            that user's effective entitlements (Requirement 4.1). ``None`` (or a
            resolver that fails) yields the restrictive default at enforcement
            time (Requirement 4.3). Also settable post-construction via
            :attr:`entitlements_resolver` so the bootstrap can wire it once the
            datastore is available.
    """

    def __init__(
        self,
        primary_bot: Any,
        registry: object,
        source: PoolCredentialSource,
        entitlements_resolver: EntitlementsResolver | None = None,
        identity_applier: Any | None = None,
        *,
        ordinal: int = 0,
        replica_count: int = 1,
    ) -> None:
        super().__init__(primary_bot, registry)
        self._source = source
        self._entitlements_resolver = entitlements_resolver
        # Shard identity (distributed-bot-sharding R1/R3). At the default
        # (0, 1) the runtime is single-shard — identical to today: it serves
        # every served guild and owns every claimed app, so the app-owner guard
        # in initialize() is a no-op (every owner ordinal is 0 == self ordinal).
        self._ordinal = ordinal
        self._replica_count = replica_count if replica_count > 1 else 1
        # Optional per-bot identity applier (name + avatar). When wired, each
        # connected secondary has its persisted BOTIDENTITY#<client_id> identity
        # applied to its own Discord application user after connect. ``None``
        # leaves pool bots at their default application identity (degraded).
        self._identity_applier = identity_applier
        # Records, per built instance index, the guild whose claim authorized
        # it (Requirement 3.5) so assignment can be scoped to the claiming guild.
        # Keyed by BotInstance.index; the base BotInstance dataclass is not
        # modified. ``None`` guild_ids are never recorded here.
        self._claimed_guild_by_index: dict[int, str] = {}

    @property
    def source(self) -> PoolCredentialSource:
        """Return the credential source (pool ∩ claims) backing this runtime."""
        return self._source

    @property
    def identity_applier(self) -> Any | None:
        """Return the per-bot identity applier, or ``None`` if unwired."""
        return self._identity_applier

    @identity_applier.setter
    def identity_applier(self, applier: Any | None) -> None:
        """Set/replace the per-bot identity applier (bootstrap-time wiring)."""
        self._identity_applier = applier

    @property
    def entitlements_resolver(self) -> EntitlementsResolver | None:
        """Return the injected entitlements resolver, or ``None`` if unwired."""
        return self._entitlements_resolver

    @entitlements_resolver.setter
    def entitlements_resolver(self, resolver: EntitlementsResolver | None) -> None:
        """Set/replace the entitlements resolver (bootstrap-time wiring)."""
        self._entitlements_resolver = resolver

    # -- entitlement quota enforcement (Requirement 4) --------------------

    def _resolve_effective(self, user_id: int) -> dict[str, Any]:
        """Resolve the owning user's effective entitlements (fail-safe, R4.3).

        Consults the injected :class:`EntitlementsResolver`
        (``effective_for_discord``) instead of the on-prem ``bot`` module the
        base method uses. On ANY failure — no resolver wired, the resolver
        raises, or it returns a falsy value — applies the restrictive
        ``DEFAULT_ENTITLEMENTS`` (limits = 1), never a more-permissive fallback
        (Requirement 4.3). The result is always a full effective map (the
        resolver returns one already; the default is passed through
        ``merge_effective`` so callers see a complete, independent copy).
        """
        from entitlements_core import DEFAULT_ENTITLEMENTS, merge_effective

        resolver = self._entitlements_resolver
        if resolver is not None:
            try:
                effective = resolver.effective_for_discord(user_id)
                if effective:
                    return dict(effective)
            except Exception as exc:  # noqa: BLE001 - fail safe to restrictive
                _LOG.warning(
                    "instance runtime: quota entitlement resolution failed for "
                    "user=%s (%s) — applying restrictive defaults",
                    user_id,
                    exc,
                )
        return merge_effective(dict(DEFAULT_ENTITLEMENTS))

    def _enforce_quotas(self, guild_id: int, user_id: int) -> None:
        """Reject an assignment that would exceed the user's quotas (R4.1/4.2).

        Same decision as the base ``_enforce_quotas`` — the per-guild bot limit
        (``effective_max_bots_per_guild``) and, for a guild the user is not
        already active in, the ``max_guilds`` limit — sourcing the pure helpers
        from the SHARED ``entitlements_core`` module (Requirement 4.4) so the
        web-ui and this runtime agree exactly. Kept as an explicit override
        (paired with the injected-resolver ``_resolve_effective`` seam) even
        though the base now uses the same shared helpers. The inherited
        assign/release counting helpers are reused unchanged.
        """
        from entitlements_core import effective_max_bots_per_guild, quota_reached

        effective = self._resolve_effective(user_id)

        # Guild limit (R4.2): only a NEW guild grows the distinct-guild count.
        active_guilds = self._user_active_guilds(user_id)
        if guild_id not in active_guilds:
            max_guilds = int(effective.get("max_guilds", 1))
            if quota_reached(len(active_guilds), max_guilds):
                raise QuotaExceededError(
                    f"Guild limit reached: you may operate in at most "
                    f"{max_guilds} guild(s)."
                )

        # Per-guild bot limit (add-instance path, R4.1).
        per_guild_limit = effective_max_bots_per_guild(effective)
        current = self._user_bot_count_in_guild(guild_id, user_id)
        if quota_reached(current, per_guild_limit):
            raise QuotaExceededError(
                f"Per-guild bot limit reached: you may run at most "
                f"{per_guild_limit} bot instance(s) in this guild."
            )

    def claimed_guild_for_index(self, index: int) -> str | None:
        """Return the guild id whose claim authorized the instance, or ``None``.

        Exposes the build-time pool∩claim binding (R3.5) so a caller can verify
        an instance is only used for the guild that claimed its application.
        """
        return self._claimed_guild_by_index.get(index)

    async def initialize(self, guild_ids: list[str] | None = None) -> None:
        """Build BotInstances from the pool ∩ claims source and connect them.

        Replaces the on-prem SQLite ``instance.<N>.*`` reads (Requirement 1.4)
        with :meth:`PoolCredentialSource.instances_for_guild`. For each guild the
        runtime serves, every connectable pool app (claimed + token-bearing,
        Requirements 1.2/1.3/3.5) becomes one voice-only :class:`BotInstance`; a
        pool app is built at most once even if several guilds claim it (the first
        claiming guild binds it). Instances are then connected in parallel with
        per-instance isolation (Requirement 2.2): a connect failure marks only
        that instance ``unhealthy``.

        Args:
            guild_ids: The guilds to build instances for. ``None``/empty leaves
                the runtime with zero instances (degraded no-op; the bootstrap
                supplies the served guilds).
        """
        import discord

        # Cross-replica single-owner map (R3.2): compute each claimed app's
        # owner ordinal ONCE from a single claims scan. When sharded (>1
        # replica) this replica connects an app ONLY if it is the app's owner,
        # so no app is ever opened by two replicas (the Discord duplicate-
        # identify invariant, R3.1). At replica_count == 1 every owner is 0 ==
        # self._ordinal, so the guard admits everything (today's behavior, R7.1).
        app_owner = (
            self._source.app_owner_map(self._replica_count)
            if self._replica_count > 1
            else {}
        )

        seen_client_ids: set[str] = set()
        index = 0
        for guild_id in guild_ids or []:
            for app in self._source.instances_for_guild(guild_id):
                if app.client_id in seen_client_ids:
                    # Already built for an earlier claiming guild — a pool app is
                    # a single Discord identity, so it connects once.
                    continue

                if self._replica_count > 1:
                    owner = app_owner.get(app.client_id)
                    if owner != self._ordinal:
                        # Owned by another replica (or unknown owner) — do NOT
                        # connect it here (R3.1/R3.2). A play request for a guild
                        # served here that needs this app is routed to the owner
                        # replica (R4), never connected locally.
                        seen_client_ids.add(app.client_id)
                        continue

                seen_client_ids.add(app.client_id)

                instance = self._build_instance(discord, index, app)
                if instance is None:
                    continue
                self._instances.append(instance)
                self._claimed_guild_by_index[index] = guild_id
                index += 1

        await self._connect_all()

        # Apply each connected secondary's persisted per-bot identity (name +
        # avatar) to its own Discord application user. Best-effort and isolated:
        # a per-bot identity failure never affects connection or the other bots.
        # The apply loop lives in ``instance_identity`` to keep this file focused.
        await apply_instance_identities(
            self._identity_applier,
            self._instances,
            self._claimed_guild_by_index,
        )

        available = sum(
            1 for inst in self._instances if inst.status == "available"
        )
        _LOG.info(
            "AWS instance orchestrator ready: %d/%d instances available",
            available,
            len(self._instances),
        )
        self._initialized = True

    # -- internals --------------------------------------------------------

    def _build_instance(
        self, discord: Any, index: int, app: PoolApp
    ) -> BotInstance | None:
        """Build one voice-only :class:`BotInstance` for a pool app.

        Mirrors the on-prem ``initialize`` client construction: minimal intents
        (guilds + voice states only). ``application_id`` is the pool app's
        ``client_id`` (Discord's application id). A non-integer ``client_id`` is
        logged and skipped (never crashes the build); no token is ever logged
        (Requirement 1.5 — ``app`` reprs credential-safe).
        """
        try:
            app_id = int(app.client_id)
        except (ValueError, TypeError):
            _LOG.error(
                "instance runtime: non-numeric client_id for %r — skipping", app
            )
            return None

        # Secondary instances only need voice — minimal intents (on-prem parity).
        intents = discord.Intents.none()
        intents.guilds = True
        intents.voice_states = True
        client = discord.Client(intents=intents)

        return BotInstance(
            index=index,
            client=client,
            token=app.bot_token,
            application_id=app_id,
            status="available",
            display_name=app.label or app.client_id,
        )

    async def _connect_all(self) -> None:
        """Connect all built instances in parallel with per-instance isolation.

        Mirrors the on-prem ``initialize`` connect phase (Requirement 2.2): each
        instance is started via the inherited ``_connect_instance``; a single
        instance's failure marks only that instance ``unhealthy`` and never
        stops the loop or affects the others.
        """
        login_tasks = [
            self._connect_instance(inst) for inst in self._instances
        ]
        if not login_tasks:
            return
        results = await asyncio.gather(*login_tasks, return_exceptions=True)
        for inst, result in zip(self._instances, results, strict=False):
            if isinstance(result, Exception):
                _LOG.error(
                    "instance runtime: failed to connect instance %d (%s): %s",
                    inst.index,
                    inst.display_name,
                    result,
                )
                inst.status = "unhealthy"

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

Task 4 (entitlement quotas): the vendored base enforces per-user quotas in
``assign_instance`` via ``_enforce_quotas`` / ``_resolve_effective``, but those
do LAZY imports of on-prem-only modules (``playback.user_entitlements``,
``bot.get_user_entitlements``) absent here. :class:`AwsInstanceOrchestrator`
takes an INJECTED ``entitlements_resolver`` and overrides ``_resolve_effective``
(restrictive ``DEFAULT_ENTITLEMENTS`` on any failure, R4.3) + ``_enforce_quotas``
(same decision, helpers sourced from the SHARED ``entitlements_core`` so web-ui,
on-prem, and this runtime agree exactly, R4.4).

Requirements: 1.1, 1.2, 1.3, 1.5, 4.1, 4.2, 4.3, 4.4
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Protocol

from hellodj_platform_logic.bot_app_pool import PoolApp, parse_pool

from .orchestrator import BotInstance, InstanceOrchestrator, QuotaExceededError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from hellodj_platform_logic.data_access import CoreTable

_LOG = logging.getLogger("playback_orchestrator.instance_runtime")

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

#: Sort-key prefix for a guild's claimed bot-application items
#: (``GUILD#<gid>``/``BOTAPP#<client_id>``, entityType ``BotAppClaim``). Matches
#: the web-ui assignment writer (``bot_app_pool.BOTAPP_SK_PREFIX``).
BOTAPP_SK_PREFIX = "BOTAPP#"


def guild_pk(guild_id: str) -> str:
    """Return the ``hellodj-core`` partition key for a guild's items.

    Matches the web-ui ``guild_admin_service.guild_pk`` so the runtime reads the
    exact partition the assignment flow wrote claims into.
    """
    return f"GUILD#{guild_id}"


def bot_app_pool_secret_name(stage: str) -> str:
    """Return the pool secret name for a stage (``hellodj/<stage>/bot-app-pool``).

    The single source of the secret-name shape, mirroring the web-ui
    ``BotAppPool.secret_name`` so the reader and the runtime resolve the same
    secret for a given stage (Requirement 1.1).
    """
    return f"hellodj/{stage}/bot-app-pool"


class SecretsClient(Protocol):
    """Subset of the boto3 ``secretsmanager`` client used to read the pool."""

    def get_secret_value(self, **kwargs: Any) -> dict[str, Any]: ...


class PoolCredentialSource:
    """Resolve the connectable bot applications for a guild (pool ∩ claims).

    Dependency-injected so it is unit-testable with a fake Secrets Manager
    client and a fake :class:`CoreTable`, mirroring the token watchdog. The pool
    read is cached after the first successful load (the pool is a small, rarely
    changing set); claim reads are always live (a guild's claims change as the
    operator assigns/releases bots).

    Args:
        secrets_client: Client exposing ``get_secret_value`` (boto3 secretsmanager).
        core_table: The ``hellodj-core`` repository used to read claim items.
        stage: Deployment stage (``beta`` / ``staging`` / ``production``) that
            selects the pool secret ``hellodj/<stage>/bot-app-pool``.
    """

    def __init__(
        self,
        secrets_client: SecretsClient,
        core_table: CoreTable,
        *,
        stage: str,
        primary_client_id: str = "",
    ) -> None:
        self._secrets = secrets_client
        self._core = core_table
        self._stage = stage
        # The Primary_Bot application id (DISCORD_CLIENT_ID). The Primary is the
        # single command-owner run by discord-bot-core and is already in every
        # guild; it must never be brought up as a secondary voice gateway (a
        # duplicate identify for the same application id is rejected by Discord).
        # Excluded from the parsed pool regardless of the secret's contents.
        self._primary_client_id = (primary_client_id or "").strip()
        self._pool_cache: list[PoolApp] | None = None

    @property
    def secret_name(self) -> str:
        """Return the pool secret name for this source's stage."""
        return bot_app_pool_secret_name(self._stage)

    def pool(self) -> list[PoolApp]:
        """Return the global bot-application pool (Requirement 1.1).

        Reads ``hellodj/<stage>/bot-app-pool`` via the injected client and parses
        it with the shared :func:`parse_pool`. Degrades to an empty pool when the
        secret is absent/denied/malformed (never raises, never logs a token) so a
        bad secret cannot crash the runtime. The result is cached after the first
        call.
        """
        if self._pool_cache is not None:
            return self._pool_cache
        raw = ""
        try:
            resp = self._secrets.get_secret_value(SecretId=self.secret_name)
            raw = resp.get("SecretString", "") or ""
        except Exception:  # noqa: BLE001 - absent/denied → empty pool
            _LOG.info(
                "instance runtime: bot-app-pool secret unavailable; empty pool"
            )
            raw = ""
        pool = parse_pool(
            raw, exclude_client_ids={self._primary_client_id}
        )
        self._pool_cache = pool
        return pool

    def claimed_client_ids(self, guild_id: str) -> set[str]:
        """Return the set of application ids the guild has claimed (R1.2).

        Reads the guild's ``BOTAPP#*`` claim items via
        :meth:`CoreTable.query_pk_prefix`. The claimed application id is taken
        from the ``BOTAPP#<client_id>`` sort key (the SK is authoritative; the
        ``data.client_id`` copy is a convenience the web-ui also writes).
        Degrades to an empty set when the read fails so a transient table error
        never crashes the runtime.
        """
        try:
            rows = self._core.query_pk_prefix(
                guild_pk(guild_id), sk_prefix=BOTAPP_SK_PREFIX
            )
        except Exception:  # noqa: BLE001 - transient read error → no claims
            _LOG.warning(
                "instance runtime: claim read failed for guild; no claims"
            )
            return set()
        claimed: set[str] = set()
        for row in rows:
            sk = str(row.get("SK", ""))
            if sk.startswith(BOTAPP_SK_PREFIX):
                claimed.add(sk[len(BOTAPP_SK_PREFIX):])
        return claimed

    def instances_for_guild(self, guild_id: str) -> list[PoolApp]:
        """Return the pool apps the guild may connect (pool ∩ claims ∩ token).

        An application is connectable for a guild only when it is (a) present in
        the global pool, (b) claimed by that guild, and (c) holds a non-empty bot
        token. A claimed-but-tokenless entry is skipped and logged (Requirement
        1.3) — it cannot open a gateway. The returned list preserves the pool's
        declared order for stable instance indexing. No token is ever logged
        (Requirement 1.5).
        """
        claimed = self.claimed_client_ids(guild_id)
        if not claimed:
            return []
        instances: list[PoolApp] = []
        for app in self.pool():
            if app.client_id not in claimed:
                continue
            if not app.has_token:
                # Claimed but no token → cannot connect; skip and log (R1.3).
                # ``app`` reprs credential-safe (label + client_id only, R1.5).
                _LOG.warning(
                    "instance runtime: skipping tokenless claimed app %r "
                    "for guild (no bot_token → cannot connect)",
                    app,
                )
                continue
            instances.append(app)
        return instances


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
    ) -> None:
        super().__init__(primary_bot, registry)
        self._source = source
        self._entitlements_resolver = entitlements_resolver
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
        already active in, the ``max_guilds`` limit — but sources the pure
        helpers from the SHARED ``entitlements_core`` module (Requirement 4.4)
        rather than the on-prem ``playback.user_entitlements`` the base imports,
        so the web-ui, on-prem orchestrator, and this runtime agree exactly. The
        inherited assign/release counting helpers are reused unchanged.
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

        seen_client_ids: set[str] = set()
        index = 0
        for guild_id in guild_ids or []:
            for app in self._source.instances_for_guild(guild_id):
                if app.client_id in seen_client_ids:
                    # Already built for an earlier claiming guild — a pool app is
                    # a single Discord identity, so it connects once.
                    continue
                seen_client_ids.add(app.client_id)

                instance = self._build_instance(discord, index, app)
                if instance is None:
                    continue
                self._instances.append(instance)
                self._claimed_guild_by_index[index] = guild_id
                index += 1

        await self._connect_all()

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

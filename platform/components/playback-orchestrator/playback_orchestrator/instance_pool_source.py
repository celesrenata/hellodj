"""Credential source for the AWS multi-bot instance runtime (pool ∩ claims).

Extracted from :mod:`playback_orchestrator.instance_runtime` (which keeps the
:class:`~playback_orchestrator.instance_runtime.AwsInstanceOrchestrator` and
re-exports these names for backward compatibility) so each file stays under the
500-line ceiling.

:class:`PoolCredentialSource` resolves which pool applications a guild may
actually connect, by intersecting the global ``hellodj/<stage>/bot-app-pool``
secret with the guild's ``BotAppClaim`` items and keeping only entries that hold
a bot token. It is the AWS replacement for the on-prem SQLite per-instance
credential model (Requirement 1.4).

Everything here is dependency-injected (a Secrets Manager client + a
``CoreTable`` + the stage) so it is unit-testable with fakes; it holds no boto3
import of its own.

Requirements: 1.1, 1.2, 1.3, 1.5
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Protocol

from hellodj_platform_logic.bot_app_pool import PoolApp, parse_pool

if TYPE_CHECKING:  # pragma: no cover - typing only
    from hellodj_platform_logic.data_access import CoreTable

_LOG = logging.getLogger("playback_orchestrator.instance_runtime")

__all__ = [
    "BOTAPP_SK_PREFIX",
    "PoolCredentialSource",
    "SecretsClient",
    "bot_app_pool_secret_name",
    "guild_pk",
]

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

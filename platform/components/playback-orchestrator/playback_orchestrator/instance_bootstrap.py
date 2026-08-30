"""Env-driven bootstrap for the AWS multi-bot instance runtime (degraded-safe).

Mirrors :mod:`playback_orchestrator.watchdog_bootstrap`: it builds the
:class:`~playback_orchestrator.instance_runtime.PoolCredentialSource` +
:class:`~playback_orchestrator.instance_runtime.AwsInstanceOrchestrator` from the
environment and runs the orchestrator's asyncio lifecycle on a **daemon thread**
next to the health server, so a secondary bot gateway failure never crashes the
health server or the token watchdog (Requirement 2.1, 2.2). Every backing piece
degrades to ``None`` / no-op when its resource is absent, so the container comes
up regardless of whether the pool secret, DynamoDB table, or discord.py are
configured (Requirement 2.3).

Degraded no-op contract (Requirement 2.3): when NO ``bot-app-pool`` secret is
configured OR the pool is empty OR discord.py is unavailable, the runtime
self-degrades to a no-op and logs ``degraded: instance runtime disabled`` — the
health server (and the token watchdog) still come up. The degraded path never
raises into ``main()``.

Shutdown (Requirement 2.4): the returned :class:`InstanceRuntimeHandle` exposes
``stop()``, which asks the runtime's own event loop to disconnect every
Bot_Instance cleanly within the shutdown window; ``main()`` calls it from the
health server's existing SIGTERM/SIGINT path.

Guild-discovery seam: the runtime builds instances from the pool ∩ each served
guild's ``BotAppClaim`` items. The set of served guilds is discovered by
enumerating the ``BotAppClaim`` items already written by the web-ui assignment
flow (:func:`discover_claimed_guild_ids`) — a guild is "served" iff it has at
least one claim. This keeps discovery declarative (claims are the source of
truth) and needs no new persistent state. A guild with no claims yields no
instances (the intersection is empty), so an over-broad enumeration is harmless.

Env:

* ``HELLODJ_STAGE``       Deployment stage (selects ``hellodj/<stage>/bot-app-pool``).
* ``HELLODJ_CORE_TABLE``  DynamoDB table name (``hellodj-core``) holding claims.
* ``AWS_REGION``          Region for boto3 clients.
* ``HELLODJ_LAVALINK_NODE_URL``  Shared Lavalink node URL (recorded for the
  single shared node; the secondaries reuse the primary's node, R2.5).

Requirements: 2.1, 2.2, 2.3, 2.4
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import threading
from typing import TYPE_CHECKING, Any

from .instance_identity import InstanceIdentityApplier
from .instance_identity_store import build_instance_identity_store
from .instance_runtime import AwsInstanceOrchestrator, PoolCredentialSource

if TYPE_CHECKING:  # pragma: no cover - typing only
    from hellodj_platform_logic.data_access import CoreTable

_LOG = logging.getLogger("playback_orchestrator.instance_bootstrap")

#: entityType of the per-guild claim items the web-ui assignment flow writes.
_BOTAPP_CLAIM_ENTITY = "BotAppClaim"

#: How long to give the runtime's loop to disconnect instances on shutdown.
_SHUTDOWN_TIMEOUT_SECONDS = 10.0

__all__ = [
    "InstanceRuntimeHandle",
    "build_instance_runtime",
    "discover_claimed_guild_ids",
    "start_instance_runtime_thread",
]


def _core_table() -> Any | None:
    """Build a CoreTable from ``HELLODJ_CORE_TABLE``, or None (degraded).

    Mirrors ``watchdog_bootstrap._core_table`` exactly so the two bootstraps
    resolve the same table with the same conventions.
    """
    table_name = os.getenv("HELLODJ_CORE_TABLE", "").strip()
    if not table_name:
        return None
    try:
        import boto3
        from hellodj_platform_logic.data_access import CoreTable

        ddb = boto3.resource(
            "dynamodb", region_name=os.getenv("AWS_REGION", "us-east-1")
        )
        return CoreTable(ddb.Table(table_name))
    except Exception:  # noqa: BLE001 - degrade to no datastore
        return None


def _secrets_client() -> Any | None:
    """Build a Secrets Manager client, or None when boto3 is unavailable."""
    try:
        import boto3

        return boto3.client(
            "secretsmanager", region_name=os.getenv("AWS_REGION", "us-east-1")
        )
    except Exception:  # noqa: BLE001 - degrade: no secrets client
        return None


def _s3_client() -> Any | None:
    """Build an S3 client for reading avatar bytes, or None when boto3 absent."""
    try:
        import boto3

        return boto3.client(
            "s3", region_name=os.getenv("AWS_REGION", "us-east-1")
        )
    except Exception:  # noqa: BLE001 - degrade: no s3 client
        return None


def _build_identity_applier(core: Any) -> InstanceIdentityApplier | None:
    """Build the per-bot identity applier from env, or None (degraded).

    Enabled only when BOTH an S3 avatar bucket (``HELLODJ_ASSETS_BUCKET``) and an
    S3 client are available — the applier needs to read avatar bytes and write
    apply status. A missing bucket / client leaves per-bot identity apply
    disabled (name+avatar stay default) without affecting the runtime. Mirrors
    the discord-bot-core ``build_identity_applier`` enable condition so the two
    processes gate on the same env.
    """
    bucket = os.getenv("HELLODJ_ASSETS_BUCKET", "").strip()
    if not bucket:
        return None
    store = build_instance_identity_store(core)
    if store is None:
        return None
    s3 = _s3_client()
    if s3 is None:
        return None
    return InstanceIdentityApplier(store, s3, avatar_bucket=bucket)


def _discord_available() -> bool:
    """Return whether discord.py is importable (Requirement 2.3).

    ``initialize()`` imports ``discord`` to build the secondary clients; when the
    orchestrator image ships without discord.py the runtime must degrade BEFORE
    building instances rather than crash. This probes the import once via
    :mod:`importlib.util` without actually importing the (heavy) module.
    """
    import importlib.util
    import sys

    # An already-imported module (or a test double installed in sys.modules)
    # counts as available without re-resolving its spec.
    if "discord" in sys.modules:
        return True
    try:
        return importlib.util.find_spec("discord") is not None
    except Exception:  # noqa: BLE001 - a broken discord install → treat absent
        return False


def _guild_id_from_pk(pk: str) -> str | None:
    """Extract ``<gid>`` from a ``GUILD#<gid>`` partition key, or None."""
    prefix = "GUILD#"
    if pk.startswith(prefix):
        gid = pk[len(prefix):]
        return gid or None
    return None


def discover_claimed_guild_ids(core_table: CoreTable) -> list[str]:
    """Return the distinct guild ids that hold at least one ``BotAppClaim``.

    Enumerates the ``BotAppClaim`` items (written by the web-ui assignment flow)
    and derives the served guilds from their ``GUILD#<gid>`` partition keys. This
    is the guild-discovery seam: claims ARE the source of truth for "which guilds
    have extra bots", so the runtime serves exactly those. Degrades to an empty
    list on any read error (a transient scan failure must not crash the runtime);
    an empty list simply means the runtime builds no instances this pass.

    Args:
        core_table: The ``hellodj-core`` repository to enumerate claims from.

    Returns:
        Sorted, de-duplicated list of guild ids with at least one claim.
    """
    guild_ids: set[str] = set()
    try:
        for item in core_table.scan_entity(_BOTAPP_CLAIM_ENTITY):
            gid = _guild_id_from_pk(str(item.get("PK", "")))
            if gid is not None:
                guild_ids.add(gid)
    except Exception:  # noqa: BLE001 - transient scan error → no served guilds
        _LOG.warning(
            "instance runtime: claim enumeration failed; no served guilds"
        )
        return []
    return sorted(guild_ids)


def build_instance_runtime() -> tuple[AwsInstanceOrchestrator, list[str]] | None:
    """Build the AWS orchestrator + served guild ids from env, or None (degraded).

    Returns ``None`` (so nothing starts) when the runtime cannot do useful work:

    * discord.py is not importable (Requirement 2.3 — the image may omit it), OR
    * the DynamoDB table / Secrets Manager client cannot be built (no datastore
      or no boto3), OR
    * the pool secret is absent/empty (no bot applications to connect), OR
    * no served guild has any claim (nothing to connect for).

    Every one of these is a degraded no-op, not an error: the caller logs the
    single ``degraded`` line and the health server still comes up. No token is
    ever read into a log line here (the source handles the pool; this only wires
    it).
    """
    if not _discord_available():
        return None

    core = _core_table()
    secrets = _secrets_client()
    if core is None or secrets is None:
        return None

    stage = os.getenv("HELLODJ_STAGE", "").strip() or "beta"
    # Exclude the Primary_Bot (DISCORD_CLIENT_ID) from the connectable pool so
    # the runtime never opens a second gateway for the command-owner's app id.
    source = PoolCredentialSource(
        secrets,
        core,
        stage=stage,
        primary_client_id=os.getenv("DISCORD_CLIENT_ID", ""),
    )

    # No bot applications in the pool → nothing to connect (degraded no-op).
    if not source.pool():
        return None

    guild_ids = discover_claimed_guild_ids(core)
    if not guild_ids:
        return None

    # Optional per-bot identity applier (name + avatar). Enabled only when an
    # avatar bucket + S3 client are available; otherwise pool bots keep their
    # default application identity (degraded, never fatal).
    identity_applier = _build_identity_applier(core)

    orchestrator = AwsInstanceOrchestrator(
        object(), object(), source, identity_applier=identity_applier
    )
    return orchestrator, guild_ids


class InstanceRuntimeHandle:
    """Owns the runtime's daemon thread + event loop, and its clean shutdown.

    The orchestrator's :meth:`initialize` and instance teardown are async, so
    the runtime owns a dedicated asyncio event loop running on a daemon thread
    (isolation, Requirement 2.2). :meth:`stop` schedules a clean disconnect of
    every Bot_Instance on that loop and waits up to the shutdown window
    (Requirement 2.4) before returning; it never raises.
    """

    def __init__(
        self,
        orchestrator: AwsInstanceOrchestrator,
        guild_ids: list[str],
    ) -> None:
        self._orchestrator = orchestrator
        self._guild_ids = guild_ids
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run,
            name="instance-runtime",
            daemon=True,
        )

    @property
    def orchestrator(self) -> AwsInstanceOrchestrator:
        """Return the orchestrator this handle drives (for inspection/tests)."""
        return self._orchestrator

    def start(self) -> None:
        """Start the daemon thread; it runs the loop and connects instances."""
        self._thread.start()

    def _run(self) -> None:
        """Thread target: own the loop, initialize the runtime, then serve."""
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(
                self._orchestrator.initialize(self._guild_ids)
            )
            self._loop.run_forever()
        except Exception as exc:  # noqa: BLE001 - never crash the process
            _LOG.error("instance runtime loop stopped unexpectedly: %s", exc)
        finally:
            with contextlib.suppress(Exception):
                self._loop.close()

    def stop(self, timeout: float = _SHUTDOWN_TIMEOUT_SECONDS) -> None:
        """Disconnect all Bot_Instances cleanly and stop the loop (R2.4).

        Runs the disconnect on the runtime's own loop (that owns the discord
        clients), waits up to ``timeout`` for it to finish, then stops the loop
        and joins the daemon thread. Never raises — shutdown is best-effort so a
        stuck instance cannot block container termination.
        """
        if not self._thread.is_alive():
            return
        future = asyncio.run_coroutine_threadsafe(
            self._disconnect_all(), self._loop
        )
        with contextlib.suppress(Exception):
            future.result(timeout=timeout)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=timeout)

    async def _disconnect_all(self) -> None:
        """Release every connected instance, then close every client (R2.4)."""
        for inst in list(self._orchestrator.instances):
            if inst.guild_id is not None and inst.channel_id is not None:
                with contextlib.suppress(Exception):
                    await self._orchestrator.release_instance(
                        inst.guild_id, inst.channel_id
                    )
            with contextlib.suppress(Exception):
                await inst.client.close()


def start_instance_runtime_thread() -> InstanceRuntimeHandle | None:
    """Start the instance runtime on a daemon thread, or log degraded + None.

    Called from ``__main__.main`` next to :func:`start_watchdog_thread`. When the
    runtime cannot be built (degraded mode — no pool / no datastore / discord.py
    absent / no claimed guilds) it logs a single ``degraded: instance runtime
    disabled`` line and returns ``None`` so the health server still runs
    (Requirement 2.3). The thread is a daemon so it never blocks shutdown; the
    returned handle's :meth:`InstanceRuntimeHandle.stop` drives the clean
    disconnect on SIGTERM/SIGINT (Requirement 2.4).
    """
    built = build_instance_runtime()
    if built is None:
        _LOG.info(
            "degraded: instance runtime disabled "
            "(no pool/claims/datastore or discord.py absent)"
        )
        return None
    orchestrator, guild_ids = built
    handle = InstanceRuntimeHandle(orchestrator, guild_ids)
    handle.start()
    _LOG.info(
        "instance runtime thread started (serving %d guild(s))",
        len(guild_ids),
    )
    return handle

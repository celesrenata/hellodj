"""Tests for the cross-replica app-owner tiebreak (distributed-bot-sharding T3).

Covers:

* ``PoolCredentialSource.app_owner_map`` / ``app_owner_ordinal`` — each claimed
  pool app maps to exactly ONE owner ordinal, `shard(min(claiming guild ids),
  N)` (Property 2 / R3.2); scan failure → empty map (skip all, never
  double-connect); N==1 → every owner 0.
* ``AwsInstanceOrchestrator.initialize`` guard — a sharded replica connects an
  app ONLY when it is the app's owner; a remote-owned app is skipped even for a
  guild the replica serves (R3.1).

Uses a fake CoreTable exposing ``scan_entity`` + a fake ``discord`` so no AWS or
real discord.py is needed.

Tagged: Feature: distributed-bot-sharding, Property 2 (single app owner).
"""

from __future__ import annotations

import sys
import types
from typing import Any

from playback_orchestrator.instance_pool_source import (
    BOTAPP_CLAIM_ENTITY,
    BOTAPP_SK_PREFIX,
)
from playback_orchestrator.instance_runtime import (
    AwsInstanceOrchestrator,
    PoolCredentialSource,
)
from playback_orchestrator.sharding import shard

# --- fakes -----------------------------------------------------------------


class _FakeCore:
    """CoreTable-shaped fake exposing scan_entity + query_pk_prefix over claims.

    Claims are (guild_id, client_id) pairs; scan_entity('BotAppClaim') yields
    key-projected items, and query_pk_prefix serves instances_for_guild.
    """

    def __init__(self, claims: list[tuple[str, str]]) -> None:
        self._claims = claims
        self.scan_should_raise = False

    def add(self, guild_id: str, client_id: str) -> None:
        self._claims.append((guild_id, client_id))

    def scan_entity(self, entity_type: str) -> Any:
        if entity_type != BOTAPP_CLAIM_ENTITY:
            return
        if self.scan_should_raise:
            raise RuntimeError("scan failed")
        for gid, cid in self._claims:
            yield {"PK": f"GUILD#{gid}", "SK": f"{BOTAPP_SK_PREFIX}{cid}"}

    def query_pk_prefix(self, pk: str, *, sk_prefix: str | None = None) -> Any:
        gid = pk.removeprefix("GUILD#")
        rows = []
        for cg, cid in self._claims:
            if cg != gid:
                continue
            sk = f"{BOTAPP_SK_PREFIX}{cid}"
            if sk_prefix is None or sk.startswith(sk_prefix):
                rows.append({"PK": pk, "SK": sk})
        return rows


class _FakeSecrets:
    def __init__(self, payload: str) -> None:
        self._payload = payload

    def get_secret_value(self, **_kwargs: Any) -> dict[str, Any]:
        return {"SecretString": self._payload}


def _pool_json(client_ids: list[str]) -> str:
    import json

    return json.dumps(
        [
            {
                "label": f"HelloDJ-{c}",
                "client_id": c,
                "client_secret": "s",
                "bot_token": f"tok-{c}",
            }
            for c in client_ids
        ]
    )


def _source(claims: list[tuple[str, str]], client_ids: list[str]) -> tuple[
    PoolCredentialSource, _FakeCore
]:
    core = _FakeCore(list(claims))
    src = PoolCredentialSource(_FakeSecrets(_pool_json(client_ids)), core, stage="beta")
    return src, core


# --- app_owner_map / app_owner_ordinal -------------------------------------


def test_app_owner_map_single_owner_per_app() -> None:
    """Property 2 (R3.2): every claimed app maps to exactly one ordinal."""
    # app "100" claimed by guilds 5 and 9; app "200" claimed by guild 5.
    src, _ = _source([("5", "100"), ("9", "100"), ("5", "200")], ["100", "200"])
    n = 4
    owners = src.app_owner_map(n)
    # Owner is shard(min claiming guild, n) — deterministic single owner.
    assert owners["100"] == shard("5", n)  # min("5","9") == "5"
    assert owners["200"] == shard("5", n)


def test_app_owner_map_min_guild_tiebreak_stable() -> None:
    """The owner is the smallest claiming guild id regardless of claim order."""
    src_a, _ = _source([("9", "100"), ("5", "100")], ["100"])
    src_b, _ = _source([("5", "100"), ("9", "100")], ["100"])
    assert src_a.app_owner_map(6)["100"] == src_b.app_owner_map(6)["100"]
    assert src_a.app_owner_map(6)["100"] == shard("5", 6)


def test_app_owner_map_scan_failure_is_empty() -> None:
    """A claims-scan failure → empty map (caller skips all; never double-connect)."""
    src, core = _source([("5", "100")], ["100"])
    core.scan_should_raise = True
    assert src.app_owner_map(4) == {}


def test_app_owner_ordinal_none_when_unclaimed() -> None:
    src, _ = _source([("5", "100")], ["100", "200"])
    assert src.app_owner_ordinal("200", 4) is None  # 200 has no claim
    assert src.app_owner_ordinal("100", 4) == shard("5", 4)


# --- initialize() guard ----------------------------------------------------


class _FakeIntents:
    guilds = False
    voice_states = False

    @staticmethod
    def none() -> _FakeIntents:
        return _FakeIntents()


class _FakeClient:
    def __init__(self, intents: Any = None) -> None:
        self.intents = intents

    async def close(self) -> None:  # pragma: no cover - not exercised here
        pass


def _install_fake_discord(monkeypatch) -> None:
    mod = types.ModuleType("discord")
    mod.Intents = _FakeIntents  # type: ignore[attr-defined]
    mod.Client = _FakeClient  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "discord", mod)


async def _init(orch: AwsInstanceOrchestrator, guilds: list[str]) -> None:
    # Avoid real gateway connects: stub the connect phase.
    async def _noop_connect_all() -> None:
        return None

    orch._connect_all = _noop_connect_all  # type: ignore[assignment]
    await orch.initialize(guilds)


def test_initialize_owner_guard_numeric(monkeypatch) -> None:
    """R3.1/R3.2 with numeric client ids: only owner-ordinal apps are built."""
    import asyncio

    _install_fake_discord(monkeypatch)
    n = 2
    g0 = next(str(g) for g in range(1, 1000) if shard(str(g), n) == 0)
    g1 = next(str(g) for g in range(1, 1000) if shard(str(g), n) == 1)
    src, _ = _source([(g0, "100"), (g1, "200")], ["100", "200"])

    orch0 = AwsInstanceOrchestrator(
        object(), object(), src, ordinal=0, replica_count=n
    )
    asyncio.run(_init(orch0, [g0, g1]))
    built0 = {inst.application_id for inst in orch0.instances}
    assert built0 == {100}  # only app 100 (owner 0); app 200 skipped (owner 1)

    src1, _ = _source([(g0, "100"), (g1, "200")], ["100", "200"])
    orch1 = AwsInstanceOrchestrator(
        object(), object(), src1, ordinal=1, replica_count=n
    )
    asyncio.run(_init(orch1, [g0, g1]))
    built1 = {inst.application_id for inst in orch1.instances}
    assert built1 == {200}  # only app 200 (owner 1)


def test_initialize_single_replica_connects_all(monkeypatch) -> None:
    """R7.1: replica_count==1 builds every claimed+token app (today's behavior)."""
    import asyncio

    _install_fake_discord(monkeypatch)
    src, _ = _source([("5", "100"), ("9", "200")], ["100", "200"])
    orch = AwsInstanceOrchestrator(
        object(), object(), src, ordinal=0, replica_count=1
    )
    asyncio.run(_init(orch, ["5", "9"]))
    built = {inst.application_id for inst in orch.instances}
    assert built == {100, 200}

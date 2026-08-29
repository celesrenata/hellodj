"""Tests for :class:`AwsInstanceOrchestrator` (Task 3).

Covers the AWS orchestrator's overridden ``initialize()`` — the ONLY method it
overrides — building voice-only :class:`BotInstance`s from the pool ∩ claims
credential source and connecting them with per-instance isolation. The inherited
assign / release / health / quota logic is exercised by the on-prem
``tests/test_orchestrator.py`` suite and is intentionally NOT re-tested here
(design.md: do not fork the assignment logic).

A fake ``discord`` module (with a fake ``discord.Client``) is installed in
``sys.modules`` so the real discord.py library is not required — matching the
runtime reality that discord.py may be absent (Requirement 2.3 handling lives in
the bootstrap; here we assert the build/connect behavior when it IS present).

Asserts:

* ``initialize`` builds exactly the connectable apps (claimed + token-bearing),
  in pool order, one per distinct application even across multiple claiming
  guilds (Requirements 1.2, 1.3, 3.5).
* Each built instance is voice-only (guilds + voice_states intents), with
  ``application_id`` = the pool ``client_id`` and ``token`` = the bot token.
* A single instance's connect failure marks THAT instance ``unhealthy`` and
  leaves the others ``available`` — per-instance isolation, loop never crashes
  (Requirement 2.2).
* No bot token / client secret ever appears in a log line (Requirement 1.5).
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import types
from dataclasses import dataclass, field
from typing import Any

import pytest
from hellodj_platform_logic.data_access import CoreTable

from playback_orchestrator.instance_runtime import (
    BOTAPP_SK_PREFIX,
    AwsInstanceOrchestrator,
    PoolCredentialSource,
    guild_pk,
)

_GID = "42"
_GID2 = "77"
_SECRET = "super-secret-value-should-never-render"
_TOKEN_100 = "tok-100-should-never-render"
_TOKEN_102 = "tok-102-should-never-render"

_POOL = [
    {"label": "HelloDJ-00", "client_id": "100",
     "client_secret": _SECRET, "bot_token": _TOKEN_100},
    # 101 is tokenless → must be skipped even when claimed (R1.3).
    {"label": "HelloDJ-01", "client_id": "101",
     "client_secret": _SECRET, "bot_token": ""},
    {"label": "HelloDJ-02", "client_id": "102",
     "client_secret": _SECRET, "bot_token": _TOKEN_102},
]


# ── fake discord module (installed in sys.modules for initialize()) ──────────


class _FakeIntents:
    """Stand-in for discord.Intents with the flags initialize() sets."""

    def __init__(self) -> None:
        self.guilds = False
        self.voice_states = False

    @classmethod
    def none(cls) -> _FakeIntents:
        return cls()


class _FakeClient:
    """Fake discord.Client: records the token it was started with.

    ``fail_start`` makes ``start`` raise so we can assert per-instance
    isolation. ``ready`` controls ``is_ready`` for the post-connect probe.
    """

    def __init__(self, *, intents: _FakeIntents | None = None) -> None:
        self.intents = intents
        self.started_with: str | None = None
        self.fail_start = False
        self._ready = True
        self.voice_clients: list[Any] = []

    async def start(self, token: str) -> None:
        self.started_with = token
        if self.fail_start:
            raise RuntimeError("gateway connect failed")

    def is_ready(self) -> bool:
        return self._ready

    def is_closed(self) -> bool:
        return False

    @property
    def latency(self) -> float:
        return 0.05


@pytest.fixture
def fake_discord(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    """Install a fake ``discord`` module exposing Intents + Client."""
    module = types.ModuleType("discord")
    module.Intents = _FakeIntents  # type: ignore[attr-defined]
    module.Client = _FakeClient  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "discord", module)
    return module


# ── fakes for the credential source ──────────────────────────────────────────


@dataclass
class _FakeSecrets:
    payload: str

    def get_secret_value(self, **kwargs: Any) -> dict[str, Any]:
        return {"SecretString": self.payload}


@dataclass
class _FakeTable:
    _items: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)

    def get_item(self, **kwargs: Any) -> dict[str, Any]:
        item = self._items.get((kwargs["Key"]["PK"], kwargs["Key"]["SK"]))
        return {"Item": dict(item)} if item is not None else {}

    def query(self, **kwargs: Any) -> dict[str, Any]:
        values = kwargs.get("ExpressionAttributeValues", {})
        pk = values[":pk"]
        prefix = values.get(":skp")
        items = [
            dict(it)
            for (ipk, isk), it in self._items.items()
            if ipk == pk and (prefix is None or isk.startswith(prefix))
        ]
        return {"Items": items}

    def put_item(self, **kwargs: Any) -> dict[str, Any]:
        item = kwargs["Item"]
        self._items[(item["PK"], item["SK"])] = dict(item)
        return {}


def _source(pool_entries: Any = _POOL) -> tuple[PoolCredentialSource, CoreTable]:
    core = CoreTable(_FakeTable())
    payload = "" if pool_entries is None else json.dumps(pool_entries)
    return PoolCredentialSource(_FakeSecrets(payload), core, stage="beta"), core


def _claim(core: CoreTable, guild_id: str, client_id: str) -> None:
    core.put_new(
        guild_pk(guild_id),
        f"{BOTAPP_SK_PREFIX}{client_id}",
        "BotAppClaim",
        {"client_id": client_id, "label": f"L-{client_id}", "claimed_at": 1},
    )


def _orch(source: PoolCredentialSource) -> AwsInstanceOrchestrator:
    orch = AwsInstanceOrchestrator(object(), object(), source)
    # Zero the connect grace so the inherited _connect_instance does not sleep
    # for its 2s production grace period during unit tests.
    orch._connect_grace_seconds = 0.0  # noqa: SLF001
    return orch


# ── initialize builds the right instances ────────────────────────────────────


def test_initialize_builds_claimed_token_instances_in_pool_order(fake_discord):
    src, core = _source()
    _claim(core, _GID, "100")
    _claim(core, _GID, "102")
    orch = _orch(src)

    asyncio.run(orch.initialize([_GID]))

    insts = orch.instances
    assert [i.application_id for i in insts] == [100, 102]
    assert [i.display_name for i in insts] == ["HelloDJ-00", "HelloDJ-02"]
    # Tokens carried onto the instances (used only to start the gateway).
    assert [i.token for i in insts] == [_TOKEN_100, _TOKEN_102]
    # All connected successfully → available.
    assert all(i.status == "available" for i in insts)
    assert orch._initialized is True  # noqa: SLF001


def test_initialize_skips_tokenless_and_unclaimed(fake_discord):
    src, core = _source()
    # Claim 100 (token), 101 (tokenless → skip), leave 102 unclaimed.
    _claim(core, _GID, "100")
    _claim(core, _GID, "101")
    orch = _orch(src)

    asyncio.run(orch.initialize([_GID]))

    assert [i.application_id for i in orch.instances] == [100]


def test_initialize_builds_voice_only_clients(fake_discord):
    src, core = _source()
    _claim(core, _GID, "100")
    orch = _orch(src)

    asyncio.run(orch.initialize([_GID]))

    (inst,) = orch.instances
    client = inst.client
    assert isinstance(client, _FakeClient)
    # Minimal intents: guilds + voice states only (on-prem parity).
    assert client.intents.guilds is True
    assert client.intents.voice_states is True
    # It was started with its own token.
    assert client.started_with == _TOKEN_100


def test_initialize_records_claiming_guild_per_instance(fake_discord):
    src, core = _source()
    _claim(core, _GID, "100")
    _claim(core, _GID2, "102")
    orch = _orch(src)

    asyncio.run(orch.initialize([_GID, _GID2]))

    # Instance built from _GID's claim binds to _GID; from _GID2's binds to _GID2
    # (R3.5: an instance is only authorized for the guild that claimed its app).
    idx_by_app = {i.application_id: i.index for i in orch.instances}
    assert orch.claimed_guild_for_index(idx_by_app[100]) == _GID
    assert orch.claimed_guild_for_index(idx_by_app[102]) == _GID2


def test_initialize_dedups_app_claimed_by_multiple_guilds(fake_discord):
    src, core = _source()
    # Both guilds claim app 100 → a single Discord identity, built once.
    _claim(core, _GID, "100")
    _claim(core, _GID2, "100")
    orch = _orch(src)

    asyncio.run(orch.initialize([_GID, _GID2]))

    assert [i.application_id for i in orch.instances] == [100]
    # The first claiming guild binds it.
    assert orch.claimed_guild_for_index(orch.instances[0].index) == _GID


def test_initialize_no_guilds_yields_no_instances(fake_discord):
    src, core = _source()
    _claim(core, _GID, "100")
    orch = _orch(src)

    asyncio.run(orch.initialize(None))

    assert orch.instances == []
    assert orch._initialized is True  # noqa: SLF001


def test_initialize_non_numeric_client_id_skipped(fake_discord):
    src, core = _source(
        pool_entries=[
            {"label": "Bad", "client_id": "not-an-int", "bot_token": "t"},
            {"label": "Good", "client_id": "100", "bot_token": _TOKEN_100},
        ]
    )
    _claim(core, _GID, "not-an-int")
    _claim(core, _GID, "100")
    orch = _orch(src)

    asyncio.run(orch.initialize([_GID]))

    # The bogus id is skipped; the valid one builds.
    assert [i.application_id for i in orch.instances] == [100]


# ── per-instance isolation on connect failure (R2.2) ─────────────────────────


def test_connect_failure_marks_only_that_instance_unhealthy(fake_discord, monkeypatch):
    src, core = _source()
    _claim(core, _GID, "100")
    _claim(core, _GID, "102")
    orch = _orch(src)

    # Make the gateway start for app 102 fail while app 100 succeeds, then
    # assert only 102 is marked unhealthy (per-instance isolation, R2.2). We
    # patch _connect_instance so the failing one raises by token.
    async def _fake_connect(inst):
        if inst.token == _TOKEN_102:
            raise RuntimeError("gateway connect failed")
        await inst.client.start(inst.token)  # success path

    monkeypatch.setattr(orch, "_connect_instance", _fake_connect)

    asyncio.run(orch.initialize([_GID]))

    by_app = {i.application_id: i for i in orch.instances}
    assert by_app[100].status == "available"  # unaffected neighbor
    assert by_app[102].status == "unhealthy"  # the failing instance
    # The loop completed and built both instances despite the failure.
    assert set(by_app) == {100, 102}


def test_all_connects_failing_still_completes(fake_discord, monkeypatch):
    src, core = _source()
    _claim(core, _GID, "100")
    _claim(core, _GID, "102")
    orch = _orch(src)

    async def _always_fail(inst):
        raise RuntimeError("connect failed")

    monkeypatch.setattr(orch, "_connect_instance", _always_fail)

    asyncio.run(orch.initialize([_GID]))

    assert all(i.status == "unhealthy" for i in orch.instances)
    assert orch._initialized is True  # noqa: SLF001


# ── credential safety (R1.5) ─────────────────────────────────────────────────


def test_initialize_never_logs_token_material(fake_discord, caplog):
    src, core = _source()
    _claim(core, _GID, "100")
    _claim(core, _GID, "101")  # tokenless → skip-log path
    orch = _orch(src)

    with caplog.at_level(logging.DEBUG):
        asyncio.run(orch.initialize([_GID]))

    joined = "\n".join(r.getMessage() for r in caplog.records)
    assert _SECRET not in joined
    assert _TOKEN_100 not in joined
    assert _TOKEN_102 not in joined

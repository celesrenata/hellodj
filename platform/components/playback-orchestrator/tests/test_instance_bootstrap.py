"""Unit tests for the AWS multi-bot instance runtime bootstrap (Task 5).

Exercises :mod:`playback_orchestrator.instance_bootstrap` — the env-driven
daemon-thread host that mirrors ``watchdog_bootstrap`` — plus its wiring into
``__main__``:

* **degraded no-op** — with no pool / no claims / no datastore / discord.py
  absent, ``build_instance_runtime`` returns ``None`` and
  ``start_instance_runtime_thread`` logs the single
  ``degraded: instance runtime disabled`` line and returns ``None`` (the health
  server is unaffected because nothing is started) — Requirement 2.3.
* **guild discovery** — ``discover_claimed_guild_ids`` returns exactly the
  guilds holding a ``BotAppClaim`` (the seam), de-duplicated, and degrades to
  an empty list on a scan error.
* **startup wiring** — with a populated pool + claims + a fake ``discord``,
  the bootstrap builds an :class:`AwsInstanceOrchestrator`, connects the
  claimed+token instances on its own loop, and ``stop()`` disconnects them
  cleanly (Requirement 2.1, 2.2, 2.4).
* **__main__ integration** — ``main`` calls ``start_instance_runtime_thread``
  next to ``start_watchdog_thread`` and never lets a degraded runtime crash the
  health server (Requirement 2.1, 2.3).

All tests use fakes (in-memory ``CoreTable`` + fake Secrets Manager + a fake
``discord`` module) so no live AWS or real discord.py is required.
"""

from __future__ import annotations

import json
import logging
import sys
import time
import types
from dataclasses import dataclass, field
from typing import Any

import pytest
from hellodj_platform_logic.data_access import CoreTable

from playback_orchestrator import instance_bootstrap
from playback_orchestrator.instance_bootstrap import (
    InstanceRuntimeHandle,
    build_instance_runtime,
    discover_claimed_guild_ids,
    start_instance_runtime_thread,
)
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

_POOL = [
    {"label": "HelloDJ-00", "client_id": "100",
     "client_secret": _SECRET, "bot_token": _TOKEN_100},
]


# ── fake discord module ───────────────────────────────────────────────────────


class _FakeIntents:
    def __init__(self) -> None:
        self.guilds = False
        self.voice_states = False

    @classmethod
    def none(cls) -> _FakeIntents:
        return cls()


class _FakeClient:
    def __init__(self, *, intents: _FakeIntents | None = None) -> None:
        self.intents = intents
        self.started_with: str | None = None
        self._ready = True
        self.closed = False
        self.voice_clients: list[Any] = []

    async def start(self, token: str) -> None:
        self.started_with = token

    async def close(self) -> None:
        self.closed = True

    def is_ready(self) -> bool:
        return self._ready

    def is_closed(self) -> bool:
        return self.closed

    @property
    def latency(self) -> float:
        return 0.05


@pytest.fixture
def fake_discord(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
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
    fail_scan: bool = False

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

    def scan(self, **kwargs: Any) -> dict[str, Any]:
        if self.fail_scan:
            raise RuntimeError("scan blew up")
        et = kwargs["ExpressionAttributeValues"][":et"]
        items = [
            {"PK": ipk, "SK": isk, "entityType": it.get("entityType")}
            for (ipk, isk), it in self._items.items()
            if it.get("entityType") == et
        ]
        return {"Items": items}

    def put_item(self, **kwargs: Any) -> dict[str, Any]:
        item = kwargs["Item"]
        self._items[(item["PK"], item["SK"])] = dict(item)
        return {}


def _core(fail_scan: bool = False) -> tuple[CoreTable, _FakeTable]:
    table = _FakeTable(fail_scan=fail_scan)
    return CoreTable(table), table


def _claim(core: CoreTable, guild_id: str, client_id: str) -> None:
    core.put_new(
        guild_pk(guild_id),
        f"{BOTAPP_SK_PREFIX}{client_id}",
        "BotAppClaim",
        {"client_id": client_id, "label": f"L-{client_id}", "claimed_at": 1},
    )


def _source(core: CoreTable, pool: Any = _POOL) -> PoolCredentialSource:
    payload = "" if pool is None else json.dumps(pool)
    return PoolCredentialSource(_FakeSecrets(payload), core, stage="beta")


# ── guild discovery ───────────────────────────────────────────────────────────


def test_discover_claimed_guild_ids_returns_guilds_with_claims():
    core, _table = _core()
    _claim(core, _GID, "100")
    _claim(core, _GID, "101")  # same guild, another claim → dedup to one gid
    _claim(core, _GID2, "100")
    assert discover_claimed_guild_ids(core) == sorted([_GID, _GID2])


def test_discover_claimed_guild_ids_empty_when_no_claims():
    core, _table = _core()
    assert discover_claimed_guild_ids(core) == []


def test_discover_claimed_guild_ids_degrades_on_scan_error():
    core, _table = _core(fail_scan=True)
    assert discover_claimed_guild_ids(core) == []


# ── degraded no-op paths (Requirement 2.3) ────────────────────────────────────


def test_build_returns_none_when_discord_absent(monkeypatch):
    # discord.py not importable → degrade BEFORE building instances (R2.3).
    monkeypatch.setattr(instance_bootstrap, "_discord_available", lambda: False)
    monkeypatch.setattr(instance_bootstrap, "_core_table", lambda: _core()[0])
    monkeypatch.setattr(
        instance_bootstrap, "_secrets_client", lambda: _FakeSecrets("[]")
    )
    assert build_instance_runtime() is None


def test_build_returns_none_when_no_datastore(monkeypatch, fake_discord):
    monkeypatch.setattr(instance_bootstrap, "_core_table", lambda: None)
    monkeypatch.setattr(
        instance_bootstrap, "_secrets_client", lambda: _FakeSecrets("[]")
    )
    assert build_instance_runtime() is None


def test_build_returns_none_when_no_secrets_client(monkeypatch, fake_discord):
    core, _table = _core()
    monkeypatch.setattr(instance_bootstrap, "_core_table", lambda: core)
    monkeypatch.setattr(instance_bootstrap, "_secrets_client", lambda: None)
    assert build_instance_runtime() is None


def test_build_returns_none_when_pool_empty(monkeypatch, fake_discord):
    core, _table = _core()
    _claim(core, _GID, "100")
    monkeypatch.setattr(instance_bootstrap, "_core_table", lambda: core)
    # Empty pool secret → nothing to connect (degraded no-op).
    monkeypatch.setattr(
        instance_bootstrap, "_secrets_client", lambda: _FakeSecrets("[]")
    )
    assert build_instance_runtime() is None


def test_build_returns_none_when_no_claimed_guilds(monkeypatch, fake_discord):
    core, _table = _core()  # pool populated but NO claims anywhere
    monkeypatch.setattr(instance_bootstrap, "_core_table", lambda: core)
    monkeypatch.setattr(
        instance_bootstrap,
        "_secrets_client",
        lambda: _FakeSecrets(json.dumps(_POOL)),
    )
    assert build_instance_runtime() is None


def test_start_thread_degraded_logs_and_returns_none(monkeypatch, caplog):
    # When build returns None the starter logs the degraded line + returns None,
    # so main()'s health server is unaffected (R2.3).
    monkeypatch.setattr(
        instance_bootstrap, "build_instance_runtime", lambda: None
    )
    with caplog.at_level(logging.INFO):
        handle = start_instance_runtime_thread()
    assert handle is None
    joined = "\n".join(r.getMessage() for r in caplog.records)
    assert "degraded: instance runtime disabled" in joined


# ── startup wiring (Requirement 2.1, 2.2, 2.4) ────────────────────────────────


def test_build_wires_orchestrator_and_served_guilds(monkeypatch, fake_discord):
    core, _table = _core()
    _claim(core, _GID, "100")
    monkeypatch.setattr(instance_bootstrap, "_core_table", lambda: core)
    monkeypatch.setattr(
        instance_bootstrap,
        "_secrets_client",
        lambda: _FakeSecrets(json.dumps(_POOL)),
    )
    monkeypatch.setenv("HELLODJ_STAGE", "beta")

    built = build_instance_runtime()
    assert built is not None
    orchestrator, guild_ids = built
    assert isinstance(orchestrator, AwsInstanceOrchestrator)
    assert guild_ids == [_GID]
    assert orchestrator.source.secret_name == "hellodj/beta/bot-app-pool"


def test_handle_connects_instances_then_stops_cleanly(fake_discord):
    core, _table = _core()
    _claim(core, _GID, "100")
    orch = AwsInstanceOrchestrator(object(), object(), _source(core))
    orch._connect_grace_seconds = 0.0  # noqa: SLF001

    handle = InstanceRuntimeHandle(orch, [_GID])
    handle.start()

    # Wait for the runtime loop to initialize + connect the instance.
    deadline = time.time() + 5.0
    while time.time() < deadline and not orch.instances:
        time.sleep(0.02)

    assert [i.application_id for i in orch.instances] == [100]
    client = orch.instances[0].client
    assert isinstance(client, _FakeClient)
    assert client.started_with == _TOKEN_100

    # Clean shutdown disconnects (closes) every instance client (R2.4).
    handle.stop(timeout=5.0)
    assert client.closed is True


def test_start_thread_returns_running_handle(monkeypatch, fake_discord, caplog):
    core, _table = _core()
    _claim(core, _GID, "100")
    monkeypatch.setattr(instance_bootstrap, "_core_table", lambda: core)
    monkeypatch.setattr(
        instance_bootstrap,
        "_secrets_client",
        lambda: _FakeSecrets(json.dumps(_POOL)),
    )

    with caplog.at_level(logging.INFO):
        handle = start_instance_runtime_thread()
    try:
        assert isinstance(handle, InstanceRuntimeHandle)
        joined = "\n".join(r.getMessage() for r in caplog.records)
        assert "instance runtime thread started" in joined
        # No token material in the startup logs (R1.5 carried through).
        assert _TOKEN_100 not in joined
        assert _SECRET not in joined
    finally:
        if handle is not None:
            handle.stop(timeout=5.0)


# ── __main__ integration (Requirement 2.1, 2.3) ───────────────────────────────


def test_main_starts_instance_runtime_next_to_watchdog(monkeypatch):
    # main() must call start_instance_runtime_thread alongside the watchdog and
    # survive a degraded (None) runtime — the health server still serves.
    from playback_orchestrator import __main__ as main_mod

    calls: dict[str, bool] = {"watchdog": False, "instance": False}

    monkeypatch.setattr(
        "playback_orchestrator.watchdog_bootstrap.start_watchdog_thread",
        lambda: calls.__setitem__("watchdog", True),
    )
    monkeypatch.setattr(
        "playback_orchestrator.instance_bootstrap.start_instance_runtime_thread",
        lambda: calls.__setitem__("instance", True) or None,
    )

    started_server: dict[str, Any] = {}

    class _FakeServer:
        def __init__(self, *_a: Any, **_k: Any) -> None:
            started_server["created"] = True

        def serve_forever(self) -> None:
            # Return immediately so main() proceeds to teardown.
            return None

        def server_close(self) -> None:
            started_server["closed"] = True

    monkeypatch.setattr(main_mod, "ThreadingHTTPServer", _FakeServer)
    monkeypatch.setattr(main_mod, "_install_signal_handlers", lambda *a, **k: None)

    main_mod.main()

    assert calls["watchdog"] is True
    assert calls["instance"] is True
    assert started_server.get("created") is True
    assert started_server.get("closed") is True

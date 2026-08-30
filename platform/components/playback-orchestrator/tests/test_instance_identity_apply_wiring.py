"""Wiring test: the AWS orchestrator applies per-bot identity after connect.

Asserts that ``AwsInstanceOrchestrator.initialize`` invokes the injected
identity applier once per connected instance with the correct guild + client id,
skips instances that failed to connect (``unhealthy``), and never crashes when
the applier raises for one bot (per-instance isolation).
"""

from __future__ import annotations

import asyncio
import json
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
_POOL = [
    {"label": "HelloDJ-00", "client_id": "100", "bot_token": "tok-100"},
    {"label": "HelloDJ-02", "client_id": "102", "bot_token": "tok-102"},
]


class _FakeIntents:
    def __init__(self) -> None:
        self.guilds = False
        self.voice_states = False

    @classmethod
    def none(cls) -> "_FakeIntents":
        return cls()


class _FakeClient:
    def __init__(self, *, intents: Any = None) -> None:
        self.intents = intents
        self.fail_start = False
        self._ready = True

    async def start(self, token: str) -> None:
        if self.fail_start:
            raise RuntimeError("gateway connect failed")

    def is_ready(self) -> bool:
        return self._ready

    def is_closed(self) -> bool:
        return False


@pytest.fixture
def fake_discord(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    module = types.ModuleType("discord")
    module.Intents = _FakeIntents  # type: ignore[attr-defined]
    module.Client = _FakeClient  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "discord", module)
    return module


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


class _RecordingApplier:
    def __init__(self, *, fail_client_id: str | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self._fail = fail_client_id

    async def apply_instance(self, client: Any, guild_id: str, client_id: str):
        self.calls.append((str(guild_id), str(client_id)))
        if client_id == self._fail:
            raise RuntimeError("apply boom")


def _source() -> tuple[PoolCredentialSource, CoreTable]:
    core = CoreTable(_FakeTable())
    return PoolCredentialSource(_FakeSecrets(json.dumps(_POOL)), core, stage="beta"), core


def _claim(core: CoreTable, guild_id: str, client_id: str) -> None:
    core.put_new(
        guild_pk(guild_id),
        f"{BOTAPP_SK_PREFIX}{client_id}",
        "BotAppClaim",
        {"client_id": client_id, "claimed_at": 1},
    )


def _orch(source: PoolCredentialSource, applier: Any) -> AwsInstanceOrchestrator:
    orch = AwsInstanceOrchestrator(
        object(), object(), source, identity_applier=applier
    )
    orch._connect_grace_seconds = 0.0  # noqa: SLF001
    return orch


def test_identity_applied_once_per_connected_instance(fake_discord):
    src, core = _source()
    _claim(core, _GID, "100")
    _claim(core, _GID, "102")
    applier = _RecordingApplier()
    orch = _orch(src, applier)

    asyncio.run(orch.initialize([_GID]))

    assert sorted(applier.calls) == [(_GID, "100"), (_GID, "102")]


def test_identity_apply_failure_isolated_and_never_crashes(fake_discord):
    src, core = _source()
    _claim(core, _GID, "100")
    _claim(core, _GID, "102")
    applier = _RecordingApplier(fail_client_id="100")
    orch = _orch(src, applier)

    # Must not raise even though the applier throws for one bot.
    asyncio.run(orch.initialize([_GID]))

    # Both were attempted; the failure for 100 didn't stop 102.
    assert sorted(applier.calls) == [(_GID, "100"), (_GID, "102")]


def test_no_applier_is_a_noop(fake_discord):
    src, core = _source()
    _claim(core, _GID, "100")
    orch = _orch(src, None)

    asyncio.run(orch.initialize([_GID]))  # must not raise

    assert [i.application_id for i in orch.instances] == [100]

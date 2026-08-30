"""Tests for the per-bot ``CoreTableInstanceIdentityStore``.

Verifies the store reads/writes the per-bot ``BOTIDENTITY#<client_id>`` item
(not the legacy bare key) and that ``set_apply_status`` preserves other data
fields under the optimistic lock.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from hellodj_platform_logic.data_access import CoreTable

from playback_orchestrator.instance_identity_store import (
    build_instance_identity_store,
)


@dataclass
class _FakeTable:
    _items: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)

    def get_item(self, **kwargs: Any) -> dict[str, Any]:
        item = self._items.get((kwargs["Key"]["PK"], kwargs["Key"]["SK"]))
        return {"Item": dict(item)} if item is not None else {}

    def put_item(self, **kwargs: Any) -> dict[str, Any]:
        item = kwargs["Item"]
        self._items[(item["PK"], item["SK"])] = dict(item)
        return {}


def _store():
    core = CoreTable(_FakeTable())
    return build_instance_identity_store(core), core


def test_build_returns_none_without_table():
    assert build_instance_identity_store(None) is None


def test_reads_per_bot_key_not_legacy():
    store, core = _store()
    core.put_new(
        "GUILD#1", "BOTIDENTITY#9", "GuildBotIdentity",
        {"nickname": "DJ Cat", "avatar_version": "abc"},
    )
    # A legacy bare-key item must NOT be what the per-bot store returns.
    core.put_new(
        "GUILD#1", "BOTIDENTITY", "GuildBotIdentity", {"nickname": "LEGACY"}
    )

    data = store.get_identity_data("1", client_id="9")
    assert data["nickname"] == "DJ Cat"
    assert data["avatar_version"] == "abc"


def test_get_returns_none_when_absent():
    store, _core = _store()
    assert store.get_identity_data("1", client_id="nope") is None


def test_set_apply_status_preserves_other_fields():
    store, core = _store()
    core.put_new(
        "GUILD#1", "BOTIDENTITY#9", "GuildBotIdentity",
        {"nickname": "DJ Cat", "avatar_version": "abc", "desired_at": 5},
    )

    store.set_apply_status(
        "1",
        client_id="9",
        status="applied",
        applied_at=1234,
        apply_error="",
        applied_version="name=DJ Cat\x1favatar=abc",
    )

    data = store.get_identity_data("1", client_id="9")
    assert data["nickname"] == "DJ Cat"       # preserved
    assert data["desired_at"] == 5            # preserved
    assert data["apply_status"] == "applied"  # written
    assert data["applied_at"] == 1234
    assert data["applied_version"] == "name=DJ Cat\x1favatar=abc"

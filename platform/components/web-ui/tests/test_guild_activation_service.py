"""Tests for the per-guild activation key service (on-prem /activate parity).

Covers `guild_activation_service` over an in-memory fake CoreTable:

* `get_or_create_key` mints a key on first view and is idempotent (a second
  call returns the same key, never regenerating one already handed to an admin);
* `status` reports the stored key + activated flag (empty/False when absent);
* `regenerate_key` mints a NEW key AND clears activation (invalidating the old
  key), matching the on-prem deactivate flow.
"""

from __future__ import annotations

from typing import Any

from hellodj_platform_logic.data_access import CoreTable

from guild_activation_service import ACTIVATION_SK, GuildActivationService
from guild_admin_service import guild_pk


class _ClientError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class _FakeTable:
    def __init__(self) -> None:
        self._items: dict[tuple[str, str], dict[str, Any]] = {}

    def get_item(self, **kwargs: Any) -> dict[str, Any]:
        key = (kwargs["Key"]["PK"], kwargs["Key"]["SK"])
        item = self._items.get(key)
        return {"Item": dict(item)} if item is not None else {}

    def put_item(self, **kwargs: Any) -> dict[str, Any]:
        item = kwargs["Item"]
        key = (item["PK"], item["SK"])
        condition = kwargs.get("ConditionExpression")
        values = kwargs.get("ExpressionAttributeValues", {})
        existing = self._items.get(key)
        if condition == "attribute_not_exists(PK)" and existing is not None:
            raise _ClientError("ConditionalCheckFailedException")
        if condition == "attribute_not_exists(version)" and existing is not None:
            raise _ClientError("ConditionalCheckFailedException")
        if condition == "version = :expected":
            if existing is None or existing.get("version") != values[":expected"]:
                raise _ClientError("ConditionalCheckFailedException")
        self._items[key] = dict(item)
        return {}

    def query(self, **kwargs: Any) -> dict[str, Any]:  # pragma: no cover
        return {"Items": []}

    def delete_item(self, **kwargs: Any) -> dict[str, Any]:  # pragma: no cover
        self._items.pop((kwargs["Key"]["PK"], kwargs["Key"]["SK"]), None)
        return {}


def _service() -> tuple[GuildActivationService, CoreTable, _FakeTable]:
    table = _FakeTable()
    core = CoreTable(table)
    return GuildActivationService(core), core, table


def test_get_or_create_key_mints_once_and_is_idempotent() -> None:
    svc, _, _ = _service()

    key1 = svc.get_or_create_key("g1")
    key2 = svc.get_or_create_key("g1")

    assert key1
    assert key1 == key2


def test_status_reflects_stored_key_and_activation() -> None:
    svc, _, _ = _service()
    key = svc.get_or_create_key("g1")

    status = svc.status("g1")

    assert status["key"] == key
    assert status["activated"] is False


def test_status_empty_when_absent() -> None:
    svc, _, _ = _service()
    assert svc.status("nope") == {"key": "", "activated": False}


def test_regenerate_key_changes_key_and_clears_activation() -> None:
    svc, core, _ = _service()
    original = svc.get_or_create_key("g1")
    # Simulate an activated guild.
    core.update_with_lock(
        guild_pk("g1"),
        ACTIVATION_SK,
        lambda d: {**d, "activated": True},
        entity_type="GuildActivation",
    )
    assert svc.status("g1")["activated"] is True

    new_key = svc.regenerate_key("g1")

    assert new_key != original
    status = svc.status("g1")
    assert status["key"] == new_key
    assert status["activated"] is False

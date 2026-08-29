"""Tests for account-level delegated administration (co-admins by Discord id).

Covers :mod:`account_admin_service` — the surface the account page and the
Discord-login path rely on for Option B (an appointed Discord id logs straight
into the owner's account):

* ``appoint_admin`` persists an AccountAdmin edge keyed by Discord id under the
  owner's ``USER#<owner_sub>`` partition, sets the GSI1 reverse index
  (``DISCORD#<id>`` / ``ACCTADMIN#<owner_sub>``), and is idempotent.
* ``list_admins`` / ``remove_admin`` enumerate and delete edges scoped to one
  owner (no cross-owner leakage).
* ``owner_for_discord`` resolves the owner a Discord id co-administers via the
  GSI1 reverse index, deterministically choosing the lexically-first owner when
  a Discord id was appointed on more than one account.

Uses the same in-memory fake ``TableLike`` shape as the guild-admin tests
(PK access, GSI1 query, base-PK-prefix query) — no AWS.
"""

from __future__ import annotations

from typing import Any

from hellodj_platform_logic.data_access import CoreTable

from account_admin_service import (
    AccountAdminService,
    acct_admin_sk,
    user_pk,
)


class _ClientError(Exception):
    """Minimal botocore-shaped client error for the fake table."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class _FakeTable:
    """In-memory ``TableLike`` supporting PK access, GSI1 + base-PK queries."""

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
            expected = values[":expected"]
            if existing is None or existing.get("version") != expected:
                raise _ClientError("ConditionalCheckFailedException")
        self._items[key] = dict(item)
        return {}

    def query(self, **kwargs: Any) -> dict[str, Any]:
        expr = kwargs["KeyConditionExpression"]
        values = kwargs["ExpressionAttributeValues"]
        pk = values[":pk"]
        prefix = values.get(":skp")
        if kwargs.get("IndexName") == "GSI1":
            items = [
                dict(it)
                for it in self._items.values()
                if it.get("GSI1PK") == pk
                and (prefix is None or str(it.get("GSI1SK", "")).startswith(prefix))
            ]
            return {"Items": items}
        assert expr.startswith("PK = :pk")
        items = [
            dict(it)
            for key, it in self._items.items()
            if key[0] == pk
            and (prefix is None or str(key[1]).startswith(prefix))
        ]
        return {"Items": items}

    def delete_item(self, **kwargs: Any) -> dict[str, Any]:
        self._items.pop((kwargs["Key"]["PK"], kwargs["Key"]["SK"]), None)
        return {}


def _service() -> tuple[AccountAdminService, CoreTable]:
    table = _FakeTable()
    core = CoreTable(table)
    return AccountAdminService(core), core


def test_appoint_admin_persists_edge_with_gsi1_reverse_index() -> None:
    svc, core = _service()

    svc.appoint_admin("owner-1", "1234567890")

    stored = core.get(user_pk("owner-1"), acct_admin_sk("1234567890"))
    assert stored is not None
    assert stored["entityType"] == "AccountAdmin"
    assert stored["GSI1PK"] == "DISCORD#1234567890"
    assert stored["GSI1SK"] == "ACCTADMIN#owner-1"


def test_appoint_admin_is_idempotent() -> None:
    svc, _ = _service()

    svc.appoint_admin("owner-1", "111")
    svc.appoint_admin("owner-1", "111")

    admins = svc.list_admins("owner-1")
    assert [a["discord_id"] for a in admins] == ["111"]


def test_list_admins_scoped_to_one_owner() -> None:
    svc, _ = _service()
    svc.appoint_admin("owner-1", "111")
    svc.appoint_admin("owner-1", "222")
    # An edge on another owner's account must not leak into owner-1's listing.
    svc.appoint_admin("owner-2", "333")

    ids = {a["discord_id"] for a in svc.list_admins("owner-1")}
    assert ids == {"111", "222"}


def test_remove_admin_deletes_the_edge() -> None:
    svc, _ = _service()
    svc.appoint_admin("owner-1", "111")

    svc.remove_admin("owner-1", "111")

    assert svc.list_admins("owner-1") == []


def test_owner_for_discord_resolves_via_gsi1() -> None:
    svc, _ = _service()
    svc.appoint_admin("owner-1", "111")

    assert svc.owner_for_discord("111") == "owner-1"


def test_owner_for_discord_none_when_unappointed() -> None:
    svc, _ = _service()

    assert svc.owner_for_discord("999") is None


def test_owner_for_discord_deterministic_when_multi_appointed() -> None:
    svc, _ = _service()
    # Same Discord id appointed on two accounts — resolve deterministically to
    # the lexically-first owner subject so the login target is stable.
    svc.appoint_admin("owner-b", "111")
    svc.appoint_admin("owner-a", "111")

    assert svc.owner_for_discord("111") == "owner-a"

"""Re-invite self-heal tests for ``invite_service.invite`` (R1.5).

Reproduces and locks in the fix for the reported bug: after an account was
created from an invite and then deleted from Cognito, the DynamoDB invite
record lingered at ``status: invited`` and permanently blocked re-inviting the
same address. A fresh invite must now REPLACE any stale record — terminal
status, expired, or a legacy old-flow record (no ``token_hash``) — while a
genuine live token invite still blocks a duplicate.
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from hellodj_platform_logic.data_access import CoreTable

from invite_service import (
    INVITE_SK,
    InviteError,
    InviteService,
    hash_token,
    invite_pk,
)


class _ClientError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class _FakeTable:
    """In-memory TableLike: PK access, GSI1 query, conditional put, delete."""

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
        values = kwargs["ExpressionAttributeValues"]
        gsi1pk = values[":pk"]
        prefix = values.get(":skp")
        items = [
            dict(it)
            for it in self._items.values()
            if it.get("GSI1PK") == gsi1pk
            and (prefix is None or str(it.get("GSI1SK", "")).startswith(prefix))
        ]
        return {"Items": items}

    def delete_item(self, **kwargs: Any) -> dict[str, Any]:
        self._items.pop((kwargs["Key"]["PK"], kwargs["Key"]["SK"]), None)
        return {}


class _FakeCognito:
    """Reports no registered emails (list_users returns empty)."""

    def list_users(self, **kwargs: Any) -> dict[str, Any]:
        return {"Users": []}


def _service(ttl: int = 604800) -> tuple[InviteService, CoreTable]:
    core = CoreTable(_FakeTable())
    svc = InviteService(
        core, _FakeCognito(), user_pool_id="pool-1", token_ttl_seconds=ttl
    )
    return svc, core


def test_old_flow_pending_record_does_not_block_new_invite() -> None:
    """A legacy old-flow record (no token_hash) is replaced, not a hard block."""
    svc, core = _service()
    core.put_new(
        invite_pk("celes@frameshift.net"),
        INVITE_SK,
        "Invite",
        {
            "email": "celes@frameshift.net",
            "invited_by": "",
            "status": "invited",
            "username": "u-131b265b08eb4f7fb0a38491ebd37dbd",
        },
    )
    result = svc.invite("celes@frameshift.net", invited_by="owner@x.io")
    assert result["status"] == "invited"
    data = core.get(invite_pk("celes@frameshift.net"), INVITE_SK)["data"]
    assert data["token_hash"] == hash_token(result["raw_token"])
    assert "username" not in data  # the old-flow leftover is gone


def test_terminal_record_does_not_block_new_invite() -> None:
    """An accepted/revoked/expired record is replaced by a fresh invite."""
    svc, core = _service()
    for status in ("accepted", "revoked", "expired"):
        core.put_new(
            invite_pk("gus@example.com"),
            INVITE_SK,
            "Invite",
            {"email": "gus@example.com", "status": status},
        )
        result = svc.invite("gus@example.com", invited_by="owner@x.io")
        assert result["status"] == "invited"
        svc.delete("gus@example.com")


def test_live_new_flow_invite_still_blocks_duplicate() -> None:
    """A genuine, unexpired token invite still blocks a second one."""
    svc, _ = _service()
    svc.invite("hana@example.com", invited_by="owner@x.io")
    with pytest.raises(InviteError, match="pending invite"):
        svc.invite("hana@example.com", invited_by="owner@x.io")


def test_expired_new_flow_invite_does_not_block() -> None:
    """A token invite past its expiry is stale and gets replaced, not blocked."""
    svc, core = _service(ttl=1)
    first = svc.invite("iris@example.com", invited_by="owner@x.io")
    core.update_with_lock(
        invite_pk("iris@example.com"),
        INVITE_SK,
        lambda d: {**d, "expires_at": int(time.time()) - 10},
    )
    second = svc.invite("iris@example.com", invited_by="owner@x.io")
    assert second["raw_token"] != first["raw_token"]

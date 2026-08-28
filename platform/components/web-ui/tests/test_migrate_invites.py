"""Tests for the stale-invite migration (task 16).

Covers :func:`migrate_invites.migrate_stale_invites` (R1.2, R1.4): old-flow
pending invites (no ``token_hash``) are expired by default or re-sent under the
new flow, while new-flow invites (with a ``token_hash``) and non-pending invites
are left untouched. The summary counts are asserted for correctness.

Uses the same in-memory fake ``TableLike`` shape as the other invite tests
(supports PK access, GSI1 + base-PK queries) plus the fake Cognito/SES, so no
AWS is involved. Old-flow invites are simulated by writing an invite record
whose ``data`` omits ``token_hash`` directly through the fake table.
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from hellodj_platform_logic.data_access import CoreTable

from invite_email import InviteEmailService
from invite_service import (
    INVITE_INDEX_PK,
    INVITE_SK,
    InviteService,
    invite_index_sk,
    invite_pk,
)
from migrate_invites import MIGRATION_MARKER, migrate_stale_invites

_SENDER = "invites@beta.hellodj.bot"
_BASE = "https://beta.us-east-1.hellodj.bot"


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


class _FakeCognito:
    """Fake cognito-idp client reporting a fixed set of registered emails."""

    def __init__(self, registered: set[str] | None = None) -> None:
        self._registered = {e.lower() for e in (registered or set())}

    def list_users(self, **kwargs: Any) -> dict[str, Any]:
        filter_expr = kwargs.get("Filter", "")
        for email in self._registered:
            if f'"{email}"' in filter_expr:
                return {"Users": [{"Username": "u-existing"}]}
        return {"Users": []}

    def admin_create_user(self, **kwargs: Any) -> dict[str, Any]:
        return {"User": {"Username": kwargs["Username"], "Attributes": []}}

    def admin_set_user_password(self, **kwargs: Any) -> dict[str, Any]:
        return {}


class _FakeSES:
    """In-memory SES fake recording each ``send_email`` call."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def send_email(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {"MessageId": "msg-1"}


def _service(
    registered: set[str] | None = None,
) -> tuple[InviteService, CoreTable, _FakeSES]:
    core = CoreTable(_FakeTable())
    cognito = _FakeCognito(registered)
    ses = _FakeSES()
    email = InviteEmailService(ses, sender=_SENDER, public_base_url=_BASE)
    svc = InviteService(core, cognito, user_pool_id="pool-1", invite_email=email)
    return svc, core, ses


def _write_old_flow_invite(
    core: CoreTable,
    email: str,
    *,
    status: str = "invited",
    invited_by: str = "owner@x.io",
) -> None:
    """Write an *old-flow* invite record (no ``token_hash``) + its index pointer.

    Mirrors what the pre-token Cognito flow left behind: an invite item with a
    status but no ``token_hash`` / GSI1 token slot. The index pointer is added
    so the migration's ``list_invites`` enumeration surfaces it.
    """
    now = int(time.time())
    core.put_new(
        invite_pk(email),
        INVITE_SK,
        "Invite",
        {
            "email": email,
            "invited_by": invited_by,
            "expires_at": now + 3600,
            "status": status,
            "created_at": now,
        },
    )
    core.put_new(
        INVITE_INDEX_PK,
        invite_index_sk(email),
        "InviteIndex",
        {"email": email},
    )


# -- expire mode (default) -------------------------------------------------


def test_old_flow_pending_invite_is_expired_by_default() -> None:
    svc, core, ses = _service()
    _write_old_flow_invite(core, "old@example.com")

    summary = migrate_stale_invites(core, svc)

    assert summary.expired == 1
    assert summary.resent == 0
    assert summary.scanned == 1
    assert summary.skipped == 0
    # The record is flipped to expired; no email was sent in expire mode.
    stored = core.get(invite_pk("old@example.com"), INVITE_SK)
    assert stored["data"]["status"] == "expired"
    assert ses.calls == []


def test_new_flow_invite_is_left_untouched() -> None:
    svc, core, ses = _service()
    # A real new-flow invite carries a token_hash + GSI1 token slot.
    svc.invite("new@example.com", invited_by="owner@x.io")

    summary = migrate_stale_invites(core, svc)

    assert summary.scanned == 1
    assert summary.skipped == 1
    assert summary.expired == 0
    assert summary.resent == 0
    stored = core.get(invite_pk("new@example.com"), INVITE_SK)
    assert stored["data"]["status"] == "invited"
    # Only the original invite email; migration sent nothing.
    assert len(ses.calls) == 1


def test_accepted_old_flow_invite_is_skipped() -> None:
    svc, core, _ = _service()
    _write_old_flow_invite(core, "done@example.com", status="accepted")

    summary = migrate_stale_invites(core, svc)

    assert summary.scanned == 1
    assert summary.skipped == 1
    assert summary.expired == 0
    stored = core.get(invite_pk("done@example.com"), INVITE_SK)
    assert stored["data"]["status"] == "accepted"


# -- resend mode -----------------------------------------------------------


def test_old_flow_pending_invite_is_resent_in_resend_mode() -> None:
    svc, core, ses = _service()
    _write_old_flow_invite(core, "old@example.com", invited_by="boss@x.io")

    summary = migrate_stale_invites(core, svc, mode="resend")

    assert summary.resent == 1
    assert summary.expired == 0
    assert summary.migrated == 1
    # A fresh token_hash now exists (new-flow), and a branded email went out.
    stored = core.get(invite_pk("old@example.com"), INVITE_SK)
    assert stored["data"]["token_hash"]
    assert stored["data"]["status"] == "invited"
    assert len(ses.calls) == 1
    assert ses.calls[0]["Destination"]["ToAddresses"] == ["old@example.com"]


def test_resend_uses_migration_marker_when_no_invited_by() -> None:
    svc, core, _ = _service()
    _write_old_flow_invite(core, "orphan@example.com", invited_by="")

    migrate_stale_invites(core, svc, mode="resend")

    stored = core.get(invite_pk("orphan@example.com"), INVITE_SK)
    assert stored["data"]["invited_by"] == MIGRATION_MARKER


# -- mixed set + summary correctness ---------------------------------------


def test_mixed_invites_yield_correct_summary_counts() -> None:
    svc, core, _ = _service()
    _write_old_flow_invite(core, "stale1@example.com")
    _write_old_flow_invite(core, "stale2@example.com")
    _write_old_flow_invite(core, "accepted@example.com", status="accepted")
    svc.invite("fresh@example.com", invited_by="owner@x.io")

    summary = migrate_stale_invites(core, svc)

    assert summary.scanned == 4
    assert summary.expired == 2  # the two old-flow pending invites
    assert summary.skipped == 2  # accepted old-flow + new-flow invite
    assert summary.resent == 0
    assert summary.migrated == 2
    assert summary.errors == []


def test_summary_as_dict_reports_all_counts() -> None:
    svc, core, _ = _service()
    _write_old_flow_invite(core, "stale@example.com")

    summary = migrate_stale_invites(core, svc)
    data = summary.as_dict()

    assert data == {
        "scanned": 1,
        "resent": 0,
        "expired": 1,
        "skipped": 0,
        "migrated": 1,
        "errors": [],
    }


def test_invalid_mode_is_rejected() -> None:
    svc, core, _ = _service()

    with pytest.raises(ValueError, match="mode must be"):
        migrate_stale_invites(core, svc, mode="nuke")


def test_empty_datastore_yields_zero_summary() -> None:
    svc, core, _ = _service()

    summary = migrate_stale_invites(core, svc)

    assert summary.as_dict() == {
        "scanned": 0,
        "resent": 0,
        "expired": 0,
        "skipped": 0,
        "migrated": 0,
        "errors": [],
    }

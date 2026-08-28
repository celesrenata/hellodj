"""Tests for wiring ``invite_service.invite()`` to the branded SES email.

Task 6 covers sending the invitation email after recording the pending invite,
and rolling back the just-created invite record when the send fails so a retry
starts clean (R1.1, R1.2). An ``InviteService`` with no email sender preserves
the record-only behavior (graceful degrade).
"""

from __future__ import annotations

from typing import Any

import pytest
from hellodj_platform_logic.data_access import CoreTable

from invite_email import InviteEmailService
from invite_service import (
    INVITE_SK,
    InviteError,
    InviteService,
    invite_pk,
)

_SENDER = "invites@beta.hellodj.bot"
_BASE = "https://beta.us-east-1.hellodj.bot"


class _ClientError(Exception):
    """Minimal botocore-shaped client error for the fake table."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class _FakeTable:
    """In-memory ``TableLike`` supporting PK access + a GSI1 query + delete."""

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
    """Fake cognito-idp client that reports no already-registered emails."""

    def list_users(self, **kwargs: Any) -> dict[str, Any]:
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


class _BrokenSES:
    """An SES fake whose ``send_email`` always fails."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def send_email(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        raise RuntimeError("ses is down")


def _email(ses: Any) -> InviteEmailService:
    return InviteEmailService(ses, sender=_SENDER, public_base_url=_BASE)


def _service(
    ses: Any,
) -> tuple[InviteService, CoreTable]:
    """Build an InviteService wired with a real InviteEmailService over ``ses``."""
    core = CoreTable(_FakeTable())
    svc = InviteService(
        core,
        _FakeCognito(),
        user_pool_id="pool-1",
        invite_email=_email(ses),
    )
    return svc, core


def test_invite_sends_email_to_recipient_with_token_link_and_sender() -> None:
    ses = _FakeSES()
    svc, _ = _service(ses)

    result = svc.invite("sara@example.com", invited_by="owner@x.io")
    raw = result["raw_token"]

    assert len(ses.calls) == 1
    call = ses.calls[0]
    # Recipient is the invited email; sender is the configured identity.
    assert call["Destination"]["ToAddresses"] == ["sara@example.com"]
    assert call["Source"] == _SENDER
    # The registration link carries the raw token verbatim (in both bodies).
    expected_link = f"{_BASE}/invite/{raw}"
    assert expected_link in call["Message"]["Body"]["Html"]["Data"]
    assert expected_link in call["Message"]["Body"]["Text"]["Data"]


def test_invite_rolls_back_when_email_send_fails() -> None:
    ses = _BrokenSES()
    svc, core = _service(ses)

    with pytest.raises(InviteError, match="could not send"):
        svc.invite("tim@example.com", invited_by="owner@x.io")

    # The send was attempted, then the invite record was rolled back.
    assert len(ses.calls) == 1
    assert core.get(invite_pk("tim@example.com"), INVITE_SK) is None


def test_invite_rollback_allows_clean_retry() -> None:
    ses = _BrokenSES()
    svc, core = _service(ses)

    with pytest.raises(InviteError, match="could not send"):
        svc.invite("uma@example.com", invited_by="owner@x.io")

    # After rollback a retry is not blocked by a duplicate-pending check.
    svc._invite_email = _email(_FakeSES())  # noqa: SLF001 - working sender
    result = svc.invite("uma@example.com", invited_by="owner@x.io")

    assert result["status"] == "invited"
    assert core.get(invite_pk("uma@example.com"), INVITE_SK) is not None


def test_invite_without_email_sender_records_invite_only() -> None:
    # No invite_email wired: the invite is recorded, no email attempted.
    core = CoreTable(_FakeTable())
    svc = InviteService(core, _FakeCognito(), user_pool_id="pool-1")

    result = svc.invite("vera@example.com", invited_by="owner@x.io")

    assert result["status"] == "invited"
    assert core.get(invite_pk("vera@example.com"), INVITE_SK) is not None

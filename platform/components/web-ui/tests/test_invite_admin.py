"""Tests for admin invite management: list / resend / revoke (task 9).

Covers the service-layer methods the admin panel drives (R1.2, R1.4):

* ``list_invites`` enumerates every invite with an *effective* status —
  ``invited`` past its expiry is surfaced as ``expired`` without mutating the
  record, ``accepted`` / ``revoked`` are shown as stored, newest first.
* ``revoke`` flips a pending invite to ``revoked`` so its token stops resolving,
  and rejects a non-pending / unknown invite.
* ``resend`` mints a *fresh* token (invalidating the old one) and re-sends the
  branded email, and rejects an already-registered email.

Uses an in-memory fake ``TableLike`` that supports BOTH the GSI1 query (token
lookup) and the base-table PK-prefix query (invite index enumeration), plus the
fake Cognito + fake SES senders — no AWS, no real sleeps (expiry is simulated
by mutating ``expires_at``).
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from hellodj_platform_logic.data_access import CoreTable

from invite_email import InviteEmailService
from invite_service import (
    INVITE_SK,
    InviteConsumedError,
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
            # GSI1 query: match on the GSI1PK / GSI1SK attributes.
            items = [
                dict(it)
                for it in self._items.values()
                if it.get("GSI1PK") == pk
                and (prefix is None or str(it.get("GSI1SK", "")).startswith(prefix))
            ]
            return {"Items": items}
        # Base-table query on PK (used by query_pk_prefix for the index).
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
        self.created: list[dict[str, Any]] = []

    def list_users(self, **kwargs: Any) -> dict[str, Any]:
        filter_expr = kwargs.get("Filter", "")
        for email in self._registered:
            if f'"{email}"' in filter_expr:
                return {"Users": [{"Username": "u-existing"}]}
        return {"Users": []}

    def admin_create_user(self, **kwargs: Any) -> dict[str, Any]:
        self.created.append(kwargs)
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
    *,
    with_email: bool = False,
) -> tuple[InviteService, CoreTable, _FakeCognito, _FakeSES]:
    core = CoreTable(_FakeTable())
    cognito = _FakeCognito(registered)
    ses = _FakeSES()
    email = (
        InviteEmailService(ses, sender=_SENDER, public_base_url=_BASE)
        if with_email
        else None
    )
    svc = InviteService(
        core, cognito, user_pool_id="pool-1", invite_email=email
    )
    return svc, core, cognito, ses


def _expire(core: CoreTable, email: str) -> None:
    stored = core.get(invite_pk(email), INVITE_SK)
    core.update_with_lock(
        stored["PK"],
        stored["SK"],
        lambda d: {**d, "expires_at": int(time.time()) - 10},
    )


# -- list_invites ----------------------------------------------------------


def test_list_invites_empty_when_none_recorded() -> None:
    svc, _, _, _ = _service()
    assert svc.list_invites() == []


def test_list_invites_returns_recorded_pending_invite() -> None:
    svc, _, _, _ = _service()
    svc.invite("alice@example.com", invited_by="owner@x.io")

    rows = svc.list_invites()

    assert len(rows) == 1
    row = rows[0]
    assert row["email"] == "alice@example.com"
    assert row["status"] == "invited"
    assert row["invited_by"] == "owner@x.io"


def test_list_invites_surfaces_expired_status_for_lapsed_invite() -> None:
    svc, core, _, _ = _service()
    svc.invite("bob@example.com", invited_by="owner@x.io")
    _expire(core, "bob@example.com")

    rows = svc.list_invites()

    assert rows[0]["status"] == "expired"
    # The stored record is untouched (still 'invited'); only the view differs.
    stored = core.get(invite_pk("bob@example.com"), INVITE_SK)
    assert stored["data"]["status"] == "invited"


def test_list_invites_shows_accepted_status_after_registration() -> None:
    svc, _, _, _ = _service()
    raw = svc.invite("carol@example.com", invited_by="owner@x.io")["raw_token"]
    svc.register(raw)

    rows = svc.list_invites()

    assert rows[0]["status"] == "accepted"


def test_list_invites_sorted_newest_first() -> None:
    svc, core, _, _ = _service()
    svc.invite("old@example.com", invited_by="owner@x.io")
    # Force the first invite to look older than the second.
    stored = core.get(invite_pk("old@example.com"), INVITE_SK)
    core.update_with_lock(
        stored["PK"], stored["SK"], lambda d: {**d, "created_at": 1}
    )
    svc.invite("new@example.com", invited_by="owner@x.io")

    emails = [row["email"] for row in svc.list_invites()]

    assert emails[0] == "new@example.com"
    assert emails[-1] == "old@example.com"


# -- revoke ----------------------------------------------------------------


def test_revoke_flips_pending_invite_to_revoked() -> None:
    svc, core, _, _ = _service()
    raw = svc.invite("dana@example.com", invited_by="owner@x.io")["raw_token"]

    result = svc.revoke("dana@example.com")

    assert result["status"] == "revoked"
    stored = core.get(invite_pk("dana@example.com"), INVITE_SK)
    assert stored["data"]["status"] == "revoked"
    # A revoked token can no longer be resolved or consumed (R1.4).
    with pytest.raises(InviteConsumedError):
        svc.resolve_by_token(raw)
    with pytest.raises(InviteConsumedError):
        svc.consume(raw)


def test_revoke_is_case_insensitive_on_email() -> None:
    svc, core, _, _ = _service()
    svc.invite("erin@example.com", invited_by="owner@x.io")

    svc.revoke("Erin@Example.com")

    stored = core.get(invite_pk("erin@example.com"), INVITE_SK)
    assert stored["data"]["status"] == "revoked"


def test_revoke_unknown_invite_is_rejected() -> None:
    svc, _, _, _ = _service()
    with pytest.raises(InviteError, match="no pending invite"):
        svc.revoke("ghost@example.com")


def test_revoke_already_accepted_invite_is_rejected() -> None:
    svc, _, _, _ = _service()
    raw = svc.invite("finn@example.com", invited_by="owner@x.io")["raw_token"]
    svc.register(raw)

    with pytest.raises(InviteError, match="no pending invite"):
        svc.revoke("finn@example.com")


# -- resend ----------------------------------------------------------------


def test_resend_mints_a_fresh_token_and_invalidates_the_old_one() -> None:
    svc, _, _, ses = _service(with_email=True)
    first = svc.invite("gina@example.com", invited_by="owner@x.io")
    old_raw = first["raw_token"]

    second = svc.resend("gina@example.com", invited_by="owner@x.io")
    new_raw = second["raw_token"]

    assert new_raw != old_raw
    # The new token resolves; the old token no longer does (R1.4).
    assert svc.resolve_by_token(new_raw)["email"] == "gina@example.com"
    with pytest.raises(InviteConsumedError):
        svc.resolve_by_token(old_raw)
    # Two emails were sent (original + resend), each carrying its own link.
    assert len(ses.calls) == 2


def test_resend_still_lists_a_single_pending_invite() -> None:
    svc, _, _, _ = _service()
    svc.invite("huy@example.com", invited_by="owner@x.io")
    svc.resend("huy@example.com", invited_by="owner@x.io")

    rows = [r for r in svc.list_invites() if r["email"] == "huy@example.com"]

    assert len(rows) == 1
    assert rows[0]["status"] == "invited"


def test_resend_rejects_already_registered_email() -> None:
    svc, _, _, _ = _service(registered={"iris@example.com"})
    with pytest.raises(InviteError, match="already registered"):
        svc.resend("iris@example.com", invited_by="owner@x.io")

"""Tests for the Invite_Token model + lifecycle in ``invite_service``.

Task 1 covers minting (opaque token, only the hash persisted, TTL + GSI1 keys)
and duplicate rejection: a pending invite for the same email, and an
already-registered Cognito email (R1.1, R1.2, R1.3, R1.5, R7.4).

Task 2 covers token validation + single-use consume: ``resolve_by_token``
returns a still-valid invite and rejects unknown/expired/consumed tokens with
the fixed outcome, and ``consume`` flips ``invited -> accepted`` exactly once
even under concurrent attempts (R2.2, R2.3, R2.5).
"""

from __future__ import annotations

import time
import uuid
from typing import Any

import pytest
from hellodj_platform_logic.data_access import CoreTable

from invite_service import (
    DEFAULT_INVITE_TTL_SECONDS,
    INVITE_SK,
    InviteConsumedError,
    InviteError,
    InviteService,
    hash_token,
    invite_pk,
    token_gsi1pk,
)
from user_profile import UserProfileService


class _ClientError(Exception):
    """Minimal botocore-shaped client error for the fake table."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class _FakeTable:
    """In-memory ``TableLike`` supporting PK access + a GSI1 query."""

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
    """Fake cognito-idp client that reports a fixed set of registered emails.

    Records ``admin_create_user`` / ``admin_set_user_password`` calls so tests
    can assert the CONFIRMED, no-email account-creation contract (R2.2). Each
    created user is assigned a stable ``sub`` returned in the create response.
    """

    def __init__(self, registered: set[str] | None = None) -> None:
        self._registered = {e.lower() for e in (registered or set())}
        self.calls: list[dict[str, Any]] = []
        self.created: list[dict[str, Any]] = []
        self.passwords: list[dict[str, Any]] = []
        self._next_sub = 0

    def list_users(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        filter_expr = kwargs.get("Filter", "")
        for email in self._registered:
            if f'"{email}"' in filter_expr:
                return {"Users": [{"Username": "u-existing"}]}
        return {"Users": []}

    def admin_create_user(self, **kwargs: Any) -> dict[str, Any]:
        self.created.append(kwargs)
        self._next_sub += 1
        sub = f"sub-{self._next_sub}"
        return {
            "User": {
                "Username": kwargs["Username"],
                "Attributes": [{"Name": "sub", "Value": sub}],
            }
        }

    def admin_set_user_password(self, **kwargs: Any) -> dict[str, Any]:
        self.passwords.append(kwargs)
        return {}


class _ExplodingCognito(_FakeCognito):
    """A cognito fake that fails if account creation is ever attempted.

    Used to prove ``register`` rejects a bad token *before* touching Cognito.
    """

    def admin_create_user(self, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError("admin_create_user must not be called")

    def admin_set_user_password(self, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError("admin_set_user_password must not be called")


def _service(
    registered: set[str] | None = None,
    *,
    ttl: int = DEFAULT_INVITE_TTL_SECONDS,
) -> tuple[InviteService, CoreTable, _FakeCognito]:
    table = _FakeTable()
    core = CoreTable(table)
    cognito = _FakeCognito(registered)
    svc = InviteService(
        core, cognito, user_pool_id="pool-1", token_ttl_seconds=ttl
    )
    return svc, core, cognito


def test_invite_mints_token_and_records_pending_invite() -> None:
    svc, core, _ = _service()

    result = svc.invite("Alice@Example.com", invited_by="owner@x.io")

    assert result["email"] == "alice@example.com"
    assert result["status"] == "invited"
    raw = result["raw_token"]
    assert raw  # a non-empty opaque token is returned to the caller

    stored = core.get(invite_pk("alice@example.com"), INVITE_SK)
    assert stored is not None
    data = stored["data"]
    assert data["status"] == "invited"
    assert data["invited_by"] == "owner@x.io"
    assert data["email"] == "alice@example.com"
    # The default TTL (7 days) is honoured.
    assert data["expires_at"] - data["created_at"] == DEFAULT_INVITE_TTL_SECONDS


def test_only_the_hash_is_persisted_never_the_raw_token() -> None:
    svc, core, _ = _service()

    result = svc.invite("bob@example.com", invited_by="owner@x.io")
    raw = result["raw_token"]

    stored = core.get(invite_pk("bob@example.com"), INVITE_SK)
    assert stored is not None
    data = stored["data"]
    assert data["token_hash"] == hash_token(raw)
    # The raw token must never appear anywhere in the stored item (R7.4).
    assert raw not in str(stored)


def test_invite_sets_gsi1_keys_for_token_lookup() -> None:
    svc, core, _ = _service()

    result = svc.invite("carol@example.com", invited_by="owner@x.io")
    token_hash = hash_token(result["raw_token"])

    rows = core.query_gsi1(token_gsi1pk(token_hash), sk_prefix=INVITE_SK)
    assert len(rows) == 1
    assert rows[0]["data"]["email"] == "carol@example.com"


def test_configurable_ttl_is_applied() -> None:
    svc, core, _ = _service(ttl=3600)

    svc.invite("dana@example.com", invited_by="owner@x.io")

    data = core.get(invite_pk("dana@example.com"), INVITE_SK)["data"]
    assert data["expires_at"] - data["created_at"] == 3600


def test_duplicate_pending_invite_is_rejected() -> None:
    svc, _, _ = _service()
    svc.invite("erin@example.com", invited_by="owner@x.io")

    with pytest.raises(InviteError, match="pending invite"):
        svc.invite("erin@example.com", invited_by="owner@x.io")


def test_already_registered_email_is_rejected() -> None:
    svc, _, cognito = _service(registered={"frank@example.com"})

    with pytest.raises(InviteError, match="already registered"):
        svc.invite("Frank@example.com", invited_by="owner@x.io")
    assert cognito.calls  # the registration check queried Cognito


def test_invalid_email_is_rejected() -> None:
    svc, _, _ = _service()

    with pytest.raises(InviteError, match="valid email"):
        svc.invite("not-an-email", invited_by="owner@x.io")


def test_two_invites_mint_distinct_tokens() -> None:
    svc, _, _ = _service()

    a = svc.invite("g1@example.com", invited_by="owner@x.io")
    b = svc.invite("g2@example.com", invited_by="owner@x.io")

    assert a["raw_token"] != b["raw_token"]
    assert hash_token(a["raw_token"]) != hash_token(b["raw_token"])


# -- task 2: token validation + single-use consume -------------------------


def test_resolve_by_token_returns_valid_invite() -> None:
    svc, _, _ = _service()
    raw = svc.invite("hank@example.com", invited_by="owner@x.io")["raw_token"]

    data = svc.resolve_by_token(raw)

    assert data["email"] == "hank@example.com"
    assert data["status"] == "invited"


def test_resolve_by_token_rejects_unknown_token() -> None:
    svc, _, _ = _service()

    with pytest.raises(InviteConsumedError, match="used or has expired"):
        svc.resolve_by_token("never-minted")


def test_resolve_by_token_rejects_expired_token() -> None:
    svc, core, _ = _service()
    raw = svc.invite("ida@example.com", invited_by="owner@x.io")["raw_token"]
    stored = core.get(invite_pk("ida@example.com"), INVITE_SK)
    # Push expiry into the past to simulate a lapsed invite.
    core.update_with_lock(
        stored["PK"],
        stored["SK"],
        lambda d: {**d, "expires_at": int(time.time()) - 10},
    )

    with pytest.raises(InviteConsumedError, match="used or has expired"):
        svc.resolve_by_token(raw)


def test_consume_flips_invited_to_accepted() -> None:
    svc, core, _ = _service()
    raw = svc.invite("jane@example.com", invited_by="owner@x.io")["raw_token"]

    data = svc.consume(raw)

    assert data["status"] == "accepted"
    assert isinstance(data["accepted_at"], int)
    stored = core.get(invite_pk("jane@example.com"), INVITE_SK)
    assert stored["data"]["status"] == "accepted"


def test_consume_is_single_use() -> None:
    svc, _, _ = _service()
    raw = svc.invite("kyle@example.com", invited_by="owner@x.io")["raw_token"]

    svc.consume(raw)  # first use succeeds
    with pytest.raises(InviteConsumedError, match="used or has expired"):
        svc.consume(raw)  # second use is rejected


def test_consume_rejects_unknown_token() -> None:
    svc, _, _ = _service()

    with pytest.raises(InviteConsumedError, match="used or has expired"):
        svc.consume("never-minted")


def test_consume_rejects_expired_token() -> None:
    svc, core, _ = _service(ttl=1)
    raw = svc.invite("lena@example.com", invited_by="owner@x.io")["raw_token"]
    stored = core.get(invite_pk("lena@example.com"), INVITE_SK)
    core.update_with_lock(
        stored["PK"],
        stored["SK"],
        lambda d: {**d, "expires_at": int(time.time()) - 10},
    )

    with pytest.raises(InviteConsumedError, match="used or has expired"):
        svc.consume(raw)


def test_concurrent_consume_yields_exactly_one_success() -> None:
    """Two racing consumes of one token yield exactly one success (R2.5).

    A ``CoreTable`` with ``lock_retries=0`` plus a fake table whose ``get``
    returns a stale (version-1, still-``invited``) snapshot to the second caller
    models the interleaving where both readers observe ``invited`` but only the
    first commit satisfies the ``version = 1`` condition.
    """
    table = _RacyTable()
    core = CoreTable(table, lock_retries=0)
    cognito = _FakeCognito()
    svc = InviteService(core, cognito, user_pool_id="pool-1")
    raw = svc.invite("mona@example.com", invited_by="owner@x.io")["raw_token"]
    table.arm_race()  # next two reads both see the version-1 invited snapshot

    successes = 0
    failures = 0
    for _ in range(2):
        try:
            svc.consume(raw)
            successes += 1
        except InviteConsumedError:
            failures += 1

    assert successes == 1
    assert failures == 1


class _RacyTable(_FakeTable):
    """A ``_FakeTable`` that can replay a stale snapshot to force a lock race."""

    def __init__(self) -> None:
        super().__init__()
        self._race_snapshots = 0
        self._snapshot: dict[str, Any] | None = None

    def arm_race(self) -> None:
        """Serve the next two ``get_item`` reads a frozen invited snapshot."""
        for item in self._items.values():
            if item.get("entityType") == "Invite":
                self._snapshot = dict(item)
                self._race_snapshots = 2
                break

    def get_item(self, **kwargs: Any) -> dict[str, Any]:
        if self._race_snapshots > 0 and self._snapshot is not None:
            key = (kwargs["Key"]["PK"], kwargs["Key"]["SK"])
            if key == (self._snapshot["PK"], self._snapshot["SK"]):
                self._race_snapshots -= 1
                return {"Item": dict(self._snapshot)}
        return super().get_item(**kwargs)


# -- task 3: create CONFIRMED Cognito account on registration --------------


def _reg_service(
    *,
    cognito: _FakeCognito | None = None,
) -> tuple[InviteService, CoreTable, _FakeCognito, UserProfileService]:
    """Build an InviteService wired with a UserProfileService for register()."""
    table = _FakeTable()
    core = CoreTable(table)
    profiles = UserProfileService(core)
    cog = cognito if cognito is not None else _FakeCognito()
    svc = InviteService(
        core, cog, user_pool_id="pool-1", user_profiles=profiles
    )
    return svc, core, cog, profiles


def _is_valid_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        return False
    return True


def test_register_creates_confirmed_account_with_no_email() -> None:
    svc, _, cognito, _ = _reg_service()
    raw = svc.invite("nina@example.com", invited_by="owner@x.io")["raw_token"]

    account = svc.register(raw)

    # Exactly one account created, with a suppressed (no-email) message action.
    assert len(cognito.created) == 1
    create = cognito.created[0]
    assert create["MessageAction"] == "SUPPRESS"
    # Username is an opaque UUID, not the email.
    assert _is_valid_uuid(create["Username"])
    assert create["Username"] != "nina@example.com"
    # Email attribute is set and marked verified.
    attrs = {a["Name"]: a["Value"] for a in create["UserAttributes"]}
    assert attrs["email"] == "nina@example.com"
    assert attrs["email_verified"] == "true"
    # A permanent password is set so the account is CONFIRMED, no temp password.
    assert len(cognito.passwords) == 1
    pwd = cognito.passwords[0]
    assert pwd["Permanent"] is True
    assert pwd["Username"] == create["Username"]
    assert account["email"] == "nina@example.com"
    assert account["sub"] == "sub-1"


def test_register_persists_profile_bound_to_subject_with_invited_by() -> None:
    svc, _, _, profiles = _reg_service()
    raw = svc.invite("omar@example.com", invited_by="owner@x.io")["raw_token"]

    account = svc.register(raw)

    profile = profiles.get(account["sub"])
    assert profile["email"] == "omar@example.com"
    assert profile["discord_linked"] is False
    assert profile["invited_by"] == "owner@x.io"


def test_register_marks_invite_accepted_single_use() -> None:
    svc, core, cognito, _ = _reg_service()
    raw = svc.invite("pia@example.com", invited_by="owner@x.io")["raw_token"]

    svc.register(raw)

    stored = core.get(invite_pk("pia@example.com"), INVITE_SK)
    assert stored["data"]["status"] == "accepted"
    # A second registration attempt on the burnt token creates no new account.
    with pytest.raises(InviteConsumedError, match="used or has expired"):
        svc.register(raw)
    assert len(cognito.created) == 1


def test_register_rejects_bad_token_before_touching_cognito() -> None:
    svc, _, _, profiles = _reg_service(cognito=_ExplodingCognito())

    # An unknown token must raise before any account-creation call (the
    # exploding fake would assert-fail if Cognito were touched).
    with pytest.raises(InviteConsumedError, match="used or has expired"):
        svc.register("never-minted")


def test_register_rejects_expired_token_before_touching_cognito() -> None:
    svc, core, _, _ = _reg_service(cognito=_ExplodingCognito())
    raw = svc.invite("quinn@example.com", invited_by="owner@x.io")["raw_token"]
    stored = core.get(invite_pk("quinn@example.com"), INVITE_SK)
    core.update_with_lock(
        stored["PK"],
        stored["SK"],
        lambda d: {**d, "expires_at": int(time.time()) - 10},
    )

    with pytest.raises(InviteConsumedError, match="used or has expired"):
        svc.register(raw)


def test_register_without_profile_service_still_creates_account() -> None:
    table = _FakeTable()
    core = CoreTable(table)
    cognito = _FakeCognito()
    svc = InviteService(core, cognito, user_pool_id="pool-1")
    raw = svc.invite("ravi@example.com", invited_by="owner@x.io")["raw_token"]

    account = svc.register(raw)

    assert len(cognito.created) == 1
    assert account["email"] == "ravi@example.com"

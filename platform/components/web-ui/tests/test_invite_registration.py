"""Tests for :mod:`invite_registration` — the Cognito account-create step.

Covers the mechanics extracted from ``InviteService.register`` (R2.2, R2.6):

* a CONFIRMED, no-email account with an opaque UUID username, verified email,
  and the chosen name as ``preferred_username``;
* a permanent password (random when omitted);
* mapping a create-time ``AliasExistsException`` to :class:`UsernameTakenError`;
* subject extraction from the create response.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from invite_registration import (
    UsernameTakenError,
    cognito_subject,
    create_confirmed_account,
)


class _ClientError(Exception):
    """Minimal botocore-shaped client error."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class _FakeCognito:
    """Records create/password calls; returns a stable ``sub``."""

    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []
        self.passwords: list[dict[str, Any]] = []

    def admin_create_user(self, **kwargs: Any) -> dict[str, Any]:
        self.created.append(kwargs)
        return {
            "User": {
                "Username": kwargs["Username"],
                "Attributes": [{"Name": "sub", "Value": "sub-1"}],
            }
        }

    def admin_set_user_password(self, **kwargs: Any) -> dict[str, Any]:
        self.passwords.append(kwargs)
        return {}


class _NameRaceCognito(_FakeCognito):
    """Fails create with ``UsernameExistsException`` (same-instant name race)."""

    def admin_create_user(self, **kwargs: Any) -> dict[str, Any]:
        raise _ClientError("UsernameExistsException")


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        return False
    return True


def test_create_confirmed_account_uses_chosen_name_as_username() -> None:
    cognito = _FakeCognito()

    username, sub = create_confirmed_account(
        cognito,
        user_pool_id="pool-1",
        email="celes@frameshift.net",
        chosen_name="celes",
        password="Sup3rSecret!!xy",
    )

    # The chosen name IS the account Username (so the user can sign in with it),
    # and is also mirrored into preferred_username for display.
    assert username == "celes"
    assert sub == "sub-1"
    create = cognito.created[0]
    assert create["Username"] == "celes"
    assert create["MessageAction"] == "SUPPRESS"
    attrs = {a["Name"]: a["Value"] for a in create["UserAttributes"]}
    assert attrs["email"] == "celes@frameshift.net"
    assert attrs["email_verified"] == "true"
    assert attrs["preferred_username"] == "celes"
    pwd = cognito.passwords[0]
    assert pwd["Permanent"] is True
    assert pwd["Username"] == "celes"


def test_create_confirmed_account_uuid_username_when_no_name_chosen() -> None:
    cognito = _FakeCognito()

    username, _ = create_confirmed_account(
        cognito,
        user_pool_id="pool-1",
        email="nina@example.com",
        chosen_name="",
        password=None,
    )

    # No chosen name → opaque UUID Username; permanent (random) password set so
    # the account is CONFIRMED, and no preferred_username attribute is added.
    assert _is_uuid(username)
    assert cognito.passwords[0]["Permanent"] is True
    assert cognito.passwords[0]["Password"]
    attrs = {a["Name"] for a in cognito.created[0]["UserAttributes"]}
    assert "preferred_username" not in attrs


def test_create_confirmed_account_maps_username_exists_to_username_taken() -> None:
    cognito = _NameRaceCognito()

    with pytest.raises(UsernameTakenError, match="already taken"):
        create_confirmed_account(
            cognito,
            user_pool_id="pool-1",
            email="dana@example.com",
            chosen_name="dana",
            password="Sup3rSecret!!xy",
        )


def test_create_confirmed_account_reraises_other_cognito_errors() -> None:
    class _Boom(_FakeCognito):
        def admin_create_user(self, **kwargs: Any) -> dict[str, Any]:
            raise _ClientError("InternalErrorException")

    with pytest.raises(_ClientError):
        create_confirmed_account(
            _Boom(),
            user_pool_id="pool-1",
            email="x@example.com",
            chosen_name="x",
            password="Sup3rSecret!!xy",
        )


def test_cognito_subject_falls_back_to_none_when_absent() -> None:
    assert cognito_subject({"User": {"Attributes": []}}) is None
    assert cognito_subject({}) is None
    assert (
        cognito_subject({"User": {"Attributes": [{"Name": "sub", "Value": "s"}]}})
        == "s"
    )

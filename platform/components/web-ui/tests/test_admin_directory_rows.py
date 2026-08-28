"""Tests for :meth:`AdminDirectory.list_users` row normalization.

Invited accounts are created with an opaque UUID ``Username`` and the name the
invitee picked stored as the ``preferred_username`` attribute. The picker /
admin panel must therefore show the friendly name (not the UUID) while keeping
the raw ``Username`` (``login``) for account actions and surfacing the stable
``sub`` so per-user entitlements can be linked.
"""

from __future__ import annotations

from typing import Any

from admin_directory import AdminDirectory


class _FakeCognito:
    """Return a canned page of Cognito users, no admin group membership."""

    def __init__(self, users: list[dict[str, Any]]) -> None:
        self._users = users

    def list_users(self, **kwargs: Any) -> dict[str, Any]:
        return {"Users": self._users}

    def admin_list_groups_for_user(self, **kwargs: Any) -> dict[str, Any]:
        return {"Groups": []}


def _user(username: str, attrs: dict[str, str], **extra: Any) -> dict[str, Any]:
    return {
        "Username": username,
        "Attributes": [{"Name": k, "Value": v} for k, v in attrs.items()],
        "UserStatus": extra.get("status", "CONFIRMED"),
        "Enabled": extra.get("enabled", True),
    }


def test_row_prefers_preferred_username_over_uuid_login() -> None:
    """The display username is the chosen name, not the opaque UUID login."""
    cognito = _FakeCognito(
        [
            _user(
                "76039644-59a8-4249-9923-4a58e54e89e2",
                {
                    "sub": "76039644-59a8-4249-9923-4a58e54e89e2",
                    "email": "celes@frameshift.net",
                    "preferred_username": "celes",
                },
            )
        ]
    )
    rows = AdminDirectory(cognito, "pool-1").list_users()

    assert len(rows) == 1
    row = rows[0]
    assert row["username"] == "celes"
    assert row["login"] == "76039644-59a8-4249-9923-4a58e54e89e2"
    assert row["sub"] == "76039644-59a8-4249-9923-4a58e54e89e2"
    assert row["email"] == "celes@frameshift.net"


def test_row_falls_back_to_email_then_login_for_display() -> None:
    """No preferred_username -> show email; no email -> show the raw login."""
    cognito = _FakeCognito(
        [
            _user("uuid-1", {"sub": "s1", "email": "a@example.com"}),
            _user("uuid-2", {"sub": "s2"}),
        ]
    )
    rows = {r["login"]: r for r in AdminDirectory(cognito, "pool-1").list_users()}

    assert rows["uuid-1"]["username"] == "a@example.com"
    assert rows["uuid-2"]["username"] == "uuid-2"
    assert rows["uuid-1"]["sub"] == "s1"
    assert rows["uuid-2"]["sub"] == "s2"


def test_row_surfaces_sub_for_entitlement_linking() -> None:
    """Every row carries the stable Cognito ``sub`` (never empty when present)."""
    cognito = _FakeCognito(
        [_user("uuid-3", {"sub": "the-subject", "preferred_username": "dj"})]
    )
    (row,) = AdminDirectory(cognito, "pool-1").list_users()

    assert row["sub"] == "the-subject"

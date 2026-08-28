"""Tests for permanent account deletion in the admin panel.

Covers the ``AdminDirectory.delete_user`` primitive (Cognito
``admin_delete_user``) and the ``POST /admin/users/<username>/delete`` route,
including the admin-only guard shared by every admin route (unauthenticated ->
login, non-admin -> dashboard). Deletion is distinct from the reversible
enable/disable flag: it removes the account outright (R8.2).
"""

from __future__ import annotations

from typing import Any

from admin_directory import AdminDirectory


class _FakeCognito:
    """Records admin_* calls for the directory under test."""

    def __init__(self) -> None:
        self.deleted: list[str] = []

    def admin_delete_user(self, **kwargs: Any) -> dict[str, Any]:
        self.deleted.append(kwargs["Username"])
        return {}


class _FakeDirectory:
    """Minimal AdminDirectory stand-in the route drives."""

    def __init__(self) -> None:
        self.deleted: list[str] = []
        self.raise_on_delete: str | None = None

    def list_users(self) -> list[dict[str, Any]]:
        return []

    def delete_user(self, username: str) -> None:
        self.deleted.append(username)
        if self.raise_on_delete is not None:
            raise RuntimeError(self.raise_on_delete)


def _login_admin(client) -> None:
    with client.session_transaction() as sess:
        sess["user"] = {"email": "owner@x.io", "is_admin": True}


def _login_user(client) -> None:
    with client.session_transaction() as sess:
        sess["user"] = {"email": "user@x.io", "is_admin": False}


# -- AdminDirectory.delete_user --------------------------------------------


def test_directory_delete_user_calls_cognito_admin_delete() -> None:
    cognito = _FakeCognito()
    directory = AdminDirectory(cognito, "pool-1")

    directory.delete_user("u-123")

    assert cognito.deleted == ["u-123"]


# -- POST /admin/users/<username>/delete -----------------------------------


def test_admin_delete_user_calls_directory_and_refreshes(app) -> None:
    directory = _FakeDirectory()
    app.extensions["admin_directory"] = directory
    client = app.test_client()
    _login_admin(client)

    resp = client.post("/admin/users/u-123/delete")
    body = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert directory.deleted == ["u-123"]
    assert "Account u-123 deleted." in body


def test_admin_delete_user_surfaces_error(app) -> None:
    directory = _FakeDirectory()
    directory.raise_on_delete = "cognito exploded"
    app.extensions["admin_directory"] = directory
    client = app.test_client()
    _login_admin(client)

    resp = client.post("/admin/users/u-err/delete")
    body = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert "cognito exploded" in body


def test_admin_delete_user_requires_login(app) -> None:
    app.extensions["admin_directory"] = _FakeDirectory()
    client = app.test_client()

    resp = client.post("/admin/users/u-123/delete")

    assert resp.status_code in (301, 302, 303)
    assert resp.headers["Location"].endswith("/login")


def test_admin_delete_user_forbidden_for_non_admin(app) -> None:
    directory = _FakeDirectory()
    app.extensions["admin_directory"] = directory
    client = app.test_client()
    _login_user(client)

    resp = client.post("/admin/users/u-123/delete")

    assert resp.status_code in (301, 302, 303)
    assert directory.deleted == []

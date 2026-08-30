"""Tests for admin-triggered password-reset emails in the admin panel.

Covers the ``AdminDirectory.reset_password`` primitive (Cognito
``admin_reset_user_password``, which emails the user a reset code) and the
``POST /admin/users/<username>/reset-password`` route, including the admin-only
guard shared by every admin route (unauthenticated -> login, non-admin ->
dashboard). The admin never sees or sets the new password — Cognito emails the
user a reset code they complete on the recovery form (R8.2, R8.5).
"""

from __future__ import annotations

from typing import Any

from admin_directory import AdminDirectory


class _FakeCognito:
    """Records admin_* calls for the directory under test."""

    def __init__(self) -> None:
        self.reset: list[str] = []

    def admin_reset_user_password(self, **kwargs: Any) -> dict[str, Any]:
        self.reset.append(kwargs["Username"])
        return {}


class _FakeDirectory:
    """Minimal AdminDirectory stand-in the route drives."""

    def __init__(self) -> None:
        self.reset: list[str] = []
        self.raise_on_reset: str | None = None

    def list_users(self) -> list[dict[str, Any]]:
        return []

    def reset_password(self, username: str) -> None:
        self.reset.append(username)
        if self.raise_on_reset is not None:
            raise RuntimeError(self.raise_on_reset)


def _login_admin(client) -> None:
    with client.session_transaction() as sess:
        sess["user"] = {"email": "owner@x.io", "is_admin": True}


def _login_user(client) -> None:
    with client.session_transaction() as sess:
        sess["user"] = {"email": "user@x.io", "is_admin": False}


# -- AdminDirectory.reset_password -----------------------------------------


def test_directory_reset_password_calls_cognito_admin_reset() -> None:
    cognito = _FakeCognito()
    directory = AdminDirectory(cognito, "pool-1")

    directory.reset_password("u-123")

    assert cognito.reset == ["u-123"]


# -- POST /admin/users/<username>/reset-password ---------------------------


def test_admin_reset_password_calls_directory_and_refreshes(app) -> None:
    directory = _FakeDirectory()
    app.extensions["admin_directory"] = directory
    client = app.test_client()
    _login_admin(client)

    resp = client.post("/admin/users/u-123/reset-password")
    body = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert directory.reset == ["u-123"]
    assert "Password-reset email sent to u-123." in body


def test_admin_reset_password_surfaces_error(app) -> None:
    directory = _FakeDirectory()
    directory.raise_on_reset = "no verified email"
    app.extensions["admin_directory"] = directory
    client = app.test_client()
    _login_admin(client)

    resp = client.post("/admin/users/u-err/reset-password")
    body = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert "no verified email" in body


def test_admin_reset_password_requires_login(app) -> None:
    app.extensions["admin_directory"] = _FakeDirectory()
    client = app.test_client()

    resp = client.post("/admin/users/u-123/reset-password")

    assert resp.status_code in (301, 302, 303)
    assert resp.headers["Location"].endswith("/login")


def test_admin_reset_password_forbidden_for_non_admin(app) -> None:
    directory = _FakeDirectory()
    app.extensions["admin_directory"] = directory
    client = app.test_client()
    _login_user(client)

    resp = client.post("/admin/users/u-123/reset-password")

    assert resp.status_code in (301, 302, 303)
    assert directory.reset == []

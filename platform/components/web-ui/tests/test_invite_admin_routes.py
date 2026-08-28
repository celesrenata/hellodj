"""Route tests for the admin invite-management blueprint (task 9).

Covers ``GET /admin/invites`` (renders each invite's status), ``POST
/admin/invite`` (mint+send), ``POST /admin/invite/<email>/resend``, and ``POST
/admin/invite/<email>/revoke`` — including the admin-only guard shared by every
admin route (unauthenticated -> login, non-admin -> dashboard) (R1.2, R1.4).
"""

from __future__ import annotations

from typing import Any

from invite_service import InviteError


class _FakeInviteService:
    """Records calls and returns a canned invite list for the panel."""

    def __init__(self, invites: list[dict[str, Any]] | None = None) -> None:
        self._invites = invites or []
        self.invite_calls: list[tuple[str, str]] = []
        self.resend_calls: list[tuple[str, str]] = []
        self.revoke_calls: list[str] = []
        self.raise_on_revoke: str | None = None

    def list_invites(self) -> list[dict[str, Any]]:
        return list(self._invites)

    def invite(self, email: str, *, invited_by: str) -> dict[str, Any]:
        self.invite_calls.append((email, invited_by))
        return {"email": email, "status": "invited", "raw_token": "t"}

    def resend(self, email: str, *, invited_by: str) -> dict[str, Any]:
        self.resend_calls.append((email, invited_by))
        return {"email": email, "status": "invited", "raw_token": "t2"}

    def revoke(self, email: str) -> dict[str, Any]:
        self.revoke_calls.append(email)
        if self.raise_on_revoke is not None:
            raise InviteError(self.raise_on_revoke)
        return {"email": email, "status": "revoked"}


def _login_admin(client) -> None:
    with client.session_transaction() as sess:
        sess["user"] = {"email": "owner@x.io", "is_admin": True}


def _login_user(client) -> None:
    with client.session_transaction() as sess:
        sess["user"] = {"email": "user@x.io", "is_admin": False}


def _invites() -> list[dict[str, Any]]:
    return [
        {"email": "a@example.com", "status": "invited", "invited_by": "owner@x.io"},
        {"email": "b@example.com", "status": "accepted", "invited_by": "owner@x.io"},
        {"email": "c@example.com", "status": "expired", "invited_by": "owner@x.io"},
    ]


# -- GET /admin/invites ----------------------------------------------------


def test_invite_list_renders_statuses_for_admin(app) -> None:
    app.extensions["invite_service"] = _FakeInviteService(_invites())
    client = app.test_client()
    _login_admin(client)

    resp = client.get("/admin/invites")
    body = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert "a@example.com" in body
    assert "Invited" in body
    assert "Accepted" in body
    assert "Expired" in body


def test_invite_list_requires_login(app) -> None:
    app.extensions["invite_service"] = _FakeInviteService(_invites())
    client = app.test_client()

    resp = client.get("/admin/invites")

    assert resp.status_code in (301, 302, 303)
    assert resp.headers["Location"].endswith("/login")


def test_invite_list_forbidden_for_non_admin(app) -> None:
    app.extensions["invite_service"] = _FakeInviteService(_invites())
    client = app.test_client()
    _login_user(client)

    resp = client.get("/admin/invites")

    # A regular user is redirected to the dashboard, never sees the panel.
    assert resp.status_code in (301, 302, 303)
    assert resp.headers["Location"].endswith("/")


# -- POST /admin/invite ----------------------------------------------------


def test_invite_create_sends_and_refreshes_list(app) -> None:
    service = _FakeInviteService(_invites())
    app.extensions["invite_service"] = service
    client = app.test_client()
    _login_admin(client)

    resp = client.post("/admin/invite", data={"email": "new@example.com"})
    body = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert service.invite_calls == [("new@example.com", "owner@x.io")]
    assert "Invite sent to new@example.com." in body
    # The refreshed invite-list partial is returned.
    assert "a@example.com" in body


def test_invite_create_forbidden_for_non_admin(app) -> None:
    service = _FakeInviteService()
    app.extensions["invite_service"] = service
    client = app.test_client()
    _login_user(client)

    resp = client.post("/admin/invite", data={"email": "new@example.com"})

    assert resp.status_code in (301, 302, 303)
    assert service.invite_calls == []


def test_invite_create_surfaces_service_error(app) -> None:
    class _Boom(_FakeInviteService):
        def invite(self, email: str, *, invited_by: str) -> dict[str, Any]:
            raise InviteError("already has a pending invite")

    app.extensions["invite_service"] = _Boom(_invites())
    client = app.test_client()
    _login_admin(client)

    resp = client.post("/admin/invite", data={"email": "dup@example.com"})
    body = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert "already has a pending invite" in body


# -- POST /admin/invite/<email>/resend -------------------------------------


def test_invite_resend_calls_service_and_refreshes(app) -> None:
    service = _FakeInviteService(_invites())
    app.extensions["invite_service"] = service
    client = app.test_client()
    _login_admin(client)

    resp = client.post("/admin/invite/a@example.com/resend")
    body = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert service.resend_calls == [("a@example.com", "owner@x.io")]
    assert "Invite re-sent to a@example.com." in body


def test_invite_resend_forbidden_for_non_admin(app) -> None:
    service = _FakeInviteService(_invites())
    app.extensions["invite_service"] = service
    client = app.test_client()
    _login_user(client)

    resp = client.post("/admin/invite/a@example.com/resend")

    assert resp.status_code in (301, 302, 303)
    assert service.resend_calls == []


# -- POST /admin/invite/<email>/revoke -------------------------------------


def test_invite_revoke_calls_service_and_refreshes(app) -> None:
    service = _FakeInviteService(_invites())
    app.extensions["invite_service"] = service
    client = app.test_client()
    _login_admin(client)

    resp = client.post("/admin/invite/a@example.com/revoke")
    body = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert service.revoke_calls == ["a@example.com"]
    assert "Invite for a@example.com revoked." in body


def test_invite_revoke_surfaces_service_error(app) -> None:
    service = _FakeInviteService(_invites())
    service.raise_on_revoke = "no pending invite for a@example.com to revoke"
    app.extensions["invite_service"] = service
    client = app.test_client()
    _login_admin(client)

    resp = client.post("/admin/invite/a@example.com/revoke")
    body = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert "no pending invite" in body


def test_invite_revoke_forbidden_for_non_admin(app) -> None:
    service = _FakeInviteService(_invites())
    app.extensions["invite_service"] = service
    client = app.test_client()
    _login_user(client)

    resp = client.post("/admin/invite/a@example.com/revoke")

    assert resp.status_code in (301, 302, 303)
    assert service.revoke_calls == []

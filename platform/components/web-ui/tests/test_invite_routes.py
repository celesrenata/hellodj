"""Route tests for the public ``/invite/<token>`` registration page.

Task 7: the GET route requires no session. A valid token renders the
registration page bound to the invite's email (shown read-only); any invalid,
consumed, expired, or unknown token renders exactly the fixed used/expired
message and never the registration form (R2.1, R2.3).
"""

from __future__ import annotations

from invite_service import InviteConsumedError
from pages import INVITE_USED_MESSAGE


class _FakeInviteService:
    """Minimal invite service: resolves one known token, rejects the rest.

    ``register`` consumes the token: the first call for the valid token
    succeeds and the token is thereafter treated as used (both ``register`` and
    ``resolve_by_token`` reject it), mirroring the single-use guarantee.
    """

    def __init__(self, valid_token: str, email: str) -> None:
        self._valid_token = valid_token
        self._email = email
        self._consumed = False
        self.register_calls: list[str] = []

    def resolve_by_token(self, raw_token: str) -> dict[str, str]:
        if raw_token == self._valid_token and not self._consumed:
            return {"email": self._email, "status": "invited"}
        raise InviteConsumedError("invitation link has been used or has expired")

    def register(self, raw_token: str) -> dict[str, str]:
        self.register_calls.append(raw_token)
        if raw_token != self._valid_token or self._consumed:
            raise InviteConsumedError(
                "invitation link has been used or has expired"
            )
        self._consumed = True
        return {
            "email": self._email,
            "sub": "sub-123",
            "username": "user-123",
            "invited_by": "admin@example.com",
        }


def test_valid_token_renders_registration_page_with_email(app) -> None:
    app.extensions["invite_service"] = _FakeInviteService(
        "good-token", "invitee@example.com"
    )
    client = app.test_client()

    resp = client.get("/invite/good-token")

    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    # The registration form is rendered, bound to the invite's email read-only.
    assert "invitee@example.com" in body
    assert "readonly" in body
    assert 'name="password"' in body
    # The POST target is the same /invite/<token> URL (task 8 handles POST).
    assert 'action="/invite/good-token"' in body
    # It is the registration page, not the used/expired message.
    assert INVITE_USED_MESSAGE not in body


def test_valid_token_requires_no_session(app) -> None:
    app.extensions["invite_service"] = _FakeInviteService(
        "good-token", "invitee@example.com"
    )
    client = app.test_client()

    # No login/session is established; the route must still render (not a
    # redirect to the login page).
    resp = client.get("/invite/good-token")

    assert resp.status_code == 200
    assert resp.headers.get("Location") is None


def test_unknown_token_renders_used_or_expired_message(app) -> None:
    app.extensions["invite_service"] = _FakeInviteService(
        "good-token", "invitee@example.com"
    )
    client = app.test_client()

    resp = client.get("/invite/nope")

    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert INVITE_USED_MESSAGE in body
    # The exact required copy from R2.3.
    assert "Sorry, this invitation link has been used or has expired!" in body
    # The registration form must NOT be shown for an invalid token.
    assert 'name="password"' not in body


def test_degraded_mode_no_service_renders_used_or_expired(app) -> None:
    # In degraded mode the invite service is None; the route must still render
    # the fixed used/expired message rather than error.
    app.extensions["invite_service"] = None
    client = app.test_client()

    resp = client.get("/invite/anything")

    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert INVITE_USED_MESSAGE in body


# ----- POST /invite/<token> (task 8) --------------------------------------- #


class _RaceLostInviteService:
    """Invite service whose ``register`` always loses the single-use race.

    ``resolve_by_token`` still succeeds (the token looked valid when the form
    was rendered) but ``register`` raises :class:`InviteConsumedError` — the
    token was consumed by a concurrent request or expired mid-flow (R2.5).
    """

    def __init__(self, valid_token: str, email: str) -> None:
        self._valid_token = valid_token
        self._email = email

    def resolve_by_token(self, raw_token: str) -> dict[str, str]:
        if raw_token == self._valid_token:
            return {"email": self._email, "status": "invited"}
        raise InviteConsumedError("invitation link has been used or has expired")

    def register(self, raw_token: str) -> dict[str, str]:
        raise InviteConsumedError("invitation link has been used or has expired")


def test_successful_register_redirects_into_discord_linking(app) -> None:
    service = _FakeInviteService("good-token", "invitee@example.com")
    app.extensions["invite_service"] = service
    client = app.test_client()

    resp = client.post(
        "/invite/good-token",
        data={"password": "hunter2!", "password_confirm": "hunter2!"},
    )

    # Consumed the token via register, then redirected into Discord linking.
    assert service.register_calls == ["good-token"]
    assert resp.status_code in (301, 302, 303)
    assert resp.headers["Location"].endswith("/auth/discord/link")


def test_successful_register_sets_no_authenticated_session(app) -> None:
    # The registration link grants no lasting session (R2.4): after the POST
    # the client must not be authenticated.
    service = _FakeInviteService("good-token", "invitee@example.com")
    app.extensions["invite_service"] = service
    client = app.test_client()

    client.post(
        "/invite/good-token",
        data={"password": "hunter2!", "password_confirm": "hunter2!"},
    )

    with client.session_transaction() as sess:
        assert sess.get("user") is None


def test_register_race_lost_renders_used_or_expired(app) -> None:
    # register() raises InviteConsumedError (token consumed by a concurrent
    # request or expired mid-flow) -> the fixed used/expired message (R2.5).
    app.extensions["invite_service"] = _RaceLostInviteService(
        "good-token", "invitee@example.com"
    )
    client = app.test_client()

    resp = client.post(
        "/invite/good-token",
        data={"password": "hunter2!", "password_confirm": "hunter2!"},
    )

    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert INVITE_USED_MESSAGE in body
    assert "Sorry, this invitation link has been used or has expired!" in body


def test_mismatched_passwords_rerender_form_with_error(app) -> None:
    service = _FakeInviteService("good-token", "invitee@example.com")
    app.extensions["invite_service"] = service
    client = app.test_client()

    resp = client.post(
        "/invite/good-token",
        data={"password": "hunter2!", "password_confirm": "different!"},
    )

    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    # The form is re-rendered (bound to the email) with an inline error, and
    # the token was NOT consumed.
    assert service.register_calls == []
    assert "invitee@example.com" in body
    assert 'name="password"' in body
    assert "Passwords do not match." in body
    assert INVITE_USED_MESSAGE not in body


def test_empty_password_rerenders_form_with_error(app) -> None:
    service = _FakeInviteService("good-token", "invitee@example.com")
    app.extensions["invite_service"] = service
    client = app.test_client()

    resp = client.post(
        "/invite/good-token",
        data={"password": "", "password_confirm": ""},
    )

    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert service.register_calls == []
    assert 'name="password"' in body
    assert "Please choose a password." in body


def test_post_degraded_mode_no_service_renders_used_or_expired(app) -> None:
    app.extensions["invite_service"] = None
    client = app.test_client()

    resp = client.post(
        "/invite/anything",
        data={"password": "hunter2!", "password_confirm": "hunter2!"},
    )

    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert INVITE_USED_MESSAGE in body

"""Route tests for the public ``/invite/<token>`` registration page.

The GET route requires no session. A valid token renders the registration page
bound to the invite's email (shown read-only), with a chosen-username field, a
live password-rule checklist, and a confirm-password field; any invalid,
consumed, expired, or unknown token renders exactly the fixed used/expired
message and never the registration form (R2.1, R2.3).

POST validates the chosen username + password (against the shared
``register_policy``) and confirms the pair before consuming the token and
creating the account, then hands off to Discord linking (R2.2, R2.4, R2.5).
``GET /invite/<token>/username-available`` returns the JSON "as you type" hint.
"""

from __future__ import annotations

from typing import Any

from invite_public_routes import INVITE_USED_MESSAGE
from invite_service import InviteConsumedError

#: A password that satisfies the Cognito policy (>=12, upper, lower, num, sym).
GOOD_PASSWORD = "Hunter2!Hunter2"
#: A valid chosen username.
GOOD_USERNAME = "dj_nova"


def _reg_form(**overrides: str) -> dict[str, str]:
    """Return a valid registration POST body, with optional field overrides."""
    data = {
        "username": GOOD_USERNAME,
        "password": GOOD_PASSWORD,
        "password_confirm": GOOD_PASSWORD,
    }
    data.update(overrides)
    return data


class _FakeInviteService:
    """Minimal invite service: resolves one known token, rejects the rest.

    ``register`` consumes the token: the first call for the valid token
    succeeds and the token is thereafter treated as used (both ``register`` and
    ``resolve_by_token`` reject it), mirroring the single-use guarantee.
    ``display_name_available`` reports every name free unless pre-seeded.
    """

    def __init__(self, valid_token: str, email: str) -> None:
        self._valid_token = valid_token
        self._email = email
        self._consumed = False
        self.register_calls: list[dict[str, Any]] = []
        self.taken: set[str] = set()

    def resolve_by_token(self, raw_token: str) -> dict[str, str]:
        if raw_token == self._valid_token and not self._consumed:
            return {"email": self._email, "status": "invited"}
        raise InviteConsumedError("invitation link has been used or has expired")

    def register(
        self,
        raw_token: str,
        *,
        display_name: str | None = None,
        password: str | None = None,
    ) -> dict[str, str]:
        self.register_calls.append(
            {"token": raw_token, "display_name": display_name, "password": password}
        )
        if raw_token != self._valid_token or self._consumed:
            raise InviteConsumedError(
                "invitation link has been used or has expired"
            )
        self._consumed = True
        return {
            "email": self._email,
            "sub": "sub-123",
            "username": "user-123",
            "display_name": display_name or "",
            "invited_by": "admin@example.com",
        }

    def display_name_available(self, display_name: str) -> bool:
        return display_name.strip().lower() not in self.taken


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
    # Username selection + confirm-password fields are present.
    assert 'name="username"' in body
    assert 'name="password_confirm"' in body
    # The password-policy checklist is rendered.
    assert "At least 12 characters" in body
    # The POST target is the same /invite/<token> URL.
    assert 'action="/invite/good-token"' in body
    # It is the registration page, not the used/expired message.
    assert INVITE_USED_MESSAGE not in body


def test_valid_token_requires_no_session(app) -> None:
    app.extensions["invite_service"] = _FakeInviteService(
        "good-token", "invitee@example.com"
    )
    client = app.test_client()

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
    assert "Sorry, this invitation link has been used or has expired!" in body
    assert 'name="password"' not in body


def test_degraded_mode_no_service_renders_used_or_expired(app) -> None:
    app.extensions["invite_service"] = None
    client = app.test_client()

    resp = client.get("/invite/anything")

    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert INVITE_USED_MESSAGE in body


# ----- GET /invite/<token>/username-available (live hint) ------------------ #


def test_username_available_returns_true_for_free_name(app) -> None:
    app.extensions["invite_service"] = _FakeInviteService(
        "good-token", "invitee@example.com"
    )
    client = app.test_client()

    resp = client.get("/invite/good-token/username-available?u=dj_nova")

    assert resp.status_code == 200
    assert resp.get_json() == {"valid": True, "available": True, "error": ""}


def test_username_available_reports_taken_name(app) -> None:
    service = _FakeInviteService("good-token", "invitee@example.com")
    service.taken.add("dj_nova")
    app.extensions["invite_service"] = service
    client = app.test_client()

    resp = client.get("/invite/good-token/username-available?u=dj_nova")

    data = resp.get_json()
    assert data["valid"] is True
    assert data["available"] is False


def test_username_available_rejects_malformed_name(app) -> None:
    app.extensions["invite_service"] = _FakeInviteService(
        "good-token", "invitee@example.com"
    )
    client = app.test_client()

    resp = client.get("/invite/good-token/username-available?u=ab")

    data = resp.get_json()
    assert data["valid"] is False
    assert data["available"] is False
    assert data["error"]


def test_username_available_for_bad_token_reports_unavailable(app) -> None:
    app.extensions["invite_service"] = _FakeInviteService(
        "good-token", "invitee@example.com"
    )
    client = app.test_client()

    resp = client.get("/invite/nope/username-available?u=dj_nova")

    data = resp.get_json()
    assert data["available"] is False


# ----- POST /invite/<token> ------------------------------------------------ #


class _RaceLostInviteService:
    """Invite service whose ``register`` always loses the single-use race."""

    def __init__(self, valid_token: str, email: str) -> None:
        self._valid_token = valid_token
        self._email = email

    def resolve_by_token(self, raw_token: str) -> dict[str, str]:
        if raw_token == self._valid_token:
            return {"email": self._email, "status": "invited"}
        raise InviteConsumedError("invitation link has been used or has expired")

    def register(self, raw_token: str, **_: Any) -> dict[str, str]:
        raise InviteConsumedError("invitation link has been used or has expired")

    def display_name_available(self, display_name: str) -> bool:
        return True


def test_successful_register_redirects_into_discord_linking(app) -> None:
    service = _FakeInviteService("good-token", "invitee@example.com")
    app.extensions["invite_service"] = service
    client = app.test_client()

    resp = client.post("/invite/good-token", data=_reg_form())

    # Consumed the token via register (with chosen name + password), then
    # redirected into Discord linking.
    assert len(service.register_calls) == 1
    call = service.register_calls[0]
    assert call["token"] == "good-token"
    assert call["display_name"] == GOOD_USERNAME
    assert call["password"] == GOOD_PASSWORD
    assert resp.status_code in (301, 302, 303)
    assert resp.headers["Location"].endswith("/auth/discord/link")


def test_successful_register_sets_no_authenticated_session(app) -> None:
    service = _FakeInviteService("good-token", "invitee@example.com")
    app.extensions["invite_service"] = service
    client = app.test_client()

    client.post("/invite/good-token", data=_reg_form())

    with client.session_transaction() as sess:
        assert sess.get("user") is None


def test_register_race_lost_renders_used_or_expired(app) -> None:
    app.extensions["invite_service"] = _RaceLostInviteService(
        "good-token", "invitee@example.com"
    )
    client = app.test_client()

    resp = client.post("/invite/good-token", data=_reg_form())

    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert INVITE_USED_MESSAGE in body


def test_mismatched_passwords_rerender_form_with_error(app) -> None:
    service = _FakeInviteService("good-token", "invitee@example.com")
    app.extensions["invite_service"] = service
    client = app.test_client()

    resp = client.post(
        "/invite/good-token",
        data=_reg_form(password_confirm="Different2!Diff"),
    )

    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    # Re-rendered (bound to the email) with an inline error; token NOT consumed.
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
        data=_reg_form(password="", password_confirm=""),
    )

    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert service.register_calls == []
    assert 'name="password"' in body
    assert "Please choose a password." in body


def test_malformed_username_rerenders_form_with_error(app) -> None:
    service = _FakeInviteService("good-token", "invitee@example.com")
    app.extensions["invite_service"] = service
    client = app.test_client()

    resp = client.post("/invite/good-token", data=_reg_form(username="ab"))

    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    # A malformed username is rejected before the token is consumed.
    assert service.register_calls == []
    assert 'name="username"' in body
    assert INVITE_USED_MESSAGE not in body


def test_weak_password_rerenders_form_without_consuming_token(app) -> None:
    # The route delegates policy enforcement to service.register, which the real
    # service raises PasswordPolicyError for. Model that here to prove the route
    # surfaces the enumerated requirement message and does not redirect.
    from register_policy import PasswordPolicyError

    class _PolicyRejectingService(_FakeInviteService):
        def register(self, raw_token: str, **_: Any) -> dict[str, str]:
            raise PasswordPolicyError(["An uppercase letter", "A symbol"])

    service = _PolicyRejectingService("good-token", "invitee@example.com")
    app.extensions["invite_service"] = service
    client = app.test_client()

    resp = client.post(
        "/invite/good-token",
        data=_reg_form(password="lowercaseonly1", password_confirm="lowercaseonly1"),
    )

    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "Password does not meet the requirements" in body
    assert "An uppercase letter" in body
    assert INVITE_USED_MESSAGE not in body


def test_post_degraded_mode_no_service_renders_used_or_expired(app) -> None:
    app.extensions["invite_service"] = None
    client = app.test_client()

    resp = client.post("/invite/anything", data=_reg_form())

    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert INVITE_USED_MESSAGE in body

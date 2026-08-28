"""Route + property tests for the first-party auth forms (``auth_forms``).

Covers task 11 / Requirements 1.5, 3.4, 4.2, 5.2, 5.3, 6.4: each auth GET
renders its branded form; each POST drives injected fakes to the correct next
step; invalid credentials/codes never establish a session and never enumerate;
and the routes degrade cleanly when Cognito is unconfigured.

The app is built via ``create_app()`` and the auth services on
``app.extensions`` are swapped for in-memory fakes (no AWS, no network).
"""

from __future__ import annotations

from typing import Any

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app import create_app
from cognito_auth import AuthError, AuthResult


class _FakeAuth:
    """Fake CognitoAuth returning scripted AuthResults / raising AuthError."""

    def __init__(self) -> None:
        self.initiate_result: AuthResult | None = None
        self.initiate_error: Exception | None = None
        self.respond_result: AuthResult | None = None
        self.signup_result: AuthResult | None = None
        self.confirm_error: Exception | None = None
        self.forgot_calls: list[str] = []

    def initiate_auth(self, username: str, password: str) -> AuthResult:
        if self.initiate_error:
            raise self.initiate_error
        return self.initiate_result or AuthResult()

    def respond_challenge(self, **kwargs: Any) -> AuthResult:
        return self.respond_result or AuthResult()

    def sign_up(self, email: str, password: str) -> AuthResult:
        return self.signup_result or AuthResult(pending_confirmation=True)

    def confirm_sign_up(self, email: str, code: str) -> None:
        if self.confirm_error:
            raise self.confirm_error

    def forgot_password(self, email: str) -> None:
        self.forgot_calls.append(email)

    def confirm_forgot_password(
        self, email: str, code: str, new_password: str
    ) -> None:
        return None


class _FakeVerifier:
    """Fake CognitoJwtVerifier: trusts tokens shaped by the fake auth."""

    def __init__(self, *, admin: bool = False, ok: bool = True) -> None:
        self._admin = admin
        self._ok = ok

    def verify(self, token: str, *, expected_use: str) -> dict[str, Any]:
        if not self._ok:
            from cognito_jwt import CognitoJwtError

            raise CognitoJwtError("bad")
        return {"sub": "sub-1", "cognito:groups": (["admins"] if self._admin else [])}

    def groups(self, claims: dict[str, Any]) -> list[str]:
        raw = claims.get("cognito:groups", [])
        return list(raw) if isinstance(raw, list) else []

    def is_admin(self, claims: dict[str, Any]) -> bool:
        return "admins" in self.groups(claims)


class _OpenModeStore:
    """Minimal ConfigStore stand-in reporting registration mode OPEN.

    The ``auth.register`` gate reads ``get_global()`` and normalizes the
    ``registration_mode`` field; returning OPEN lets the first-party sign-up
    flow run so these form-rendering tests exercise the OPEN path.
    """

    def get_global(self) -> dict[str, Any]:
        return {"registration_mode": "OPEN"}


def _app(auth: _FakeAuth | None, verifier: _FakeVerifier | None):
    application = create_app(
        overrides={"TESTING": True, "SECRET_KEY": "t", "HELLODJ_STAGE": "beta"}
    )
    application.extensions["cognito_auth"] = auth
    application.extensions["cognito_jwt"] = verifier
    return application


# -- GET renders --------------------------------------------------------- #


def test_admin_login_get_renders_form():
    app = _app(_FakeAuth(), _FakeVerifier())
    resp = app.test_client().get("/auth/admin")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert 'name="username"' in html and 'name="password"' in html


def test_register_get_renders_form():
    app = _app(_FakeAuth(), _FakeVerifier())
    app.extensions["config_store"] = _OpenModeStore()
    resp = app.test_client().get("/auth/register")
    assert resp.status_code == 200
    assert 'name="email"' in resp.get_data(as_text=True)


def test_recover_get_renders_form():
    app = _app(_FakeAuth(), _FakeVerifier())
    resp = app.test_client().get("/auth/recover")
    assert resp.status_code == 200
    assert 'name="email"' in resp.get_data(as_text=True)


# -- login POST paths ---------------------------------------------------- #


def test_login_success_sets_session_and_redirects():
    auth = _FakeAuth()
    auth.initiate_result = AuthResult(tokens={"IdToken": "x"})
    app = _app(auth, _FakeVerifier(admin=True))
    client = app.test_client()
    resp = client.post(
        "/auth/admin", data={"username": "a@x.com", "password": "pw"}
    )
    assert resp.status_code == 302
    with client.session_transaction() as sess:
        assert sess["user"]["is_admin"] is True
        assert sess["user"]["provider"] == "cognito"


def test_login_bad_credentials_no_session():
    auth = _FakeAuth()
    auth.initiate_error = AuthError("Incorrect username or password.")
    app = _app(auth, _FakeVerifier())
    client = app.test_client()
    resp = client.post(
        "/auth/admin", data={"username": "a@x.com", "password": "bad"}
    )
    assert resp.status_code == 200
    assert "Incorrect username or password." in resp.get_data(as_text=True)
    with client.session_transaction() as sess:
        assert "user" not in sess


def test_login_challenge_renders_new_password():
    auth = _FakeAuth()
    auth.initiate_result = AuthResult(
        challenge_name="NEW_PASSWORD_REQUIRED", session="s"
    )
    app = _app(auth, _FakeVerifier())
    resp = app.test_client().post(
        "/auth/admin", data={"username": "a@x.com", "password": "pw"}
    )
    assert resp.status_code == 200
    assert 'name="new_password"' in resp.get_data(as_text=True)


def test_login_token_verify_failure_no_session():
    # Cognito returns tokens but JWKS verification fails → no session (P1/R4.2).
    auth = _FakeAuth()
    auth.initiate_result = AuthResult(tokens={"IdToken": "forged"})
    app = _app(auth, _FakeVerifier(ok=False))
    client = app.test_client()
    resp = client.post(
        "/auth/admin", data={"username": "a@x.com", "password": "pw"}
    )
    assert resp.status_code == 200
    with client.session_transaction() as sess:
        assert "user" not in sess


# -- register / recover POST -------------------------------------------- #


def test_register_start_advances_to_confirm():
    app = _app(_FakeAuth(), _FakeVerifier())
    app.extensions["config_store"] = _OpenModeStore()
    resp = app.test_client().post(
        "/auth/register",
        data={"step": "start", "email": "n@x.com", "password": "pw"},
    )
    assert resp.status_code == 200
    assert 'name="code"' in resp.get_data(as_text=True)


def test_recover_start_is_non_enumerating():
    auth = _FakeAuth()
    app = _app(auth, _FakeVerifier())
    resp = app.test_client().post(
        "/auth/recover", data={"step": "start", "email": "maybe@x.com"}
    )
    # Always advances to the confirm step regardless of whether the account
    # exists — the fake records the call but the response is identical.
    assert resp.status_code == 200
    assert 'name="code"' in resp.get_data(as_text=True)
    assert auth.forgot_calls == ["maybe@x.com"]


# -- degraded mode (R6.4) ----------------------------------------------- #


def test_degraded_mode_renders_unavailable():
    app = _app(None, None)
    resp = app.test_client().post(
        "/auth/admin", data={"username": "a@x.com", "password": "pw"}
    )
    assert resp.status_code == 200
    assert "unavailable" in resp.get_data(as_text=True).lower()


# -- property: invalid creds never authenticate, never enumerate -------- #


@settings(max_examples=60, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    username=st.text(min_size=0, max_size=40),
    password=st.text(min_size=0, max_size=40),
)
def test_prop_bad_login_never_authenticates(username: str, password: str):
    auth = _FakeAuth()
    auth.initiate_error = AuthError("Incorrect username or password.")
    app = _app(auth, _FakeVerifier())
    client = app.test_client()
    resp = client.post(
        "/auth/admin", data={"username": username, "password": password}
    )
    body = resp.get_data(as_text=True)
    # Never a redirect (no auth), always the one generic message, no session.
    assert resp.status_code == 200
    assert "Incorrect username or password." in body
    # The password field never round-trips a value attribute (never reflected),
    # so a submitted secret is never echoed back into the form.
    assert 'name="password"' in body
    assert 'name="password" value=' not in body
    with client.session_transaction() as sess:
        assert "user" not in sess

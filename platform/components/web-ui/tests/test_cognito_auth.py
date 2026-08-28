"""Tests for the first-party Cognito auth service (``cognito_auth``).

Covers task 3 / Requirements 1.2-1.5, 2.2-2.4, 3.2-3.4, 5.3: each flow
(login, challenge, sign-up, confirm, recover) drives an injected fake
``cognito-idp`` client to the right :class:`AuthResult`, and Cognito exceptions
map to NON-ENUMERATING :class:`AuthError` messages. A dedicated test asserts the
password and confirmation code never appear in any surfaced error (R5.3).
"""

from __future__ import annotations

from typing import Any

import pytest

from cognito_auth import (
    GENERIC_AUTH_ERROR,
    GENERIC_CODE_ERROR,
    AuthError,
    CognitoAuth,
)

_CLIENT_ID = "testclient123"
_SECRET_PASSWORD = "SuperSecretPassw0rd!"
_SECRET_CODE = "918273"


class _ClientError(Exception):
    """botocore-shaped ClientError carrying an ``Error.Code``/``Message``."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(code)
        self.response = {"Error": {"Code": code, "Message": message}}


class _FakeIdp:
    """Configurable fake ``cognito-idp`` client.

    Each method returns ``self.responses[name]`` or raises
    ``self.errors[name]`` when set; calls are recorded for assertions.
    """

    def __init__(self) -> None:
        self.responses: dict[str, dict[str, Any]] = {}
        self.errors: dict[str, Exception] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def _run(self, name: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append((name, kwargs))
        if name in self.errors:
            raise self.errors[name]
        return self.responses.get(name, {})

    def initiate_auth(self, **kwargs: Any) -> dict[str, Any]:
        return self._run("initiate_auth", **kwargs)

    def respond_to_auth_challenge(self, **kwargs: Any) -> dict[str, Any]:
        return self._run("respond_to_auth_challenge", **kwargs)

    def sign_up(self, **kwargs: Any) -> dict[str, Any]:
        return self._run("sign_up", **kwargs)

    def confirm_sign_up(self, **kwargs: Any) -> dict[str, Any]:
        return self._run("confirm_sign_up", **kwargs)

    def forgot_password(self, **kwargs: Any) -> dict[str, Any]:
        return self._run("forgot_password", **kwargs)

    def confirm_forgot_password(self, **kwargs: Any) -> dict[str, Any]:
        return self._run("confirm_forgot_password", **kwargs)


def _auth(idp: _FakeIdp) -> CognitoAuth:
    return CognitoAuth(idp, client_id=_CLIENT_ID)


# -- login --------------------------------------------------------------- #


def test_login_success_returns_tokens():
    idp = _FakeIdp()
    idp.responses["initiate_auth"] = {
        "AuthenticationResult": {"IdToken": "id", "AccessToken": "acc"}
    }
    result = _auth(idp).initiate_auth("user@x.com", _SECRET_PASSWORD)
    assert result.authenticated
    assert result.tokens["IdToken"] == "id"


def test_login_new_password_challenge():
    idp = _FakeIdp()
    idp.responses["initiate_auth"] = {
        "ChallengeName": "NEW_PASSWORD_REQUIRED",
        "Session": "sess-abc",
        "ChallengeParameters": {"requiredAttributes": "[]"},
    }
    result = _auth(idp).initiate_auth("user@x.com", _SECRET_PASSWORD)
    assert result.needs_challenge
    assert result.challenge_name == "NEW_PASSWORD_REQUIRED"
    assert result.session == "sess-abc"


def test_login_mfa_challenge():
    idp = _FakeIdp()
    idp.responses["initiate_auth"] = {
        "ChallengeName": "SOFTWARE_TOKEN_MFA",
        "Session": "sess-mfa",
    }
    result = _auth(idp).initiate_auth("user@x.com", _SECRET_PASSWORD)
    assert result.needs_challenge
    assert result.challenge_name == "SOFTWARE_TOKEN_MFA"


def test_login_bad_credentials_generic():
    idp = _FakeIdp()
    idp.errors["initiate_auth"] = _ClientError("NotAuthorizedException")
    with pytest.raises(AuthError) as ei:
        _auth(idp).initiate_auth("user@x.com", _SECRET_PASSWORD)
    assert str(ei.value) == GENERIC_AUTH_ERROR


def test_login_unknown_user_is_indistinguishable():
    idp = _FakeIdp()
    idp.errors["initiate_auth"] = _ClientError("UserNotFoundException")
    with pytest.raises(AuthError) as ei:
        _auth(idp).initiate_auth("nobody@x.com", _SECRET_PASSWORD)
    # Same message as the bad-password case → no enumeration.
    assert str(ei.value) == GENERIC_AUTH_ERROR


def test_login_unconfirmed_routes_to_confirm():
    idp = _FakeIdp()
    idp.errors["initiate_auth"] = _ClientError("UserNotConfirmedException")
    result = _auth(idp).initiate_auth("user@x.com", _SECRET_PASSWORD)
    assert result.pending_confirmation
    assert not result.authenticated


# -- challenge response -------------------------------------------------- #


def test_respond_challenge_success():
    idp = _FakeIdp()
    idp.responses["respond_to_auth_challenge"] = {
        "AuthenticationResult": {"IdToken": "id2"}
    }
    result = _auth(idp).respond_challenge(
        challenge_name="NEW_PASSWORD_REQUIRED",
        session="sess",
        username="user@x.com",
        responses={"NEW_PASSWORD": _SECRET_PASSWORD},
    )
    assert result.authenticated
    # Username merged into the challenge responses.
    _, kwargs = idp.calls[-1]
    assert kwargs["ChallengeResponses"]["USERNAME"] == "user@x.com"


def test_respond_challenge_bad_code_generic():
    idp = _FakeIdp()
    idp.errors["respond_to_auth_challenge"] = _ClientError(
        "CodeMismatchException"
    )
    with pytest.raises(AuthError) as ei:
        _auth(idp).respond_challenge(
            challenge_name="SOFTWARE_TOKEN_MFA",
            session="sess",
            username="user@x.com",
            responses={"SOFTWARE_TOKEN_MFA_CODE": _SECRET_CODE},
        )
    assert str(ei.value) == GENERIC_CODE_ERROR


# -- registration -------------------------------------------------------- #


def test_sign_up_sends_confirmation():
    idp = _FakeIdp()
    result = _auth(idp).sign_up("new@x.com", _SECRET_PASSWORD)
    assert result.pending_confirmation
    name, kwargs = idp.calls[-1]
    assert name == "sign_up"
    assert kwargs["Username"] == "new@x.com"


def test_sign_up_existing_user_non_enumerating():
    idp = _FakeIdp()
    idp.errors["sign_up"] = _ClientError("UsernameExistsException")
    # Proceeds to confirm step rather than revealing the account exists.
    result = _auth(idp).sign_up("taken@x.com", _SECRET_PASSWORD)
    assert result.pending_confirmation


def test_sign_up_weak_password_surfaces_policy():
    idp = _FakeIdp()
    idp.errors["sign_up"] = _ClientError(
        "InvalidPasswordException", "Password must be longer."
    )
    with pytest.raises(AuthError) as ei:
        _auth(idp).sign_up("new@x.com", "weak")
    assert "Password" in str(ei.value)


def test_confirm_sign_up_bad_code_generic():
    idp = _FakeIdp()
    idp.errors["confirm_sign_up"] = _ClientError("ExpiredCodeException")
    with pytest.raises(AuthError) as ei:
        _auth(idp).confirm_sign_up("new@x.com", _SECRET_CODE)
    assert str(ei.value) == GENERIC_CODE_ERROR


# -- recovery ------------------------------------------------------------ #


def test_forgot_password_unknown_email_no_error():
    idp = _FakeIdp()
    idp.errors["forgot_password"] = _ClientError("UserNotFoundException")
    # Must NOT raise — caller renders the same non-enumerating confirmation.
    _auth(idp).forgot_password("nobody@x.com")


def test_confirm_forgot_password_success():
    idp = _FakeIdp()
    _auth(idp).confirm_forgot_password(
        "user@x.com", _SECRET_CODE, _SECRET_PASSWORD
    )
    name, kwargs = idp.calls[-1]
    assert name == "confirm_forgot_password"
    assert kwargs["Password"] == _SECRET_PASSWORD


def test_confirm_forgot_password_bad_code_generic():
    idp = _FakeIdp()
    idp.errors["confirm_forgot_password"] = _ClientError("CodeMismatchException")
    with pytest.raises(AuthError) as ei:
        _auth(idp).confirm_forgot_password(
            "user@x.com", _SECRET_CODE, _SECRET_PASSWORD
        )
    assert str(ei.value) == GENERIC_CODE_ERROR


# -- secret hygiene (R5.3) ---------------------------------------------- #


def test_errors_never_contain_password_or_code():
    """No surfaced AuthError message contains the password or code."""
    for code in (
        "NotAuthorizedException",
        "CodeMismatchException",
        "ExpiredCodeException",
    ):
        idp = _FakeIdp()
        idp.errors["initiate_auth"] = _ClientError(code)
        idp.errors["confirm_forgot_password"] = _ClientError(code)
        try:
            _auth(idp).initiate_auth("u@x.com", _SECRET_PASSWORD)
        except AuthError as e:
            assert _SECRET_PASSWORD not in str(e)
        try:
            _auth(idp).confirm_forgot_password(
                "u@x.com", _SECRET_CODE, _SECRET_PASSWORD
            )
        except AuthError as e:
            assert _SECRET_PASSWORD not in str(e)
            assert _SECRET_CODE not in str(e)

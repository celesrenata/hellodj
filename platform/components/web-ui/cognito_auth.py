"""First-party Cognito auth operations for the branded login/register/recover
forms.

Wraps the ``cognito-idp`` client the web-ui calls SERVER-SIDE so the
HelloDJ-styled forms replace the Cognito hosted UI while Cognito stays the
identity provider (the auth-routing invariant is preserved — this is a
presentation change to the Cognito-routed purposes only). The app client is
public (no secret), so no ``SECRET_HASH`` is attached.

The auth flow is ``USER_PASSWORD_AUTH`` (design decision: simple, appropriate
for an admin panel behind end-to-end TLS; SRP was rejected for complexity).
Login may return a challenge (``NEW_PASSWORD_REQUIRED`` for the seeded admin's
first login, or ``SOFTWARE_TOKEN_MFA``) which the caller completes via
:meth:`respond_challenge`.

Every method normalizes Cognito exceptions to a small set of NON-ENUMERATING
outcomes (:class:`AuthResult` / :class:`AuthError`) so a route can surface a
generic message without revealing whether an account exists (R1.5, R3.4).
Passwords, confirmation codes, and the opaque challenge ``Session`` are treated
as secrets: they are never logged and never placed in an error message (R5.3).

The client is injectable (a :class:`CognitoIdpClient` Protocol) so the flows are
unit-testable without AWS; :func:`build_cognito_auth` degrades to ``None`` when
the app client id is unconfigured (auth routes then render "auth unavailable").

Requirements: 1.2, 1.3, 1.4, 1.5, 2.2, 2.3, 2.4, 3.2, 3.3, 3.4, 5.3, 6.4
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Protocol

__all__ = [
    "CognitoIdpClient",
    "CognitoAuth",
    "AuthResult",
    "AuthError",
    "build_cognito_auth",
    "GENERIC_AUTH_ERROR",
    "GENERIC_CODE_ERROR",
]

#: Non-enumerating copy for bad credentials / unknown user (R1.5).
GENERIC_AUTH_ERROR = "Incorrect username or password."
#: Non-enumerating copy for a bad/expired confirmation or reset code.
GENERIC_CODE_ERROR = "That code is invalid or expired."


class AuthError(Exception):
    """A user-facing, non-enumerating auth failure.

    ``message`` is safe to show as-is (already generic); it never contains the
    password, code, or challenge session.
    """


@dataclass
class AuthResult:
    """Outcome of an auth operation.

    Exactly one of the shapes is populated:

    * ``tokens`` set → authentication succeeded; ``tokens`` holds Cognito's
      ``AuthenticationResult`` (``IdToken`` / ``AccessToken`` / ...). The caller
      verifies these via ``cognito_jwt`` before establishing a session.
    * ``challenge_name`` + ``session`` set → Cognito needs a follow-up
      (``NEW_PASSWORD_REQUIRED`` / ``SOFTWARE_TOKEN_MFA``); the caller renders
      the matching form and calls :meth:`CognitoAuth.respond_challenge`.
    * ``pending_confirmation`` True → the account exists but is unconfirmed;
      the caller routes to the confirm-code step.
    """

    tokens: dict[str, Any] | None = None
    challenge_name: str | None = None
    session: str | None = None
    pending_confirmation: bool = False
    #: True when the account is in RESET_REQUIRED (e.g. an admin ran
    #: AdminResetUserPassword): the user must complete a code+new-password
    #: reset before they can sign in. The caller routes them to the recover
    #: confirm stage (where the emailed code is entered).
    password_reset_required: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def authenticated(self) -> bool:
        """Whether authentication fully succeeded (tokens present)."""
        return self.tokens is not None

    @property
    def needs_challenge(self) -> bool:
        """Whether a challenge must be completed before authentication."""
        return self.challenge_name is not None and self.session is not None


class CognitoIdpClient(Protocol):
    """Subset of the boto3 ``cognito-idp`` client the auth flows use."""

    def initiate_auth(self, **kwargs: Any) -> dict[str, Any]: ...

    def respond_to_auth_challenge(self, **kwargs: Any) -> dict[str, Any]: ...

    def sign_up(self, **kwargs: Any) -> dict[str, Any]: ...

    def confirm_sign_up(self, **kwargs: Any) -> dict[str, Any]: ...

    def forgot_password(self, **kwargs: Any) -> dict[str, Any]: ...

    def confirm_forgot_password(self, **kwargs: Any) -> dict[str, Any]: ...


def _error_code(error: Exception) -> str:
    """Extract the Cognito error code from a botocore ClientError-shaped exc."""
    response = getattr(error, "response", None)
    if isinstance(response, dict):
        code = response.get("Error", {}).get("Code")
        if isinstance(code, str):
            return code
    return type(error).__name__


class CognitoAuth:
    """Server-side Cognito auth operations for the first-party forms.

    Args:
        client: An injected ``cognito-idp`` client.
        client_id: The app client id used on every call.
    """

    def __init__(self, client: CognitoIdpClient, *, client_id: str) -> None:
        self._client = client
        self._client_id = client_id

    # -- login ------------------------------------------------------------- #

    def initiate_auth(self, username: str, password: str) -> AuthResult:
        """Start ``USER_PASSWORD_AUTH`` for ``username`` / ``password``.

        Returns an :class:`AuthResult` that is authenticated, carries a
        challenge, or (for an unconfirmed account) flags confirmation. Raises
        :class:`AuthError` with :data:`GENERIC_AUTH_ERROR` for bad credentials
        or an unknown user — the two are indistinguishable to the caller
        (R1.5).
        """
        try:
            resp = self._client.initiate_auth(
                ClientId=self._client_id,
                AuthFlow="USER_PASSWORD_AUTH",
                AuthParameters={"USERNAME": username, "PASSWORD": password},
            )
        except Exception as error:  # noqa: BLE001 - normalized below
            code = _error_code(error)
            if code == "UserNotConfirmedException":
                return AuthResult(pending_confirmation=True)
            if code == "PasswordResetRequiredException":
                # An admin reset the password (AdminResetUserPassword), or
                # Cognito otherwise flagged RESET_REQUIRED. The user has (or
                # can request) an emailed code — route them to the reset
                # confirm stage instead of the dead-end "incorrect password".
                return AuthResult(password_reset_required=True)
            raise AuthError(GENERIC_AUTH_ERROR) from error
        return self._result_from_response(resp)

    def respond_challenge(
        self,
        *,
        challenge_name: str,
        session: str,
        username: str,
        responses: dict[str, str],
    ) -> AuthResult:
        """Complete a login challenge (new-password / MFA).

        ``responses`` holds the challenge-specific fields (e.g.
        ``{"NEW_PASSWORD": ...}`` or ``{"SOFTWARE_TOKEN_MFA_CODE": ...}``); the
        username is merged in as Cognito requires. May return another challenge
        or the authenticated tokens.
        """
        challenge_responses = {"USERNAME": username, **responses}
        try:
            resp = self._client.respond_to_auth_challenge(
                ClientId=self._client_id,
                ChallengeName=challenge_name,
                Session=session,
                ChallengeResponses=challenge_responses,
            )
        except Exception as error:  # noqa: BLE001
            code = _error_code(error)
            if code in ("CodeMismatchException", "ExpiredCodeException"):
                raise AuthError(GENERIC_CODE_ERROR) from error
            if code == "InvalidPasswordException":
                raise AuthError(_password_policy_message(error)) from error
            raise AuthError(GENERIC_AUTH_ERROR) from error
        return self._result_from_response(resp)

    # -- registration ------------------------------------------------------ #

    def sign_up(self, email: str, password: str) -> AuthResult:
        """Self-register ``email`` / ``password`` (Cognito ``SignUp``).

        Returns an :class:`AuthResult` flagging that a confirmation code was
        sent. Raises :class:`AuthError` with a policy message for a weak
        password; other failures surface generically.
        """
        try:
            self._client.sign_up(
                ClientId=self._client_id,
                Username=email,
                Password=password,
                UserAttributes=[{"Name": "email", "Value": email}],
            )
        except Exception as error:  # noqa: BLE001
            code = _error_code(error)
            if code == "InvalidPasswordException":
                raise AuthError(_password_policy_message(error)) from error
            if code == "UsernameExistsException":
                # Non-enumerating: proceed to the confirm step as if sent.
                return AuthResult(pending_confirmation=True)
            raise AuthError("Could not complete registration.") from error
        return AuthResult(pending_confirmation=True)

    def confirm_sign_up(self, email: str, code: str) -> None:
        """Confirm a self-registration with the emailed ``code``."""
        try:
            self._client.confirm_sign_up(
                ClientId=self._client_id, Username=email, ConfirmationCode=code
            )
        except Exception as error:  # noqa: BLE001
            code_name = _error_code(error)
            if code_name in ("CodeMismatchException", "ExpiredCodeException"):
                raise AuthError(GENERIC_CODE_ERROR) from error
            raise AuthError("Could not confirm the account.") from error

    # -- recovery ---------------------------------------------------------- #

    def forgot_password(self, email: str) -> None:
        """Start account recovery (Cognito ``ForgotPassword``).

        Deliberately swallows ``UserNotFoundException`` so the caller can always
        render the same "if an account exists, a code was sent" copy (R3.4, no
        enumeration).
        """
        try:
            self._client.forgot_password(
                ClientId=self._client_id, Username=email
            )
        except Exception as error:  # noqa: BLE001
            code = _error_code(error)
            if code in ("UserNotFoundException", "InvalidParameterException"):
                return
            # Throttling and other errors: stay generic, do not leak.
            return

    def confirm_forgot_password(
        self, email: str, code: str, new_password: str
    ) -> None:
        """Complete recovery with ``code`` + ``new_password``."""
        try:
            self._client.confirm_forgot_password(
                ClientId=self._client_id,
                Username=email,
                ConfirmationCode=code,
                Password=new_password,
            )
        except Exception as error:  # noqa: BLE001
            code_name = _error_code(error)
            if code_name in ("CodeMismatchException", "ExpiredCodeException"):
                raise AuthError(GENERIC_CODE_ERROR) from error
            if code_name == "InvalidPasswordException":
                raise AuthError(_password_policy_message(error)) from error
            raise AuthError("Could not reset the password.") from error

    # -- internals --------------------------------------------------------- #

    def _result_from_response(self, resp: dict[str, Any]) -> AuthResult:
        """Map a Cognito auth/challenge response to an :class:`AuthResult`."""
        auth = resp.get("AuthenticationResult")
        if auth:
            return AuthResult(tokens=auth)
        challenge = resp.get("ChallengeName")
        session = resp.get("Session")
        if challenge and session:
            return AuthResult(
                challenge_name=challenge,
                session=session,
                extra=resp.get("ChallengeParameters", {}) or {},
            )
        # No tokens and no actionable challenge: treat as a generic failure.
        raise AuthError(GENERIC_AUTH_ERROR)


def _password_policy_message(error: Exception) -> str:
    """Return a safe password-policy message.

    Cognito's ``InvalidPasswordException`` message describes the policy (no user
    data), so it is safe to surface; fall back to a generic policy hint.
    """
    msg = str(getattr(error, "response", {}).get("Error", {}).get("Message", ""))
    if msg:
        return msg
    return (
        "Password does not meet the requirements (min 12 chars, upper, lower, "
        "number, and symbol)."
    )


def build_cognito_auth(
    client: CognitoIdpClient | None = None,
) -> CognitoAuth | None:
    """Build a :class:`CognitoAuth` from env, or ``None`` when unconfigured.

    Returns ``None`` (degraded mode — auth routes render "auth unavailable")
    unless the app client id is present. When ``client`` is omitted a real boto3
    ``cognito-idp`` client is constructed lazily.
    """
    client_id = os.getenv("COGNITO_CLIENT_ID", "")
    if not client_id:
        return None
    if client is None:
        try:
            import boto3

            client = boto3.client(
                "cognito-idp",
                region_name=os.getenv("AWS_REGION", "us-east-1"),
            )
        except Exception:  # noqa: BLE001
            return None
    return CognitoAuth(client, client_id=client_id)

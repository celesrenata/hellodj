"""First-party auth form controllers (login / register / recover).

These are the flow controllers behind the branded Flask forms that replace the
Cognito hosted UI. They are factored out of the ``auth`` blueprint so the
blueprint stays thin (under the 500-line ceiling) and the flow logic is
unit-testable with the Flask test client + injected fakes.

Each controller pulls its collaborators off ``current_app.extensions``:

* ``cognito_auth``      — :class:`cognito_auth.CognitoAuth` (server-side calls)
* ``cognito_jwt``       — :class:`cognito_jwt.CognitoJwtVerifier` (token verify)
* ``auth_rate_limiter`` — :class:`auth_ratelimit.RateLimiter` (best-effort)

When ``cognito_auth`` or ``cognito_jwt`` is unconfigured the controllers render
an "auth unavailable" state instead of crashing (R6.4). The Cognito challenge
``Session`` and the pending username are held server-side in the Flask session
(never exposed to the page) and cleared on completion.

Cognito remains the identity provider — this is a presentation change to the
Cognito-routed purposes (admin auth / registration / recovery); the auth-routing
invariant in ``auth_routing.py`` is unchanged.

Requirements: 1.1-1.5, 2.1-2.4, 3.1-3.4, 4.2, 5.1, 5.2, 5.3, 6.3, 6.4, 7.1
"""

from __future__ import annotations

from typing import Any

from flask import (
    current_app,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from hellodj_platform_logic.types import AuthProvider

from auth_ratelimit import RateLimited
from cognito_auth import AuthError, AuthResult
from cognito_jwt import CognitoJwtError

__all__ = [
    "handle_login",
    "handle_login_challenge",
    "handle_register",
    "handle_recover",
]

#: Session keys holding an in-flight challenge (never rendered to the page).
_CHALLENGE_NAME = "auth_challenge_name"
_CHALLENGE_SESSION = "auth_challenge_session"
_CHALLENGE_USER = "auth_challenge_user"

#: Generic, non-enumerating "please wait" copy when throttled (R5.1).
_THROTTLED = "Too many attempts. Please wait a moment and try again."
#: Rendered when Cognito is unconfigured (degraded mode, R6.4).
_UNAVAILABLE = "Sign-in is temporarily unavailable. Please try again later."


def _svc(name: str) -> Any | None:
    return current_app.extensions.get(name)


def _client_ip() -> str:
    """Best-effort client ip for rate-limit keying (trusts XFF first hop)."""
    xff = request.headers.get("X-Forwarded-For", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.remote_addr or "unknown"


def _rate_key(route: str) -> str:
    return f"{_client_ip()}:{route}"


def _check_rate(route: str) -> bool:
    """Return True if allowed; False if currently throttled."""
    limiter = _svc("auth_rate_limiter")
    if limiter is None:
        return True
    try:
        limiter.check(_rate_key(route))
        return True
    except RateLimited:
        return False


def _record_failure(route: str) -> None:
    limiter = _svc("auth_rate_limiter")
    if limiter is not None:
        limiter.record_failure(_rate_key(route))


def _reset_rate(route: str) -> None:
    limiter = _svc("auth_rate_limiter")
    if limiter is not None:
        limiter.reset(_rate_key(route))


def _establish_session_from_tokens(tokens: dict[str, Any]) -> bool:
    """Verify the id token and set ``session['user']``; return success.

    The id token's signature + claims are verified via ``cognito_jwt`` BEFORE
    any claim is trusted (R4.1, R4.2); ``is_admin`` comes from the verified
    ``cognito:groups`` claim (Property 2). Returns False when verification
    fails so the caller shows the generic auth error and establishes NO session.
    """
    verifier = _svc("cognito_jwt")
    id_token = tokens.get("IdToken", "")
    if verifier is None or not id_token:
        return False
    try:
        claims = verifier.verify(id_token, expected_use="id")
    except CognitoJwtError:
        return False
    session["user"] = {
        "provider": AuthProvider.COGNITO.value,
        "sub": claims.get("sub", ""),
        "is_admin": verifier.is_admin(claims),
        "groups": verifier.groups(claims),
    }
    return True


def _clear_challenge() -> None:
    for key in (_CHALLENGE_NAME, _CHALLENGE_SESSION, _CHALLENGE_USER):
        session.pop(key, None)


# -- login --------------------------------------------------------------- #


def handle_login():
    """Render/submit the branded admin login form (R1.1, R1.2, R1.5)."""
    auth = _svc("cognito_auth")
    if auth is None:
        return render_template("pages/login.html", error=_UNAVAILABLE)
    if request.method == "GET":
        return render_template("pages/login.html", error=None)

    if not _check_rate("login"):
        return render_template("pages/login.html", error=_THROTTLED)
    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""
    try:
        result = auth.initiate_auth(username, password)
    except AuthError as error:
        _record_failure("login")
        return render_template("pages/login.html", error=str(error))

    if result.pending_confirmation:
        # Account exists but is unconfirmed → send them to confirm the email.
        session["pending_confirm_email"] = username
        return redirect(url_for("auth.register"))
    if result.needs_challenge:
        session[_CHALLENGE_NAME] = result.challenge_name
        session[_CHALLENGE_SESSION] = result.session
        session[_CHALLENGE_USER] = username
        return _render_challenge(result)
    return _finish_login(result, route="login")


def _render_challenge(result: AuthResult):
    """Render the template matching the pending challenge."""
    if result.challenge_name == "NEW_PASSWORD_REQUIRED":
        return render_template("pages/auth_new_password.html", error=None)
    if result.challenge_name == "SOFTWARE_TOKEN_MFA":
        return render_template("pages/auth_mfa.html", error=None)
    # Unknown challenge type → fail closed with the generic error.
    _clear_challenge()
    return render_template(
        "pages/login.html", error="Incorrect username or password."
    )


def handle_login_challenge():
    """Submit a login challenge response (new-password / MFA) (R1.3, R1.4)."""
    auth = _svc("cognito_auth")
    challenge_name = session.get(_CHALLENGE_NAME)
    challenge_session = session.get(_CHALLENGE_SESSION)
    username = session.get(_CHALLENGE_USER)
    if auth is None or not challenge_name or not challenge_session:
        _clear_challenge()
        return redirect(url_for("pages.login"))

    if not _check_rate("challenge"):
        return _render_challenge_by_name(challenge_name, _THROTTLED)
    responses = _challenge_responses(challenge_name)
    if responses is None:
        return _render_challenge_by_name(challenge_name, "Missing response.")
    try:
        result = auth.respond_challenge(
            challenge_name=challenge_name,
            session=challenge_session,
            username=username or "",
            responses=responses,
        )
    except AuthError as error:
        _record_failure("challenge")
        return _render_challenge_by_name(challenge_name, str(error))

    if result.needs_challenge:
        # Chained challenge (e.g. new-password then MFA): update and re-render.
        session[_CHALLENGE_NAME] = result.challenge_name
        session[_CHALLENGE_SESSION] = result.session
        return _render_challenge(result)
    return _finish_login(result, route="challenge")


def _challenge_responses(challenge_name: str) -> dict[str, str] | None:
    """Collect the challenge-specific form fields, or None if absent."""
    if challenge_name == "NEW_PASSWORD_REQUIRED":
        new_password = request.form.get("new_password") or ""
        if not new_password:
            return None
        return {"NEW_PASSWORD": new_password}
    if challenge_name == "SOFTWARE_TOKEN_MFA":
        code = (request.form.get("code") or "").strip()
        if not code:
            return None
        return {"SOFTWARE_TOKEN_MFA_CODE": code}
    return None


def _render_challenge_by_name(challenge_name: str, error: str | None):
    template = (
        "pages/auth_new_password.html"
        if challenge_name == "NEW_PASSWORD_REQUIRED"
        else "pages/auth_mfa.html"
    )
    return render_template(template, error=error)


def _finish_login(result: AuthResult, *, route: str):
    """Verify tokens, establish the session, and land on the dashboard."""
    if not result.authenticated or not _establish_session_from_tokens(
        result.tokens or {}
    ):
        _record_failure(route)
        _clear_challenge()
        return render_template(
            "pages/login.html", error="Incorrect username or password."
        )
    _reset_rate(route)
    _clear_challenge()
    return redirect(url_for("pages.dashboard"))


# -- registration -------------------------------------------------------- #


def handle_register():
    """Render/submit the branded self-registration + confirm forms (R2.x)."""
    auth = _svc("cognito_auth")
    if auth is None:
        return render_template("pages/auth_register.html", error=_UNAVAILABLE)
    if request.method == "GET":
        # A login bounce for an unconfirmed account pre-fills the confirm step.
        pending = session.pop("pending_confirm_email", "")
        stage = "confirm" if pending else "start"
        return render_template(
            "pages/auth_register.html",
            error=None,
            stage=stage,
            email=pending,
        )

    step = request.form.get("step", "start")
    if step == "confirm":
        return _register_confirm(auth)
    return _register_start(auth)


def _register_start(auth):
    if not _check_rate("register"):
        return render_template("pages/auth_register.html", error=_THROTTLED)
    email = (request.form.get("email") or "").strip()
    password = request.form.get("password") or ""
    try:
        auth.sign_up(email, password)
    except AuthError as error:
        _record_failure("register")
        return render_template(
            "pages/auth_register.html", error=str(error), stage="start"
        )
    return render_template(
        "pages/auth_register.html", error=None, stage="confirm", email=email
    )


def _register_confirm(auth):
    email = (request.form.get("email") or "").strip()
    code = (request.form.get("code") or "").strip()
    try:
        auth.confirm_sign_up(email, code)
    except AuthError as error:
        return render_template(
            "pages/auth_register.html",
            error=str(error),
            stage="confirm",
            email=email,
        )
    return redirect(url_for("pages.login", registered="1"))


# -- recovery ------------------------------------------------------------ #


def handle_recover():
    """Render/submit the branded forgot-password + reset forms (R3.x)."""
    auth = _svc("cognito_auth")
    if auth is None:
        return render_template("pages/auth_recover.html", error=_UNAVAILABLE)
    if request.method == "GET":
        return render_template(
            "pages/auth_recover.html", error=None, stage="start"
        )

    step = request.form.get("step", "start")
    if step == "confirm":
        return _recover_confirm(auth)
    return _recover_start(auth)


def _recover_start(auth):
    if not _check_rate("recover"):
        return render_template("pages/auth_recover.html", error=_THROTTLED)
    email = (request.form.get("email") or "").strip()
    # Always non-enumerating: forgot_password swallows unknown-email (R3.4).
    auth.forgot_password(email)
    return render_template(
        "pages/auth_recover.html", error=None, stage="confirm", email=email
    )


def _recover_confirm(auth):
    email = (request.form.get("email") or "").strip()
    code = (request.form.get("code") or "").strip()
    new_password = request.form.get("new_password") or ""
    try:
        auth.confirm_forgot_password(email, code, new_password)
    except AuthError as error:
        return render_template(
            "pages/auth_recover.html",
            error=str(error),
            stage="confirm",
            email=email,
        )
    return redirect(url_for("pages.login", reset="1"))

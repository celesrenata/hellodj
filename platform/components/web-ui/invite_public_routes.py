"""Public (unauthenticated) invite-registration routes.

Split out of :mod:`pages` so both stay under the per-file line ceiling (R13.3)
and the single-use-invite onboarding surface has one cohesive home:

* ``GET  /invite/<token>``                 — render the HelloDJ-hosted
                                             registration form for a valid token,
                                             else the fixed used/expired message
                                             (R2.1, R2.3).
* ``POST /invite/<token>``                 — validate the chosen username +
                                             password, consume the token, and
                                             create the CONFIRMED account, then
                                             hand off to Discord linking (R2.2,
                                             R2.4, R2.5).
* ``GET  /invite/<token>/username-available`` — JSON "as you type" availability
                                             hint for the chosen username.

The registration form lets the invitee pick a username (checked for
availability), set a password against the Cognito policy (each rule ticks off
live in the template), and confirm it — see :mod:`register_policy` for the
shared rules that drive BOTH the server-side guard and the client checklist.

No session is established here; login is established only after the subsequent
Discord OAuth (R2.4).

Requirements: 2.1, 2.2, 2.3, 2.4, 2.5
"""

from __future__ import annotations

from typing import Any

from flask import (
    Blueprint,
    current_app,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

import register_policy
from invite_service import InviteConsumedError

__all__ = ["build_invite_public_blueprint", "INVITE_USED_MESSAGE"]

#: The single fixed message shown for any invalid/consumed/expired/unknown
#: invite token (R2.3). Kept as a constant so the route and template agree.
INVITE_USED_MESSAGE = "Sorry, this invitation link has been used or has expired!"


def _invite_service():
    """Return the app's InviteService or ``None`` in degraded mode."""
    return current_app.extensions.get("invite_service")


def _password_rules() -> list[dict[str, str]]:
    """Return the ``{id,label}`` password rules for the registration checklist."""
    return [
        {"id": rid, "label": label}
        for rid, label, _ in register_policy.PASSWORD_RULES
    ]


def _render_form(token: str, email: str, *, username: str = "", error=None):
    """Render the invite registration form bound to ``token``/``email``."""
    return render_template(
        "pages/invite_register.html",
        token=token,
        email=email,
        username=username,
        error=error,
        password_rules=_password_rules(),
    )


def _used():
    """Render the fixed used/expired message page (R2.3)."""
    return render_template("pages/invite_used.html", message=INVITE_USED_MESSAGE)


def build_invite_public_blueprint() -> Blueprint:
    """Construct the public invite-registration blueprint."""
    bp = Blueprint("invite_public", __name__)

    @bp.route("/invite/<token>", methods=["GET", "POST"])
    def invite_register(token: str):  # type: ignore[unused-ignore]
        """Public registration page / submit for a single-use invite link.

        GET renders the form for a valid, unused, unexpired token (bound to the
        invite email, shown read-only); any invalid/consumed/expired/unknown
        token — or a degraded app — renders the fixed used/expired message and
        never the form (R2.1, R2.3). POST delegates to :func:`_submit`.
        """
        service = _invite_service()
        if service is None:
            return _used()
        if request.method == "POST":
            return _submit(service, token)
        try:
            invite = service.resolve_by_token(token)
        except InviteConsumedError:
            return _used()
        return _render_form(token, invite.get("email", ""))

    @bp.route("/invite/<token>/username-available")
    def invite_username_available(token: str):  # type: ignore[unused-ignore]
        """Live availability check for a chosen username (JSON).

        Public, GET-only. Returns ``{"valid", "available", "error"}`` for the
        ``u`` query param so the page shows an "as you type" hint. Requires a
        still-valid token (a bad token yields ``available: false`` without
        leaking whether the name exists). Authoritatively re-checked at register.
        """
        service = _invite_service()
        candidate = request.args.get("u", "")
        try:
            clean = register_policy.validate_username(candidate)
        except register_policy.UsernamePolicyError as exc:
            return jsonify(valid=False, available=False, error=str(exc))
        if service is None:
            return jsonify(valid=True, available=False, error="")
        try:
            service.resolve_by_token(token)
        except InviteConsumedError:
            return jsonify(valid=True, available=False, error="")
        return jsonify(
            valid=True, available=service.display_name_available(clean), error=""
        )

    return bp


def _submit(service: Any, token: str):
    """Handle the ``POST /invite/<token>`` registration submission.

    Validates the chosen username + password pair, then consumes the token and
    creates the account via ``service.register`` (chosen password + preferred
    username). On success the invitee is sent into Discord linking with **no**
    authenticated session (R2.4). A malformed username, password mismatch, or
    policy failure re-renders the form (retaining the username, showing the
    error); a token that no longer resolves shows the used/expired page (R2.5).
    """
    username = request.form.get("username", "")
    password = request.form.get("password", "")
    confirm = request.form.get("password_confirm", "")

    def _rerender(error: str):
        try:
            invite = service.resolve_by_token(token)
        except InviteConsumedError:
            return _used()
        return _render_form(
            token, invite.get("email", ""), username=username, error=error
        )

    try:
        clean_username = register_policy.validate_username(username)
    except register_policy.UsernamePolicyError as exc:
        return _rerender(str(exc))
    if not password:
        return _rerender("Please choose a password.")
    if password != confirm:
        return _rerender("Passwords do not match.")

    try:
        account = service.register(
            token, display_name=clean_username, password=password
        )
    except register_policy.PasswordPolicyError as exc:
        return _rerender(
            "Password does not meet the requirements: " + ", ".join(exc.unmet)
        )
    except InviteConsumedError:
        return _used()

    # Registration grants no lasting session (R2.4): stash only the new account's
    # Cognito subject as a pending-link handoff so the Discord-link flow can bind
    # the identity. The session is established only after Discord OAuth succeeds.
    sub = account.get("sub") if isinstance(account, dict) else None
    if sub:
        session["pending_link_sub"] = sub
    return redirect(url_for("auth.discord_link"))

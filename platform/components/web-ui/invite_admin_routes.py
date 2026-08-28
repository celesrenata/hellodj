"""Admin invite-management routes (Platform_Owner only).

Split out of :mod:`pages` to keep each source file under the per-file line
ceiling (R13.3). Provides the admin surface for the tokenized invite flow:

* ``POST /admin/invite``               — mint a single-use token + send the
                                          branded SES invitation (R1.1).
* ``POST /admin/invite/<email>/resend`` — mint a *fresh* token and re-send it,
                                          invalidating the prior link (R1.4).
* ``POST /admin/invite/<email>/revoke`` — revoke a pending invite so its token
                                          can no longer be used (R1.4).
* ``GET  /admin/invites``               — HTMX partial listing every invite with
                                          its status (invited/accepted/expired)
                                          (R1.2, R1.4).

Every route mirrors the admin-only guard used by the other admin routes:
unauthenticated callers are redirected to login and non-admins to the
dashboard, so the invite surface is never exposed to a regular user.

Requirements: 1.2, 1.4
"""

from __future__ import annotations

from typing import Any

from flask import (
    Blueprint,
    current_app,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

__all__ = ["build_invite_admin_blueprint"]


def _invite_service() -> Any | None:
    """Return the app's InviteService or ``None`` in degraded mode."""
    return current_app.extensions.get("invite_service")


def _require_login() -> bool:
    """Return whether an authenticated session exists."""
    return bool(session.get("user"))


def _is_admin() -> bool:
    """Return whether the current session belongs to an administrator."""
    return bool((session.get("user") or {}).get("is_admin"))


def _invited_by() -> str:
    """Return the acting admin's email to stamp as the inviter."""
    return (session.get("user") or {}).get("email", "")


def _invite_list(error: str | None = None, success: str | None = None):
    """Render the invite-list partial with the current invites + any notice."""
    service = _invite_service()
    invites = service.list_invites() if service else []
    return render_template(
        "partials/admin_invite_list.html",
        invites=invites,
        invite_error=error,
        invite_success=success,
    )


def build_invite_admin_blueprint() -> Blueprint:
    """Construct the admin invite-management blueprint."""
    bp = Blueprint("invite_admin", __name__)

    @bp.route("/admin/invites")
    def invite_list():  # type: ignore[unused-ignore]
        """HTMX partial: list every invite with its status. Admin-only."""
        if not _require_login():
            return redirect(url_for("pages.login"))
        if not _is_admin():
            return redirect(url_for("pages.dashboard"))
        return _invite_list()

    @bp.route("/admin/invite", methods=["POST"])
    def invite_create():  # type: ignore[unused-ignore]
        """Mint a single-use token + send the branded invite. Admin-only."""
        if not _require_login():
            return redirect(url_for("pages.login"))
        if not _is_admin():
            return redirect(url_for("pages.dashboard"))
        email = request.form.get("email", "").strip()
        service = _invite_service()
        error = None
        success = None
        if not service:
            error = "invites are not available (no directory configured)"
        else:
            try:
                service.invite(email, invited_by=_invited_by())
                success = f"Invite sent to {email}."
            except Exception as exc:  # noqa: BLE001 - surface message to admin
                error = str(exc)
        return _invite_list(error=error, success=success)

    @bp.route("/admin/invite/<email>/resend", methods=["POST"])
    def invite_resend(email: str):  # type: ignore[unused-ignore]
        """Mint a fresh token and re-send it, invalidating the old link."""
        if not _require_login():
            return redirect(url_for("pages.login"))
        if not _is_admin():
            return redirect(url_for("pages.dashboard"))
        service = _invite_service()
        error = None
        success = None
        if not service:
            error = "invites are not available (no directory configured)"
        else:
            try:
                service.resend(email, invited_by=_invited_by())
                success = f"Invite re-sent to {email}."
            except Exception as exc:  # noqa: BLE001 - surface message to admin
                error = str(exc)
        return _invite_list(error=error, success=success)

    @bp.route("/admin/invite/<email>/revoke", methods=["POST"])
    def invite_revoke(email: str):  # type: ignore[unused-ignore]
        """Revoke a pending invite so its token can no longer be used."""
        if not _require_login():
            return redirect(url_for("pages.login"))
        if not _is_admin():
            return redirect(url_for("pages.dashboard"))
        service = _invite_service()
        error = None
        success = None
        if not service:
            error = "invites are not available (no directory configured)"
        else:
            try:
                service.revoke(email)
                success = f"Invite for {email} revoked."
            except Exception as exc:  # noqa: BLE001 - surface message to admin
                error = str(exc)
        return _invite_list(error=error, success=success)

    @bp.route("/admin/invite/<email>/delete", methods=["POST"])
    def invite_delete(email: str):  # type: ignore[unused-ignore]
        """Permanently delete an invite record so it drops off the list.

        Unlike revoke (which leaves a ``revoked`` row), this removes the invite
        entirely — used to clear out accepted/expired/revoked clutter. Admin-only.
        """
        if not _require_login():
            return redirect(url_for("pages.login"))
        if not _is_admin():
            return redirect(url_for("pages.dashboard"))
        service = _invite_service()
        error = None
        success = None
        if not service:
            error = "invites are not available (no directory configured)"
        else:
            try:
                service.delete(email)
                success = f"Invite for {email} deleted."
            except Exception as exc:  # noqa: BLE001 - surface message to admin
                error = str(exc)
        return _invite_list(error=error, success=success)

    return bp

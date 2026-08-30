"""Admin-panel user-management routes (role / enable / delete / reset).

Extracted from ``pages.py`` to keep that module under the 500-line ceiling.
These are the HTMX partial routes the admin user directory drives:

* ``POST /admin/users/<username>/role`` — promote/demote the ``admins`` group;
* ``POST /admin/users/<username>/enabled`` — reversible enable/disable;
* ``POST /admin/users/<username>/delete`` — permanent Cognito account delete;
* ``POST /admin/users/<username>/reset-password`` — trigger a Cognito
  password-reset email (``AdminResetUserPassword``); the admin never sees or
  sets the new password.

Every route is admin-only (unauthenticated -> login, non-admin -> dashboard)
and re-renders ``partials/admin_user_list.html``. The pages-module helpers the
routes depend on (auth guards + directory/invite accessors) are injected via
:func:`register_admin_user_routes` so this module has no circular import back
into ``pages.py``.

Requirements: 8.2 (admin auth manages accounts), 8.5 (account recovery),
6.5 (web admin UI).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from flask import Blueprint, redirect, render_template, request, url_for

__all__ = ["register_admin_user_routes"]


def register_admin_user_routes(
    bp: Blueprint,
    *,
    require_login: Callable[[], bool],
    is_admin: Callable[[], bool],
    admin_directory: Callable[[], Any],
    admin_users: Callable[[], list[dict[str, Any]]],
    email_for_username: Callable[[str], str],
    delete_invite_for: Callable[[str], None],
) -> None:
    """Attach the admin user-management routes to ``bp``.

    Args:
        bp: The pages blueprint the routes are registered on.
        require_login: Returns whether the session is authenticated.
        is_admin: Returns whether the session is an administrator.
        admin_directory: Returns the :class:`AdminDirectory` or ``None``.
        admin_users: Returns the current directory rows for re-render.
        email_for_username: Resolves an account's email (for invite cleanup).
        delete_invite_for: Clears a lingering invite record by email.
    """

    @bp.route("/admin/users/<username>/role", methods=["POST"])
    def admin_set_role(username: str):  # type: ignore[unused-ignore]
        """Promote/demote a user's admin role, then return the row. Admin-only."""
        if not require_login():
            return redirect(url_for("pages.login"))
        if not is_admin():
            return redirect(url_for("pages.dashboard"))
        make_admin = request.form.get("admin") == "true"
        directory = admin_directory()
        if directory:
            directory.set_admin(username, make_admin)
        return render_template(
            "partials/admin_user_list.html", users=admin_users()
        )

    @bp.route("/admin/users/<username>/enabled", methods=["POST"])
    def admin_set_enabled(username: str):  # type: ignore[unused-ignore]
        """Enable/disable an account, then return the list. Admin-only."""
        if not require_login():
            return redirect(url_for("pages.login"))
        if not is_admin():
            return redirect(url_for("pages.dashboard"))
        enabled = request.form.get("enabled") == "true"
        directory = admin_directory()
        if directory:
            directory.set_enabled(username, enabled)
        return render_template(
            "partials/admin_user_list.html", users=admin_users()
        )

    @bp.route("/admin/users/<username>/delete", methods=["POST"])
    def admin_delete_user(username: str):  # type: ignore[unused-ignore]
        """Permanently delete an account, then return the list. Admin-only.

        Distinct from disabling (a reversible flag): this removes the account
        from Cognito outright, so it drops off the directory. A surfaced error
        (e.g. Cognito failure) is shown as a notice above the refreshed list.
        """
        if not require_login():
            return redirect(url_for("pages.login"))
        if not is_admin():
            return redirect(url_for("pages.dashboard"))
        directory = admin_directory()
        error = None
        success = None
        if not directory:
            error = "user management is not available (no directory configured)"
        else:
            # Resolve the account's email BEFORE deletion so we can also clear
            # any lingering invite record — deleting the Cognito user alone
            # leaves the invite stuck "pending" and blocks re-inviting the same
            # address (the two live in separate systems).
            email = email_for_username(username)
            try:
                directory.delete_user(username)
                delete_invite_for(email)
                success = f"Account {username} deleted."
            except Exception as exc:  # noqa: BLE001 - surface message to admin
                error = str(exc)
        return render_template(
            "partials/admin_user_list.html",
            users=admin_users(),
            invite_error=error,
            invite_success=success,
        )

    @bp.route("/admin/users/<username>/reset-password", methods=["POST"])
    def admin_reset_password(username: str):  # type: ignore[unused-ignore]
        """Trigger a password-reset email for an account. Admin-only.

        Calls Cognito AdminResetUserPassword, which emails the user a reset
        code (via the pool's branded SES template) and puts them in
        RESET_REQUIRED. The admin never sees or sets the new password. A
        surfaced error (e.g. no verified email) is shown above the refreshed
        list.
        """
        if not require_login():
            return redirect(url_for("pages.login"))
        if not is_admin():
            return redirect(url_for("pages.dashboard"))
        directory = admin_directory()
        error = None
        success = None
        if not directory:
            error = "user management is not available (no directory configured)"
        else:
            try:
                directory.reset_password(username)
                success = f"Password-reset email sent to {username}."
            except Exception as exc:  # noqa: BLE001 - surface message to admin
                error = str(exc)
        return render_template(
            "partials/admin_user_list.html",
            users=admin_users(),
            invite_error=error,
            invite_success=success,
        )

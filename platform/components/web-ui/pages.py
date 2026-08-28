"""Page and HTMX-partial routes for the web-ui.

Renders the full pages (dashboard, config, guilds, login) that extend the
sidebar shell (``base.html``) and the HTMX partial fragments swapped into the
main content area. Configuration data is read from / written to DynamoDB via
the :class:`ConfigStore` stored on the app; when no store is configured the
routes degrade gracefully to empty data so template rendering still works.

Requirements: 6.5, 14.1, 14.2, 14.3
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

from invite_service import InviteConsumedError

__all__ = ["build_pages_blueprint"]

#: The single fixed message shown for any invalid/consumed/expired/unknown
#: invite token (R2.3). Kept as a constant so the route and templates agree.
INVITE_USED_MESSAGE = "Sorry, this invitation link has been used or has expired!"

#: Base sidebar navigation model (icon key + label + endpoint). The admin entry
#: is appended only for users in the Cognito ``admins`` group (see `_nav_for`).
NAV_ITEMS = [
    {"key": "dashboard", "label": "Dashboard", "endpoint": "pages.dashboard"},
    {"key": "config", "label": "Config", "endpoint": "pages.config"},
    {"key": "guilds", "label": "Guilds", "endpoint": "pages.guilds"},
]

#: The admin-only nav entry, shown to Cognito ``admins`` group members only.
ADMIN_NAV_ITEM = {
    "key": "admin",
    "label": "Admin",
    "endpoint": "pages.admin",
}


def _config_store():
    """Return the app's ConfigStore or ``None`` in degraded mode."""
    return current_app.extensions.get("config_store")


def _admin_directory():
    """Return the app's AdminDirectory (Cognito user admin) or ``None``."""
    return current_app.extensions.get("admin_directory")


def _invite_service():
    """Return the app's InviteService or ``None`` in degraded mode."""
    return current_app.extensions.get("invite_service")


def _require_login() -> bool:
    """Return whether an authenticated session exists."""
    return bool(session.get("user"))


def _is_admin() -> bool:
    """Return whether the current session belongs to an administrator.

    An administrator authenticated through Cognito and is a member of the
    ``admins`` group; the group membership is captured on the session at login
    (``user.is_admin``). This gates the admin panel and its routes — a regular
    (Discord-OAuth) user never sees or reaches admin functionality.
    """
    user = session.get("user") or {}
    return bool(user.get("is_admin"))


def _nav_for_current_user() -> list[dict[str, Any]]:
    """Return the nav items visible to the current user (admins get +Admin)."""
    if _is_admin():
        return [*NAV_ITEMS, ADMIN_NAV_ITEM]
    return list(NAV_ITEMS)


def _layout() -> str:
    """Pick the template layout: partial for HTMX nav, full shell otherwise.

    An HTMX navigation (`hx-get` into `#main-content`) sends the `HX-Request`
    header. Returning the full `base.html` shell for those requests nests the
    entire sidebar inside the content area (the reported nav-nesting bug), so
    HTMX requests render `_partial.html` (content + out-of-band heading/title)
    instead. A normal full-page load renders `base.html`.
    """
    if request.headers.get("HX-Request") == "true":
        return "_partial.html"
    return "base.html"


def build_pages_blueprint() -> Blueprint:
    """Construct the pages blueprint."""
    bp = Blueprint("pages", __name__)

    @bp.route("/login")
    def login():  # type: ignore[unused-ignore]
        """Public login landing page (Discord / Cognito / register entry)."""
        return render_template(
            "pages/login.html", error=request.args.get("error")
        )

    @bp.route("/invite/<token>", methods=["GET", "POST"])
    def invite_register(token: str):  # type: ignore[unused-ignore]
        """Public registration page for a single-use invite link.

        No session is required — this is the entry point for a brand-new,
        unregistered invitee.

        GET (R2.1, R2.3): the token is resolved via the invite service; a
        valid, unused, unexpired token renders a HelloDJ-hosted registration
        page bound to the invite's email (shown read-only). Any invalid,
        consumed, expired, or unknown token — or a degraded app with no invite
        service — renders exactly the fixed "used or has expired" message
        (R2.3) and never the registration form.

        POST (R2.2, R2.4, R2.5): validates the submitted password pair and
        consumes the token via ``invite_service.register`` — a single-use,
        atomic flip that creates the CONFIRMED Cognito account. On success the
        user is redirected into Discord linking (their login method going
        forward); the registration POST itself establishes **no** authenticated
        session (R2.4). A token that lost the single-use race or expired
        mid-flow raises :class:`InviteConsumedError` and renders the fixed
        used/expired message (R2.5).
        """
        service = _invite_service()
        if service is None:
            return render_template(
                "pages/invite_used.html", message=INVITE_USED_MESSAGE
            )
        if request.method == "POST":
            return _invite_register_submit(service, token)
        try:
            invite = service.resolve_by_token(token)
        except InviteConsumedError:
            return render_template(
                "pages/invite_used.html", message=INVITE_USED_MESSAGE
            )
        return render_template(
            "pages/invite_register.html",
            token=token,
            email=invite.get("email", ""),
        )

    @bp.route("/")
    def dashboard():  # type: ignore[unused-ignore]
        """Dashboard with status cards. Login-required."""
        if not _require_login():
            return redirect(url_for("pages.login"))
        stats = _dashboard_stats()
        return render_template(
            "pages/dashboard.html",
            layout=_layout(),
            nav_items=_nav_for_current_user(),
            active="dashboard",
            stats=stats,
        )

    @bp.route("/config")
    def config():  # type: ignore[unused-ignore]
        """Global platform configuration page. Login-required."""
        if not _require_login():
            return redirect(url_for("pages.login"))
        store = _config_store()
        values = store.get_global() if store else {}
        return render_template(
            "pages/config.html",
            layout=_layout(),
            nav_items=_nav_for_current_user(),
            active="config",
            config=values,
            tidal=request.args.get("tidal"),
        )

    @bp.route("/config", methods=["POST"])
    def config_save():  # type: ignore[unused-ignore]
        """Persist global config edits; returns the HTMX form partial."""
        if not _require_login():
            return redirect(url_for("pages.login"))
        store = _config_store()
        values = _form_values(request.form)
        saved = store.set_global(values) if store else values
        return render_template(
            "partials/config_form.html", config=saved, saved=True
        )

    @bp.route("/guilds")
    def guilds():  # type: ignore[unused-ignore]
        """Guild list page. Login-required."""
        if not _require_login():
            return redirect(url_for("pages.login"))
        return render_template(
            "pages/guilds.html",
            layout=_layout(),
            nav_items=_nav_for_current_user(),
            active="guilds",
            guilds=_guild_list(),
        )

    @bp.route("/guilds/search")
    def guilds_search():  # type: ignore[unused-ignore]
        """HTMX live-search partial over the guild list."""
        if not _require_login():
            return redirect(url_for("pages.login"))
        query = request.args.get("q", "").strip().lower()
        matches = [
            g for g in _guild_list() if query in g["name"].lower()
        ]
        return render_template("partials/guild_list.html", guilds=matches)

    # ----- Admin panel: manage all accounts (admins group only) ----------- #

    @bp.route("/admin")
    def admin():  # type: ignore[unused-ignore]
        """Admin panel: list and manage all user accounts. Admin-only.

        Unlike a standard user account (which only administers itself), an
        administrator manages every account on the platform: listing users,
        promoting/demoting admins, and disabling/enabling accounts. Regular
        users are redirected to the dashboard — the panel is never exposed to
        a non-admin (R8.2).
        """
        if not _require_login():
            return redirect(url_for("pages.login"))
        if not _is_admin():
            return redirect(url_for("pages.dashboard"))
        return render_template(
            "pages/admin.html",
            layout=_layout(),
            nav_items=_nav_for_current_user(),
            active="admin",
            users=_admin_users(),
            invites=_admin_invites(),
        )

    @bp.route("/admin/users/search")
    def admin_users_search():  # type: ignore[unused-ignore]
        """HTMX live-search partial over the user directory. Admin-only."""
        if not _require_login():
            return redirect(url_for("pages.login"))
        if not _is_admin():
            return redirect(url_for("pages.dashboard"))
        query = request.args.get("q", "").strip().lower()
        matches = [
            u
            for u in _admin_users()
            if query in u["username"].lower() or query in u["email"].lower()
        ]
        return render_template("partials/admin_user_list.html", users=matches)

    @bp.route("/admin/users/<username>/role", methods=["POST"])
    def admin_set_role(username: str):  # type: ignore[unused-ignore]
        """Promote/demote a user's admin role, then return the row. Admin-only."""
        if not _require_login():
            return redirect(url_for("pages.login"))
        if not _is_admin():
            return redirect(url_for("pages.dashboard"))
        make_admin = request.form.get("admin") == "true"
        directory = _admin_directory()
        if directory:
            directory.set_admin(username, make_admin)
        return render_template("partials/admin_user_list.html", users=_admin_users())

    @bp.route("/admin/users/<username>/enabled", methods=["POST"])
    def admin_set_enabled(username: str):  # type: ignore[unused-ignore]
        """Enable/disable an account, then return the list. Admin-only."""
        if not _require_login():
            return redirect(url_for("pages.login"))
        if not _is_admin():
            return redirect(url_for("pages.dashboard"))
        enabled = request.form.get("enabled") == "true"
        directory = _admin_directory()
        if directory:
            directory.set_enabled(username, enabled)
        return render_template("partials/admin_user_list.html", users=_admin_users())

    @bp.route("/admin/users/<username>/delete", methods=["POST"])
    def admin_delete_user(username: str):  # type: ignore[unused-ignore]
        """Permanently delete an account, then return the list. Admin-only.

        Distinct from disabling (a reversible flag): this removes the account
        from Cognito outright, so it drops off the directory. A surfaced error
        (e.g. Cognito failure) is shown as a notice above the refreshed list.
        """
        if not _require_login():
            return redirect(url_for("pages.login"))
        if not _is_admin():
            return redirect(url_for("pages.dashboard"))
        directory = _admin_directory()
        error = None
        success = None
        if not directory:
            error = "user management is not available (no directory configured)"
        else:
            try:
                directory.delete_user(username)
                success = f"Account {username} deleted."
            except Exception as exc:  # noqa: BLE001 - surface message to admin
                error = str(exc)
        return render_template(
            "partials/admin_user_list.html",
            users=_admin_users(),
            invite_error=error,
            invite_success=success,
        )

    return bp


def _invite_register_submit(service: Any, token: str):
    """Handle the ``POST /invite/<token>`` registration submission.

    Validates the password pair, then consumes the token and creates the
    account via ``service.register``. On success the invitee is sent into
    Discord linking (R2.4) with **no** authenticated session set on this
    request. A password mismatch/empty re-renders the form with an error (and
    the read-only email), unless the token no longer resolves — in which case
    the fixed used/expired message is shown. A token that lost the single-use
    race or expired mid-flow raises :class:`InviteConsumedError` and likewise
    renders the used/expired message (R2.5).
    """
    password = request.form.get("password", "")
    confirm = request.form.get("password_confirm", "")
    if not password or password != confirm:
        # Re-render the form; the email must still be resolvable to bind the
        # form. If the token no longer resolves, show used/expired instead.
        try:
            invite = service.resolve_by_token(token)
        except InviteConsumedError:
            return render_template(
                "pages/invite_used.html", message=INVITE_USED_MESSAGE
            )
        error = (
            "Passwords do not match."
            if password
            else "Please choose a password."
        )
        return render_template(
            "pages/invite_register.html",
            token=token,
            email=invite.get("email", ""),
            error=error,
        )
    try:
        account = service.register(token)
    except InviteConsumedError:
        # Lost the single-use race or expired mid-flow (R2.5).
        return render_template(
            "pages/invite_used.html", message=INVITE_USED_MESSAGE
        )
    # Registration grants no lasting session (R2.4). We stash only the new
    # account's Cognito subject as a pending-link handoff so the Discord-link
    # flow can bind the identity; this is deliberately NOT a full authenticated
    # session — the session is established only after Discord OAuth succeeds.
    sub = account.get("sub") if isinstance(account, dict) else None
    if sub:
        session["pending_link_sub"] = sub
    # The invitee links Discord next, which becomes their login method going
    # forward (R2.4).
    return redirect(url_for("auth.discord_link"))


def _form_values(form: Any) -> dict[str, Any]:
    """Normalize a submitted config form into a plain dict."""
    return {key: value for key, value in form.items() if key != "csrf_token"}


def _dashboard_stats() -> list[dict[str, Any]]:
    """Return status-card data for the dashboard.

    Values are derived from config where available; the shape is stable so the
    template renders identically in tests and at runtime.
    """
    store = _config_store()
    cfg = store.get_global() if store else {}
    return [
        {"label": "Active Guilds", "value": cfg.get("active_guilds", 0)},
        {"label": "Tracks Today", "value": cfg.get("tracks_today", 0)},
        {"label": "Voice Sessions", "value": cfg.get("voice_sessions", 0)},
    ]


def _guild_list() -> list[dict[str, Any]]:
    """Return the guild rows to render (empty until wired to live data)."""
    return []


def _admin_users() -> list[dict[str, Any]]:
    """Return the user directory rows for the admin panel.

    Sourced from the Cognito-backed :class:`AdminDirectory` when configured;
    degrades to an empty list (so template rendering still works in tests /
    no-datastore mode).
    """
    directory = _admin_directory()
    if not directory:
        return []
    return directory.list_users()


def _admin_invites() -> list[dict[str, Any]]:
    """Return the invite rows for the admin panel's invite list.

    Sourced from the :class:`InviteService` when configured; degrades to an
    empty list so the panel renders in tests / no-datastore mode. The HTMX
    ``load`` trigger on the list also refreshes this immediately client-side.
    """
    service = _invite_service()
    if not service:
        return []
    return service.list_invites()

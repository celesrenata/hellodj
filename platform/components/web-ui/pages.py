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

# Aliased: ``registration_mode`` is also a template context variable name.
import registration_mode as registration_mode_module
from admin_dashboard import admin_dashboard_stats
from config_store import effective_default_source

__all__ = ["build_pages_blueprint"]

#: Sidebar navigation for a regular (Discord-OAuth) member. A member manages
#: only themselves: platform Config, their Guilds, and their Account source
#: connections. Administrators do NOT see these — they run the platform, not a
#: personal account — and get :data:`ADMIN_NAV_ITEMS` instead (see
#: ``_nav_for_current_user``).
USER_NAV_ITEMS = [
    {"key": "dashboard", "label": "Dashboard", "endpoint": "pages.dashboard"},
    {"key": "config", "label": "Config", "endpoint": "pages.config"},
    {"key": "guilds", "label": "Guilds", "endpoint": "pages.guilds"},
    {"key": "account", "label": "Account", "endpoint": "guild.account"},
]

#: Backwards-compatible alias for the member navigation model.
NAV_ITEMS = USER_NAV_ITEMS

#: Sidebar navigation for an administrator (Cognito ``admins`` group). An admin
#: administers the *platform*, not a personal account, so the member-only
#: Config/Guilds/Account entries are intentionally absent — their landing page
#: is the KPI ``Dashboard`` and they manage accounts (``Admin``) and per-user
#: ``Entitlements`` (its own blueprint).
ADMIN_NAV_ITEMS = [
    {"key": "dashboard", "label": "Dashboard", "endpoint": "pages.dashboard"},
    {"key": "admin", "label": "Admin", "endpoint": "pages.admin"},
    {
        "key": "entitlements",
        "label": "Entitlements",
        "endpoint": "entitlements.entitlements_index",
    },
]


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
    """Return the nav items visible to the current user.

    An administrator runs the platform, not a personal account, so they get a
    dedicated navigation — the KPI ``Dashboard``, the account-management
    ``Admin`` panel, and the per-user ``Entitlements`` control plane — WITHOUT
    the member-only Config/Guilds/Account entries. A regular (Discord-OAuth)
    member gets exactly the member navigation and never the admin/entitlements
    control planes (R1.4).
    """
    if _is_admin():
        return list(ADMIN_NAV_ITEMS)
    return list(USER_NAV_ITEMS)


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
        """Public login landing page (Discord / Cognito / register entry).

        Reads the global Registration_Mode so the page shows the fixed per-mode
        banner and exposes the ``/register`` link only when OPEN (R3.1-R3.4). In
        no-datastore mode the store is ``None`` ⇒ ``current_mode({})`` ⇒ CLOSED
        (secure default). Hiding the link is advisory; the route is authoritative.
        """
        store = _config_store()
        mode = registration_mode_module.current_mode(
            store.get_global() if store else {}
        )
        return render_template(
            "pages/login.html",
            error=request.args.get("error"),
            registration_mode=mode,
            registration_open=(mode == registration_mode_module.OPEN),
            registration_banner=registration_mode_module.banner_text(mode),
            registration_closed_notice=(
                request.args.get("registration") == "closed"
            ),
        )

    @bp.route("/")
    def dashboard():  # type: ignore[unused-ignore]
        """Landing dashboard. Login-required.

        An administrator lands on a service-wide KPI dashboard (real at-a-glance
        metrics for the whole platform); a regular member lands on the per-user
        dashboard. Both share the ``/`` route and the sidebar shell — only the
        rendered content and the nav differ by role.
        """
        if not _require_login():
            return redirect(url_for("pages.login"))
        if _is_admin():
            return render_template(
                "pages/admin_dashboard.html",
                layout=_layout(),
                nav_items=_nav_for_current_user(),
                active="dashboard",
                stats=_admin_stats(),
            )
        return render_template(
            "pages/dashboard.html",
            layout=_layout(),
            nav_items=_nav_for_current_user(),
            active="dashboard",
            stats=_dashboard_stats(),
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
            config=_config_for_render(values),
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
            "partials/config_form.html",
            config=_config_for_render(saved),
            saved=True,
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
        store = _config_store()
        mode = registration_mode_module.current_mode(
            store.get_global() if store else {}
        )
        return render_template(
            "pages/admin.html",
            layout=_layout(),
            nav_items=_nav_for_current_user(),
            active="admin",
            users=_admin_users(),
            invites=_admin_invites(),
            registration_mode=mode,
            registration_open=(mode == registration_mode_module.OPEN),
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

    @bp.route("/admin/registration-mode", methods=["POST"])
    def admin_set_registration_mode():  # type: ignore[unused-ignore]
        """Set the global Registration_Mode. Admin-only (R4.1, R4.2, R5.1).

        Two-layer guard mirroring ``entitlement_routes.py``: a non-admin is
        redirected before any change, and a hardened in-body ``_is_admin``
        fallback denies with a 403 + session clear if the redirect guard is
        somehow bypassed (defense in depth, R4.3, R4.4). In no-datastore mode
        the config store is ``None`` — nothing is mutated and the panel shows an
        ``unavailable`` notice. Otherwise the submitted value is audited and
        persisted via ``registration_mode.apply_mode_change`` (normalization
        makes a tampered value only ever resolve to CLOSED; an unchanged
        submission is a no-op, R5.2).
        """
        if not _require_login():
            return redirect(url_for("pages.login"))
        if not _is_admin():
            return redirect(url_for("pages.dashboard"))
        if not _is_admin():  # hardened fallback (Property: admin-only)
            session.clear()
            return "Forbidden", 403
        store = _config_store()
        if store is None:
            return redirect(url_for("pages.admin", regmode="unavailable"))
        registration_mode_module.apply_mode_change(
            store,
            store.core_table,
            requested=request.form.get("mode", ""),
            admin_sub=(session.get("user") or {}).get("sub", ""),
        )
        return redirect(url_for("pages.admin", regmode="saved"))

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
            # Resolve the account's email BEFORE deletion so we can also clear
            # any lingering invite record — deleting the Cognito user alone
            # leaves the invite stuck "pending" and blocks re-inviting the same
            # address (the two live in separate systems).
            email = _email_for_username(username)
            try:
                directory.delete_user(username)
                _delete_invite_for(email)
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


def _form_values(form: Any) -> dict[str, Any]:
    """Normalize a submitted config form into a plain dict."""
    return {key: value for key, value in form.items() if key != "csrf_token"}


def _config_for_render(config: dict[str, Any]) -> dict[str, Any]:
    """Return a render-ready copy of ``config`` with the default source resolved.

    The config form preselects ``youtube`` when no ``default_source`` is stored
    (R7.2), so the effective default is materialized into the payload the
    template renders rather than left to the template's ``.get`` fallback.
    """
    rendered = dict(config)
    rendered["default_source"] = effective_default_source(config)
    return rendered


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


def _admin_stats() -> list[dict[str, Any]]:
    """Return the administrator dashboard KPI cards from live platform data.

    Delegates to :func:`admin_dashboard.admin_dashboard_stats`, wiring the
    Cognito user directory, the invite service, and the ``hellodj-core`` table
    (each ``None`` in degraded mode). Every metric degrades to ``0``
    independently, so the admin dashboard always renders a full card set.
    """
    store = _config_store()
    core_table = store.core_table if store else None
    return admin_dashboard_stats(_admin_directory(), _invite_service(), core_table)


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


def _email_for_username(username: str) -> str:
    """Return the email attribute of a directory user, or '' if unknown.

    ``username`` here is the Cognito ``Username`` (the opaque login id the
    admin routes address the account by), so match it against the row's
    ``login`` — the display ``username`` field is now the friendly name
    (preferred_username / email), not the login id.
    """
    for row in _admin_users():
        if row.get("login") == username or row.get("username") == username:
            return row.get("email", "") or ""
    return ""


def _delete_invite_for(email: str) -> None:
    """Best-effort delete of any invite record for ``email``.

    Called after deleting a Cognito account so a lingering invite (any status,
    including a legacy old-flow record) doesn't block re-inviting the address.
    Idempotent and non-fatal: a missing record or absent invite service is a
    no-op, and any failure here must not fail the account deletion.
    """
    if not email:
        return
    service = _invite_service()
    if not service:
        return
    try:
        service.delete(email)
    except Exception:  # noqa: BLE001 - cleanup is best-effort
        pass


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

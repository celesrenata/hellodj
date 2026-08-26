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

__all__ = ["build_pages_blueprint"]

#: Sidebar navigation model (icon key + label + endpoint) shared by every page.
NAV_ITEMS = [
    {"key": "dashboard", "label": "Dashboard", "endpoint": "pages.dashboard"},
    {"key": "config", "label": "Config", "endpoint": "pages.config"},
    {"key": "guilds", "label": "Guilds", "endpoint": "pages.guilds"},
]


def _config_store():
    """Return the app's ConfigStore or ``None`` in degraded mode."""
    return current_app.extensions.get("config_store")


def _require_login() -> bool:
    """Return whether an authenticated session exists."""
    return bool(session.get("user"))


def build_pages_blueprint() -> Blueprint:
    """Construct the pages blueprint."""
    bp = Blueprint("pages", __name__)

    @bp.route("/login")
    def login():  # type: ignore[unused-ignore]
        """Public login landing page (Discord / Cognito / register entry)."""
        return render_template(
            "pages/login.html", error=request.args.get("error")
        )

    @bp.route("/")
    def dashboard():  # type: ignore[unused-ignore]
        """Dashboard with status cards. Login-required."""
        if not _require_login():
            return redirect(url_for("pages.login"))
        stats = _dashboard_stats()
        return render_template(
            "pages/dashboard.html",
            nav_items=NAV_ITEMS,
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
            nav_items=NAV_ITEMS,
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
            nav_items=NAV_ITEMS,
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

    return bp


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

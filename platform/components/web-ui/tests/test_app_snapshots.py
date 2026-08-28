"""Flask unit + template-snapshot tests for the web-ui (R14).

These run ``create_app()`` in degraded (no-datastore) mode with a Flask test
client, so no DynamoDB / Secrets Manager is required. They assert:

* the ``/healthz`` liveness endpoint returns 200 with the stage,
* the public ``/login`` page renders (200) with the expected login markup,
* an unauthenticated ``/`` (and the other login-required pages) redirect to
  the login page,
* the authenticated pages render and extend the sidebar shell (``base.html``) —
  snapshotting the stable key elements (sidebar ``<nav>``, ``glass-panel``
  surfaces, nav items) rather than the whole byte-for-byte document.

Requirements: 6.5, 14.1, 14.2, 14.3, 14.4
"""

from __future__ import annotations


def _login(client) -> None:
    """Seed an authenticated session (bypasses the OAuth round-trip)."""
    with client.session_transaction() as sess:
        sess["user"] = {"provider": "discord_oauth"}


# --------------------------------------------------------------------------- #
# Health + public login
# --------------------------------------------------------------------------- #


def test_healthz_ok(client) -> None:
    resp = client.get("/healthz")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == "ok"
    assert body["stage"] == "beta"


def test_login_page_renders(client) -> None:
    resp = client.get("/login")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    # Key login markup snapshot.
    assert 'class="glass-panel login-card"' in html
    assert "Sign in to the control panel" in html
    assert "Log in with Discord" in html
    # First-party admin credential form posts to /auth/admin (no hosted UI).
    assert 'action="/auth/admin"' in html
    assert 'name="username"' in html
    assert 'name="password"' in html
    # In degraded (no-datastore) mode the registration mode defaults to CLOSED,
    # so the self-registration link is omitted and the closed banner shows; the
    # recover link is always present.
    assert "/auth/register" not in html
    assert "Registration is currently closed" in html
    assert "/auth/recover" in html


def test_login_page_shows_error_notice(client) -> None:
    # The error param renders the generic notice (error text is passed through).
    resp = client.get("/login?error=Session%20expired.")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert 'class="notice notice--danger"' in html
    assert 'role="alert"' in html


# --------------------------------------------------------------------------- #
# Auth gating: unauthenticated access redirects to login
# --------------------------------------------------------------------------- #


def test_root_redirects_to_login_when_unauthenticated(client) -> None:
    resp = client.get("/")
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]


def test_protected_pages_redirect_to_login(client) -> None:
    for path in ("/", "/config", "/guilds", "/guilds/search"):
        resp = client.get(path)
        assert resp.status_code == 302, f"{path} should redirect"
        assert "/login" in resp.headers["Location"], path


# --------------------------------------------------------------------------- #
# Authenticated pages extend the sidebar shell (snapshot key elements)
# --------------------------------------------------------------------------- #


def _assert_extends_shell(html: str) -> None:
    """Assert the rendered page is built on base.html's sidebar shell."""
    assert "<!doctype html>" in html.lower()
    # Sidebar shell markers from base.html.
    assert 'class="app-shell"' in html
    assert 'aria-label="Primary"' in html  # sidebar <aside>
    assert "sidebar glass-panel" in html
    assert 'class="sidebar__nav"' in html
    # Main content region HTMX swaps into.
    assert 'id="main-content"' in html
    # Nav items for every registered page.
    for label in ("Dashboard", "Config", "Guilds"):
        assert f">{label}</span>" in html, f"missing nav item {label}"
    # Logout control in the footer.
    assert "/auth/logout" in html


def test_dashboard_renders_and_extends_shell(client) -> None:
    _login(client)
    resp = client.get("/")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    _assert_extends_shell(html)
    # Dashboard-specific snapshot: stat cards + now-playing panel.
    assert 'class="dashboard-grid"' in html
    assert "Active Guilds" in html
    assert "Now Playing" in html
    # Active nav state is computed client-side (Alpine) from the current path so
    # it updates on HTMX navigation without re-rendering the shell. The shell
    # therefore carries the Alpine binding + the isActive() helper rather than a
    # server-rendered aria-current.
    assert ':aria-current="isActive(' in html
    assert "isActive(href)" in html


def test_config_page_renders_and_extends_shell(client) -> None:
    _login(client)
    resp = client.get("/config")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    _assert_extends_shell(html)
    # Config-specific snapshot: tablist + the Tidal reconnect entry point.
    assert 'role="tablist"' in html
    assert "Reconnect Tidal" in html
    assert "/auth/tidal/callback" in html


def test_guilds_page_renders_and_extends_shell(client) -> None:
    _login(client)
    resp = client.get("/guilds")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    _assert_extends_shell(html)
    # Guilds-specific snapshot: HTMX live-search input + list container.
    assert 'id="guild-search"' in html
    assert 'id="guild-list"' in html
    assert "/guilds/search" in html


def test_guilds_search_returns_partial_not_full_page(client) -> None:
    _login(client)
    resp = client.get("/guilds/search?q=anything")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    # Partial fragment: must NOT re-render the whole shell.
    assert "<!doctype html>" not in html.lower()
    assert 'class="app-shell"' not in html


def test_logout_clears_session_and_redirects(client) -> None:
    _login(client)
    resp = client.post("/auth/logout")
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]
    # After logout the dashboard is protected again.
    follow = client.get("/")
    assert follow.status_code == 302
    assert "/login" in follow.headers["Location"]

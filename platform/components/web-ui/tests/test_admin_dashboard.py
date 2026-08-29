"""Tests for the administrator dashboard + role-scoped navigation.

An administrator runs the platform, so they land on a service-wide KPI
dashboard and their sidebar drops the member-only Config/Guilds/Account entries
(the admin has no personal account to manage — they administer the platform).
A regular member is unchanged: member nav + the per-user dashboard.

Requirements: 6.5 (web admin UI), 8.2 (admin manages the platform).
"""

from __future__ import annotations

from admin_dashboard import admin_dashboard_stats


def _login_admin(client) -> None:
    """Seed an authenticated administrator session (Cognito ``admins``)."""
    with client.session_transaction() as sess:
        sess["user"] = {"provider": "cognito", "is_admin": True, "sub": "admin-1"}


def _login_member(client) -> None:
    """Seed an authenticated regular (Discord-OAuth) member session."""
    with client.session_transaction() as sess:
        sess["user"] = {"provider": "discord_oauth"}


# --------------------------------------------------------------------------- #
# Navigation is role-scoped
# --------------------------------------------------------------------------- #


def test_admin_nav_omits_member_only_entries(client) -> None:
    _login_admin(client)
    resp = client.get("/")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    # Admin gets Dashboard + Admin + Entitlements ...
    assert ">Dashboard</span>" in html
    assert ">Admin</span>" in html
    assert ">Entitlements</span>" in html
    # ... and NOT the member-only Config/Guilds/Account entries.
    assert ">Config</span>" not in html
    assert ">Guilds</span>" not in html
    assert ">Account</span>" not in html


def test_member_nav_keeps_member_entries(client) -> None:
    _login_member(client)
    resp = client.get("/")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    for label in ("Dashboard", "Config", "Guilds", "Account"):
        assert f">{label}</span>" in html, f"missing member nav item {label}"
    # A member never sees the admin control planes.
    assert ">Admin</span>" not in html
    assert ">Entitlements</span>" not in html


# --------------------------------------------------------------------------- #
# Admin dashboard renders KPI cards
# --------------------------------------------------------------------------- #


def test_admin_dashboard_renders_kpi_cards(client) -> None:
    _login_admin(client)
    resp = client.get("/")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert 'class="dashboard-grid"' in html
    for label in (
        "Total Users",
        "Administrators",
        "Disabled Accounts",
        "Pending Invites",
        "Guilds",
        "Connected Sources",
    ):
        assert label in html, f"missing KPI card {label}"
    assert "Service Overview" in html
    # It's the admin dashboard, not the member "Now Playing" one.
    assert "Now Playing" not in html


def test_member_dashboard_is_the_member_view(client) -> None:
    _login_member(client)
    resp = client.get("/")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    # Member landing keeps the per-user dashboard, not the KPI overview.
    assert "Now Playing" in html
    assert "Service Overview" not in html
    assert "Total Users" not in html


# --------------------------------------------------------------------------- #
# admin_dashboard_stats unit behavior (real counts + graceful degrade)
# --------------------------------------------------------------------------- #


class _FakeDirectory:
    def __init__(self, users):
        self._users = users

    def list_users(self):
        return list(self._users)


class _FakeInvites:
    def __init__(self, invites):
        self._invites = invites

    def list_invites(self):
        return list(self._invites)


class _FakeCore:
    def __init__(self, by_entity):
        self._by_entity = by_entity

    def scan_entity(self, entity_type):
        yield from self._by_entity.get(entity_type, [])


def test_admin_dashboard_stats_counts_live_data() -> None:
    directory = _FakeDirectory(
        [
            {"is_admin": True, "enabled": True},
            {"is_admin": False, "enabled": True},
            {"is_admin": False, "enabled": False},
        ]
    )
    invites = _FakeInvites(
        [
            {"status": "invited"},
            {"status": "invited"},
            {"status": "accepted"},
            {"status": "expired"},
        ]
    )
    core = _FakeCore(
        {
            "GuildOwner": [{"PK": "GUILD#1"}, {"PK": "GUILD#2"}],
            "SourceCredential": [{"PK": "USER#a"}],
        }
    )
    stats = {c["label"]: c["value"] for c in admin_dashboard_stats(directory, invites, core)}
    assert stats["Total Users"] == 3
    assert stats["Administrators"] == 1
    assert stats["Disabled Accounts"] == 1
    assert stats["Pending Invites"] == 2
    assert stats["Guilds"] == 2
    assert stats["Connected Sources"] == 1


def test_admin_dashboard_stats_degrades_to_zero_without_services() -> None:
    stats = {c["label"]: c["value"] for c in admin_dashboard_stats(None, None, None)}
    assert set(stats) == {
        "Total Users",
        "Administrators",
        "Disabled Accounts",
        "Pending Invites",
        "Guilds",
        "Connected Sources",
    }
    assert all(v == 0 for v in stats.values())


def test_admin_dashboard_stats_isolates_a_failing_metric() -> None:
    class _BoomCore:
        def scan_entity(self, entity_type):
            raise RuntimeError("dynamo down")

    directory = _FakeDirectory([{"is_admin": True, "enabled": True}])
    stats = {
        c["label"]: c["value"]
        for c in admin_dashboard_stats(directory, None, _BoomCore())
    }
    # The scan failure degrades those two cards to 0 without breaking the rest.
    assert stats["Total Users"] == 1
    assert stats["Administrators"] == 1
    assert stats["Guilds"] == 0
    assert stats["Connected Sources"] == 0

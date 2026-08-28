"""Route tests for the admin entitlement control-plane blueprint (task 4.3).

Exercises the ``entitlement_routes`` blueprint through the Flask test client
with injected fakes (an in-memory ``EntitlementService`` and an
``AdminDirectory`` stub), no live AWS. Covers:

* the admin-only guard shared by every entitlement route — an admin reaches the
  user picker and per-user view, while a non-admin (or unauthenticated) request
  is redirected/denied and the response body contains **no** admin entitlement
  content (Property 9, R1.2/R1.3);
* the flag toggle route flips the current effective value, persists it, and
  re-renders the flag partial marked saved (R2.3, R4.1/R4.2);
* the quota route rejects a value < 1 with a field-level validation error and
  does not persist (R12.2, R2.4);
* a save failure (persistence error) renders an error notice and does not report
  the change as saved (R2.4).

The fakes mirror the ``_Fake*`` style already used by
``test_invite_admin_routes.py``. Effective entitlements are resolved through the
real, side-effect-free :mod:`entitlements_core` so the merge/defaults behavior
the templates render matches production.

Requirements: 1.2, 1.3, 2.3, 2.4, 12.2
"""

from __future__ import annotations

from typing import Any

import entitlements_core

_SUB = "user-sub-1"

#: Admin-content fingerprints that must NEVER appear in a non-admin response
#: body (Property 9). Drawn from the picker and per-user detail/partials.
_ADMIN_MARKERS = (
    "Govern a user",
    "Playback sources",
    "Capabilities",
    "Per-guild",
    "AI usage",
    "Reset AI tally",
)


class _FakeAdminDirectory:
    """Minimal ``AdminDirectory`` stub returning canned account rows."""

    def __init__(self, users: list[dict[str, Any]] | None = None) -> None:
        self._users = users or [
            {
                "username": "alice",
                "email": "alice@example.com",
                "status": "CONFIRMED",
                "enabled": True,
                "sub": _SUB,
            }
        ]

    def list_users(self) -> list[dict[str, Any]]:
        return [dict(u) for u in self._users]


class _FakeEntitlementService:
    """In-memory ``EntitlementService`` for route tests.

    Stores an explicit record so a flip is observable on the next read, resolves
    effective entitlements through the real ``entitlements_core`` merge, and
    supports injecting a persistence failure to exercise the save-failure path.
    """

    def __init__(self) -> None:
        self._raw: dict[str, Any] | None = None
        self._tally: dict[str, Any] = {}
        self._pricing: dict[str, Any] = {"markup": 1.0, "currency": "USD"}
        self._history: list[dict[str, Any]] = []
        #: When set, ``set_fields`` raises this to simulate a save failure.
        self.raise_on_set: Exception | None = None
        self.set_calls: list[tuple[str, dict[str, Any], str]] = []
        self.reset_calls: list[tuple[str, str]] = []

    def get_raw(self, sub: str) -> dict[str, Any] | None:
        return dict(self._raw) if self._raw is not None else None

    def get_effective(self, sub: str) -> dict[str, Any]:
        return entitlements_core.merge_effective(self._raw)

    def get_tally(self, sub: str) -> dict[str, Any]:
        return dict(self._tally)

    def get_pricing(self) -> dict[str, Any]:
        return dict(self._pricing)

    def history(self, sub: str) -> list[dict[str, Any]]:
        return [dict(h) for h in self._history]

    def set_fields(
        self, sub: str, changes: dict[str, Any], *, admin_sub: str
    ) -> dict[str, Any]:
        # Validate quotas exactly as the real service does (R12.2) so the route
        # surfaces a value < 1 as a field-level error before persisting.
        for field in ("max_bots_per_guild", "max_guilds"):
            if field in changes:
                changes[field] = entitlements_core.validate_quota(
                    int(changes[field])
                )
        if self.raise_on_set is not None:
            raise self.raise_on_set
        self.set_calls.append((sub, dict(changes), admin_sub))
        self._raw = {**(self._raw or {}), **changes}
        return dict(self._raw)

    def reset_tally(self, sub: str, *, admin_sub: str) -> None:
        self.reset_calls.append((sub, admin_sub))
        self._tally = {"accumulated_cost": 0.0, "currency": "USD"}


def _login_admin(client) -> None:
    with client.session_transaction() as sess:
        sess["user"] = {
            "email": "owner@x.io",
            "sub": "admin-sub",
            "is_admin": True,
        }


def _login_user(client) -> None:
    with client.session_transaction() as sess:
        sess["user"] = {"email": "user@x.io", "sub": _SUB, "is_admin": False}


def _wire(app, service: _FakeEntitlementService | None = None) -> None:
    app.extensions["entitlement_service"] = service
    app.extensions["admin_directory"] = _FakeAdminDirectory()


def _assert_no_admin_content(body: str) -> None:
    for marker in _ADMIN_MARKERS:
        assert marker not in body, f"leaked admin content: {marker!r}"


# -- GET /admin/entitlements (user picker) ---------------------------------


def test_index_renders_picker_for_admin(app) -> None:
    _wire(app, _FakeEntitlementService())
    client = app.test_client()
    _login_admin(client)

    resp = client.get("/admin/entitlements")
    body = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert "Govern a user" in body
    assert "alice@example.com" in body


def test_index_requires_login(app) -> None:
    _wire(app, _FakeEntitlementService())
    client = app.test_client()

    resp = client.get("/admin/entitlements")

    assert resp.status_code in (301, 302, 303)
    assert resp.headers["Location"].endswith("/login")
    _assert_no_admin_content(resp.get_data(as_text=True))


def test_index_forbidden_for_non_admin(app) -> None:
    _wire(app, _FakeEntitlementService())
    client = app.test_client()
    _login_user(client)

    resp = client.get("/admin/entitlements")

    # Redirected to the dashboard before any admin content is produced (R1.2).
    assert resp.status_code in (301, 302, 303)
    assert resp.headers["Location"].endswith("/")
    _assert_no_admin_content(resp.get_data(as_text=True))


# -- GET /admin/entitlements/<sub> (per-user view) -------------------------


def test_detail_renders_for_admin(app) -> None:
    _wire(app, _FakeEntitlementService())
    client = app.test_client()
    _login_admin(client)

    resp = client.get(f"/admin/entitlements/{_SUB}")
    body = resp.get_data(as_text=True)

    assert resp.status_code == 200
    # Admin content present: the per-user flags/quotas/AI surface.
    assert "Playback sources" in body
    assert "Capabilities" in body
    # No stored record -> effective values shown as secure defaults (R2.2).
    assert "default" in body


def test_detail_forbidden_for_non_admin(app) -> None:
    _wire(app, _FakeEntitlementService())
    client = app.test_client()
    _login_user(client)

    resp = client.get(f"/admin/entitlements/{_SUB}")

    assert resp.status_code in (301, 302, 303)
    assert resp.headers["Location"].endswith("/")
    _assert_no_admin_content(resp.get_data(as_text=True))


# -- POST /admin/entitlements/<sub>/flags (toggle) -------------------------


def test_flag_toggle_flips_and_persists_and_reports_saved(app) -> None:
    service = _FakeEntitlementService()
    _wire(app, service)
    client = app.test_client()
    _login_admin(client)

    # video_activities defaults to False -> a flip must persist True.
    resp = client.post(
        f"/admin/entitlements/{_SUB}/flags",
        data={"flag": "video_activities"},
    )
    body = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert service.set_calls == [(_SUB, {"video_activities": True}, "admin-sub")]
    assert service.get_effective(_SUB)["video_activities"] is True
    # The re-rendered flag partial reports the save and reflects the new state.
    assert "Saved." in body
    assert "entitlement-flags" in body


def test_source_flag_toggle_flips_within_sources_map(app) -> None:
    service = _FakeEntitlementService()
    _wire(app, service)
    client = app.test_client()
    _login_admin(client)

    # youtube defaults to False -> flipping stores a full sources map with it on.
    resp = client.post(
        f"/admin/entitlements/{_SUB}/flags",
        data={"flag": "youtube"},
    )

    assert resp.status_code == 200
    assert service.get_effective(_SUB)["sources"]["youtube"] is True
    # Other providers are preserved at their effective values.
    assert service.get_effective(_SUB)["sources"]["soundcloud"] is True


def test_flag_toggle_forbidden_for_non_admin(app) -> None:
    service = _FakeEntitlementService()
    _wire(app, service)
    client = app.test_client()
    _login_user(client)

    resp = client.post(
        f"/admin/entitlements/{_SUB}/flags",
        data={"flag": "video_activities"},
    )

    assert resp.status_code in (301, 302, 303)
    assert service.set_calls == []
    _assert_no_admin_content(resp.get_data(as_text=True))


def test_flag_save_failure_shows_error_and_not_saved(app) -> None:
    service = _FakeEntitlementService()
    service.raise_on_set = RuntimeError("dynamo down")
    _wire(app, service)
    client = app.test_client()
    _login_admin(client)

    resp = client.post(
        f"/admin/entitlements/{_SUB}/flags",
        data={"flag": "video_activities"},
    )
    body = resp.get_data(as_text=True)

    assert resp.status_code == 200
    # An error notice is surfaced and the change is NOT reported saved (R2.4).
    assert "dynamo down" in body
    assert "Saved." not in body
    # Nothing was persisted.
    assert service.get_effective(_SUB)["video_activities"] is False


# -- POST /admin/entitlements/<sub>/quotas ---------------------------------


def test_quota_valid_value_persists_and_reports_saved(app) -> None:
    service = _FakeEntitlementService()
    _wire(app, service)
    client = app.test_client()
    _login_admin(client)

    resp = client.post(
        f"/admin/entitlements/{_SUB}/quotas",
        data={"max_bots_per_guild": "3", "max_guilds": "2"},
    )
    body = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert "Quotas saved." in body
    assert service.get_effective(_SUB)["max_bots_per_guild"] == 3
    assert service.get_effective(_SUB)["max_guilds"] == 2


def test_quota_below_one_shows_validation_error_and_not_saved(app) -> None:
    service = _FakeEntitlementService()
    _wire(app, service)
    client = app.test_client()
    _login_admin(client)

    resp = client.post(
        f"/admin/entitlements/{_SUB}/quotas",
        data={"max_bots_per_guild": "0"},
    )
    body = resp.get_data(as_text=True)

    assert resp.status_code == 200
    # A value < 1 is rejected with a validation error and not persisted (R12.2).
    assert "Quotas saved." not in body
    assert "notice--danger" in body
    assert service.set_calls == []
    assert service.get_effective(_SUB)["max_bots_per_guild"] == 1


def test_quota_non_integer_shows_error_and_not_saved(app) -> None:
    service = _FakeEntitlementService()
    _wire(app, service)
    client = app.test_client()
    _login_admin(client)

    resp = client.post(
        f"/admin/entitlements/{_SUB}/quotas",
        data={"max_guilds": "many"},
    )
    body = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert "Quotas saved." not in body
    assert "notice--danger" in body
    assert service.set_calls == []


# -- POST /admin/entitlements/<sub>/ai/reset -------------------------------


def test_ai_reset_zeroes_tally_and_reports_saved(app) -> None:
    service = _FakeEntitlementService()
    service._tally = {"accumulated_cost": 4.2, "currency": "USD"}
    _wire(app, service)
    client = app.test_client()
    _login_admin(client)

    resp = client.post(f"/admin/entitlements/{_SUB}/ai/reset")
    body = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert service.reset_calls == [(_SUB, "admin-sub")]
    assert "AI settings saved." in body


def test_ai_reset_forbidden_for_non_admin(app) -> None:
    service = _FakeEntitlementService()
    _wire(app, service)
    client = app.test_client()
    _login_user(client)

    resp = client.post(f"/admin/entitlements/{_SUB}/ai/reset")

    assert resp.status_code in (301, 302, 303)
    assert service.reset_calls == []
    _assert_no_admin_content(resp.get_data(as_text=True))


# -- degraded mode (no datastore) ------------------------------------------


def test_flag_toggle_degraded_mode_reports_unavailable(app) -> None:
    # No entitlement_service configured -> writes are unavailable, not saved.
    _wire(app, None)
    client = app.test_client()
    _login_admin(client)

    resp = client.post(
        f"/admin/entitlements/{_SUB}/flags",
        data={"flag": "video_activities"},
    )
    body = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert "Saved." not in body
    assert "unavailable" in body.lower()

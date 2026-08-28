"""Route tests: the admin registration-mode change surface (task 8).

The admin-change half of the registration-mode route suite (the login banner,
register gate, and invite independence live in
``test_registration_mode_routes``). Uses the shared in-process fakes from
``_registration_mode_fakes`` — no AWS, no Cognito, no network. Covers:

* Property 5 — admin mode change round-trips (8.3)
* Property 6 — only admins can change the mode (8.4)
* Property 7 — every actual change is audited (8.5)
* Property 8 — unchanged submission is idempotent at the route level (8.6)

Feature: registration-mode-control
"""

from __future__ import annotations

from _registration_mode_fakes import (
    ADMIN_SUB,
    _FakeCoreTable,
    login_admin,
    login_discord_non_admin,
    make_app,
)
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

import registration_mode
from registration_mode import (
    AUDIT_ENTITY_TYPE,
    AUDIT_SK_PREFIX,
    CLOSED,
    OPEN,
    VALID_MODES,
)


def _stored_mode(app) -> str:
    """Read the effective mode back through the real ConfigStore."""
    store = app.extensions["config_store"]
    return registration_mode.current_mode(store.get_global())


# --------------------------------------------------------------------------- #
# 8.3 — Property 5: admin mode change round-trips                              #
# --------------------------------------------------------------------------- #


@settings(
    max_examples=100,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(initial=st.sampled_from(VALID_MODES), target=st.sampled_from(VALID_MODES))
def test_property5_admin_change_round_trips(initial: str, target: str) -> None:
    """Feature: registration-mode-control, Property 5: Admin mode change
    round-trips.

    For any initial/target mode pair, after an admin POSTs ``target`` to
    ``/admin/registration-mode`` the mode read back through the store equals the
    submitted target (both directions, including no-op).

    Validates: Requirements 2.3, 4.2
    """
    application = make_app(initial_mode=initial)
    client = application.test_client()
    login_admin(client)

    resp = client.post("/admin/registration-mode", data={"mode": target})

    assert resp.status_code == 302
    assert _stored_mode(application) == target


# --------------------------------------------------------------------------- #
# 8.4 — Property 6: only admins can change the mode                            #
# --------------------------------------------------------------------------- #


@settings(
    max_examples=100,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    session_kind=st.sampled_from(["anonymous", "discord"]),
    initial=st.sampled_from(VALID_MODES),
    target=st.sampled_from(VALID_MODES),
)
def test_property6_only_admins_can_change_mode(
    session_kind: str, initial: str, target: str
) -> None:
    """Feature: registration-mode-control, Property 6: Only admins can change
    the mode.

    For an anonymous or Discord (non-admin) session and any submitted target,
    the change route denies the request (redirect) and the stored mode is
    identical before and after.

    Validates: Requirements 4.3, 4.4
    """
    application = make_app(initial_mode=initial)
    client = application.test_client()
    if session_kind == "discord":
        login_discord_non_admin(client)

    before = _stored_mode(application)
    resp = client.post("/admin/registration-mode", data={"mode": target})
    after = _stored_mode(application)

    # Denied (redirected to login or dashboard) or forbidden — never a mutation.
    assert resp.status_code in (301, 302, 303, 403)
    assert before == initial
    assert after == initial


@settings(
    max_examples=100,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(session_kind=st.sampled_from(["anonymous", "discord"]))
def test_property6_admin_panel_omits_control_for_non_admin(
    session_kind: str,
) -> None:
    """Feature: registration-mode-control, Property 6: Only admins can change
    the mode.

    GET ``/admin`` for a non-admin redirects before any content renders, so the
    registration-mode control is never emitted to a non-admin.

    Validates: Requirements 4.3, 4.4
    """
    application = make_app(initial_mode=OPEN)
    client = application.test_client()
    if session_kind == "discord":
        login_discord_non_admin(client)

    resp = client.get("/admin")
    body = resp.get_data(as_text=True)

    assert resp.status_code in (301, 302, 303)
    # The mode control action never appears in a non-admin response body.
    assert "/admin/registration-mode" not in body
    assert "Self-registration" not in body


def test_property6_admin_panel_renders_control_for_admin() -> None:
    """Sanity: the admin panel DOES render the control (and current mode) for an
    admin, so the Property-6 omission is meaningful."""
    application = make_app(initial_mode=OPEN)
    client = application.test_client()
    login_admin(client)

    resp = client.get("/admin")
    body = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert "/admin/registration-mode" in body
    assert "Self-registration" in body
    assert OPEN in body


# --------------------------------------------------------------------------- #
# 8.5 — Property 7: every actual change is audited                             #
# --------------------------------------------------------------------------- #


@settings(
    max_examples=100,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(initial=st.sampled_from(VALID_MODES))
def test_property7_every_change_is_audited(initial: str) -> None:
    """Feature: registration-mode-control, Property 7: Every actual change is
    audited.

    Changing the mode to the other value as an admin writes exactly one
    ``REGMODEAUDIT#`` item on ``CONFIG#GLOBAL`` carrying the acting admin,
    correct old/new, and an ``at`` timestamp.

    Validates: Requirements 5.1
    """
    target = OPEN if initial == CLOSED else CLOSED
    core = _FakeCoreTable()
    application = make_app(initial_mode=initial, core=core)
    client = application.test_client()
    login_admin(client)

    resp = client.post("/admin/registration-mode", data={"mode": target})

    assert resp.status_code == 302
    audits = core.audit_rows()
    assert len(audits) == 1
    record = audits[0]
    assert record["entityType"] == AUDIT_ENTITY_TYPE
    assert record["SK"].startswith(AUDIT_SK_PREFIX)
    data = record["data"]
    assert data["admin_sub"] == ADMIN_SUB
    assert data["old"] == initial
    assert data["new"] == target
    assert isinstance(data["at"], str) and data["at"]


# --------------------------------------------------------------------------- #
# 8.6 — Property 8: unchanged submission is idempotent (route level)           #
# --------------------------------------------------------------------------- #


@settings(
    max_examples=100,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(current=st.sampled_from(VALID_MODES))
def test_property8_route_noop_writes_no_audit(current: str) -> None:
    """Feature: registration-mode-control, Property 8: Unchanged submission is
    idempotent.

    POSTing the current mode as an admin leaves the stored mode unchanged and
    writes no new ``REGMODEAUDIT#`` item.

    Validates: Requirements 5.2
    """
    core = _FakeCoreTable()
    application = make_app(initial_mode=current, core=core)
    client = application.test_client()
    login_admin(client)

    resp = client.post("/admin/registration-mode", data={"mode": current})

    assert resp.status_code == 302
    assert _stored_mode(application) == current
    assert core.audit_rows() == []

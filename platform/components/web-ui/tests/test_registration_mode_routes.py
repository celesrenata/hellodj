"""Route tests: login banner, register gate, and invite independence (task 8).

The display/enforcement half of the registration-mode route suite (the admin
change route lives in ``test_registration_mode_admin_routes``). Uses the shared
in-process fakes from ``_registration_mode_fakes`` — no AWS, no Cognito, no
network. Covers:

* Property 3 — login page reflects the current mode (8.1)
* Property 4 — CLOSED rejects registration on GET and POST (8.2)
* Property 9 — invites are independent of the mode (8.7)
* OPEN happy path — GET renders the form, POST reaches sign_up (8.8)

Feature: registration-mode-control
"""

from __future__ import annotations

from _registration_mode_fakes import (
    REGISTER_FORM_MARKER,
    _FakeInviteService,
    _SpyCognitoAuth,
    make_app,
)
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from registration_mode import (
    BANNER_CLOSED,
    BANNER_OPEN,
    CLOSED,
    OPEN,
    VALID_MODES,
)

# --------------------------------------------------------------------------- #
# 8.1 — Property 3: login page reflects the current mode                       #
# --------------------------------------------------------------------------- #


def test_property3_login_open_shows_open_banner_and_register_link() -> None:
    """Feature: registration-mode-control, Property 3: Login page reflects the
    current mode.

    OPEN → the exact BANNER_OPEN string is shown and the /register link is
    present.

    Validates: Requirements 3.1, 3.3
    """
    application = make_app(initial_mode=OPEN)
    resp = application.test_client().get("/login")
    body = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert BANNER_OPEN in body
    assert BANNER_CLOSED not in body
    assert "/auth/register" in body


def test_property3_login_closed_shows_closed_banner_no_register_link() -> None:
    """Feature: registration-mode-control, Property 3: Login page reflects the
    current mode.

    CLOSED → the exact BANNER_CLOSED string is shown and the /register link is
    omitted.

    Validates: Requirements 3.2, 3.4
    """
    application = make_app(initial_mode=CLOSED)
    resp = application.test_client().get("/login")
    body = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert BANNER_CLOSED in body
    assert BANNER_OPEN not in body
    assert "/auth/register" not in body


def test_property3_login_unset_defaults_closed() -> None:
    """Feature: registration-mode-control, Property 3: Login page reflects the
    current mode.

    An unset mode resolves to the secure default CLOSED: closed banner, no link.

    Validates: Requirements 3.2, 3.4
    """
    application = make_app(initial_mode=None)
    resp = application.test_client().get("/login")
    body = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert BANNER_CLOSED in body
    assert "/auth/register" not in body


# --------------------------------------------------------------------------- #
# 8.2 — Property 4: CLOSED rejects registration on GET and POST                #
# --------------------------------------------------------------------------- #


@settings(
    max_examples=100,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    method=st.sampled_from(["GET", "POST"]),
    email=st.text(min_size=0, max_size=30),
    password=st.text(min_size=0, max_size=30),
)
def test_property4_closed_rejects_register_get_and_post(
    method: str, email: str, password: str
) -> None:
    """Feature: registration-mode-control, Property 4: CLOSED rejects
    registration on GET and POST.

    For either method against ``/auth/register`` while CLOSED: a 302 redirect to
    the login page carrying ``registration=closed``, the self-registration form
    is never rendered, and the Cognito ``sign_up`` collaborator is never called.

    Validates: Requirements 2.1, 2.2
    """
    auth = _SpyCognitoAuth()
    application = make_app(initial_mode=CLOSED, auth=auth)
    client = application.test_client()

    if method == "GET":
        resp = client.get("/auth/register")
    else:
        resp = client.post(
            "/auth/register",
            data={"step": "start", "email": email, "password": password},
        )
    body = resp.get_data(as_text=True)

    assert resp.status_code == 302
    location = resp.headers["Location"]
    assert "/login" in location
    assert "registration=closed" in location
    # The self-registration form was never rendered on this response.
    assert REGISTER_FORM_MARKER not in body
    # Cognito SignUp was never invoked (POST path short-circuited).
    assert auth.sign_up_calls == []


def test_property4_closed_redirect_login_shows_closed_notice() -> None:
    """Following the CLOSED register redirect lands on a login page whose
    closed-notice is shown (R2.1/R2.2 user-visible outcome)."""
    application = make_app(initial_mode=CLOSED, auth=_SpyCognitoAuth())
    resp = application.test_client().get("/auth/register", follow_redirects=True)
    body = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert "Registration is currently closed" in body
    assert REGISTER_FORM_MARKER not in body


# --------------------------------------------------------------------------- #
# 8.7 — Property 9: invites are independent of the mode                        #
# --------------------------------------------------------------------------- #


@settings(
    max_examples=100,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    mode=st.sampled_from(VALID_MODES),
    # Realistic invite tokens are URL-safe path segments; constrain the
    # generator to those so the property tests the mode gate, not Flask's URL
    # routing of reserved characters like "/" or "#".
    token=st.text(
        alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_",
        min_size=1,
        max_size=20,
    ),
)
def test_property9_invites_independent_of_mode(mode: str, token: str) -> None:
    """Feature: registration-mode-control, Property 9: Invites are independent
    of the mode.

    For either mode, GET ``/invite/<token>`` reaches invite handling (the fake
    invite service resolves the token) and is never redirected by the
    registration-mode gate.

    Validates: Requirements 2.5
    """
    invites = _FakeInviteService()
    application = make_app(initial_mode=mode, invites=invites)
    resp = application.test_client().get(f"/invite/{token}")

    # Rendered the invite page (200) — not a mode-gate redirect to /login.
    assert resp.status_code == 200
    # The request reached invite handling for exactly this token.
    assert token in invites.resolved_tokens
    # It was not bounced with a registration-closed notice.
    assert "registration=closed" not in resp.headers.get("Location", "")


# --------------------------------------------------------------------------- #
# 8.8 — OPEN happy path (example)                                             #
# --------------------------------------------------------------------------- #


def test_open_get_renders_registration_form() -> None:
    """OPEN + GET ``/auth/register`` renders the self-registration form.

    Validates: Requirements 2.3
    """
    application = make_app(initial_mode=OPEN, auth=_SpyCognitoAuth())
    resp = application.test_client().get("/auth/register")
    body = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert REGISTER_FORM_MARKER in body


def test_open_post_reaches_sign_up() -> None:
    """OPEN + POST ``/auth/register`` reaches ``handle_register`` → Cognito
    ``sign_up`` (the mode gate does not short-circuit the flow).

    Validates: Requirements 2.4
    """
    auth = _SpyCognitoAuth()
    application = make_app(initial_mode=OPEN, auth=auth)
    resp = application.test_client().post(
        "/auth/register",
        data={"step": "start", "email": "new@x.com", "password": "pw"},
    )

    assert resp.status_code == 200
    assert auth.sign_up_calls == [("new@x.com", "pw")]

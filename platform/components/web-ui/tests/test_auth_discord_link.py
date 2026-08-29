"""Auth tests for the Discord link + login flow (task 10, R3.1-R3.4).

These exercise the post-registration Discord-link handoff and the returning
Discord login against a fake ``UserProfileService`` and monkeypatched OAuth
code-exchange helpers — no real Discord or AWS calls. They assert:

* the link callback links the Discord id (setting the GSI1 reverse index via
  ``link_discord``) and establishes an authenticated session afterwards so
  Discord OAuth is the login method thereafter (R3.2, R3.3);
* a Discord id already linked to a different account is rejected with a clear
  ``already_linked`` error and never a 500 (R3.4);
* a returning linked user who signs in via Discord is resolved through
  ``user_for_discord`` and gets a session (R3.2);
* the post-registration handoff (``pending_link_sub``, no authenticated
  session) can begin and complete linking without a prior full session (R2.4).
"""

from __future__ import annotations

import auth as auth_module


class FakeProfiles:
    """In-memory ``UserProfileService`` stand-in with the GSI1 reverse index.

    Enforces the one-account-per-Discord-identity rule (R3.4): ``link_discord``
    raises ``ValueError`` when the id is already linked to a different subject.
    """

    def __init__(self) -> None:
        # sub -> profile payload
        self._profiles: dict[str, dict[str, object]] = {}
        # discord_id -> sub  (the GSI1 reverse index)
        self._by_discord: dict[str, str] = {}

    def ensure(self, sub: str, *, email: str) -> dict[str, object]:
        self._profiles.setdefault(sub, {"email": email, "discord_linked": False})
        return dict(self._profiles[sub])

    def get(self, sub: str) -> dict[str, object]:
        return dict(self._profiles.get(sub, {}))

    def user_for_discord(self, discord_id: str) -> str | None:
        return self._by_discord.get(discord_id)

    def link_discord(self, sub: str, discord_id: str) -> None:
        existing = self._by_discord.get(discord_id)
        if existing is not None and existing != sub:
            raise ValueError(
                "that Discord account is already linked to another user"
            )
        self._profiles.setdefault(sub, {"discord_linked": False})
        self._profiles[sub].update(
            {"discord_id": discord_id, "discord_linked": True}
        )
        self._by_discord[discord_id] = sub


def _configure(app, discord_id: str | None) -> FakeProfiles:
    """Wire a fake profiles service + a fixed Discord code-exchange result."""
    profiles = FakeProfiles()
    app.extensions["user_profiles"] = profiles
    app.config["DISCORD_CLIENT_ID"] = "cid"
    app.config["DISCORD_CLIENT_SECRET"] = "secret"
    # Bypass the real Discord token exchange / /users/@me network call.
    auth_module.discord_id_from_code = lambda code, redirect_uri: discord_id
    return profiles


def _seed_link_state(client, *, state: str, session_user=None, pending=None):
    """Seed the link-callback CSRF state and the caller's session context."""
    with client.session_transaction() as sess:
        sess["discord_link_state"] = state
        if session_user is not None:
            sess["user"] = session_user
        if pending is not None:
            sess["pending_link_sub"] = pending


# --------------------------------------------------------------------------- #
# Link callback: links Discord id + establishes the session (R3.2, R3.3)
# --------------------------------------------------------------------------- #


def test_link_callback_links_discord_and_sets_session(app) -> None:
    profiles = _configure(app, discord_id="disc-1")
    profiles.ensure("sub-1", email="u@example.com")
    client = app.test_client()
    _seed_link_state(
        client, state="s1", session_user={"provider": "discord_oauth", "sub": "sub-1"}
    )

    resp = client.get("/auth/discord/link/callback?state=s1&code=abc")

    # The Discord id is linked (GSI1 reverse index established).
    assert profiles.user_for_discord("disc-1") == "sub-1"
    assert profiles.get("sub-1")["discord_linked"] is True
    # Redirects to the account page (no error).
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/account")
    # An authenticated Discord session is established afterwards (R3.2).
    with client.session_transaction() as sess:
        user = sess["user"]
        assert user["provider"] == "discord_oauth"
        assert user["sub"] == "sub-1"
        assert user["discord_id"] == "disc-1"
        assert user["discord_linked"] is True


def test_post_registration_handoff_links_without_prior_session(app) -> None:
    # R2.4/R3.1: the invitee just registered — no authenticated session, only
    # the pending handoff subject. Linking must still start and complete.
    profiles = _configure(app, discord_id="disc-new")
    profiles.ensure("sub-new", email="new@example.com")
    client = app.test_client()

    # /auth/discord/link begins the flow from the pending handoff alone.
    with client.session_transaction() as sess:
        sess["pending_link_sub"] = "sub-new"
    start = client.get("/auth/discord/link")
    assert start.status_code == 302
    assert "discord.com/oauth2/authorize" in start.headers["Location"]

    # Carry the state the start step stored into the callback.
    with client.session_transaction() as sess:
        state = sess["discord_link_state"]
    resp = client.get(f"/auth/discord/link/callback?state={state}&code=abc")

    assert profiles.user_for_discord("disc-new") == "sub-new"
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/account")
    with client.session_transaction() as sess:
        assert sess["user"]["sub"] == "sub-new"
        assert sess.get("pending_link_sub") is None


def test_discord_link_without_any_context_redirects_to_login(app) -> None:
    _configure(app, discord_id="disc-x")
    client = app.test_client()

    resp = client.get("/auth/discord/link")

    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/login")


# --------------------------------------------------------------------------- #
# One-account-per-Discord-identity (R3.4): clear error, never a 500
# --------------------------------------------------------------------------- #


def test_discord_id_linked_to_other_account_is_rejected(app) -> None:
    profiles = _configure(app, discord_id="disc-shared")
    # disc-shared already belongs to another account.
    profiles.ensure("owner-sub", email="owner@example.com")
    profiles.link_discord("owner-sub", "disc-shared")
    profiles.ensure("sub-2", email="second@example.com")
    client = app.test_client()
    _seed_link_state(
        client, state="s2", session_user={"provider": "discord_oauth", "sub": "sub-2"}
    )

    resp = client.get("/auth/discord/link/callback?state=s2&code=abc")

    # Clear error, not a 500 (R3.4).
    assert resp.status_code == 302
    assert "error=already_linked" in resp.headers["Location"]
    assert resp.headers["Location"].split("?")[0].endswith("/account")
    # The mapping is unchanged: still the original owner (the losing account
    # was not linked, and its profile carries no Discord link).
    assert profiles.user_for_discord("disc-shared") == "owner-sub"
    assert profiles.get("sub-2").get("discord_linked") is not True
    assert "discord_id" not in profiles.get("sub-2")


def test_relinking_same_account_is_idempotent(app) -> None:
    profiles = _configure(app, discord_id="disc-1")
    profiles.ensure("sub-1", email="u@example.com")
    profiles.link_discord("sub-1", "disc-1")
    client = app.test_client()
    _seed_link_state(
        client, state="s1", session_user={"provider": "discord_oauth", "sub": "sub-1"}
    )

    resp = client.get("/auth/discord/link/callback?state=s1&code=abc")

    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/account")
    assert profiles.user_for_discord("disc-1") == "sub-1"


# --------------------------------------------------------------------------- #
# Returning Discord login resolves via user_for_discord (R3.2)
# --------------------------------------------------------------------------- #


def test_returning_linked_user_logs_in_via_discord(app) -> None:
    profiles = _configure(app, discord_id="disc-1")
    profiles.ensure("sub-1", email="u@example.com")
    profiles.link_discord("sub-1", "disc-1")
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["discord_state"] = "login-state"

    resp = client.get("/auth/discord/callback?state=login-state&code=abc")

    assert resp.status_code == 302
    # Landed on the dashboard (logged in), not bounced to login.
    assert not resp.headers["Location"].endswith("/login")
    with client.session_transaction() as sess:
        user = sess["user"]
        assert user["provider"] == "discord_oauth"
        assert user["sub"] == "sub-1"
        assert user["discord_id"] == "disc-1"
        assert user["email"] == "u@example.com"


def test_unlinked_discord_login_bounces_to_login(app) -> None:
    # A Discord identity not linked to any account cannot log in (R3.2/R3.4).
    _configure(app, discord_id="disc-unknown")
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["discord_state"] = "login-state"

    resp = client.get("/auth/discord/callback?state=login-state&code=abc")

    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]
    assert "error=not_linked" in resp.headers["Location"]
    with client.session_transaction() as sess:
        assert sess.get("user") is None


# --------------------------------------------------------------------------- #
# Account co-admin (Option B): an appointed Discord id logs INTO the owner's
# account — its Discord identity is not linked to any account of its own.
# --------------------------------------------------------------------------- #


class _FakeAccountAdmin:
    """In-memory ``AccountAdminService`` stand-in: discord_id -> owner_sub."""

    def __init__(self, mapping: dict[str, str]) -> None:
        self._by_discord = mapping

    def owner_for_discord(self, discord_id: str) -> str | None:
        return self._by_discord.get(discord_id)


def test_appointed_account_admin_logs_into_owner_account(app) -> None:
    # The co-admin's Discord id is NOT linked to its own account, but it IS
    # appointed on owner "sub-owner" — Option B logs it straight into that
    # account (session identity becomes the owner) and lands on the dashboard.
    profiles = _configure(app, discord_id="disc-admin")
    profiles.ensure("sub-owner", email="owner@example.com")
    profiles.link_discord("sub-owner", "disc-owner")
    app.extensions["account_admin"] = _FakeAccountAdmin(
        {"disc-admin": "sub-owner"}
    )
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["discord_state"] = "login-state"

    resp = client.get("/auth/discord/callback?state=login-state&code=abc")

    assert resp.status_code == 302
    assert not resp.headers["Location"].endswith("/login")
    with client.session_transaction() as sess:
        user = sess["user"]
        # Session identity is the OWNER, not the co-admin.
        assert user["sub"] == "sub-owner"
        assert user["email"] == "owner@example.com"
        # Owner-scoped authorization facts use the OWNER's linked Discord id.
        assert user["discord_id"] == "disc-owner"
        # The acting co-admin is recorded for auditability.
        assert user["acting_as_account_admin"] is True
        assert user["admin_actor_discord_id"] == "disc-admin"


def test_unappointed_unlinked_discord_still_bounces(app) -> None:
    # A Discord id that is neither linked nor appointed cannot log in even with
    # an account-admin service wired.
    _configure(app, discord_id="disc-nobody")
    app.extensions["account_admin"] = _FakeAccountAdmin({})
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["discord_state"] = "login-state"

    resp = client.get("/auth/discord/callback?state=login-state&code=abc")

    assert resp.status_code == 302
    assert "error=not_linked" in resp.headers["Location"]
    with client.session_transaction() as sess:
        assert sess.get("user") is None


def test_discord_login_state_mismatch_bounces_to_login(app) -> None:
    _configure(app, discord_id="disc-1")
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["discord_state"] = "expected"

    resp = client.get("/auth/discord/callback?state=wrong&code=abc")

    assert resp.status_code == 302
    assert "error=state_mismatch" in resp.headers["Location"]

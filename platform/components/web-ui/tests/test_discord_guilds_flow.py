"""Tests for the "add a server" flow (Discord guilds scope → claim).

Covers `discord_guilds_routes` (registered on the auth blueprint) end to end
against a fake `GuildAdminService` and a monkeypatched Discord guilds fetch —
no real Discord or AWS calls:

* the connect route redirects to Discord with the `identify guilds` scope and a
  CSRF state stashed in the session;
* the callback validates state, fetches the manageable guilds, stashes the
  candidate set in the session, and renders the picker;
* the claim route ONLY claims a guild in the session candidate set (a forged
  guild id is refused), and on success claims ownership and redirects to the
  guild-detail page;
* first-come-first-served: claiming a guild already owned by someone else
  bounces back to the guild list with an ``already_claimed`` notice.
"""

from __future__ import annotations

import auth_oauth
import discord_guilds_routes as guilds_routes


class _FakeGuildAdmin:
    """In-memory GuildAdminService stand-in: guild_id -> owner_sub."""

    def __init__(self) -> None:
        self._owner: dict[str, str] = {}
        self._name: dict[str, str] = {}

    def claim_ownership(self, guild_id: str, user_sub: str, *, name: str = "") -> None:
        if guild_id in self._owner:
            return
        self._owner[guild_id] = user_sub
        self._name[guild_id] = name

    def owner_of(self, guild_id: str) -> str | None:
        return self._owner.get(guild_id)

    def guild_name(self, guild_id: str) -> str:
        return self._name.get(guild_id, "")

    def guilds_owned_by(self, user_sub: str):
        return [
            {"guild_id": gid, "name": self._name.get(gid, "")}
            for gid, sub in self._owner.items()
            if sub == user_sub
        ]

    def guilds_administered_by_discord(self, discord_id: str):
        return []


def _login(client, *, sub: str = "sub-1", discord_id: str = "disc-1") -> None:
    with client.session_transaction() as sess:
        sess["user"] = {
            "provider": "discord_oauth",
            "sub": sub,
            "discord_id": discord_id,
        }


def _configure(app, monkeypatch, *, candidates=None) -> _FakeGuildAdmin:
    ga = _FakeGuildAdmin()
    app.extensions["guild_admin"] = ga
    app.config["DISCORD_CLIENT_ID"] = "cid"
    app.config["DISCORD_CLIENT_SECRET"] = "secret"
    # Use monkeypatch so these auto-revert and never pollute other tests.
    monkeypatch.setattr(guilds_routes, "_discord_client_id", lambda: "cid")
    if candidates is not None:
        monkeypatch.setattr(
            auth_oauth,
            "discord_manageable_guilds_from_code",
            lambda code, redirect_uri: candidates,
        )
    return ga


# --------------------------------------------------------------------------- #
# connect
# --------------------------------------------------------------------------- #


def test_connect_redirects_to_discord_with_guilds_scope(app, monkeypatch) -> None:
    _configure(app, monkeypatch)
    client = app.test_client()
    _login(client)

    resp = client.get("/auth/discord/guilds/connect")

    assert resp.status_code == 302
    loc = resp.headers["Location"]
    assert loc.startswith("https://discord.com/oauth2/authorize")
    assert "scope=identify+guilds" in loc or "scope=identify%20guilds" in loc
    with client.session_transaction() as sess:
        assert sess.get("add_guild_state")


def test_connect_requires_login(app, monkeypatch) -> None:
    _configure(app, monkeypatch)
    client = app.test_client()

    resp = client.get("/auth/discord/guilds/connect")

    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]


# --------------------------------------------------------------------------- #
# callback
# --------------------------------------------------------------------------- #


def test_callback_stashes_candidates_and_renders_picker(app, monkeypatch) -> None:
    _configure(
        app,
        monkeypatch,
        candidates=[
            {"id": "111", "name": "My Server", "owner": "1"},
            {"id": "222", "name": "Other", "owner": ""},
        ],
    )
    client = app.test_client()
    _login(client)
    with client.session_transaction() as sess:
        sess["add_guild_state"] = "st"

    resp = client.get("/auth/discord/guilds/callback?state=st&code=abc")

    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "My Server" in body
    assert "Other" in body
    with client.session_transaction() as sess:
        assert sess["add_guild_candidates"] == {"111": "My Server", "222": "Other"}


def test_callback_state_mismatch_bounces(app, monkeypatch) -> None:
    _configure(app, monkeypatch, candidates=[])
    client = app.test_client()
    _login(client)
    with client.session_transaction() as sess:
        sess["add_guild_state"] = "expected"

    resp = client.get("/auth/discord/guilds/callback?state=wrong&code=abc")

    assert resp.status_code == 302
    assert "add=state_mismatch" in resp.headers["Location"]


def test_callback_no_manageable_guilds_bounces_with_none(app, monkeypatch) -> None:
    _configure(app, monkeypatch, candidates=[])
    client = app.test_client()
    _login(client)
    with client.session_transaction() as sess:
        sess["add_guild_state"] = "st"

    resp = client.get("/auth/discord/guilds/callback?state=st&code=abc")

    assert resp.status_code == 302
    assert "add=none" in resp.headers["Location"]


# --------------------------------------------------------------------------- #
# claim
# --------------------------------------------------------------------------- #


def test_claim_authorized_guild_claims_and_redirects_to_detail(app, monkeypatch) -> None:
    ga = _configure(app, monkeypatch)
    client = app.test_client()
    _login(client, sub="sub-1")
    with client.session_transaction() as sess:
        sess["add_guild_candidates"] = {"111": "My Server"}

    resp = client.post("/auth/discord/guilds/claim", data={"guild_id": "111"})

    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/guilds/111")
    assert ga.owner_of("111") == "sub-1"
    assert ga.guild_name("111") == "My Server"


def test_claim_forged_guild_id_is_refused(app, monkeypatch) -> None:
    ga = _configure(app, monkeypatch)
    client = app.test_client()
    _login(client)
    with client.session_transaction() as sess:
        sess["add_guild_candidates"] = {"111": "My Server"}

    # A guild id NOT in the candidate set (the user never proved they manage it).
    resp = client.post("/auth/discord/guilds/claim", data={"guild_id": "999"})

    assert resp.status_code == 302
    assert "add=not_authorized" in resp.headers["Location"]
    assert ga.owner_of("999") is None


def test_claim_already_owned_by_other_bounces(app, monkeypatch) -> None:
    ga = _configure(app, monkeypatch)
    ga.claim_ownership("111", "someone-else", name="Theirs")
    client = app.test_client()
    _login(client, sub="sub-1")
    with client.session_transaction() as sess:
        sess["add_guild_candidates"] = {"111": "My Server"}

    resp = client.post("/auth/discord/guilds/claim", data={"guild_id": "111"})

    assert resp.status_code == 302
    assert "add=already_claimed" in resp.headers["Location"]
    # Ownership is unchanged (first-come-first-served).
    assert ga.owner_of("111") == "someone-else"

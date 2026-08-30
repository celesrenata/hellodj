"""The guild-detail page header shows the guild's Discord NAME, not its id.

Reported bug: ``/guilds/<gid>`` rendered the raw snowflake ("Guild
1501686893765595296") instead of the stored Discord name ("Guild Under the
Influence"). The name is recorded at claim time on the ``OWNER`` item
(``GuildAdminService.claim_ownership(..., name=...)``) and read back via
``guild_name``; the route now threads it to the template heading, which falls
back to "Guild <id>" only when the name is unknown.
"""

from __future__ import annotations

from typing import Any

from app import create_app

STAGE = "beta"


class _FakeGuildAdmin:
    """Minimal ``guild_admin`` extension returning a stored name + no admins."""

    def __init__(self, names: dict[str, str]) -> None:
        self._names = names

    def guild_name(self, guild_id: str) -> str:
        return self._names.get(guild_id, "")

    def list_admins(self, guild_id: str) -> list[dict[str, Any]]:
        return []

    def owner_of(self, guild_id: str) -> str | None:
        return None

    def admin_discord_ids(self, guild_id: str) -> set[str]:
        return set()


def _app(names: dict[str, str]) -> Any:
    app = create_app(
        overrides={
            "TESTING": True,
            "SECRET_KEY": "test-secret",
            "HELLODJ_STAGE": STAGE,
            "PUBLIC_BASE_URL": "https://beta.example.test",
        }
    )
    # A super-admin session passes can_manage_guild for any guild, so the
    # ownership gate lets us render the page; the fake guild_admin supplies the
    # stored name the header should use.
    app.extensions["guild_admin"] = _FakeGuildAdmin(names)
    return app


def _login_admin(client: Any) -> None:
    with client.session_transaction() as sess:
        sess["user"] = {"is_admin": True, "sub": "admin-sub"}


def test_header_shows_guild_name_when_known() -> None:
    gid = "1501686893765595296"
    app = _app({gid: "Guild Under the Influence"})
    client = app.test_client()
    _login_admin(client)

    resp = client.get(f"/guilds/{gid}")

    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "Guild Under the Influence" in html
    # The raw "Guild <id>" fallback must NOT be the heading when a name exists.
    assert f"Guild {gid}" not in html


def test_header_falls_back_to_id_when_name_unknown() -> None:
    gid = "222333444555"
    app = _app({})  # no stored name
    client = app.test_client()
    _login_admin(client)

    resp = client.get(f"/guilds/{gid}")

    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert f"Guild {gid}" in html

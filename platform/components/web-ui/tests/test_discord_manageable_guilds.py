"""Unit test for the manageable-guilds permission filter in auth_oauth.

`discord_manageable_guilds_from_code` must keep ONLY the guilds a user may add a
bot to: those they OWN or where they hold the MANAGE_GUILD permission bit. It
exchanges the code for an access token then reads /users/@me/guilds; here both
network steps are faked so the pure filtering is asserted without Discord.
"""

from __future__ import annotations

import io
import json
from typing import Any

import auth_oauth


class _FakeResp:
    """Context-manager stand-in for urlopen returning a fixed JSON body."""

    def __init__(self, payload: Any) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> io.BytesIO:
        return io.BytesIO(self._body)

    def __exit__(self, *exc: object) -> None:
        return None


def test_filters_to_owned_or_manage_guild(monkeypatch) -> None:
    # A valid access token (token exchange faked).
    monkeypatch.setattr(
        auth_oauth, "_discord_access_token", lambda code, redirect_uri: "tok"
    )
    guilds = [
        {"id": "1", "name": "Owned", "owner": True, "permissions": "0"},
        # MANAGE_GUILD (0x20 = 32) set among other bits.
        {"id": "2", "name": "Manager", "owner": False, "permissions": "40"},
        # No manage permission, not owner → excluded.
        {"id": "3", "name": "Member", "owner": False, "permissions": "1024"},
        # Missing id → excluded even if owner.
        {"name": "NoId", "owner": True, "permissions": "0"},
    ]
    monkeypatch.setattr(
        auth_oauth.urllib.request, "urlopen", lambda *a, **k: _FakeResp(guilds)
    )

    result = auth_oauth.discord_manageable_guilds_from_code("code", "https://cb")

    ids = [g["id"] for g in result]
    assert ids == ["1", "2"]
    assert result[0]["owner"] == "1"
    assert result[1]["owner"] == ""


def test_empty_when_no_access_token(monkeypatch) -> None:
    monkeypatch.setattr(
        auth_oauth, "_discord_access_token", lambda code, redirect_uri: None
    )
    assert auth_oauth.discord_manageable_guilds_from_code("code", "cb") == []

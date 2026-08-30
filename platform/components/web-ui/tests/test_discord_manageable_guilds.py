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


def test_token_exchange_logs_discord_error_body(monkeypatch, caplog) -> None:
    """A Discord token-endpoint HTTPError logs the response body (e.g.
    ``invalid_client``) so a credential/redirect issue is self-diagnosing,
    and the helper degrades to ``None`` rather than raising."""
    import urllib.error

    monkeypatch.setattr(
        auth_oauth,
        "discord_client_credentials",
        lambda: ("client-id", "client-secret"),
        raising=False,
    )
    # Also patch the lazily-imported name used inside the helper.
    import source_token_exchange

    monkeypatch.setattr(
        source_token_exchange,
        "discord_client_credentials",
        lambda: ("client-id", "client-secret"),
    )

    body = b'{"error":"invalid_client","error_description":"Invalid client_id or client_secret"}'

    def _raise_401(*a: object, **k: object):
        raise urllib.error.HTTPError(
            "https://discord.com/api/oauth2/token",
            401,
            "Unauthorized",
            {},
            io.BytesIO(body),
        )

    monkeypatch.setattr(auth_oauth.urllib.request, "urlopen", _raise_401)

    with caplog.at_level("WARNING"):
        token = auth_oauth._discord_access_token("code", "https://cb")

    assert token is None
    joined = " ".join(r.getMessage() for r in caplog.records)
    assert "HTTP 401" in joined
    assert "invalid_client" in joined

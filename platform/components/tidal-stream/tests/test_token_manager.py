"""Unit tests for the Tidal token manager.

Covers load-from-Secrets-Manager, first-party refresh via the shared decision
logic (R9.4), persistence of new tokens (R9.2), and code exchange for the
callback. Legacy key-split rejection (R9.3) is enforced by the client and shared
guard and is covered in the OAuth client tests.
"""

from __future__ import annotations

import json

import pytest
from hellodj_platform_logic.tidal_refresh import (
    FIRST_PARTY_SINGLE_APP_ID_MODE,
    FirstPartyClientConfig,
)

from tidal_stream.oauth_client import FirstPartyTidalOAuthClient
from tidal_stream.secrets import TidalRefreshTokenStore
from tidal_stream.token_manager import TidalTokenManager

TOKEN_URL = "https://auth.tidal.com/v1/oauth2/token"


class FakeSecretsClient:
    """In-memory Secrets Manager double supporting get/put_secret_value."""

    def __init__(self, initial: dict) -> None:
        self._value = json.dumps(initial)
        self.puts: list[dict] = []

    def get_secret_value(self, **kwargs):
        return {"SecretString": self._value}

    def put_secret_value(self, **kwargs):
        self._value = kwargs["SecretString"]
        self.puts.append(json.loads(kwargs["SecretString"]))
        return {}


def _config():
    return FirstPartyClientConfig(
        app_id="hellodj-app",
        callback_url="https://hellodj.bot/tidal/callback",
        auth_mode=FIRST_PARTY_SINGLE_APP_ID_MODE,
    )


def _manager(secrets_client, poster, *, now_value):
    store = TidalRefreshTokenStore("tidal/refresh", client=secrets_client)
    client = FirstPartyTidalOAuthClient(_config(), token_url=TOKEN_URL, poster=poster)
    return TidalTokenManager(
        store, client, clock=lambda: now_value, expiry_skew_seconds=0.0
    )


def test_valid_token_is_returned_without_refresh():
    """A still-valid stored token is returned without calling the OAuth client."""
    secrets = FakeSecretsClient(
        {"access_token": "valid", "refresh_token": "r1", "expires_at": 5000.0}
    )
    calls: list[str] = []

    def poster(url, data, *, timeout):
        calls.append(data["grant_type"])
        return {}

    manager = _manager(secrets, poster, now_value=1000.0)
    assert manager.get_access_token() == "valid"
    assert calls == []
    assert secrets.puts == []


def test_expired_token_is_refreshed_and_persisted():
    """An expired token is refreshed via first-party path and persisted (R9.4, R9.2)."""
    secrets = FakeSecretsClient(
        {"access_token": "old", "refresh_token": "r1", "expires_at": 500.0}
    )

    def poster(url, data, *, timeout):
        assert data["grant_type"] == "refresh_token"
        return {"access_token": "fresh", "refresh_token": "r2", "expires_in": 3600}

    manager = _manager(secrets, poster, now_value=1000.0)
    token = manager.get_access_token()

    assert token == "fresh"
    assert len(secrets.puts) == 1
    assert secrets.puts[0]["access_token"] == "fresh"
    assert secrets.puts[0]["refresh_token"] == "r2"
    assert secrets.puts[0]["expires_at"] == 1000.0 + 3600


def test_refresh_preserves_refresh_token_when_not_rotated():
    """When the provider omits a new refresh token, the old one is preserved."""
    secrets = FakeSecretsClient(
        {"access_token": "old", "refresh_token": "keep-me", "expires_at": 0.0}
    )

    def poster(url, data, *, timeout):
        return {"access_token": "fresh", "expires_in": 100}

    manager = _manager(secrets, poster, now_value=1000.0)
    manager.get_access_token()
    assert secrets.puts[0]["refresh_token"] == "keep-me"


def test_complete_authorization_exchanges_and_persists():
    """The callback code exchange persists tokens to Secrets Manager (R9.2)."""
    secrets = FakeSecretsClient(
        {"access_token": "", "refresh_token": "seed", "expires_at": 0.0}
    )

    def poster(url, data, *, timeout):
        assert data["grant_type"] == "authorization_code"
        return {"access_token": "a", "refresh_token": "r", "expires_in": 200}

    manager = _manager(secrets, poster, now_value=100.0)
    token = manager.complete_authorization("the-code")

    assert token.access_token == "a"
    assert token.expires_at == 300.0
    assert secrets.puts[-1]["refresh_token"] == "r"


def test_missing_refresh_token_in_secret_raises_on_load():
    """A stored payload without a refresh token is rejected on load."""
    secrets = FakeSecretsClient({"access_token": "x", "expires_at": 0.0})

    def poster(url, data, *, timeout):
        return {}

    manager = _manager(secrets, poster, now_value=1000.0)
    with pytest.raises(ValueError):
        manager.get_access_token()

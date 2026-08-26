"""Unit tests for the first-party single-app-id Tidal OAuth client.

Covers Requirements 9.1 (single app id), 9.2 (callback code exchange), 9.3
(legacy key-split rejected at construction), 9.4 (refresh yields a token).
"""

from __future__ import annotations

import pytest
from hellodj_platform_logic.tidal_refresh import (
    FIRST_PARTY_SINGLE_APP_ID_MODE,
    LEGACY_KEY_SPLIT_MODE,
    FirstPartyClientConfig,
    LegacyKeySplitRejectedError,
    TidalRefreshFailedError,
)

from tidal_stream.oauth_client import FirstPartyTidalOAuthClient, TidalOAuthHTTPError

TOKEN_URL = "https://auth.tidal.com/v1/oauth2/token"


def _config(app_id: str = "hellodj-app", mode: str = FIRST_PARTY_SINGLE_APP_ID_MODE):
    return FirstPartyClientConfig(
        app_id=app_id,
        callback_url="https://hellodj.bot/tidal/callback",
        auth_mode=mode,
    )


def test_construction_rejects_legacy_key_split():
    """A legacy two-client-id key-split config cannot build the client (R9.3)."""
    with pytest.raises(LegacyKeySplitRejectedError):
        FirstPartyTidalOAuthClient(
            _config(mode=LEGACY_KEY_SPLIT_MODE), token_url=TOKEN_URL
        )


def test_refresh_uses_single_app_id_and_returns_token():
    """Refresh posts the single app id and yields a token with expiry (R9.1, R9.4)."""
    captured: dict[str, object] = {}

    def poster(url, data, *, timeout):
        captured["url"] = url
        captured["data"] = data
        return {"access_token": "new-access", "refresh_token": "r2", "expires_in": 3600}

    client = FirstPartyTidalOAuthClient(_config(), token_url=TOKEN_URL, poster=poster)
    token = client.refresh("r1", now=1000.0)

    assert token.access_token == "new-access"
    assert token.expires_at == 1000.0 + 3600
    assert captured["data"]["grant_type"] == "refresh_token"
    assert captured["data"]["client_id"] == "hellodj-app"
    assert captured["data"]["refresh_token"] == "r1"
    # single-app-id: no second client id is ever sent
    assert "client_id_2" not in captured["data"]


def test_exchange_code_uses_callback_and_app_id():
    """Code exchange posts the app id and HelloDJ callback (R9.1, R9.2)."""
    captured: dict[str, object] = {}

    def poster(url, data, *, timeout):
        captured["data"] = data
        return {"access_token": "a", "refresh_token": "r", "expires_in": 100}

    client = FirstPartyTidalOAuthClient(_config(), token_url=TOKEN_URL, poster=poster)
    token = client.exchange_code("auth-code", now=0.0)

    assert token.refresh_token == "r"
    assert captured["data"]["grant_type"] == "authorization_code"
    assert captured["data"]["code"] == "auth-code"
    assert captured["data"]["redirect_uri"] == "https://hellodj.bot/tidal/callback"
    assert captured["data"]["client_id"] == "hellodj-app"


def test_refresh_without_access_token_raises():
    """A response with no access token is a failed refresh (R9.4)."""

    def poster(url, data, *, timeout):
        return {"refresh_token": "r", "expires_in": 100}

    client = FirstPartyTidalOAuthClient(_config(), token_url=TOKEN_URL, poster=poster)
    with pytest.raises(TidalRefreshFailedError):
        client.refresh("r1", now=0.0)


def test_http_error_propagates():
    """Transport failures surface as TidalOAuthHTTPError."""

    def poster(url, data, *, timeout):
        raise TidalOAuthHTTPError("boom")

    client = FirstPartyTidalOAuthClient(_config(), token_url=TOKEN_URL, poster=poster)
    with pytest.raises(TidalOAuthHTTPError):
        client.refresh("r1", now=0.0)

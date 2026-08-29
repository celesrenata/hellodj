"""Per-account source OAuth route tests: ONE fixed callback per provider (B2).

Feature: per-account source auth with a single fixed callback URI. Exercises
``/auth/oauth/<provider>/connect`` and ``/auth/oauth/<provider>/callback``
end-to-end through the Flask test client against a REAL
:class:`SourceCredentialService` over an in-memory ``CoreTable`` + envelope
``FakeKms`` — no live AWS / provider calls (the token exchange HTTP seam is
monkeypatched).

Asserts:

* Connect redirects to the provider authorize URL carrying the SINGLE FIXED
  callback ``redirect_uri`` (``/auth/oauth/<provider>/callback`` — no guild/user
  in the path) and a ``state`` that is stashed server-side.
* Connect to an unconfigured provider surfaces a clear account error, never a
  provider redirect (R1.2).
* The callback rejects a ``state`` mismatch (CSRF, R1.5) and stores nothing.
* A valid Spotify callback exchanges the code and stores an encrypted per-user
  credential keyed by the session sub (R1.4, R2.1).
"""

from __future__ import annotations

import os
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

from hellodj_platform_logic.data_access import CoreTable

import source_token_exchange
from app import create_app
from source_credential_service import (
    SourceCredentialService,
    sourcecred_sk,
    user_pk,
)

STAGE = "beta"
_SUB = "acct-sub-oauth"
_BASE = "https://beta.example.test"
_WRAP_PREFIX = b"wrapped::"


@dataclass
class FakeKms:
    key_id: str = "arn:aws:kms:us-east-1:000000000000:key/source-creds"

    def generate_data_key(self, **kwargs: Any) -> dict[str, Any]:
        plaintext = os.urandom(32)
        return {
            "Plaintext": plaintext,
            "CiphertextBlob": _WRAP_PREFIX + plaintext,
            "KeyId": kwargs.get("KeyId", self.key_id),
        }

    def decrypt(self, **kwargs: Any) -> dict[str, Any]:
        return {"Plaintext": kwargs["CiphertextBlob"][len(_WRAP_PREFIX):]}


class _ClientError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


@dataclass
class _FakeTable:
    _items: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)

    def get_item(self, **kwargs: Any) -> dict[str, Any]:
        item = self._items.get((kwargs["Key"]["PK"], kwargs["Key"]["SK"]))
        return {"Item": dict(item)} if item is not None else {}

    def put_item(self, **kwargs: Any) -> dict[str, Any]:
        item = kwargs["Item"]
        key = (item["PK"], item["SK"])
        condition = kwargs.get("ConditionExpression")
        existing = self._items.get(key)
        if condition == "attribute_not_exists(PK)" and existing is not None:
            raise _ClientError("ConditionalCheckFailedException")
        self._items[key] = dict(item)
        return {}

    def delete_item(self, **kwargs: Any) -> dict[str, Any]:
        self._items.pop((kwargs["Key"]["PK"], kwargs["Key"]["SK"]), None)
        return {}


def _make_app(*, configured: bool = True) -> tuple[Any, CoreTable, _FakeTable]:
    table = _FakeTable()
    core = CoreTable(table)
    kms = FakeKms()
    creds = SourceCredentialService(core, kms, kms.key_id)
    overrides: dict[str, Any] = {
        "TESTING": True,
        "SECRET_KEY": "test-secret",
        "HELLODJ_STAGE": STAGE,
        "PUBLIC_BASE_URL": _BASE,
    }
    if configured:
        overrides.update(
            {
                "GOOGLE_CLIENT_ID": "google-client-abc",
                "SPOTIFY_CLIENT_ID": "spotify-client-abc",
                "SPOTIFY_CLIENT_SECRET": "spotify-secret-abc",
                "TIDAL_CLIENT_ID": "tidal-client-abc",
            }
        )
    app = create_app(overrides=overrides)
    app.extensions["source_credentials"] = creds
    return app, core, table


def _client(app: Any) -> Any:
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["user"] = {"is_admin": False, "sub": _SUB}
    return client


def test_connect_redirects_to_provider_with_fixed_callback():
    app, _core, _table = _make_app(configured=True)
    client = _client(app)

    resp = client.get("/auth/oauth/spotify/connect")

    assert resp.status_code == 302
    location = resp.headers["Location"]
    assert location.startswith("https://accounts.spotify.com/authorize")
    params = urllib.parse.parse_qs(urllib.parse.urlparse(location).query)
    # The FIXED per-account callback — no guild/user in the path.
    assert params["redirect_uri"][0] == (
        f"{_BASE}/auth/oauth/spotify/callback"
    )
    assert params["state"][0]
    # The state was stashed server-side for the callback CSRF check.
    with client.session_transaction() as sess:
        assert sess["source_oauth_state"] == params["state"][0]
        assert sess["source_oauth_provider"] == "spotify"


def test_connect_unconfigured_provider_shows_account_error():
    app, _core, _table = _make_app(configured=False)
    client = _client(app)

    resp = client.get("/auth/oauth/spotify/connect")

    assert resp.status_code == 302
    # Bounces to the account page with a clear error, NOT to the provider.
    assert "/account" in resp.headers["Location"]
    assert "provider_not_configured" in resp.headers["Location"]


def test_connect_unknown_provider_rejected():
    app, _core, _table = _make_app(configured=True)
    client = _client(app)

    resp = client.get("/auth/oauth/bogus/connect")

    assert resp.status_code == 302
    assert "unknown_provider" in resp.headers["Location"]


def test_callback_state_mismatch_stores_nothing():
    app, core, _table = _make_app(configured=True)
    client = _client(app)
    with client.session_transaction() as sess:
        sess["source_oauth_state"] = "the-real-state"
        sess["source_oauth_provider"] = "spotify"

    resp = client.get("/auth/oauth/spotify/callback?state=WRONG&code=abc")

    assert resp.status_code == 302
    assert "state_mismatch" in resp.headers["Location"]
    assert core.get(user_pk(_SUB), sourcecred_sk("spotify")) is None


def test_spotify_callback_exchanges_and_stores(monkeypatch):
    app, core, _table = _make_app(configured=True)
    client = _client(app)

    # Stub the token endpoint so no live Spotify call happens.
    def _fake_post_form(url: str, form: dict[str, str], timeout: int = 10):
        assert form["grant_type"] == "authorization_code"
        # The exchange must use the SAME fixed callback redirect_uri.
        assert form["redirect_uri"] == f"{_BASE}/auth/oauth/spotify/callback"
        return {"refresh_token": "sp-refresh-xyz", "scope": "streaming"}

    monkeypatch.setattr(
        source_token_exchange, "_http_post_form", _fake_post_form
    )

    with client.session_transaction() as sess:
        sess["source_oauth_state"] = "state-1"
        sess["source_oauth_provider"] = "spotify"

    resp = client.get("/auth/oauth/spotify/callback?state=state-1&code=abc")

    assert resp.status_code == 302
    assert "connected=spotify" in resp.headers["Location"]
    # An encrypted per-user credential was stored keyed by the session sub.
    item = core.get(user_pk(_SUB), sourcecred_sk("spotify"))
    assert item is not None
    assert item.get("data", {}).get("connected") is True
    # Token material never stored in plaintext.
    assert "sp-refresh-xyz" not in str(item)

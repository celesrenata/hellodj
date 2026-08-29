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

    def query(self, **kwargs: Any) -> dict[str, Any]:
        values = kwargs.get("ExpressionAttributeValues", {})
        pk = values[":pk"]
        prefix = values.get(":skp")
        items = [
            dict(it)
            for (ipk, isk), it in self._items.items()
            if ipk == pk and (prefix is None or isk.startswith(prefix))
        ]
        return {"Items": items}

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


# ── YouTube device-code flow (public plugin client, no operator Google app) ──


def test_youtube_connect_renders_device_code_no_redirect(monkeypatch):
    """YouTube Connect starts the device flow and renders the code inline.

    Unlike Spotify/Tidal (browser redirect), YouTube authenticates via the
    youtube-source plugin's PUBLIC device client, so Connect returns a 200 HTML
    partial with the user code + verification URL — even with NO GOOGLE_CLIENT_ID
    configured — and stashes the (server-only) device_code in the session.
    """
    import source_account_routes

    def _fake_start(*, http_post=None):
        return {
            "device_code": "DEVICE-SECRET",
            "user_code": "ABCD-EFGH",
            "verification_url": "https://www.youtube.com/activate",
            "interval": 5,
            "expires_in": 1800,
        }

    monkeypatch.setattr(
        source_account_routes, "start_device_authorization", _fake_start
    )

    app, _core, _table = _make_app(configured=False)  # no google client id
    client = _client(app)

    resp = client.get("/auth/oauth/youtube/connect")

    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    # User-facing code + verification URL are shown; the device_code is NOT.
    assert "ABCD-EFGH" in body
    assert "www.youtube.com/activate" in body
    assert "DEVICE-SECRET" not in body
    # HTMX poller wired to the poll route.
    assert "youtube/device/poll" in body
    # The secret poll handle lives only in the session.
    with client.session_transaction() as sess:
        assert sess["yt_device_code"] == "DEVICE-SECRET"
        assert sess["yt_device_provider"] == "youtube"


def test_youtube_device_poll_pending_then_success(monkeypatch):
    """Poll returns the device partial while pending, then stores on success."""
    import source_account_routes as sar

    # First poll: pending → re-render device partial (keep polling).
    monkeypatch.setattr(
        sar, "poll_device_token", lambda code: {"status": "pending"}
    )
    # PoToken fetch stubbed so the compose step completes on success.
    monkeypatch.setattr(
        sar,
        "fetch_guild_potoken",
        lambda: {"pot_token": "PT", "pot_visitor_data": "VD"},
    )

    app, core, _table = _make_app(configured=False)
    client = _client(app)
    with client.session_transaction() as sess:
        sess["yt_device_code"] = "DEVICE-SECRET"
        sess["yt_device_provider"] = "youtube"
        sess["yt_device_user_code"] = "ABCD-EFGH"
        sess["yt_device_verification_url"] = "https://www.youtube.com/activate"
        sess["yt_device_interval"] = 5

    pending = client.post("/auth/oauth/youtube/device/poll")
    assert pending.status_code == 200
    assert "youtube/device/poll" in pending.get_data(as_text=True)
    # Nothing stored while pending.
    assert core.get(user_pk(_SUB), sourcecred_sk("youtube")) is None

    # Second poll: authorization complete → refresh token arrives.
    monkeypatch.setattr(
        sar,
        "poll_device_token",
        lambda code: {"status": "ok", "oauth_refresh_token": "1//0gREFRESH"},
    )
    done = client.post("/auth/oauth/youtube/device/poll")
    assert done.status_code == 200
    body = done.get_data(as_text=True)
    # The connections list is swapped back in (fragment, no full shell).
    assert "<html" not in body.lower()
    # An encrypted per-user YouTube credential was stored keyed by the sub.
    item = core.get(user_pk(_SUB), sourcecred_sk("youtube"))
    assert item is not None
    assert item.get("data", {}).get("connected") is True
    # Neither the refresh token nor the device code leaks into the store.
    assert "1//0gREFRESH" not in str(item)
    assert "DEVICE-SECRET" not in str(item)
    # The transient device state was cleared from the session.
    with client.session_transaction() as sess:
        assert "yt_device_code" not in sess


def test_youtube_device_poll_error_stores_nothing(monkeypatch):
    """A terminal device error surfaces an error and stores no credential."""
    import source_account_routes as sar

    monkeypatch.setattr(
        sar,
        "poll_device_token",
        lambda code: {"status": "error", "error": "access_denied"},
    )

    app, core, _table = _make_app(configured=False)
    client = _client(app)
    with client.session_transaction() as sess:
        sess["yt_device_code"] = "DEVICE-SECRET"
        sess["yt_device_provider"] = "youtube"

    resp = client.post("/auth/oauth/youtube/device/poll")

    assert resp.status_code == 200
    assert core.get(user_pk(_SUB), sourcecred_sk("youtube")) is None
    with client.session_transaction() as sess:
        assert "yt_device_code" not in sess

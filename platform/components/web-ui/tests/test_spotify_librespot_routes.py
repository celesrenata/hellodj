"""Route tests for the Spotify librespot capture flow (task 2.2).

Feature: multi-tenant-source-streaming. Exercises the web-ui orchestration
end-to-end through the Flask test client against a REAL
:class:`SourceCredentialService` (in-memory CoreTable + envelope FakeKms) and an
INJECTED :class:`SpotifyLibrespotCapture` (fake sidecar transport) — no live
sidecar / AWS / Spotify.

Asserts:

* A successful Spotify OAuth callback CHAINS into the librespot capture: it
  renders the ``account_spotify_librespot`` partial with the sidecar-minted
  authorize URL and stashes a CSRF state (when a sidecar is wired).
* With NO sidecar wired the Spotify callback falls through to the normal
  ``connected=spotify`` redirect (the Web-API credential is still stored).
* The librespot callback validates state (CSRF), forwards the code to the
  sidecar, and attaches the reusable blob into the encrypted Spotify item
  (``extra.librespot_credentials``) with no plaintext leak.
* A state mismatch or a sidecar capture failure surfaces a clear error and
  stores no librespot blob.

Validates: Requirements 3.3, 6.4, 10.3
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

from hellodj_platform_logic.data_access import CoreTable

import source_token_exchange
from app import create_app
from source_credential_service import SourceCredentialService, sourcecred_sk, user_pk
from spotify_librespot_capture import LIBRESPOT_CREDENTIALS_EXTRA_KEY

STAGE = "beta"
_SUB = "acct-sub-librespot-route"
_BASE = "https://beta.example.test"
_WRAP_PREFIX = b"wrapped::"

_BLOB = {
    "username": "canonical-user",
    "credentials": "REUSABLE-SECRET-b64",
    "type": "AUTHENTICATION_STORED_SPOTIFY_CREDENTIALS",
}


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
        return {
            "Items": [
                dict(it)
                for (ipk, isk), it in self._items.items()
                if ipk == pk and (prefix is None or isk.startswith(prefix))
            ]
        }

    def put_item(self, **kwargs: Any) -> dict[str, Any]:
        item = kwargs["Item"]
        key = (item["PK"], item["SK"])
        condition = kwargs.get("ConditionExpression")
        existing = self._items.get(key)
        if condition == "attribute_not_exists(PK)" and existing is not None:
            raise _ClientError("ConditionalCheckFailedException")
        if condition == "attribute_not_exists(version)" and existing is not None:
            raise _ClientError("ConditionalCheckFailedException")
        if condition == "version = :expected":
            expected = kwargs["ExpressionAttributeValues"][":expected"]
            if existing is None or existing.get("version") != expected:
                raise _ClientError("ConditionalCheckFailedException")
        self._items[key] = dict(item)
        return {}

    def delete_item(self, **kwargs: Any) -> dict[str, Any]:
        self._items.pop((kwargs["Key"]["PK"], kwargs["Key"]["SK"]), None)
        return {}


class _FakeCapture:
    """Injected librespot capture double with scripted start/complete."""

    def __init__(
        self,
        *,
        authorize_url: str | None = "https://accounts.spotify.com/authorize?lr=1",
        blob: dict[str, Any] | None = None,
    ) -> None:
        self._authorize_url = authorize_url
        self._blob = blob
        self.started: list[tuple[str, str]] = []
        self.completed: list[tuple[str, str]] = []

    def start(self, sub: str, redirect_uri: str) -> str | None:
        self.started.append((sub, redirect_uri))
        return self._authorize_url

    def complete(self, sub: str, code: str) -> dict[str, Any] | None:
        self.completed.append((sub, code))
        return self._blob


def _make_app(capture: Any | None) -> tuple[Any, CoreTable, _FakeTable]:
    table = _FakeTable()
    core = CoreTable(table)
    kms = FakeKms()
    creds = SourceCredentialService(core, kms, kms.key_id)
    app = create_app(
        overrides={
            "TESTING": True,
            "SECRET_KEY": "test-secret",
            "HELLODJ_STAGE": STAGE,
            "PUBLIC_BASE_URL": _BASE,
            "SPOTIFY_CLIENT_ID": "spotify-client-abc",
            "SPOTIFY_CLIENT_SECRET": "spotify-secret-abc",
        }
    )
    app.extensions["source_credentials"] = creds
    if capture is not None:
        app.extensions["spotify_librespot"] = capture
    return app, core, table


def _client(app: Any) -> Any:
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["user"] = {"is_admin": False, "sub": _SUB}
    return client


def _spotify_oauth_ok(monkeypatch) -> None:
    monkeypatch.setattr(
        source_token_exchange,
        "_http_post_form",
        lambda *a, **k: {"refresh_token": "sp-refresh", "scope": "streaming"},
    )


# ── Spotify OAuth callback chains into librespot capture ───────────────────


def test_spotify_callback_chains_into_librespot(monkeypatch):
    _spotify_oauth_ok(monkeypatch)
    capture = _FakeCapture()
    app, core, _table = _make_app(capture)
    client = _client(app)
    with client.session_transaction() as sess:
        sess["source_oauth_state"] = "s-1"
        sess["source_oauth_provider"] = "spotify"

    resp = client.get("/auth/oauth/spotify/callback?state=s-1&code=abc")

    # 200 partial (not a redirect): the librespot authorize step is rendered.
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "accounts.spotify.com/authorize?lr=1" in body
    assert "<html" not in body.lower()  # fragment, not full shell
    # The Web-API credential was still stored, and capture.start was invoked
    # with the FIXED librespot callback redirect_uri.
    assert core.get(user_pk(_SUB), sourcecred_sk("spotify")) is not None
    assert capture.started
    assert capture.started[0][1] == f"{_BASE}/auth/oauth/spotify/librespot/callback"
    # A CSRF state for the librespot leg was stashed.
    with client.session_transaction() as sess:
        assert sess.get("librespot_state")


def test_spotify_callback_no_sidecar_falls_through(monkeypatch):
    _spotify_oauth_ok(monkeypatch)
    app, core, _table = _make_app(capture=None)  # no sidecar wired
    client = _client(app)
    with client.session_transaction() as sess:
        sess["source_oauth_state"] = "s-1"
        sess["source_oauth_provider"] = "spotify"

    resp = client.get("/auth/oauth/spotify/callback?state=s-1&code=abc")

    # Falls through to the normal connected redirect; Web-API credential stored.
    assert resp.status_code == 302
    assert "connected=spotify" in resp.headers["Location"]
    assert core.get(user_pk(_SUB), sourcecred_sk("spotify")) is not None


# ── librespot callback stores the reusable blob ────────────────────────────


def test_librespot_callback_stores_blob(monkeypatch):
    _spotify_oauth_ok(monkeypatch)
    capture = _FakeCapture(blob=_BLOB)
    app, core, table = _make_app(capture)
    client = _client(app)

    # Pre-store the Spotify OAuth credential (as the OAuth callback would) and
    # set the librespot CSRF state as the start step would.
    creds: SourceCredentialService = app.extensions["source_credentials"]
    from hellodj_platform_logic.source_refresh import TokenState

    creds.store(
        _SUB,
        "spotify",
        TokenState(access_token="", refresh_token="sp-refresh", expires_at=0.0),
        connected_by=_SUB,
    )
    with client.session_transaction() as sess:
        sess["librespot_state"] = "lr-1"

    resp = client.get("/auth/oauth/spotify/librespot/callback?state=lr-1&code=xyz")

    assert resp.status_code == 302
    assert "connected=spotify" in resp.headers["Location"]
    assert capture.completed == [(_SUB, "xyz")]
    # The reusable blob is attached inside the encrypted Spotify blob.
    loaded = creds.load_token(_SUB, "spotify")
    assert loaded is not None
    assert loaded.extra[LIBRESPOT_CREDENTIALS_EXTRA_KEY] == _BLOB
    # No plaintext leak.
    assert "REUSABLE-SECRET" not in json.dumps(list(table._items.values()))


def test_librespot_callback_state_mismatch_stores_nothing():
    capture = _FakeCapture(blob=_BLOB)
    app, _core, _table = _make_app(capture)
    client = _client(app)
    with client.session_transaction() as sess:
        sess["librespot_state"] = "the-real-state"

    resp = client.get("/auth/oauth/spotify/librespot/callback?state=WRONG&code=xyz")

    assert resp.status_code == 302
    assert "state_mismatch" in resp.headers["Location"]
    # The sidecar was never asked to complete.
    assert capture.completed == []


def test_librespot_callback_capture_failure_surfaces_error():
    capture = _FakeCapture(blob=None)  # sidecar returns no usable blob
    app, _core, _table = _make_app(capture)
    client = _client(app)

    # Pre-store a Spotify credential so the ONLY failure is the capture.
    creds: SourceCredentialService = app.extensions["source_credentials"]
    from hellodj_platform_logic.source_refresh import TokenState

    creds.store(
        _SUB,
        "spotify",
        TokenState(access_token="", refresh_token="sp-refresh", expires_at=0.0),
        connected_by=_SUB,
    )
    with client.session_transaction() as sess:
        sess["librespot_state"] = "lr-1"

    resp = client.get("/auth/oauth/spotify/librespot/callback?state=lr-1&code=xyz")

    assert resp.status_code == 302
    location = resp.headers["Location"]
    assert "spotify_playback_failed" in location
    # No librespot blob attached.
    loaded = creds.load_token(_SUB, "spotify")
    assert loaded is not None
    assert LIBRESPOT_CREDENTIALS_EXTRA_KEY not in loaded.extra

"""Spotify per-guild code->refresh-token exchange tests.

Task 5.2 of the ``bot-identity-and-source-auth`` bugfix spec (Change area C/D).
Verifies the web-ui completes the Spotify ``authorization_code`` ->
``refresh_token`` exchange (Spotify has no per-guild sidecar for the exchange,
unlike Tidal) and stores the refresh-token-centric shape the bot's global
Spotify fallback also uses (R2.2)::

    {provider, refresh_token, access_token?, expires_at?, scope, obtained_at}

Both the happy path (full shape stored in the isolated secret, metadata-only in
DynamoDB) and the clear-error path (no refresh token -> nothing stored, visible
error, no silent no-op) are covered. All network access is faked via the
``source_token_exchange`` HTTP seams — no live Spotify / AWS.

Preservation: the ``SOURCE#<provider>`` DynamoDB item stays metadata-only
(3.3); Tidal's sidecar-forward callback path is untouched (3.1).

Validates: Requirements 2.2
"""

from __future__ import annotations

import json
from typing import Any

import source_token_exchange as ste
from app import create_app
from guild_admin_service import guild_pk
from guild_sources import GuildSourcesService, guild_source_secret_name, source_sk

STAGE = "beta"


# ── In-memory fakes (mirror test_youtube_token_exchange.py) ────────────────


class _ClientError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class _FakeTable:
    def __init__(self) -> None:
        self._items: dict[tuple[str, str], dict[str, Any]] = {}

    def get_item(self, **kwargs: Any) -> dict[str, Any]:
        key = (kwargs["Key"]["PK"], kwargs["Key"]["SK"])
        item = self._items.get(key)
        return {"Item": dict(item)} if item is not None else {}

    def put_item(self, **kwargs: Any) -> dict[str, Any]:
        item = kwargs["Item"]
        key = (item["PK"], item["SK"])
        condition = kwargs.get("ConditionExpression")
        existing = self._items.get(key)
        if condition == "attribute_not_exists(PK)" and existing is not None:
            raise _ClientError("ConditionalCheckFailedException")
        if condition == "attribute_not_exists(version)" and existing is not None:
            raise _ClientError("ConditionalCheckFailedException")
        self._items[key] = dict(item)
        return {}

    def query(self, **kwargs: Any) -> dict[str, Any]:
        values = kwargs["ExpressionAttributeValues"]
        pk = values[":pk"]
        prefix = values.get(":skp")
        items = [
            dict(it)
            for key, it in self._items.items()
            if key[0] == pk and (prefix is None or str(key[1]).startswith(prefix))
        ]
        return {"Items": items}

    def delete_item(self, **kwargs: Any) -> dict[str, Any]:
        self._items.pop((kwargs["Key"]["PK"], kwargs["Key"]["SK"]), None)
        return {}


class _FakeSecrets:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def create_secret(self, **kwargs: Any) -> dict[str, Any]:
        name = kwargs["Name"]
        if name in self.store:
            raise _ClientError("ResourceExistsException")
        self.store[name] = kwargs["SecretString"]
        return {"Name": name}

    def put_secret_value(self, **kwargs: Any) -> dict[str, Any]:
        self.store[kwargs["SecretId"]] = kwargs["SecretString"]
        return {"SecretId": kwargs["SecretId"]}

    def get_secret_value(self, **kwargs: Any) -> dict[str, Any]:
        name = kwargs["SecretId"]
        if name not in self.store:
            raise _ClientError("ResourceNotFoundException")
        return {"SecretString": self.store[name]}

    def delete_secret(self, **kwargs: Any) -> dict[str, Any]:
        self.store.pop(kwargs["SecretId"], None)
        return {}


def _service() -> tuple[GuildSourcesService, Any, _FakeSecrets]:
    from hellodj_platform_logic.data_access import CoreTable

    table = _FakeTable()
    core = CoreTable(table)
    secrets = _FakeSecrets()
    return GuildSourcesService(core, secrets, stage=STAGE), core, secrets


def _make_app(sources: GuildSourcesService) -> Any:
    """A degraded-mode app with the Spotify client id/secret wired."""
    app = create_app(
        overrides={
            "TESTING": True,
            "SECRET_KEY": "test-secret",
            "HELLODJ_STAGE": STAGE,
            "PUBLIC_BASE_URL": "https://beta.example.test",
            "SPOTIFY_CLIENT_ID": "spotify-client-abc",
            "SPOTIFY_CLIENT_SECRET": "spotify-secret-xyz",
        }
    )
    app.extensions["guild_sources"] = sources
    return app


def _admin_client(app: Any) -> Any:
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["user"] = {"is_admin": True, "sub": "admin-sub"}
        sess["source_state"] = "st8"
    return client


# ── source_exchange_spotify unit ───────────────────────────────────────────


class TestSpotifyExchangeUnit:
    def test_exchange_returns_refresh_token_shape(self, monkeypatch):
        """A successful token POST yields the refresh-token-centric shape (R2.2)."""
        captured: dict[str, Any] = {}

        def fake_post_form(url, form, timeout=10):
            captured["url"] = url
            captured["form"] = form
            return {
                "refresh_token": "AQ-REFRESH",
                "access_token": "BQ-ACCESS",
                "expires_in": 3600,
                "scope": "user-read-playback-state streaming",
            }

        monkeypatch.setattr(ste, "_http_post_form", fake_post_form)
        svc, _core, _secrets = _service()
        app = _make_app(svc)
        with app.test_request_context(
            "/auth/sources/111/spotify/callback?code=AQcode"
        ):
            out = ste.source_exchange_spotify("AQcode", "111")

        assert out["provider"] == "spotify"
        assert out["refresh_token"] == "AQ-REFRESH"
        assert out["access_token"] == "BQ-ACCESS"
        assert out["scope"] == "user-read-playback-state streaming"
        assert isinstance(out["obtained_at"], int)
        assert out["obtained_at"] > 0
        assert out["expires_at"] == out["obtained_at"] + 3600
        # POSTed to Spotify's token endpoint with the resolved client creds.
        assert captured["url"] == "https://accounts.spotify.com/api/token"
        assert captured["form"]["grant_type"] == "authorization_code"
        assert captured["form"]["client_id"] == "spotify-client-abc"
        assert captured["form"]["client_secret"] == "spotify-secret-xyz"
        assert captured["form"]["code"] == "AQcode"

    def test_exchange_omits_access_token_when_absent(self, monkeypatch):
        """Only a refresh token present -> no access_token/expires_at keys."""
        monkeypatch.setattr(
            ste,
            "_http_post_form",
            lambda *a, **k: {"refresh_token": "R-only", "scope": "streaming"},
        )
        svc = _service()[0]
        app = _make_app(svc)
        with app.test_request_context("/"):
            out = ste.source_exchange_spotify("code", "111")
        assert out["refresh_token"] == "R-only"
        assert "access_token" not in out
        assert "expires_at" not in out

    def test_exchange_empty_when_no_refresh_token(self, monkeypatch):
        """No refresh token in the response -> ``{}`` (store nothing)."""
        monkeypatch.setattr(
            ste, "_http_post_form", lambda *a, **k: {"access_token": "only"}
        )
        svc = _service()[0]
        app = _make_app(svc)
        with app.test_request_context("/"):
            assert ste.source_exchange_spotify("code", "111") == {}

    def test_exchange_empty_when_code_empty(self, monkeypatch):
        """An empty code short-circuits before any POST."""
        called = {"n": 0}

        def fake_post_form(*a, **k):
            called["n"] += 1
            return {"refresh_token": "x"}

        monkeypatch.setattr(ste, "_http_post_form", fake_post_form)
        svc = _service()[0]
        app = _make_app(svc)
        with app.test_request_context("/"):
            assert ste.source_exchange_spotify("", "111") == {}
        assert called["n"] == 0

    def test_exchange_empty_without_client_secret(self, monkeypatch):
        """Missing client secret -> no exchange attempted, ``{}`` (R2.6 dep)."""
        called = {"n": 0}

        def fake_post_form(*a, **k):
            called["n"] += 1
            return {"refresh_token": "x"}

        monkeypatch.setattr(ste, "_http_post_form", fake_post_form)
        svc = _service()[0]
        app = create_app(
            overrides={
                "TESTING": True,
                "SECRET_KEY": "s",
                "HELLODJ_STAGE": STAGE,
                "SPOTIFY_CLIENT_ID": "id-only",
                "SPOTIFY_CLIENT_SECRET": "",
            }
        )
        app.extensions["guild_sources"] = svc
        with app.test_request_context("/"):
            assert ste.source_exchange_spotify("code", "111") == {}
        assert called["n"] == 0


# ── Full callback route: stores the shape / surfaces a clear error ─────────


class TestSpotifyCallbackRoute:
    def test_callback_stores_refresh_shape_in_isolated_secret(self, monkeypatch):
        """The callback stores the Spotify tokens in the guild secret only."""
        monkeypatch.setattr(
            ste,
            "_http_post_form",
            lambda *a, **k: {
                "refresh_token": "REF-SP",
                "access_token": "ACC-SP",
                "expires_in": 3600,
                "scope": "streaming",
            },
        )
        svc, core, secrets = _service()
        app = _make_app(svc)
        client = _admin_client(app)

        resp = client.get(
            "/auth/sources/111/spotify/callback?code=AQcode&state=st8"
        )

        assert resp.status_code in (301, 302)
        assert "/guilds/111" in resp.headers.get("Location", "")
        # Tokens land ONLY in the isolated secret with the refresh-centric shape.
        name = guild_source_secret_name(STAGE, "111", "spotify")
        assert name in secrets.store
        stored = json.loads(secrets.store[name])
        assert stored["provider"] == "spotify"
        assert stored["refresh_token"] == "REF-SP"
        assert stored["access_token"] == "ACC-SP"
        # DynamoDB item is metadata-only (no token material) — R3.3.
        item = core.get(guild_pk("111"), source_sk("spotify"))
        assert item is not None
        serialized = json.dumps(item)
        assert "REF-SP" not in serialized
        assert "ACC-SP" not in serialized
        assert "refresh_token" not in serialized

    def test_callback_clear_error_when_exchange_empty(self, monkeypatch):
        """No refresh token -> visible error, nothing stored (no silent no-op)."""
        monkeypatch.setattr(ste, "_http_post_form", lambda *a, **k: {})
        svc, _core, secrets = _service()
        app = _make_app(svc)
        client = _admin_client(app)

        resp = client.get(
            "/auth/sources/111/spotify/callback?code=AQcode&state=st8"
        )

        assert resp.status_code in (301, 302)
        location = resp.headers.get("Location", "")
        assert "error=spotify_connect_failed" in location
        assert "provider=spotify" in location
        # Nothing partial stored.
        assert guild_source_secret_name(STAGE, "111", "spotify") not in secrets.store

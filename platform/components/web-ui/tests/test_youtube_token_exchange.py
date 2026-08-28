"""YouTube per-guild code->refresh-token exchange + PoToken capture tests.

Task 5.1 of the ``bot-identity-and-source-auth`` bugfix spec (Change area C).
Verifies the web-ui completes the Google ``authorization_code`` -> offline
``refresh_token`` exchange and attaches a PoToken (+ visitor data) from the
in-cluster potoken-server, storing the exact per-guild secret shape the bot
playback path consumes (R2.3, R2.4)::

    {provider, oauth_refresh_token, pot_token, pot_visitor_data,
     connected_by, connected_at}

Both the happy path (full shape stored in the isolated secret) and the
clear-error paths (missing refresh token, potoken-server down -> no partial
secret, visible error) are covered. All network access is faked via the
``source_token_exchange`` HTTP seams — no live Google / potoken-server / AWS.

Preservation: the ``SOURCE#<provider>`` DynamoDB item stays metadata-only
(3.3); Spotify/Tidal callback capture is untouched.

Validates: Requirements 2.3, 2.4
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from hellodj_platform_logic.data_access import CoreTable

import source_token_exchange as ste
from app import create_app
from guild_admin_service import guild_pk
from guild_sources import GuildSourcesService, guild_source_secret_name, source_sk

STAGE = "beta"


# ── In-memory fakes (mirror test_preservation_source_auth_identity.py) ──────


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


def _service() -> tuple[GuildSourcesService, CoreTable, _FakeSecrets]:
    table = _FakeTable()
    core = CoreTable(table)
    secrets = _FakeSecrets()
    return GuildSourcesService(core, secrets, stage=STAGE), core, secrets


def _make_app(sources: GuildSourcesService) -> Any:
    """A degraded-mode app with Google client id/secret wired + potoken URL."""
    app = create_app(
        overrides={
            "TESTING": True,
            "SECRET_KEY": "test-secret",
            "HELLODJ_STAGE": STAGE,
            "PUBLIC_BASE_URL": "https://beta.example.test",
            "GOOGLE_CLIENT_ID": "google-client-abc",
            "GOOGLE_CLIENT_SECRET": "google-secret-xyz",
            "POTOKEN_SERVER_URL": "http://potoken.test:4416",
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


# ── source_exchange_google + fetch_guild_potoken units ─────────────────────


class TestGoogleExchangeUnit:
    def test_exchange_returns_refresh_token(self, monkeypatch):
        """A successful token POST yields ``oauth_refresh_token`` (R2.3)."""
        captured: dict[str, Any] = {}

        def fake_post_form(url, form, timeout=10):
            captured["url"] = url
            captured["form"] = form
            return {"refresh_token": "1//0g-REFRESH", "access_token": "ya29.x"}

        monkeypatch.setattr(ste, "_http_post_form", fake_post_form)
        _svc, _core, _secrets = _service()
        app = _make_app(_svc)
        with app.test_request_context(
            "/auth/sources/111/youtube/callback?code=4/0AcODE",
            # view_args aren't populated by test_request_context alone, so the
            # provider hint falls back to youtube — asserted below.
        ):
            out = ste.source_exchange_google("4/0AcODE", "111")

        assert out == {"oauth_refresh_token": "1//0g-REFRESH"}
        assert captured["url"] == "https://oauth2.googleapis.com/token"
        assert captured["form"]["grant_type"] == "authorization_code"
        assert captured["form"]["client_id"] == "google-client-abc"
        assert captured["form"]["client_secret"] == "google-secret-xyz"
        assert captured["form"]["code"] == "4/0AcODE"

    def test_exchange_empty_when_no_refresh_token(self, monkeypatch):
        """No refresh token in the response -> ``{}`` (store nothing)."""
        monkeypatch.setattr(
            ste, "_http_post_form", lambda *a, **k: {"access_token": "only"}
        )
        _svc = _service()[0]
        app = _make_app(_svc)
        with app.test_request_context("/"):
            assert ste.source_exchange_google("code", "111") == {}

    def test_exchange_empty_without_client_secret(self, monkeypatch):
        """Missing client secret -> no exchange attempted, ``{}`` (R2.6 dep)."""
        called = {"n": 0}

        def fake_post_form(*a, **k):
            called["n"] += 1
            return {"refresh_token": "x"}

        monkeypatch.setattr(ste, "_http_post_form", fake_post_form)
        _svc = _service()[0]
        app = create_app(
            overrides={
                "TESTING": True,
                "SECRET_KEY": "s",
                "HELLODJ_STAGE": STAGE,
                "GOOGLE_CLIENT_ID": "id-only",
                "GOOGLE_CLIENT_SECRET": "",
            }
        )
        with app.test_request_context("/"):
            assert ste.source_exchange_google("code", "111") == {}
        assert called["n"] == 0

    def test_fetch_potoken_maps_fields(self, monkeypatch):
        """potoken-server ``poToken``/``contentBinding`` map to the secret keys."""
        monkeypatch.setattr(
            ste,
            "_http_post_json",
            lambda url, body, timeout=10: {
                "poToken": "MnQ-POT",
                "contentBinding": "Cgs-VISITOR",
                "expiresAt": "2026-01-01",
            },
        )
        _svc = _service()[0]
        app = _make_app(_svc)
        with app.test_request_context("/"):
            assert ste.fetch_guild_potoken() == {
                "pot_token": "MnQ-POT",
                "pot_visitor_data": "Cgs-VISITOR",
            }

    def test_fetch_potoken_empty_when_server_down(self, monkeypatch):
        """A failed potoken POST (``{}``) -> ``{}`` (no partial secret)."""
        monkeypatch.setattr(ste, "_http_post_json", lambda *a, **k: {})
        _svc = _service()[0]
        app = _make_app(_svc)
        with app.test_request_context("/"):
            assert ste.fetch_guild_potoken() == {}


# ── compose_youtube_tokens: the exact stored shape ─────────────────────────


class TestComposeYouTubeTokens:
    @pytest.mark.parametrize("provider", ["youtube", "youtube_music"])
    def test_compose_full_shape(self, monkeypatch, provider):
        """Exchange + PoToken compose the exact per-guild secret shape (R2.4)."""
        monkeypatch.setattr(
            ste, "_http_post_form", lambda *a, **k: {"refresh_token": "R-TOK"}
        )
        monkeypatch.setattr(
            ste,
            "_http_post_json",
            lambda *a, **k: {"poToken": "P-TOK", "contentBinding": "V-DAT"},
        )
        _svc = _service()[0]
        app = _make_app(_svc)
        with app.test_request_context(
            f"/auth/sources/111/{provider}/callback?code=c"
        ):
            out = ste.compose_youtube_tokens(
                provider, "c", "111", connected_by="admin-sub"
            )

        assert out["provider"] == provider
        assert out["oauth_refresh_token"] == "R-TOK"
        assert out["pot_token"] == "P-TOK"
        assert out["pot_visitor_data"] == "V-DAT"
        assert out["connected_by"] == "admin-sub"
        assert isinstance(out["connected_at"], int)
        assert out["connected_at"] > 0

    def test_compose_empty_when_refresh_missing(self, monkeypatch):
        """No refresh token -> ``{}`` and PoToken is never even fetched."""
        pot_called = {"n": 0}

        def fake_json(*a, **k):
            pot_called["n"] += 1
            return {"poToken": "p", "contentBinding": "v"}

        monkeypatch.setattr(ste, "_http_post_form", lambda *a, **k: {})
        monkeypatch.setattr(ste, "_http_post_json", fake_json)
        _svc = _service()[0]
        app = _make_app(_svc)
        with app.test_request_context("/auth/sources/111/youtube/callback?code=c"):
            assert ste.compose_youtube_tokens(
                "youtube", "c", "111", connected_by="a"
            ) == {}
        assert pot_called["n"] == 0

    def test_compose_empty_when_potoken_down(self, monkeypatch):
        """Refresh OK but potoken-server down -> ``{}`` (no partial secret)."""
        monkeypatch.setattr(
            ste, "_http_post_form", lambda *a, **k: {"refresh_token": "R"}
        )
        monkeypatch.setattr(ste, "_http_post_json", lambda *a, **k: {})
        _svc = _service()[0]
        app = _make_app(_svc)
        with app.test_request_context("/auth/sources/111/youtube/callback?code=c"):
            assert ste.compose_youtube_tokens(
                "youtube", "c", "111", connected_by="a"
            ) == {}


# ── Full callback route: stores the shape / surfaces a clear error ─────────


class TestYouTubeCallbackRoute:
    @pytest.mark.parametrize("provider", ["youtube", "youtube_music"])
    def test_callback_stores_full_shape_in_isolated_secret(
        self, monkeypatch, provider
    ):
        """The callback stores {refresh,pot,visitor} in the guild secret only."""
        monkeypatch.setattr(
            ste, "_http_post_form", lambda *a, **k: {"refresh_token": "REF-1"}
        )
        monkeypatch.setattr(
            ste,
            "_http_post_json",
            lambda *a, **k: {"poToken": "POT-1", "contentBinding": "VIS-1"},
        )
        svc, core, secrets = _service()
        app = _make_app(svc)
        client = _admin_client(app)

        resp = client.get(
            f"/auth/sources/111/{provider}/callback?code=4/0Ac&state=st8"
        )

        assert resp.status_code in (301, 302)
        assert "/guilds/111" in resp.headers.get("Location", "")
        # Tokens land ONLY in the isolated secret with the exact shape.
        name = guild_source_secret_name(STAGE, "111", provider)
        assert name in secrets.store
        stored = json.loads(secrets.store[name])
        assert stored["provider"] == provider
        assert stored["oauth_refresh_token"] == "REF-1"
        assert stored["pot_token"] == "POT-1"
        assert stored["pot_visitor_data"] == "VIS-1"
        assert stored["connected_by"] == "admin-sub"
        # DynamoDB item is metadata-only (no token material) — R3.3.
        item = core.get(guild_pk("111"), source_sk(provider))
        assert item is not None
        serialized = json.dumps(item)
        assert "REF-1" not in serialized
        assert "POT-1" not in serialized
        assert "VIS-1" not in serialized
        assert "oauth_refresh_token" not in serialized

    def test_callback_clear_error_when_compose_empty(self, monkeypatch):
        """potoken-server down -> visible error, nothing stored (no no-op)."""
        monkeypatch.setattr(
            ste, "_http_post_form", lambda *a, **k: {"refresh_token": "REF"}
        )
        monkeypatch.setattr(ste, "_http_post_json", lambda *a, **k: {})
        svc, _core, secrets = _service()
        app = _make_app(svc)
        client = _admin_client(app)

        resp = client.get(
            "/auth/sources/111/youtube/callback?code=4/0Ac&state=st8"
        )

        assert resp.status_code in (301, 302)
        location = resp.headers.get("Location", "")
        assert "error=youtube_connect_failed" in location
        assert "provider=youtube" in location
        # Nothing partial stored.
        assert guild_source_secret_name(STAGE, "111", "youtube") not in secrets.store

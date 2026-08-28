"""Task 5 wiring tests: OAuth callbacks -> encrypted DynamoDB credential store.

Feature: unified-oauth-and-token-watchdog.

Verifies the web-ui connect/callback routes persist a *successfully exchanged*
provider token into the unified :class:`SourceCredentialService` (encrypted
DynamoDB) IN ADDITION to the existing per-guild Secrets Manager write, keyed by
the connecting user's Cognito subject — the owner (R1.4, R2.1). Also verifies
the two reject paths store NOTHING: a state mismatch (R1.5) and an exchange
failure (R1.6, surfacing ``<provider>_connect_failed``).

The unified store is a REAL :class:`SourceCredentialService` over an in-memory
``CoreTable`` fake + an envelope ``FakeKms`` (real AES-GCM), so the tests assert
the stored item is genuinely encrypted and contains NO plaintext token — not
merely that a mock was called. All provider HTTP is faked via the
``source_token_exchange`` seams (no live Google / Spotify / potoken / AWS).

Validates: Requirements 1.3, 1.4, 1.5, 1.6, 2.6
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

import pytest
from hellodj_platform_logic.data_access import CoreTable

import source_token_exchange as ste
from app import create_app
from source_credential_service import (
    REFRESH_STATUS_OK,
    SourceCredentialService,
    sourcecred_sk,
    user_pk,
)

STAGE = "beta"
_SUB = "owner-sub-42"

# Recognizable secrets so "no plaintext leak" assertions are unambiguous.
_YT_REFRESH = "1//0g-YT-REFRESH-secret"
_YT_POT = "MnQ-POT-secret"
_YT_VISITOR = "Cgs-VISITOR-secret"
_SP_REFRESH = "AQD-SPOTIFY-REFRESH-secret"
_SP_ACCESS = "BQC-SPOTIFY-ACCESS-secret"


# ── Fake KMS (envelope semantics) + CoreTable-backing table ────────────────

_WRAP_PREFIX = b"wrapped::"


@dataclass
class FakeKms:
    """Deterministic in-process KMS modeling envelope wrap/unwrap (no AWS)."""

    key_id: str = "arn:aws:kms:us-east-1:000000000000:key/source-creds"

    def generate_data_key(self, **kwargs: Any) -> dict[str, Any]:
        plaintext = os.urandom(32)
        return {
            "Plaintext": plaintext,
            "CiphertextBlob": _WRAP_PREFIX + plaintext,
            "KeyId": kwargs.get("KeyId", self.key_id),
        }

    def decrypt(self, **kwargs: Any) -> dict[str, Any]:
        blob = kwargs["CiphertextBlob"]
        return {"Plaintext": blob[len(_WRAP_PREFIX):]}


class _ClientError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


@dataclass
class _FakeTable:
    """In-memory ``TableLike`` with the create/version condition guards."""

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


# ── Per-guild GuildSourcesService fakes (legacy secret path stays wired) ────


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


@dataclass
class _Ctx:
    app: Any
    core: CoreTable
    table: _FakeTable
    creds: SourceCredentialService


def _make_ctx() -> _Ctx:
    """A degraded-mode app with the unified store + legacy source service wired."""
    from guild_sources import GuildSourcesService

    table = _FakeTable()
    core = CoreTable(table)
    kms = FakeKms()
    creds = SourceCredentialService(core, kms, kms.key_id)
    app = create_app(
        overrides={
            "TESTING": True,
            "SECRET_KEY": "test-secret",
            "HELLODJ_STAGE": STAGE,
            "PUBLIC_BASE_URL": "https://beta.example.test",
            "GOOGLE_CLIENT_ID": "google-client-abc",
            "GOOGLE_CLIENT_SECRET": "google-secret-xyz",
            "SPOTIFY_CLIENT_ID": "spotify-client-abc",
            "SPOTIFY_CLIENT_SECRET": "spotify-secret-xyz",
            "POTOKEN_SERVER_URL": "http://potoken.test:4416",
            "TIDAL_STREAM_URL": "http://tidal-stream.test:8801",
        }
    )
    app.extensions["source_credentials"] = creds
    app.extensions["guild_sources"] = GuildSourcesService(
        core, _FakeSecrets(), stage=STAGE
    )
    return _Ctx(app=app, core=core, table=table, creds=creds)


def _admin_client(app: Any) -> Any:
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["user"] = {"is_admin": True, "sub": _SUB}
        sess["source_state"] = "st8"
    return client


def _no_plaintext(table: _FakeTable, *secrets: str) -> None:
    """Assert no plaintext secret appears anywhere in the stored items."""
    serialized = json.dumps(list(table._items.values()))
    for secret in secrets:
        assert secret not in serialized


# ── R1.4 / R2.1: successful callback stores an encrypted credential item ────


class TestCallbackStoresEncryptedCredential:
    @pytest.mark.parametrize("provider", ["youtube", "youtube_music"])
    def test_youtube_callback_stores_encrypted_item(self, monkeypatch, provider):
        """A valid state+code YouTube callback persists an encrypted, keyed
        credential item with NO plaintext token (R1.4, R2.1)."""
        monkeypatch.setattr(
            ste, "_http_post_form", lambda *a, **k: {"refresh_token": _YT_REFRESH}
        )
        monkeypatch.setattr(
            ste,
            "_http_post_json",
            lambda *a, **k: {"poToken": _YT_POT, "contentBinding": _YT_VISITOR},
        )
        ctx = _make_ctx()
        client = _admin_client(ctx.app)

        resp = client.get(
            f"/auth/sources/111/{provider}/callback?code=4/0Ac&state=st8"
        )

        assert resp.status_code in (301, 302)
        # The unified item exists, keyed by the owner's sub, entity-typed.
        item = ctx.core.get(user_pk(_SUB), sourcecred_sk(provider))
        assert item is not None
        assert item["entityType"] == "SourceCredential"
        data = item["data"]
        assert data["connected"] is True
        assert data["refresh_status"] == REFRESH_STATUS_OK
        assert data["enc_blob"] and data["enc_key"] and data["enc_nonce"]
        # The decrypted blob round-trips the exact token material.
        loaded = ctx.creds.load_token(_SUB, provider)
        assert loaded is not None
        assert loaded.refresh_token == _YT_REFRESH
        assert loaded.extra["pot_token"] == _YT_POT
        assert loaded.extra["pot_visitor_data"] == _YT_VISITOR
        # No plaintext token anywhere in the stored item (R2.3).
        _no_plaintext(ctx.table, _YT_REFRESH, _YT_POT, _YT_VISITOR)

    def test_spotify_callback_stores_encrypted_item(self, monkeypatch):
        """A valid Spotify callback persists an encrypted credential (R1.4)."""
        monkeypatch.setattr(
            ste,
            "_http_post_form",
            lambda *a, **k: {
                "refresh_token": _SP_REFRESH,
                "access_token": _SP_ACCESS,
                "expires_in": 3600,
                "scope": "streaming",
            },
        )
        ctx = _make_ctx()
        client = _admin_client(ctx.app)

        resp = client.get(
            "/auth/sources/111/spotify/callback?code=sp-code&state=st8"
        )

        assert resp.status_code in (301, 302)
        item = ctx.core.get(user_pk(_SUB), sourcecred_sk("spotify"))
        assert item is not None
        loaded = ctx.creds.load_token(_SUB, "spotify")
        assert loaded is not None
        assert loaded.refresh_token == _SP_REFRESH
        assert loaded.access_token == _SP_ACCESS
        assert loaded.scope == "streaming"
        assert loaded.expires_at > 0
        _no_plaintext(ctx.table, _SP_REFRESH, _SP_ACCESS)

    def test_tidal_callback_records_status_no_token(self, monkeypatch):
        """The Tidal callback records connection status (no token) + forwards."""
        ctx = _make_ctx()
        client = _admin_client(ctx.app)

        resp = client.get("/auth/tidal/callback?code=td-code&state=whatever")

        assert resp.status_code in (301, 302)
        # Forwarded to the sidecar (Tidal token lifecycle stays there).
        assert "tidal-stream.test" in resp.headers.get("Location", "")
        # Status item present, marked connected, but carries no Tidal token.
        item = ctx.core.get(user_pk(_SUB), sourcecred_sk("tidal"))
        assert item is not None
        assert item["data"]["connected"] is True
        loaded = ctx.creds.load_token(_SUB, "tidal")
        assert loaded is not None
        assert loaded.refresh_token == ""


# ── R1.5: state mismatch rejected, NOTHING stored ──────────────────────────


class TestStateMismatchRejected:
    def test_youtube_state_mismatch_stores_nothing(self, monkeypatch):
        """A missing/mismatched state rejects the callback with no store (R1.5)."""
        exchange_called = {"n": 0}

        def fake_form(*a, **k):
            exchange_called["n"] += 1
            return {"refresh_token": _YT_REFRESH}

        monkeypatch.setattr(ste, "_http_post_form", fake_form)
        monkeypatch.setattr(
            ste,
            "_http_post_json",
            lambda *a, **k: {"poToken": _YT_POT, "contentBinding": _YT_VISITOR},
        )
        ctx = _make_ctx()
        client = _admin_client(ctx.app)

        # state=WRONG does not match the session's "st8".
        resp = client.get(
            "/auth/sources/111/youtube/callback?code=4/0Ac&state=WRONG"
        )

        assert resp.status_code in (301, 302)
        # No exchange attempted and no unified item written.
        assert exchange_called["n"] == 0
        assert ctx.core.get(user_pk(_SUB), sourcecred_sk("youtube")) is None
        _no_plaintext(ctx.table, _YT_REFRESH, _YT_POT, _YT_VISITOR)


# ── R1.6: exchange failure surfaces <provider>_connect_failed, no partial ──


class TestExchangeFailureStoresNothing:
    def test_youtube_exchange_failure_surfaces_error_no_store(self, monkeypatch):
        """Refresh-token-less exchange -> ``youtube_connect_failed`` and no
        unified credential item is written (R1.6)."""
        # Exchange returns no refresh token -> compose_youtube_tokens -> {}.
        monkeypatch.setattr(ste, "_http_post_form", lambda *a, **k: {})
        monkeypatch.setattr(
            ste,
            "_http_post_json",
            lambda *a, **k: {"poToken": _YT_POT, "contentBinding": _YT_VISITOR},
        )
        ctx = _make_ctx()
        client = _admin_client(ctx.app)

        resp = client.get(
            "/auth/sources/111/youtube/callback?code=4/0Ac&state=st8"
        )

        location = resp.headers.get("Location", "")
        assert "error=youtube_connect_failed" in location
        assert ctx.core.get(user_pk(_SUB), sourcecred_sk("youtube")) is None

    def test_spotify_exchange_failure_surfaces_error_no_store(self, monkeypatch):
        """No Spotify refresh token -> ``spotify_connect_failed`` + no item."""
        monkeypatch.setattr(
            ste, "_http_post_form", lambda *a, **k: {"access_token": "only"}
        )
        ctx = _make_ctx()
        client = _admin_client(ctx.app)

        resp = client.get(
            "/auth/sources/111/spotify/callback?code=sp&state=st8"
        )

        location = resp.headers.get("Location", "")
        assert "error=spotify_connect_failed" in location
        assert ctx.core.get(user_pk(_SUB), sourcecred_sk("spotify")) is None


# ── Degraded mode: no unified store wired -> legacy path only, no crash ─────


def test_degraded_mode_no_unified_store(monkeypatch):
    """With no ``source_credentials`` service wired the callback still succeeds
    via the legacy secret path and does not crash (R2.6)."""
    from guild_sources import GuildSourcesService

    monkeypatch.setattr(
        ste, "_http_post_form", lambda *a, **k: {"refresh_token": _YT_REFRESH}
    )
    monkeypatch.setattr(
        ste,
        "_http_post_json",
        lambda *a, **k: {"poToken": _YT_POT, "contentBinding": _YT_VISITOR},
    )
    table = _FakeTable()
    core = CoreTable(table)
    app = create_app(
        overrides={
            "TESTING": True,
            "SECRET_KEY": "s",
            "HELLODJ_STAGE": STAGE,
            "PUBLIC_BASE_URL": "https://beta.example.test",
            "GOOGLE_CLIENT_ID": "id",
            "GOOGLE_CLIENT_SECRET": "sec",
            "POTOKEN_SERVER_URL": "http://potoken.test:4416",
        }
    )
    app.extensions["source_credentials"] = None
    app.extensions["guild_sources"] = GuildSourcesService(
        core, _FakeSecrets(), stage=STAGE
    )
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["user"] = {"is_admin": True, "sub": _SUB}
        sess["source_state"] = "st8"

    resp = client.get("/auth/sources/111/youtube/callback?code=c&state=st8")

    assert resp.status_code in (301, 302)
    # No unified item (degraded), but no crash and the redirect is clean.
    assert core.get(user_pk(_SUB), sourcecred_sk("youtube")) is None
    assert "error=" not in resp.headers.get("Location", "")

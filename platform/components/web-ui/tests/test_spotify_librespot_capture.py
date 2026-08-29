"""Tests for the web-ui librespot reusable-credential capture (task 2.2).

Feature: multi-tenant-source-streaming. The Spotify data plane streams via
``librespot``, which can only build a per-user ``Session`` from a reusable
credential blob ``{username, credentials, type}``. librespot lives in the
``spotify-stream`` sidecar, so the web-ui ORCHESTRATES the capture over a small
HTTP contract and STORES the returned blob inside the SAME envelope-encrypted
Spotify token blob under ``extra.librespot_credentials`` — never a plaintext
column (R3.3, R6.4, R10.3).

Covered here (no live sidecar / AWS):

* ``SpotifyLibrespotCapture.start`` / ``.complete`` against a fake JSON transport
  (happy path, non-2xx, malformed/partial blob, degraded no-URL).
* ``valid_librespot_credentials`` acceptance/rejection.
* ``persist_librespot_credentials`` attaches the blob to a REAL
  ``SourceCredentialService`` (in-memory CoreTable + envelope FakeKms): the prior
  OAuth token is preserved, the blob round-trips through decrypt, and no
  plaintext blob leaks into the stored item.

Validates: Requirements 3.3, 6.4, 10.3
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

from hellodj_platform_logic.data_access import CoreTable
from hellodj_platform_logic.source_refresh import TokenState

from source_credential_service import SourceCredentialService, sourcecred_sk, user_pk
from source_credential_store import persist_librespot_credentials
from spotify_librespot_capture import (
    LIBRESPOT_CREDENTIALS_EXTRA_KEY,
    SpotifyLibrespotCapture,
    valid_librespot_credentials,
)

_SUB = "owner-sub-librespot"
_WRAP_PREFIX = b"wrapped::"

# A recognizable reusable blob so "no plaintext leak" assertions are unambiguous.
_BLOB = {
    "username": "canonical-user",
    "credentials": "REUSABLE-CREDS-SECRET-b64",
    "type": "AUTHENTICATION_STORED_SPOTIFY_CREDENTIALS",
}


# ── Envelope FakeKms + CoreTable-backing table ─────────────────────────────


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


def _service() -> tuple[SourceCredentialService, CoreTable, _FakeTable]:
    table = _FakeTable()
    core = CoreTable(table)
    kms = FakeKms()
    return SourceCredentialService(core, kms, kms.key_id), core, table


class _FakeTransport:
    """Records the last POST and returns a scripted ``(status, body)``."""

    def __init__(self, script: dict[str, tuple[int, dict[str, Any]]]) -> None:
        self._script = script
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, url: str, body: Any) -> tuple[int, dict[str, Any]]:
        self.calls.append((url, dict(body)))
        for suffix, response in self._script.items():
            if url.endswith(suffix):
                return response
        return 404, {}


# ── valid_librespot_credentials ────────────────────────────────────────────


class TestValidLibrespotCredentials:
    def test_accepts_complete_blob(self):
        assert valid_librespot_credentials(_BLOB) is True

    def test_rejects_non_mapping(self):
        assert valid_librespot_credentials("nope") is False
        assert valid_librespot_credentials(None) is False

    def test_rejects_missing_or_empty_field(self):
        assert valid_librespot_credentials({"username": "u", "credentials": "c"}) is False
        assert valid_librespot_credentials({**_BLOB, "credentials": ""}) is False


# ── SpotifyLibrespotCapture.start ──────────────────────────────────────────


class TestCaptureStart:
    def test_start_returns_authorize_url(self):
        transport = _FakeTransport(
            {"/auth/librespot/start": (200, {"authorize_url": "https://sp/auth?x=1"})}
        )
        capture = SpotifyLibrespotCapture("http://spotify-stream:8802", http_post=transport)
        url = capture.start(_SUB, "https://web/callback")
        assert url == "https://sp/auth?x=1"
        # The sub + redirect_uri are forwarded to the sidecar.
        assert transport.calls[0][1] == {"sub": _SUB, "redirect_uri": "https://web/callback"}

    def test_start_none_on_non_2xx(self):
        transport = _FakeTransport({"/auth/librespot/start": (502, {})})
        capture = SpotifyLibrespotCapture("http://s:8802", http_post=transport)
        assert capture.start(_SUB, "https://web/callback") is None

    def test_start_none_on_missing_url(self):
        transport = _FakeTransport({"/auth/librespot/start": (200, {"authorize_url": ""})})
        capture = SpotifyLibrespotCapture("http://s:8802", http_post=transport)
        assert capture.start(_SUB, "https://web/callback") is None

    def test_start_none_when_unconfigured(self):
        transport = _FakeTransport({})
        capture = SpotifyLibrespotCapture("", http_post=transport)
        assert capture.start(_SUB, "https://web/callback") is None
        assert transport.calls == []  # no POST attempted


# ── SpotifyLibrespotCapture.complete ───────────────────────────────────────


class TestCaptureComplete:
    def test_complete_returns_blob(self):
        transport = _FakeTransport(
            {"/auth/librespot/complete": (200, {"credentials": _BLOB})}
        )
        capture = SpotifyLibrespotCapture("http://s:8802", http_post=transport)
        creds = capture.complete(_SUB, "the-code")
        assert creds == _BLOB
        assert transport.calls[0][1] == {"sub": _SUB, "code": "the-code"}

    def test_complete_none_on_malformed_blob(self):
        transport = _FakeTransport(
            {"/auth/librespot/complete": (200, {"credentials": {"username": "u"}})}
        )
        capture = SpotifyLibrespotCapture("http://s:8802", http_post=transport)
        assert capture.complete(_SUB, "the-code") is None

    def test_complete_none_on_non_2xx(self):
        transport = _FakeTransport({"/auth/librespot/complete": (500, {})})
        capture = SpotifyLibrespotCapture("http://s:8802", http_post=transport)
        assert capture.complete(_SUB, "the-code") is None

    def test_complete_none_on_empty_code(self):
        transport = _FakeTransport({"/auth/librespot/complete": (200, {"credentials": _BLOB})})
        capture = SpotifyLibrespotCapture("http://s:8802", http_post=transport)
        assert capture.complete(_SUB, "") is None
        assert transport.calls == []


# ── persist_librespot_credentials (attach into the encrypted Spotify blob) ──


class TestPersistLibrespotCredentials:
    def test_attach_preserves_oauth_token_and_stores_blob(self):
        svc, core, table = _service()
        # Standard Spotify OAuth connect already stored a Web-API credential.
        svc.store(
            _SUB,
            "spotify",
            TokenState(
                access_token="AC-CE-SS",
                refresh_token="RE-FRE-SH",
                expires_at=123.0,
                scope="streaming",
            ),
            connected_by=_SUB,
        )

        ok = persist_librespot_credentials(svc, _SUB, _BLOB)
        assert ok is True

        loaded = svc.load_token(_SUB, "spotify")
        assert loaded is not None
        # The prior OAuth token is preserved verbatim.
        assert loaded.access_token == "AC-CE-SS"
        assert loaded.refresh_token == "RE-FRE-SH"
        assert loaded.scope == "streaming"
        # The librespot blob is attached under extra, round-tripping intact.
        assert loaded.extra[LIBRESPOT_CREDENTIALS_EXTRA_KEY] == _BLOB
        # It never appears in plaintext anywhere in the stored item.
        serialized = json.dumps(list(table._items.values()))
        assert _BLOB["credentials"] not in serialized
        assert "REUSABLE-CREDS-SECRET" not in serialized

    def test_attach_without_prior_spotify_credential_returns_false(self):
        svc, _core, table = _service()
        assert persist_librespot_credentials(svc, _SUB, _BLOB) is False
        # Nothing was written (no token-less item).
        assert core_get_absent(table)

    def test_degraded_no_service_is_false(self):
        assert persist_librespot_credentials(None, _SUB, _BLOB) is False

    def test_empty_blob_is_false(self):
        svc, _core, _table = _service()
        assert persist_librespot_credentials(svc, _SUB, {}) is False


def core_get_absent(table: _FakeTable) -> bool:
    """Return whether the Spotify credential item is absent from ``table``."""
    return (user_pk(_SUB), sourcecred_sk("spotify")) not in table._items

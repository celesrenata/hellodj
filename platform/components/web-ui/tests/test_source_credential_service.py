"""Tests for :class:`SourceCredentialService` over a fake ``CoreTable`` + KMS.

Feature: unified-oauth-and-token-watchdog (task 4).

Exercises the unified per-user source-credential store:

* **store -> status(no decrypt) -> load(decrypt) -> disconnect** round-trip:
  a stored credential round-trips its :class:`TokenState` through envelope
  encryption; ``status``/``status_for`` return the plaintext status WITHOUT
  decrypting and NEVER expose a token value (R2.1, R2.2, R2.3); ``disconnect``
  deletes only that provider's item (R2.5).
* **near-expiry enumeration** yields only items whose ``expires_at`` is within
  the threshold, carrying identity + plaintext status but never a decrypted
  blob (R5.2 access pattern for the watchdog).
* **record_refresh** write-back: the success path re-encrypts a new blob and
  sets ``refresh_status="ok"`` + new ``expires_at``; the failure path sets
  ``refresh_status="failed"`` + a short reason and LEAVES THE PRIOR BLOB INTACT
  (R5.4); and the write-back rides the optimistic lock so a concurrent writer
  cannot corrupt the item (R5.5).

The tests inject an in-memory ``TableLike`` fake (get/put/query/delete/scan with
the create/version ``ConditionExpression`` guards and the nested-attr
``ProjectionExpression`` the ``scan_entity`` layer emits) plus the envelope
``FakeKms`` from the shared ``token_crypto`` tests — no AWS, real AES-GCM.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

import pytest
from hellodj_platform_logic.data_access import CoreTable
from hellodj_platform_logic.source_refresh import TokenState

from source_credential_service import (
    REFRESH_STATUS_FAILED,
    REFRESH_STATUS_OK,
    SOURCECRED_ENTITY_TYPE,
    NearExpiryCredential,
    SourceCredentialService,
    sourcecred_sk,
    user_pk,
)

_USER = "user-sub-1"
_OTHER = "user-sub-2"

# A recognizable secret so "no token leak" assertions are unambiguous.
_SECRET_RT = "SUPER-SECRET-REFRESH-abc123"
_SECRET_AT = "SUPER-SECRET-ACCESS-xyz789"


# ---------------------------------------------------------------------------
# Fake KMS modeling envelope semantics (mirrors token_crypto tests)
# ---------------------------------------------------------------------------

_WRAP_PREFIX = b"wrapped::"


@dataclass
class FakeKms:
    """Deterministic in-process KMS that models envelope wrap/unwrap (no AWS)."""

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
        assert isinstance(blob, bytes)
        if not blob.startswith(_WRAP_PREFIX):
            raise ValueError("invalid ciphertext blob")
        return {"Plaintext": blob[len(_WRAP_PREFIX):]}


# ---------------------------------------------------------------------------
# Fake CoreTable-backing table (get/put/query/delete/scan + projection)
# ---------------------------------------------------------------------------


class _ClientError(Exception):
    """Minimal botocore-shaped client error for the fake table."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class _InjectedError(Exception):
    """A non-conditional datastore failure injected into a write."""


@dataclass
class _FakeTable:
    """In-memory ``TableLike`` implementing the surface ``CoreTable`` calls.

    Supports the create/version ``ConditionExpression`` guards, PK/SK-prefix
    ``query``, ``delete_item``, and a paginated ``scan`` honouring the nested
    ``ProjectionExpression`` the ``scan_entity`` layer emits. A ``fail_put`` hook
    lets a test inject a datastore failure on a matching write.
    """

    _items: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    fail_put: Any = None
    scan_page_size: int | None = None

    # -- reads --
    def get_item(self, **kwargs: Any) -> dict[str, Any]:
        key = (kwargs["Key"]["PK"], kwargs["Key"]["SK"])
        item = self._items.get(key)
        return {"Item": dict(item)} if item is not None else {}

    def query(self, **kwargs: Any) -> dict[str, Any]:
        values = kwargs.get("ExpressionAttributeValues", {})
        pk = values[":pk"]
        prefix = values.get(":skp")
        items = [
            dict(item)
            for (item_pk, item_sk), item in self._items.items()
            if item_pk == pk and (prefix is None or item_sk.startswith(prefix))
        ]
        return {"Items": items}

    def scan(self, **kwargs: Any) -> dict[str, Any]:
        names = kwargs.get("ExpressionAttributeNames", {})
        values = kwargs.get("ExpressionAttributeValues", {})
        ordered = list(self._items.values())
        filter_expr = kwargs.get("FilterExpression")
        if filter_expr is not None:
            assert filter_expr == "#et = :et"
            wanted = values[":et"]
            ordered = [it for it in ordered if it.get("entityType") == wanted]
        start = kwargs.get("ExclusiveStartKey")
        if start is not None:
            start_key = (start["PK"], start["SK"])
            keys = [(it["PK"], it["SK"]) for it in ordered]
            ordered = ordered[keys.index(start_key) + 1:]
        last_key: dict[str, Any] | None = None
        if self.scan_page_size is not None and len(ordered) > self.scan_page_size:
            ordered = ordered[: self.scan_page_size]
            tail = ordered[-1]
            last_key = {"PK": tail["PK"], "SK": tail["SK"]}
        projection = kwargs.get("ProjectionExpression")
        items = [self._project(it, projection, names) for it in ordered]
        response: dict[str, Any] = {"Items": items}
        if last_key is not None:
            response["LastEvaluatedKey"] = last_key
        return response

    @staticmethod
    def _project(
        item: dict[str, Any],
        projection: str | None,
        names: dict[str, str],
    ) -> dict[str, Any]:
        if not projection:
            return dict(item)
        out: dict[str, Any] = {}
        for raw in (part.strip() for part in projection.split(",")):
            resolved = names.get(raw, raw)
            if "." in raw:
                top_alias, sub = raw.split(".", 1)
                top = names.get(top_alias, top_alias)
                sub_name = names.get(sub, sub)
                nested = item.get(top)
                if isinstance(nested, dict) and sub_name in nested:
                    out.setdefault(top, {})[sub_name] = nested[sub_name]
            elif resolved in item:
                out[resolved] = item[resolved]
        return out

    # -- writes --
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
        if self.fail_put is not None and self.fail_put(item["PK"], item["SK"], item):
            raise _InjectedError("injected datastore failure")
        self._items[key] = dict(item)
        return {}

    def delete_item(self, **kwargs: Any) -> dict[str, Any]:
        key = (kwargs["Key"]["PK"], kwargs["Key"]["SK"])
        self._items.pop(key, None)
        return {}


def _make(
    *, page_size: int | None = None
) -> tuple[SourceCredentialService, _FakeTable, FakeKms]:
    fake = _FakeTable(scan_page_size=page_size)
    kms = FakeKms()
    svc = SourceCredentialService(CoreTable(fake), kms, kms.key_id)
    return svc, fake, kms


def _token(expires_at: float, *, scope: str = "playback") -> TokenState:
    return TokenState(
        access_token=_SECRET_AT,
        refresh_token=_SECRET_RT,
        expires_at=expires_at,
        scope=scope,
        extra={"visitor_data": "vd-123"},
    )


# ---------------------------------------------------------------------------
# store -> status -> load -> disconnect
# ---------------------------------------------------------------------------


def test_store_then_load_round_trips_token() -> None:
    """A stored credential decrypts back to the original token (R2.1, R3.2)."""
    svc, _, _ = _make()

    svc.store(_USER, "spotify", _token(1000.0), connected_by=_USER)

    loaded = svc.load_token(_USER, "spotify")
    assert loaded is not None
    assert loaded.access_token == _SECRET_AT
    assert loaded.refresh_token == _SECRET_RT
    assert loaded.expires_at == 1000.0
    assert loaded.scope == "playback"
    assert loaded.extra == {"visitor_data": "vd-123"}


def test_status_reflects_plaintext_and_never_exposes_token() -> None:
    """``status``/``status_for`` return plaintext status, never a token (R2.2, R8.3)."""
    svc, fake, _ = _make()
    svc.store(_USER, "spotify", _token(1000.0), connected_by=_USER)

    view = svc.status_for(_USER, "spotify")
    assert view is not None
    assert view["provider"] == "spotify"
    assert view["connected"] is True
    assert view["expires_at"] == 1000.0
    assert view["scope"] == "playback"
    assert view["refresh_status"] == REFRESH_STATUS_OK
    # No token material or encrypted-blob fields ever surface in status.
    text = repr(view)
    assert _SECRET_RT not in text
    assert _SECRET_AT not in text
    for banned in ("enc_blob", "enc_key", "enc_nonce", "access_token", "refresh_token"):
        assert banned not in view

    # But the item DOES persist the encrypted blob (only, not plaintext).
    stored = fake._items[(user_pk(_USER), sourcecred_sk("spotify"))]["data"]
    assert stored["enc_blob"]
    assert _SECRET_RT not in stored["enc_blob"]
    assert _SECRET_AT not in stored["enc_blob"]
    assert "access_token" not in stored


def test_status_lists_all_user_providers_sorted() -> None:
    """``status`` enumerates every provider the user has connected."""
    svc, _, _ = _make()
    svc.store(_USER, "spotify", _token(1000.0), connected_by=_USER)
    svc.store(_USER, "youtube", _token(2000.0), connected_by=_USER)

    providers = [row["provider"] for row in svc.status(_USER)]
    assert providers == ["spotify", "youtube"]


def test_status_for_absent_provider_is_none() -> None:
    """No stored credential yields ``None`` status and ``None`` token."""
    svc, _, _ = _make()
    assert svc.status_for(_USER, "tidal") is None
    assert svc.load_token(_USER, "tidal") is None


def test_disconnect_deletes_only_that_provider() -> None:
    """``disconnect`` removes one provider's item and nothing else (R2.5)."""
    svc, _, _ = _make()
    svc.store(_USER, "spotify", _token(1000.0), connected_by=_USER)
    svc.store(_USER, "youtube", _token(2000.0), connected_by=_USER)

    svc.disconnect(_USER, "spotify")

    assert svc.status_for(_USER, "spotify") is None
    assert svc.load_token(_USER, "spotify") is None
    # The other provider is untouched.
    assert svc.status_for(_USER, "youtube") is not None


def test_store_preserves_connected_at_on_restore() -> None:
    """Re-storing a provider keeps the original ``connected_at`` but advances
    ``updated_at`` and ``expires_at``."""
    times = iter([100.0, 250.0])
    fake = _FakeTable()
    kms = FakeKms()
    svc = SourceCredentialService(
        CoreTable(fake), kms, kms.key_id, clock=lambda: next(times)
    )

    svc.store(_USER, "spotify", _token(1000.0), connected_by=_USER)
    svc.store(_USER, "spotify", _token(3000.0), connected_by=_USER)

    view = svc.status_for(_USER, "spotify")
    assert view is not None
    assert view["connected_at"] == 100.0
    assert view["updated_at"] == 250.0
    assert view["expires_at"] == 3000.0


# ---------------------------------------------------------------------------
# near-expiry enumeration
# ---------------------------------------------------------------------------


def test_iter_near_expiry_yields_only_within_threshold() -> None:
    """Only credentials whose ``expires_at`` is within the threshold are yielded
    (R5.2), carrying identity + status but no decrypted token."""
    svc, _, _ = _make()
    now = 1_000.0
    threshold = 300.0
    # Expires soon (within threshold) -> yielded.
    svc.store(_USER, "spotify", _token(now + 100.0), connected_by=_USER)
    # Expires far in the future -> NOT yielded.
    svc.store(_USER, "youtube", _token(now + 10_000.0), connected_by=_USER)
    # Already expired -> yielded (needs refresh).
    svc.store(_OTHER, "tidal", _token(now - 50.0), connected_by=_OTHER)

    near = list(svc.iter_near_expiry(now, threshold))
    got = {(c.sub, c.provider) for c in near}
    assert got == {(_USER, "spotify"), (_OTHER, "tidal")}
    # Each yielded record carries identity + plaintext status only.
    for c in near:
        assert isinstance(c, NearExpiryCredential)
        assert c.refresh_status == REFRESH_STATUS_OK
        assert _SECRET_RT not in repr(c)
        assert _SECRET_AT not in repr(c)


def test_iter_near_expiry_paginates() -> None:
    """Enumeration walks every page of a paginated scan (R5.2)."""
    svc, _, _ = _make(page_size=2)
    now = 1_000.0
    for i in range(5):
        svc.store(f"u{i}", "spotify", _token(now + 10.0), connected_by=f"u{i}")

    near = list(svc.iter_near_expiry(now, 300.0))
    assert len(near) == 5


def test_iter_near_expiry_ignores_other_entities() -> None:
    """Non-``SourceCredential`` items never appear in the enumeration."""
    svc, fake, _ = _make()
    now = 1_000.0
    svc.store(_USER, "spotify", _token(now + 10.0), connected_by=_USER)
    # A foreign entity in the same table must be filtered out by scan_entity.
    fake._items[("GUILD#1", "META")] = {
        "PK": "GUILD#1",
        "SK": "META",
        "entityType": "Guild",
        "data": {"expires_at": now, "refresh_status": "ok"},
        "version": 1,
    }

    near = list(svc.iter_near_expiry(now, 300.0))
    assert [(c.sub, c.provider) for c in near] == [(_USER, "spotify")]


# ---------------------------------------------------------------------------
# record_refresh write-back (success + failure + lock)
# ---------------------------------------------------------------------------


def test_record_refresh_success_writes_new_blob_and_status() -> None:
    """A successful refresh re-encrypts the new token and marks ``ok`` (R5.3)."""
    svc, _, _ = _make()
    svc.store(_USER, "spotify", _token(1000.0), connected_by=_USER)

    new = TokenState(
        access_token="NEW-ACCESS",
        refresh_token="NEW-REFRESH",
        expires_at=5000.0,
        scope="playback",
    )
    svc.record_refresh(_USER, "spotify", new_state=new)

    view = svc.status_for(_USER, "spotify")
    assert view is not None
    assert view["refresh_status"] == REFRESH_STATUS_OK
    assert view["expires_at"] == 5000.0
    assert view["last_refresh_at"] != 0
    # The decrypted blob reflects the refreshed token.
    loaded = svc.load_token(_USER, "spotify")
    assert loaded is not None
    assert loaded.access_token == "NEW-ACCESS"
    assert loaded.refresh_token == "NEW-REFRESH"
    assert loaded.expires_at == 5000.0


def test_record_refresh_failure_marks_failed_and_keeps_prior_blob() -> None:
    """A failed refresh sets ``failed`` + reason but leaves the prior blob
    intact so the next tick can retry (R5.4)."""
    svc, _, _ = _make()
    svc.store(_USER, "spotify", _token(1000.0), connected_by=_USER)

    svc.record_refresh(_USER, "spotify", error="token endpoint 400")

    view = svc.status_for(_USER, "spotify")
    assert view is not None
    assert view["refresh_status"] == REFRESH_STATUS_FAILED
    assert view["refresh_error"] == "token endpoint 400"
    # The prior encrypted blob is still decryptable to the original token.
    loaded = svc.load_token(_USER, "spotify")
    assert loaded is not None
    assert loaded.refresh_token == _SECRET_RT
    assert loaded.access_token == _SECRET_AT


def test_record_refresh_requires_state_or_error() -> None:
    """``record_refresh`` with neither outcome is a programming error."""
    svc, _, _ = _make()
    svc.store(_USER, "spotify", _token(1000.0), connected_by=_USER)
    with pytest.raises(ValueError):
        svc.record_refresh(_USER, "spotify")


def test_record_refresh_uses_optimistic_lock_version() -> None:
    """The write-back advances the item ``version`` (optimistic-lock write) so a
    concurrent replica cannot silently clobber it (R5.5)."""
    svc, fake, _ = _make()
    svc.store(_USER, "spotify", _token(1000.0), connected_by=_USER)
    key = (user_pk(_USER), sourcecred_sk("spotify"))
    v_before = fake._items[key]["version"]

    svc.record_refresh(
        _USER,
        "spotify",
        new_state=TokenState("a", "b", 5000.0),
    )

    assert fake._items[key]["version"] == v_before + 1
    assert fake._items[key]["entityType"] == SOURCECRED_ENTITY_TYPE


def test_record_refresh_commits_on_advanced_baseline() -> None:
    """When a peer advanced the item ``version`` before the write-back, the
    optimistic-lock read-modify-write re-reads the newer baseline and still
    commits the refresh (R5.5)."""
    svc, fake, _ = _make()
    svc.store(_USER, "spotify", _token(1000.0), connected_by=_USER)
    key = (user_pk(_USER), sourcecred_sk("spotify"))

    # Simulate a concurrent replica having advanced the stored version.
    fake._items[key]["version"] = 2

    svc.record_refresh(
        _USER, "spotify", new_state=TokenState("a", "b", 5000.0)
    )

    # The write committed on top of the advanced baseline (version 2 -> 3).
    assert fake._items[key]["version"] == 3
    loaded = svc.load_token(_USER, "spotify")
    assert loaded is not None and loaded.expires_at == 5000.0

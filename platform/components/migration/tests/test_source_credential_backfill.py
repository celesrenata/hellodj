"""Tests for the one-shot legacy-secret -> encrypted DynamoDB backfill.

Feature: unified-oauth-and-token-watchdog (task 11, Migration & Rollout step 3).

Exercises :class:`SourceCredentialBackfill` against a fake Secrets Manager
client (list + get), a fake ``CoreTable``-backing table (get/put/query/delete
with the create/version ``ConditionExpression`` guards), and the envelope
``FakeKms`` from the shared ``token_crypto`` tests (real AES-GCM, no AWS):

* **encrypted items written** — each legacy per-guild secret becomes a
  ``SourceCredential`` item under the guild OWNER's ``USER#<sub>`` partition; the
  stored item carries the envelope-encrypted blob (``enc_blob`` present) and NO
  plaintext token anywhere (R2.6).
* **idempotent re-run** — running the backfill twice does not duplicate or
  corrupt items: the item count is stable, ``connected_at`` is preserved while
  ``version`` advances, and the decrypted blob is unchanged (R2.6).
* **no-owner skip** — a guild with no ``GUILD#<gid>`` / ``OWNER`` item is skipped
  and counted (no user partition to write under); its item is never written.
* **no token material in output** — the :class:`BackfillResult` and the logs
  carry only counts, never a refresh token / access token / PoToken / secret
  string.
* **round-trip shape** — the backfilled blob decrypts (via the same
  ``token_crypto`` seam a reader uses) to the SAME ``TokenState`` shape a fresh
  connect would write (YouTube PoToken pair in ``extra``; Spotify refresh token).

The fakes mirror the web-ui ``test_source_credential_service`` fakes so the item
format is verified against the SAME storage contract a fresh connect uses.
"""

from __future__ import annotations

import base64
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any

from hellodj_platform_logic.data_access import CoreTable
from hellodj_platform_logic.token_crypto import EncryptedBlob, decrypt_blob
from migration_job.source_credential_backfill import (
    SOURCECRED_ENTITY_TYPE,
    SourceCredentialBackfill,
    sourcecred_sk,
    user_pk,
)

_STAGE = "beta"
_OWNER_A = "cognito-sub-owner-a"
_OWNER_B = "cognito-sub-owner-b"

# Recognizable secret material so "no token leak" assertions are unambiguous.
_YT_REFRESH = "YT-SECRET-REFRESH-abc123"
_YT_POT = "YT-SECRET-POT-def456"
_YT_VISITOR = "YT-SECRET-VISITOR-ghi789"
_SP_REFRESH = "SP-SECRET-REFRESH-xyz321"

_ALL_SECRETS = (_YT_REFRESH, _YT_POT, _YT_VISITOR, _SP_REFRESH)


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
# Fake CoreTable-backing table (get/put/query/delete + version guards)
# ---------------------------------------------------------------------------


class _ClientError(Exception):
    """Minimal botocore-shaped client error for the fake table."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


@dataclass
class _FakeTable:
    """In-memory ``TableLike`` implementing the surface ``CoreTable`` calls."""

    _items: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)

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
        key = (kwargs["Key"]["PK"], kwargs["Key"]["SK"])
        self._items.pop(key, None)
        return {}


# ---------------------------------------------------------------------------
# Fake Secrets Manager (list + get, paginated)
# ---------------------------------------------------------------------------


@dataclass
class _FakeSecrets:
    """In-memory ``secretsmanager`` fake: name-prefix ``list_secrets`` + get.

    ``store`` maps a full secret name to its ``SecretString`` payload. The
    ``list_secrets`` name filter honours the boto3 ``Filters=[{Key:name,...}]``
    prefix semantics, and pagination is emulated via ``page_size``.
    """

    store: dict[str, str] = field(default_factory=dict)
    page_size: int | None = None

    def list_secrets(self, **kwargs: Any) -> dict[str, Any]:
        prefix = ""
        for f in kwargs.get("Filters", []):
            if f.get("Key") == "name" and f.get("Values"):
                prefix = f["Values"][0]
        names = sorted(n for n in self.store if n.startswith(prefix))
        start = int(kwargs.get("NextToken", "0"))
        page = names[start:]
        next_token: str | None = None
        if self.page_size is not None and len(page) > self.page_size:
            page = page[: self.page_size]
            next_token = str(start + self.page_size)
        response: dict[str, Any] = {
            "SecretList": [{"Name": n} for n in page]
        }
        if next_token is not None:
            response["NextToken"] = next_token
        return response

    def get_secret_value(self, **kwargs: Any) -> dict[str, Any]:
        name = kwargs["SecretId"]
        if name not in self.store:
            raise _ClientError("ResourceNotFoundException")
        return {"SecretString": self.store[name]}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _guild_secret(guild_id: str, provider: str) -> str:
    return f"hellodj/{_STAGE}/guild/{guild_id}/{provider}"


def _seed_owner(table: _FakeTable, guild_id: str, owner_sub: str) -> None:
    table._items[(f"GUILD#{guild_id}", "OWNER")] = {
        "PK": f"GUILD#{guild_id}",
        "SK": "OWNER",
        "entityType": "GuildOwner",
        "data": {"owner_sub": owner_sub},
        "version": 1,
    }


def _make(secrets: _FakeSecrets) -> tuple[SourceCredentialBackfill, _FakeTable, FakeKms]:
    table = _FakeTable()
    kms = FakeKms()
    backfill = SourceCredentialBackfill(
        secrets, CoreTable(table), kms, kms.key_id, stage=_STAGE
    )
    return backfill, table, kms


def _decrypt_item(item: dict[str, Any], kms: FakeKms) -> dict[str, Any]:
    data = item["data"]
    enc = EncryptedBlob(
        ciphertext=base64.b64decode(data["enc_blob"]),
        wrapped_key=base64.b64decode(data["enc_key"]),
        key_id=data["kms_key_id"],
        nonce=base64.b64decode(data["enc_nonce"]),
    )
    return json.loads(decrypt_blob(enc, kms).decode("utf-8"))


def _no_plaintext(table: _FakeTable) -> None:
    """Assert no recognizable token material appears in any stored item."""
    serialized = json.dumps(
        {f"{pk}|{sk}": v for (pk, sk), v in table._items.items()}
    )
    for secret in _ALL_SECRETS:
        assert secret not in serialized


# ---------------------------------------------------------------------------
# backfill writes encrypted items
# ---------------------------------------------------------------------------


def test_backfill_writes_encrypted_items_for_each_secret() -> None:
    """Each legacy secret becomes an encrypted item under the owner (R2.6)."""
    secrets = _FakeSecrets(
        store={
            _guild_secret("g1", "youtube"): json.dumps(
                {
                    "oauth_refresh_token": _YT_REFRESH,
                    "pot_token": _YT_POT,
                    "pot_visitor_data": _YT_VISITOR,
                }
            ),
            _guild_secret("g2", "spotify"): json.dumps(
                {"refresh_token": _SP_REFRESH, "scope": "playback"}
            ),
        }
    )
    backfill, table, kms = _make(secrets)
    _seed_owner(table, "g1", _OWNER_A)
    _seed_owner(table, "g2", _OWNER_B)

    result = backfill.run()

    assert result.secrets_scanned == 2
    assert result.items_written == 2
    assert result.items_verified == 2
    assert result.skipped_no_owner == 0
    assert result.skipped_empty == 0

    # YouTube item under owner A.
    yt = table._items[(user_pk(_OWNER_A), sourcecred_sk("youtube"))]
    assert yt["entityType"] == SOURCECRED_ENTITY_TYPE
    assert yt["data"]["connected"] is True
    assert yt["data"]["enc_blob"]
    # No plaintext token in the item.
    assert "access_token" not in yt["data"]
    assert "refresh_token" not in yt["data"]

    # Spotify item under owner B.
    sp = table._items[(user_pk(_OWNER_B), sourcecred_sk("spotify"))]
    assert sp["data"]["enc_blob"]

    _no_plaintext(table)


def test_backfilled_youtube_blob_round_trips_to_fresh_connect_shape() -> None:
    """The encrypted blob decrypts to the same shape a fresh connect writes."""
    secrets = _FakeSecrets(
        store={
            _guild_secret("g1", "youtube"): json.dumps(
                {
                    "oauth_refresh_token": _YT_REFRESH,
                    "pot_token": _YT_POT,
                    "pot_visitor_data": _YT_VISITOR,
                }
            ),
        }
    )
    backfill, table, kms = _make(secrets)
    _seed_owner(table, "g1", _OWNER_A)

    backfill.run()

    blob = _decrypt_item(
        table._items[(user_pk(_OWNER_A), sourcecred_sk("youtube"))], kms
    )
    assert blob["refresh_token"] == _YT_REFRESH
    assert blob["access_token"] == ""
    assert blob["extra"]["pot_token"] == _YT_POT
    assert blob["extra"]["pot_visitor_data"] == _YT_VISITOR


def test_backfilled_spotify_blob_carries_refresh_token() -> None:
    secrets = _FakeSecrets(
        store={
            _guild_secret("g2", "spotify"): json.dumps(
                {"refresh_token": _SP_REFRESH, "scope": "playback"}
            ),
        }
    )
    backfill, table, kms = _make(secrets)
    _seed_owner(table, "g2", _OWNER_B)

    backfill.run()

    blob = _decrypt_item(
        table._items[(user_pk(_OWNER_B), sourcecred_sk("spotify"))], kms
    )
    assert blob["refresh_token"] == _SP_REFRESH
    assert blob["scope"] == "playback"


# ---------------------------------------------------------------------------
# idempotency
# ---------------------------------------------------------------------------


def test_rerun_is_idempotent_no_duplicates_or_corruption() -> None:
    """A second run does not duplicate/corrupt: item count + blob stable,
    connected_at preserved, version advances (R2.6)."""
    secrets = _FakeSecrets(
        store={
            _guild_secret("g1", "youtube"): json.dumps(
                {
                    "oauth_refresh_token": _YT_REFRESH,
                    "pot_token": _YT_POT,
                    "pot_visitor_data": _YT_VISITOR,
                }
            ),
        }
    )
    backfill, table, kms = _make(secrets)
    _seed_owner(table, "g1", _OWNER_A)

    backfill.run()
    key = (user_pk(_OWNER_A), sourcecred_sk("youtube"))
    first = dict(table._items[key])
    first_blob = _decrypt_item(first, kms)
    cred_items_after_first = [
        k for k in table._items if k[1].startswith("SOURCECRED#")
    ]

    result2 = backfill.run()

    # No new credential items were created (still exactly one).
    cred_items_after_second = [
        k for k in table._items if k[1].startswith("SOURCECRED#")
    ]
    assert cred_items_after_first == cred_items_after_second
    assert result2.items_written == 1

    second = table._items[key]
    # connected_at preserved across the re-run; version advanced (upsert).
    assert second["data"]["connected_at"] == first["data"]["connected_at"]
    assert second["version"] == first["version"] + 1
    # The decrypted token is unchanged (no corruption).
    assert _decrypt_item(second, kms) == first_blob


# ---------------------------------------------------------------------------
# no-owner skip
# ---------------------------------------------------------------------------


def test_guild_without_owner_is_skipped_and_counted() -> None:
    """A guild with no OWNER item is skipped + counted; nothing written."""
    secrets = _FakeSecrets(
        store={
            _guild_secret("g1", "youtube"): json.dumps(
                {"oauth_refresh_token": _YT_REFRESH}
            ),
            _guild_secret("g-orphan", "spotify"): json.dumps(
                {"refresh_token": _SP_REFRESH}
            ),
        }
    )
    backfill, table, kms = _make(secrets)
    _seed_owner(table, "g1", _OWNER_A)
    # g-orphan has NO owner item.

    result = backfill.run()

    assert result.secrets_scanned == 2
    assert result.items_written == 1
    assert result.skipped_no_owner == 1
    # The orphan guild's provider was never written under any user partition.
    assert not any(
        k[1] == sourcecred_sk("spotify") for k in table._items
    )
    _no_plaintext(table)


def test_empty_secret_is_skipped_and_counted() -> None:
    """A secret with no usable token material is skipped + counted."""
    secrets = _FakeSecrets(
        store={
            _guild_secret("g1", "youtube"): json.dumps({"pot_token": _YT_POT}),
        }
    )
    backfill, table, kms = _make(secrets)
    _seed_owner(table, "g1", _OWNER_A)

    result = backfill.run()

    assert result.items_written == 0
    assert result.skipped_empty == 1
    assert not any(k[1].startswith("SOURCECRED#") for k in table._items)


# ---------------------------------------------------------------------------
# pagination + no-leak-in-output
# ---------------------------------------------------------------------------


def test_backfill_paginates_secret_listing() -> None:
    """Every page of the paginated ``list_secrets`` is enumerated."""
    store = {}
    table = _FakeTable()
    for i in range(5):
        store[_guild_secret(f"g{i}", "spotify")] = json.dumps(
            {"refresh_token": f"{_SP_REFRESH}-{i}"}
        )
        _seed_owner(table, f"g{i}", f"owner-{i}")
    secrets = _FakeSecrets(store=store, page_size=2)
    kms = FakeKms()
    backfill = SourceCredentialBackfill(
        secrets, CoreTable(table), kms, kms.key_id, stage=_STAGE
    )

    result = backfill.run()

    assert result.secrets_scanned == 5
    assert result.items_written == 5


def test_result_and_logs_carry_no_token_material(caplog) -> None:
    """The result repr + emitted logs never contain token material."""
    secrets = _FakeSecrets(
        store={
            _guild_secret("g1", "youtube"): json.dumps(
                {
                    "oauth_refresh_token": _YT_REFRESH,
                    "pot_token": _YT_POT,
                    "pot_visitor_data": _YT_VISITOR,
                }
            ),
            _guild_secret("g-orphan", "spotify"): json.dumps(
                {"refresh_token": _SP_REFRESH}
            ),
        }
    )
    backfill, table, kms = _make(secrets)
    _seed_owner(table, "g1", _OWNER_A)

    with caplog.at_level(logging.DEBUG, logger="migration_job"):
        result = backfill.run()

    text = repr(result) + "\n" + caplog.text
    for secret in _ALL_SECRETS:
        assert secret not in text

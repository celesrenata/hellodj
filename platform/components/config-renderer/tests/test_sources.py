"""Tests for the Secrets Manager and DynamoDB config sources.

Secrets Manager parsing is tested with an injected fake client (no AWS libs
required). The DynamoDB config source is tested against moto when available,
falling back to a lightweight fake ``Table`` otherwise so the suite runs in any
environment.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from config_renderer.config_source import (
    LAVALINK_CONFIG_PK,
    LAVALINK_CONFIG_SK,
    DynamoConfigSource,
)
from config_renderer.model import LavalinkSettings
from config_renderer.secrets_source import SecretsManagerCredentialSource
from hellodj_platform_logic.data_access import CoreTable


class _FakeSecretsClient:
    def __init__(self, payload: str) -> None:
        self._payload = payload
        self.requested_id: str | None = None

    def get_secret_value(self, **kwargs: Any) -> dict[str, Any]:
        self.requested_id = kwargs.get("SecretId")
        return {"SecretString": self._payload}


def test_secrets_source_parses_json_string() -> None:
    payload = json.dumps({"spotify_client_id": "sid", "tidal_token": "tt"})
    client = _FakeSecretsClient(payload)
    source = SecretsManagerCredentialSource("my-secret", client=client)
    creds = source.load()
    assert client.requested_id == "my-secret"
    assert creds.spotify_client_id == "sid"
    assert creds.tidal_token == "tt"


def test_secrets_source_rejects_non_object_json() -> None:
    client = _FakeSecretsClient(json.dumps(["not", "an", "object"]))
    source = SecretsManagerCredentialSource("s", client=client)
    with pytest.raises(ValueError):
        source.load()


def test_secrets_source_rejects_invalid_json() -> None:
    client = _FakeSecretsClient("{not json")
    source = SecretsManagerCredentialSource("s", client=client)
    with pytest.raises(ValueError):
        source.load()


def test_secrets_source_requires_secret_id() -> None:
    with pytest.raises(ValueError):
        SecretsManagerCredentialSource("", client=_FakeSecretsClient("{}"))


# -- DynamoDB config source ------------------------------------------------


class _FakeTable:
    """Minimal in-memory TableLike storing items keyed by (PK, SK)."""

    def __init__(self) -> None:
        self._items: dict[tuple[str, str], dict[str, Any]] = {}

    def put_item(self, **kwargs: Any) -> dict[str, Any]:
        item = kwargs["Item"]
        self._items[(item["PK"], item["SK"])] = dict(item)
        return {}

    def get_item(self, **kwargs: Any) -> dict[str, Any]:
        key = kwargs["Key"]
        item = self._items.get((key["PK"], key["SK"]))
        return {"Item": dict(item)} if item is not None else {}

    def update_item(self, **kwargs: Any) -> dict[str, Any]:
        return {}

    def query(self, **kwargs: Any) -> dict[str, Any]:
        return {"Items": []}


def test_config_source_uses_defaults_when_absent() -> None:
    source = DynamoConfigSource(CoreTable(_FakeTable()))
    settings = source.load()
    assert settings == LavalinkSettings()


def test_config_source_reads_stored_config() -> None:
    table = _FakeTable()
    core = CoreTable(table)
    core.put_new(
        LAVALINK_CONFIG_PK,
        LAVALINK_CONFIG_SK,
        "Config",
        {"port": 2444, "tidal_country_code": "GB"},
    )
    settings = DynamoConfigSource(core).load()
    assert settings.port == 2444
    assert settings.tidal_country_code == "GB"


def test_config_source_moto_roundtrip() -> None:
    moto = pytest.importorskip("moto")
    boto3 = pytest.importorskip("boto3")
    with moto.mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        resource.create_table(
            TableName="hellodj-core",
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        core = CoreTable(resource.Table("hellodj-core"))
        core.put_new(
            LAVALINK_CONFIG_PK,
            LAVALINK_CONFIG_SK,
            "Config",
            {"spotify_country_code": "DE"},
        )
        settings = DynamoConfigSource(core).load()
        assert settings.spotify_country_code == "DE"

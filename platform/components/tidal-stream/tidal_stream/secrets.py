"""AWS Secrets Manager persistence for the Tidal refresh token.

The Tidal refresh token is the long-lived credential that the first-party
single-app-id OAuth integration uses to mint fresh access tokens. It is stored
in AWS Secrets Manager (never in the datastore or environment) and read at
runtime by the token manager.

``boto3`` is imported lazily inside the client factory so this module stays
import-safe in environments without AWS libraries and fully unit-testable with
an injected fake client (mirrors the config-renderer secrets source).

Requirements: 9.2, 9.4, 15.1
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol

__all__ = [
    "SecretsClient",
    "StoredTidalToken",
    "TidalRefreshTokenStore",
    "build_secrets_client",
]


class SecretsClient(Protocol):
    """Minimal subset of the boto3 Secrets Manager client interface."""

    def get_secret_value(self, **kwargs: Any) -> dict[str, Any]:
        """Return a response dict with a ``SecretString`` or ``SecretBinary``."""
        ...

    def put_secret_value(self, **kwargs: Any) -> dict[str, Any]:
        """Store a new version of the secret's value."""
        ...


def build_secrets_client(region_name: str | None = None) -> SecretsClient:
    """Create a real boto3 Secrets Manager client (imported lazily)."""
    import boto3

    return boto3.client("secretsmanager", region_name=region_name)


@dataclass(frozen=True)
class StoredTidalToken:
    """Persisted Tidal token payload.

    Attributes:
        access_token: The current access token (may be empty on first store).
        refresh_token: The long-lived refresh token.
        expires_at: Absolute expiry as epoch seconds.
    """

    access_token: str
    refresh_token: str
    expires_at: float

    def to_payload(self) -> dict[str, Any]:
        """Serialize to the JSON payload stored in Secrets Manager."""
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> StoredTidalToken:
        """Parse a stored JSON payload into a typed token."""
        refresh_token = str(payload.get("refresh_token", "") or "")
        if not refresh_token:
            raise ValueError("stored Tidal secret is missing refresh_token")
        try:
            expires_at = float(payload.get("expires_at", 0) or 0)
        except (TypeError, ValueError) as error:
            raise ValueError("stored Tidal secret has non-numeric expires_at") from error
        return cls(
            access_token=str(payload.get("access_token", "") or ""),
            refresh_token=refresh_token,
            expires_at=expires_at,
        )


def _parse_secret_payload(response: dict[str, Any]) -> dict[str, Any]:
    """Extract and JSON-parse the secret payload from a boto3 response."""
    secret_string = response.get("SecretString")
    if secret_string is None:
        secret_binary = response.get("SecretBinary")
        if secret_binary is None:
            raise ValueError("secret contains neither SecretString nor SecretBinary")
        secret_string = (
            secret_binary.decode("utf-8")
            if isinstance(secret_binary, bytes | bytearray)
            else str(secret_binary)
        )
    try:
        parsed = json.loads(secret_string)
    except json.JSONDecodeError as error:
        raise ValueError(f"secret is not valid JSON: {error}") from error
    if not isinstance(parsed, dict):
        raise ValueError("secret JSON must be an object of token fields")
    return parsed


class TidalRefreshTokenStore:
    """Loads and persists the Tidal refresh token in Secrets Manager.

    Args:
        secret_id: The secret name or ARN holding the JSON token payload.
        client: An injected Secrets Manager client. Defaults to a real boto3
            client created via :func:`build_secrets_client`.
        region_name: Region used when creating the default client.
    """

    def __init__(
        self,
        secret_id: str,
        *,
        client: SecretsClient | None = None,
        region_name: str | None = None,
    ) -> None:
        if not secret_id:
            raise ValueError("secret_id is required")
        self._secret_id = secret_id
        self._client = client or build_secrets_client(region_name)

    def load(self) -> StoredTidalToken:
        """Fetch and parse the stored Tidal token."""
        response = self._client.get_secret_value(SecretId=self._secret_id)
        payload = _parse_secret_payload(response)
        return StoredTidalToken.from_payload(payload)

    def store(self, token: StoredTidalToken) -> None:
        """Persist a new version of the Tidal token to Secrets Manager."""
        self._client.put_secret_value(
            SecretId=self._secret_id,
            SecretString=json.dumps(token.to_payload()),
        )

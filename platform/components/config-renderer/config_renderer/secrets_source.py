"""AWS Secrets Manager reader for Lavalink credentials.

Fetches a single JSON secret (a bundle of the Discord/Spotify/Tidal/yt-cipher/
YouTube credentials Lavalink needs) and parses it into a
:class:`~config_renderer.model.LavalinkCredentials`. ``boto3`` is imported
lazily inside the client factory so the module stays import-safe in
environments without AWS libraries (and unit-testable with an injected client).

Requirements: 6.1, 7.3, 15.1
"""

from __future__ import annotations

import json
from typing import Any, Protocol

from .model import LavalinkCredentials

__all__ = [
    "SecretsClient",
    "SecretsManagerCredentialSource",
    "build_secrets_client",
]


class SecretsClient(Protocol):
    """Minimal subset of the boto3 Secrets Manager client interface."""

    def get_secret_value(self, **kwargs: Any) -> dict[str, Any]:
        """Return a response dict with a ``SecretString`` or ``SecretBinary``."""
        ...


def build_secrets_client(region_name: str | None = None) -> SecretsClient:
    """Create a real boto3 Secrets Manager client (imported lazily)."""
    import boto3

    return boto3.client("secretsmanager", region_name=region_name)


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
        raise ValueError("secret JSON must be an object of credential fields")
    return parsed


class SecretsManagerCredentialSource:
    """Reads Lavalink credentials from a single Secrets Manager secret.

    Args:
        secret_id: The secret name or ARN holding the JSON credential bundle.
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

    def load(self) -> LavalinkCredentials:
        """Fetch and parse the secret into typed credentials."""
        response = self._client.get_secret_value(SecretId=self._secret_id)
        payload = _parse_secret_payload(response)
        return LavalinkCredentials.from_secret(payload)

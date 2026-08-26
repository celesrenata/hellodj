"""Discord bot token provider backed by AWS Secrets Manager.

The Discord bot token is stored in AWS Secrets Manager (never in the datastore
or a config file — R8/R19 secrets handling). This module reads it through a
small, typed provider that is *injectable and mockable*: the boto3 client is a
constructor argument, so tests supply a fake client and no real AWS call is
made.

The secret payload may be either a raw token string or a JSON object with a
``token`` (or ``discord_token``) field, matching how the auth-stack writes it.
"""

from __future__ import annotations

import json
from typing import Any, Protocol

__all__ = ["SecretsManagerClient", "TokenProvider", "get_discord_token"]

# Field names accepted when the secret payload is a JSON object.
_TOKEN_JSON_FIELDS = ("token", "discord_token", "DISCORD_TOKEN")


class SecretsManagerClient(Protocol):
    """Structural type for the subset of the Secrets Manager client used here.

    boto3's ``secretsmanager`` client satisfies this Protocol at runtime; tests
    can supply any object exposing a compatible ``get_secret_value``.
    """

    def get_secret_value(self, *, SecretId: str) -> dict[str, Any]:  # noqa: N803
        """Return the secret payload for ``SecretId`` (boto3 API casing)."""
        ...


def _parse_secret_payload(payload: dict[str, Any]) -> str:
    """Extract the token string from a Secrets Manager ``get_secret_value``.

    Accepts either a raw string token in ``SecretString`` or a JSON object with
    one of :data:`_TOKEN_JSON_FIELDS`.

    Raises:
        ValueError: If no token can be recovered from the payload.
    """
    secret_string = payload.get("SecretString")
    if not secret_string:
        raise ValueError("secret has no SecretString value")

    stripped = secret_string.strip()
    if stripped.startswith("{"):
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise ValueError(f"secret JSON is invalid: {exc}") from exc
        for field in _TOKEN_JSON_FIELDS:
            value = obj.get(field)
            if isinstance(value, str) and value.strip():
                return value.strip()
        raise ValueError(
            "secret JSON does not contain a token field "
            f"(expected one of {_TOKEN_JSON_FIELDS})"
        )

    return stripped


def get_discord_token(client: SecretsManagerClient, secret_id: str) -> str:
    """Fetch and parse the Discord bot token from Secrets Manager.

    Args:
        client: An object implementing :class:`SecretsManagerClient` (a boto3
            ``secretsmanager`` client in production, a fake in tests).
        secret_id: The secret id/ARN holding the token.

    Returns:
        The Discord bot token string.

    Raises:
        ValueError: If the secret payload does not yield a usable token.
    """
    payload = client.get_secret_value(SecretId=secret_id)
    return _parse_secret_payload(payload)


class TokenProvider:
    """Caching provider for the Discord bot token.

    The provider fetches on first use and on explicit :meth:`refresh`, caching
    the value in between so the token-refresh watchdog controls how often
    Secrets Manager is polled. The boto3 client is injected, keeping the
    provider fully mockable.
    """

    def __init__(self, client: SecretsManagerClient, secret_id: str) -> None:
        """Initialise the provider.

        Args:
            client: Injected Secrets Manager client (mockable in tests).
            secret_id: The secret id/ARN holding the Discord bot token.
        """
        self._client = client
        self._secret_id = secret_id
        self._cached: str | None = None

    @property
    def secret_id(self) -> str:
        """The Secrets Manager secret id this provider reads."""
        return self._secret_id

    def get(self) -> str:
        """Return the cached token, fetching it on first use."""
        if self._cached is None:
            self._cached = get_discord_token(self._client, self._secret_id)
        return self._cached

    def refresh(self) -> str:
        """Re-read the token from Secrets Manager and update the cache.

        Returns:
            The freshly fetched token.
        """
        self._cached = get_discord_token(self._client, self._secret_id)
        return self._cached

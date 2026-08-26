"""Secrets Manager access for the web-ui component.

All OAuth client credentials and provider tokens live in AWS Secrets Manager
(not the datastore) under the stage-scoped ``hellodj/<stage>/<leaf>`` naming
established by the CDK ``AuthStack``. This module provides a thin, cached
accessor over a Secrets Manager client.

The Secrets Manager client is *injected* (or lazily created via boto3 only when
actually needed at runtime), so the module is import-safe in test/CI
environments with no AWS credentials and can be exercised with a fake client.

Requirements: 8.6, 9.2
"""

from __future__ import annotations

import json
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "secret_name",
    "SecretsClient",
    "SecretsProvider",
]


def secret_name(stage: str, leaf: str) -> str:
    """Return the stage-scoped Secrets Manager name ``hellodj/<stage>/<leaf>``.

    Mirrors the ``secretName`` helper in the CDK ``AuthStack`` so IaC and the
    runtime component resolve identical secret names.
    """
    return f"hellodj/{stage}/{leaf}"


@runtime_checkable
class SecretsClient(Protocol):
    """Minimal protocol for the Secrets Manager client used here.

    Matches the boto3 ``secretsmanager`` client's ``get_secret_value`` shape so
    a real client satisfies it directly and tests can inject a fake.
    """

    def get_secret_value(self, *, SecretId: str) -> dict[str, Any]:  # noqa: N803
        """Return a mapping containing ``SecretString`` for ``SecretId``."""
        ...


class SecretsProvider:
    """Cached resolver for platform secrets by leaf name.

    Args:
        stage: The deployment stage (``beta``/``gamma``/``prod``) used to build
            the stage-scoped secret name.
        client: An object implementing :class:`SecretsClient`. When omitted the
            provider lazily creates a boto3 ``secretsmanager`` client on first
            use, so importing this module never requires AWS credentials.
        region_name: Optional region for the lazily created boto3 client.
    """

    def __init__(
        self,
        stage: str,
        client: SecretsClient | None = None,
        *,
        region_name: str | None = None,
    ) -> None:
        self._stage = stage
        self._client = client
        self._region_name = region_name
        self._cache: dict[str, str] = {}

    def _get_client(self) -> SecretsClient:
        """Return the injected client, creating a boto3 one on first use."""
        if self._client is None:
            import boto3  # imported lazily to keep module import-safe

            self._client = boto3.client(
                "secretsmanager", region_name=self._region_name
            )
        return self._client

    def get_raw(self, leaf: str) -> str:
        """Return the raw ``SecretString`` for a leaf, using a per-leaf cache."""
        if leaf in self._cache:
            return self._cache[leaf]
        name = secret_name(self._stage, leaf)
        response = self._get_client().get_secret_value(SecretId=name)
        value = response.get("SecretString", "")
        self._cache[leaf] = value
        return value

    def get_json(self, leaf: str) -> dict[str, Any]:
        """Return a JSON-decoded secret payload, or ``{}`` when empty/invalid."""
        raw = self.get_raw(leaf)
        if not raw:
            return {}
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return decoded if isinstance(decoded, dict) else {}

    def invalidate(self, leaf: str | None = None) -> None:
        """Drop cached secret(s); all when ``leaf`` is ``None``."""
        if leaf is None:
            self._cache.clear()
        else:
            self._cache.pop(leaf, None)

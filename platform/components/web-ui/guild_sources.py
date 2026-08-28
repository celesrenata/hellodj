"""Per-guild music source ownership and OAuth token isolation.

Each guild's source (YouTube, YouTube Music, Tidal, Spotify) OAuth tokens are
stored in a Per_Guild_Secret keyed by guild id and provider, isolated from
every other guild (R5.1). The DynamoDB ``SOURCE#<provider>`` item under the
guild holds ONLY non-secret metadata (connected flag, timestamps); tokens never
touch DynamoDB. Callers must pass an already-verified ``can_manage_guild``
decision — this module never reads/writes a guild's secret without the route
having gated the caller's ownership first (R5.2).

Per_Guild_Secret naming (isolated per guild+provider):

    hellodj/<stage>/guild/<guildId>/<provider>

The identical name is used by the bot's ``guild_credentials`` resolver so the
web-ui and the playback path agree on where a guild's tokens live (R6.1).

Requirements: 5.1, 5.2, 5.3, 5.4, 5.5
"""

from __future__ import annotations

import json
from typing import Any, Protocol

from hellodj_platform_logic.data_access import CoreTable

from guild_admin_service import guild_pk

__all__ = [
    "SUPPORTED_PROVIDERS",
    "GuildSourcesService",
    "guild_source_secret_name",
    "source_sk",
]

#: The music providers a guild can own OAuth for.
SUPPORTED_PROVIDERS = ("youtube", "youtube_music", "tidal", "spotify")

SOURCE_ENTITY = "GuildSource"


def source_sk(provider: str) -> str:
    """Return the sort key for a guild's per-provider source metadata item."""
    return f"SOURCE#{provider}"


def guild_source_secret_name(stage: str, guild_id: str, provider: str) -> str:
    """Return the Per_Guild_Secret name for a guild+provider (isolated).

    Shared verbatim with the bot's ``guild_credentials`` resolver so both sides
    address the SAME secret. The ``guild/<guildId>/`` path segment is what
    isolates one guild's tokens from another's, and is the exact prefix the IAM
    grants scope to (``hellodj/<stage>/guild/*``).
    """
    return f"hellodj/{stage}/guild/{guild_id}/{provider}"


class SecretsAdminClient(Protocol):
    """Subset of the boto3 ``secretsmanager`` client the service uses."""

    def create_secret(self, **kwargs: Any) -> dict[str, Any]: ...

    def put_secret_value(self, **kwargs: Any) -> dict[str, Any]: ...

    def get_secret_value(self, **kwargs: Any) -> dict[str, Any]: ...

    def delete_secret(self, **kwargs: Any) -> dict[str, Any]: ...


class GuildSourcesService:
    """Manage per-guild source metadata + isolated OAuth token secrets."""

    def __init__(
        self,
        core_table: CoreTable,
        secrets_client: SecretsAdminClient,
        *,
        stage: str,
    ) -> None:
        self._core = core_table
        self._secrets = secrets_client
        self._stage = stage

    def is_supported(self, provider: str) -> bool:
        """Return whether ``provider`` is a supported source."""
        return provider in SUPPORTED_PROVIDERS

    def status(self, guild_id: str) -> list[dict[str, Any]]:
        """Return per-provider connection status for a guild (metadata only)."""
        rows = {
            r["SK"].split("SOURCE#", 1)[1]: r.get("data", {})
            for r in self._core.query_pk_prefix(
                guild_pk(guild_id), sk_prefix="SOURCE#"
            )
        }
        return [
            {
                "provider": p,
                "connected": bool(rows.get(p, {}).get("connected", False)),
                "connected_at": rows.get(p, {}).get("connected_at", 0),
            }
            for p in SUPPORTED_PROVIDERS
        ]

    def store_tokens(
        self,
        guild_id: str,
        provider: str,
        tokens: dict[str, Any],
        *,
        connected_by: str,
    ) -> None:
        """Persist a guild's OAuth tokens for a provider (isolated secret).

        Writes the tokens to the Per_Guild_Secret and records only non-secret
        metadata (connected flag, actor) in DynamoDB. The caller MUST have
        verified ``can_manage_guild`` for this guild first (R5.2).
        """
        if not self.is_supported(provider):
            raise ValueError(f"unsupported provider: {provider!r}")
        name = guild_source_secret_name(self._stage, guild_id, provider)
        payload = json.dumps(tokens)
        try:
            self._secrets.create_secret(Name=name, SecretString=payload)
        except Exception:  # noqa: BLE001 - already exists → update in place
            self._secrets.put_secret_value(SecretId=name, SecretString=payload)
        self._core.put_new(
            guild_pk(guild_id),
            source_sk(provider),
            SOURCE_ENTITY,
            {"connected": True, "connected_by": connected_by},
        ) if self._core.get(
            guild_pk(guild_id), source_sk(provider)
        ) is None else self._core.update_with_lock(
            guild_pk(guild_id),
            source_sk(provider),
            lambda d: {**d, "connected": True, "connected_by": connected_by},
            entity_type=SOURCE_ENTITY,
        )

    def disconnect(self, guild_id: str, provider: str) -> None:
        """Delete a guild's Per_Guild_Secret + metadata for a provider (R5.3)."""
        if not self.is_supported(provider):
            raise ValueError(f"unsupported provider: {provider!r}")
        name = guild_source_secret_name(self._stage, guild_id, provider)
        try:
            self._secrets.delete_secret(
                SecretId=name, ForceDeleteWithoutRecovery=True
            )
        except Exception:  # noqa: BLE001 - absent secret → nothing to delete
            pass
        self._core.delete(guild_pk(guild_id), source_sk(provider))

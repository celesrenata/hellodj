"""Runtime configuration for the discord-bot-core component.

Settings are read from the process environment so the component is configured
declaratively at deploy time (no self-hosted config store). The Discord bot
token itself is *not* held here — it is fetched at runtime from AWS Secrets
Manager via :mod:`discord_bot_core.secrets` (R8/R19 secrets handling), so this
object only carries the *reference* to the secret, never the secret value.

Everything in this module is pure/environment-driven and performs no network
calls, so it can be constructed and asserted in tests without AWS access.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

__all__ = ["BotConfig"]

# Default endpoints assume in-cluster service DNS; overridable via env for local
# development and the Beta/Gamma/Prod stages.
_DEFAULT_ORCHESTRATOR_URL = "http://playback-orchestrator:8080"
_DEFAULT_TOKEN_REFRESH_INTERVAL_S = 300.0
_DEFAULT_GATEWAY_HEALTH_INTERVAL_S = 30.0
_DEFAULT_GATEWAY_STALL_TIMEOUT_S = 90.0
_DEFAULT_COMMAND_PREFIX = "!hellodj "
_DEFAULT_IDENTITY_APPLY_INTERVAL_S = 300.0


def _env_float(source: dict[str, str], name: str, default: float) -> float:
    """Read a float from ``source``, falling back to ``default``."""
    raw = source.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class BotConfig:
    """Immutable runtime settings for the bot core.

    Attributes:
        discord_token_secret_id: Secrets Manager secret id/ARN holding the
            Discord bot token. The token value is fetched at runtime, never
            stored in this object.
        orchestrator_base_url: Base URL of the playback-orchestrator that this
            component delegates all playback to.
        command_prefix: Prefix for text (non-slash) commands.
        token_refresh_interval_s: How often the token-refresh watchdog re-reads
            the Discord token from Secrets Manager.
        gateway_health_interval_s: How often the gateway-health watchdog checks
            for a stalled gateway.
        gateway_stall_timeout_s: Age of the last gateway heartbeat beyond which
            the gateway is considered stalled and a reconnect is forced.
        aws_region: AWS region for the Secrets Manager client (``None`` uses the
            boto3 default resolution chain).
        core_table_name: DynamoDB ``hellodj-core`` table name holding per-guild
            ``BOTIDENTITY`` items. Empty (default) disables per-guild identity
            apply — the feature is optional and no-network when unconfigured.
        assets_bucket: S3 bucket the web-ui uploaded per-guild bot-avatar bytes
            to. Empty (default) disables identity apply.
        identity_apply_interval_s: How often the identity-apply watchdog polls
            for and applies pending per-guild identity changes.
    """

    discord_token_secret_id: str
    orchestrator_base_url: str = _DEFAULT_ORCHESTRATOR_URL
    command_prefix: str = _DEFAULT_COMMAND_PREFIX
    token_refresh_interval_s: float = _DEFAULT_TOKEN_REFRESH_INTERVAL_S
    gateway_health_interval_s: float = _DEFAULT_GATEWAY_HEALTH_INTERVAL_S
    gateway_stall_timeout_s: float = _DEFAULT_GATEWAY_STALL_TIMEOUT_S
    aws_region: str | None = None
    core_table_name: str = ""
    assets_bucket: str = ""
    identity_apply_interval_s: float = _DEFAULT_IDENTITY_APPLY_INTERVAL_S

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> BotConfig:
        """Build a :class:`BotConfig` from a process-environment mapping.

        Args:
            env: Mapping to read from; defaults to :data:`os.environ`. Passing an
                explicit mapping keeps the method pure and testable.

        Returns:
            A populated, immutable :class:`BotConfig`.

        Raises:
            ValueError: If the required ``HELLODJ_DISCORD_TOKEN_SECRET_ID`` is
                missing, since the component cannot authenticate without it.
        """
        source = os.environ if env is None else env
        secret_id = source.get("HELLODJ_DISCORD_TOKEN_SECRET_ID", "").strip()
        if not secret_id:
            raise ValueError(
                "HELLODJ_DISCORD_TOKEN_SECRET_ID is required — it names the "
                "Secrets Manager secret holding the Discord bot token."
            )
        return cls(
            discord_token_secret_id=secret_id,
            orchestrator_base_url=source.get(
                "HELLODJ_ORCHESTRATOR_URL", _DEFAULT_ORCHESTRATOR_URL
            ).strip()
            or _DEFAULT_ORCHESTRATOR_URL,
            command_prefix=source.get(
                "HELLODJ_COMMAND_PREFIX", _DEFAULT_COMMAND_PREFIX
            ),
            token_refresh_interval_s=_env_float(
                source,
                "HELLODJ_TOKEN_REFRESH_INTERVAL_S",
                _DEFAULT_TOKEN_REFRESH_INTERVAL_S,
            ),
            gateway_health_interval_s=_env_float(
                source,
                "HELLODJ_GATEWAY_HEALTH_INTERVAL_S",
                _DEFAULT_GATEWAY_HEALTH_INTERVAL_S,
            ),
            gateway_stall_timeout_s=_env_float(
                source,
                "HELLODJ_GATEWAY_STALL_TIMEOUT_S",
                _DEFAULT_GATEWAY_STALL_TIMEOUT_S,
            ),
            aws_region=(source.get("AWS_REGION") or None),
            core_table_name=source.get("HELLODJ_CORE_TABLE", "").strip(),
            assets_bucket=source.get("HELLODJ_ASSETS_BUCKET", "").strip(),
            identity_apply_interval_s=_env_float(
                source,
                "HELLODJ_IDENTITY_APPLY_INTERVAL_S",
                _DEFAULT_IDENTITY_APPLY_INTERVAL_S,
            ),
        )

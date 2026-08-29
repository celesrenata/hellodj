"""Runtime configuration for the ``tidal-stream`` component.

All settings are resolved from environment variables so the component is
independently deployable (R15.1) and reads no local database or SQLite. The
first-party OAuth application id and HelloDJ-owned callback URL are the core
Tidal source auth inputs (R9.1, R9.2) and remain **global** single-app-id config
— only the per-user token varies (multi-tenant-source-streaming R5.3).

Multi-tenant streaming (task 3.1) resolves each request's token from the unified
per-user credential store (``hellodj-core`` DynamoDB + the source-credentials
KMS CMK, Decrypt-only), so the streaming path binds NO single startup account.
The single startup-bound ``refresh_secret_id`` is therefore OPTIONAL: it backs
only the legacy first-party ``/auth/callback`` code-exchange forward and is
never read by the per-user streaming path.

Requirements: 6.1, 9.1, 9.2, 9.5, 15.1, 5.1, 5.3
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from hellodj_platform_logic.tidal_refresh import (
    FIRST_PARTY_SINGLE_APP_ID_MODE,
    FirstPartyClientConfig,
)

__all__ = ["TidalStreamSettings"]

#: Default Tidal OAuth token endpoint (single-app-id first-party flow).
DEFAULT_TOKEN_URL = "https://auth.tidal.com/v1/oauth2/token"

#: Default Tidal API base used for direct streaming/search resolution.
DEFAULT_API_BASE = "https://api.tidal.com/v1"

#: Default bind host/port for the sidecar HTTP server.
DEFAULT_HOST = "0.0.0.0"  # noqa: S104 - container sidecar binds all interfaces
DEFAULT_PORT = 8801

#: Default unified credential store table name.
DEFAULT_CORE_TABLE = "hellodj-core"

#: Default maximum number of concurrently-live per-user Tidal sessions (R8.1).
DEFAULT_MAX_SESSIONS = 32

#: Default per-user session idle timeout, in seconds (R8.2).
DEFAULT_SESSION_IDLE_TIMEOUT = 900.0


def _require(name: str) -> str:
    """Return a required environment variable or raise a clear error."""
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"required environment variable {name} is not set")
    return value


@dataclass(frozen=True)
class TidalStreamSettings:
    """Immutable runtime settings for the ``tidal-stream`` sidecar.

    Attributes:
        app_id: The single Tidal application identifier for all Tidal source
            auth (R9.1). Global single-app-id config (R5.3).
        callback_url: The HelloDJ-owned OAuth callback endpoint (R9.2). Global.
        refresh_secret_id: OPTIONAL Secrets Manager secret backing ONLY the
            legacy first-party ``/auth/callback`` code-exchange forward; the
            per-user streaming path never reads it (the single startup-bound
            account is replaced by the per-user registry — R5.1). Empty when the
            sidecar runs pure multi-tenant.
        core_table: The unified per-user credential store table (R1.1).
        source_creds_kms_key_id: The source-credentials KMS CMK id (Decrypt-only
            reader grant — R9.2). Present for parity/observability; the decrypt
            routing key travels on each stored item.
        max_sessions: Bounded per-user session-pool size (R8.1).
        session_idle_timeout_seconds: Per-user session idle timeout (R8.2).
        token_url: Tidal OAuth token endpoint for code exchange and refresh.
        api_base: Tidal API base for direct streaming/search resolution.
        country_code: ISO country code used for catalog/stream resolution.
        region_name: AWS region for the AWS clients (optional).
        host: Bind host for the HTTP server.
        port: Bind port for the HTTP server.
        expiry_skew_seconds: Skew applied when deciding token expiry.
    """

    app_id: str
    callback_url: str
    refresh_secret_id: str = ""
    core_table: str = DEFAULT_CORE_TABLE
    source_creds_kms_key_id: str = ""
    max_sessions: int = DEFAULT_MAX_SESSIONS
    session_idle_timeout_seconds: float = DEFAULT_SESSION_IDLE_TIMEOUT
    token_url: str = DEFAULT_TOKEN_URL
    api_base: str = DEFAULT_API_BASE
    country_code: str = "US"
    region_name: str | None = None
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    expiry_skew_seconds: float = 60.0

    def client_config(self) -> FirstPartyClientConfig:
        """Build the shared first-party client config (single-app-id mode)."""
        return FirstPartyClientConfig(
            app_id=self.app_id,
            callback_url=self.callback_url,
            auth_mode=FIRST_PARTY_SINGLE_APP_ID_MODE,
        )

    @classmethod
    def from_env(cls) -> TidalStreamSettings:
        """Build settings from environment variables.

        Raises:
            ValueError: If a required variable (app id or callback URL) is
                missing, or a numeric variable is malformed.
        """
        port = _int_env("TIDAL_STREAM_PORT", DEFAULT_PORT)
        skew = _float_env("TIDAL_EXPIRY_SKEW_SECONDS", 60.0)
        max_sessions = _int_env("TIDAL_MAX_SESSIONS", DEFAULT_MAX_SESSIONS)
        idle_timeout = _float_env(
            "TIDAL_SESSION_IDLE_TIMEOUT", DEFAULT_SESSION_IDLE_TIMEOUT
        )

        return cls(
            app_id=_require("TIDAL_APP_ID"),
            callback_url=_require("TIDAL_CALLBACK_URL"),
            refresh_secret_id=os.environ.get("TIDAL_REFRESH_SECRET_ID", "").strip(),
            core_table=os.environ.get("HELLODJ_CORE_TABLE", DEFAULT_CORE_TABLE).strip()
            or DEFAULT_CORE_TABLE,
            source_creds_kms_key_id=os.environ.get(
                "HELLODJ_SOURCE_CREDS_KMS_KEY_ID", ""
            ).strip(),
            max_sessions=max_sessions,
            session_idle_timeout_seconds=idle_timeout,
            token_url=os.environ.get("TIDAL_TOKEN_URL", DEFAULT_TOKEN_URL).strip()
            or DEFAULT_TOKEN_URL,
            api_base=os.environ.get("TIDAL_API_BASE", DEFAULT_API_BASE).strip()
            or DEFAULT_API_BASE,
            country_code=os.environ.get("TIDAL_COUNTRY_CODE", "US").strip() or "US",
            region_name=os.environ.get("AWS_REGION", "").strip() or None,
            host=os.environ.get("TIDAL_STREAM_HOST", DEFAULT_HOST).strip()
            or DEFAULT_HOST,
            port=port,
            expiry_skew_seconds=skew,
        )


def _int_env(name: str, default: int) -> int:
    """Parse an integer environment variable, raising a clear error if malformed."""
    raw = os.environ.get(name, str(default)).strip()
    try:
        return int(raw)
    except ValueError as error:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from error


def _float_env(name: str, default: float) -> float:
    """Parse a float environment variable, raising a clear error if malformed."""
    raw = os.environ.get(name, str(default)).strip()
    try:
        return float(raw)
    except ValueError as error:
        raise ValueError(f"{name} must be a number, got {raw!r}") from error

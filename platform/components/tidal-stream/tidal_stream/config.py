"""Runtime configuration for the ``tidal-stream`` component.

All settings are resolved from environment variables so the component is
independently deployable (R15.1) and reads no local database or SQLite. The
first-party OAuth application id and HelloDJ-owned callback URL are the core
Tidal source auth inputs (R9.1, R9.2); the refresh token is never taken from
the environment — it is loaded from AWS Secrets Manager at runtime.

Requirements: 6.1, 9.1, 9.2, 9.5, 15.1
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
            auth (R9.1).
        callback_url: The HelloDJ-owned OAuth callback endpoint (R9.2).
        refresh_secret_id: Secrets Manager secret name/ARN holding the Tidal
            refresh token payload.
        token_url: Tidal OAuth token endpoint for code exchange and refresh.
        api_base: Tidal API base for direct streaming/search resolution.
        country_code: ISO country code used for catalog/stream resolution.
        region_name: AWS region for the Secrets Manager client (optional).
        host: Bind host for the HTTP server.
        port: Bind port for the HTTP server.
        expiry_skew_seconds: Skew applied when deciding token expiry.
    """

    app_id: str
    callback_url: str
    refresh_secret_id: str
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
            ValueError: If a required variable (app id, callback URL, or
                refresh secret id) is missing.
        """
        port_raw = os.environ.get("TIDAL_STREAM_PORT", str(DEFAULT_PORT)).strip()
        try:
            port = int(port_raw)
        except ValueError as error:
            raise ValueError(
                f"TIDAL_STREAM_PORT must be an integer, got {port_raw!r}"
            ) from error

        skew_raw = os.environ.get("TIDAL_EXPIRY_SKEW_SECONDS", "60").strip()
        try:
            skew = float(skew_raw)
        except ValueError as error:
            raise ValueError(
                f"TIDAL_EXPIRY_SKEW_SECONDS must be a number, got {skew_raw!r}"
            ) from error

        return cls(
            app_id=_require("TIDAL_APP_ID"),
            callback_url=_require("TIDAL_CALLBACK_URL"),
            refresh_secret_id=_require("TIDAL_REFRESH_SECRET_ID"),
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

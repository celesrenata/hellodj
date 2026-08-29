"""Runtime configuration for the ``spotify-stream`` component (multi-tenant).

All settings are resolved from environment variables so the component is
independently deployable (R9.1) and reads no local database. The per-user
streaming path resolves each request's Spotify credential from the unified
per-user credential store (``hellodj-core`` DynamoDB + the source-credentials
KMS CMK, Decrypt-only), so the sidecar binds NO single ambient account
(multi-tenant-source-streaming R3.6, R10.5).

Env (design "Deployment & least privilege"):
    HELLODJ_CORE_TABLE                unified credential store table.
    HELLODJ_SOURCE_CREDS_KMS_KEY_ID   source-credentials CMK id (Decrypt-only).
    AWS_REGION                        AWS region for the boto3 clients.
    SPOTIFY_MAX_SESSIONS              bounded per-user session-pool size (R8.1).
    SPOTIFY_SESSION_IDLE_TIMEOUT      per-user session idle timeout, s (R8.2).
    SPOTIFY_STREAM_PORT / _HOST       HTTP bind.
    DATA_DIR                          per-user librespot cache root (R9.3).
    SPOTIFY_EXPIRY_SKEW_SECONDS       skew for the read-only expiry re-read.

Requirements: 3.1, 3.6, 9.1, 9.3, 10.5
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

__all__ = ["SpotifyStreamSettings"]

#: Default unified credential store table name.
DEFAULT_CORE_TABLE = "hellodj-core"

#: Default bind host/port for the sidecar HTTP server (matches the legacy port).
DEFAULT_HOST = "0.0.0.0"  # noqa: S104 - container sidecar binds all interfaces
DEFAULT_PORT = 8802

#: Default maximum number of concurrently-live per-user librespot sessions (R8.1).
DEFAULT_MAX_SESSIONS = 16

#: Default per-user session idle timeout, in seconds (R8.2).
DEFAULT_SESSION_IDLE_TIMEOUT = 900.0

#: Default per-user track audio cache bound + TTL (R8.3), keyed by (sub, track).
DEFAULT_TRACK_CACHE_MAX = 32
DEFAULT_TRACK_CACHE_TTL = 300.0

#: Default per-user librespot cache root; each user's cache lives under
#: ``<DATA_DIR>/<sub>/`` so a restart never mixes users' credentials (R9.3).
DEFAULT_DATA_DIR = "/app/data"

#: Default read-only expiry skew (seconds) for the unified resolver (R2.2).
DEFAULT_EXPIRY_SKEW_SECONDS = 30.0


@dataclass(frozen=True)
class SpotifyStreamSettings:
    """Immutable runtime settings for the ``spotify-stream`` sidecar.

    Attributes:
        core_table: The unified per-user credential store table (R1.1).
        source_creds_kms_key_id: The source-credentials KMS CMK id (Decrypt-only
            reader grant — R9.2). Present for parity/observability; the decrypt
            routing key travels on each stored item.
        region_name: AWS region for the AWS clients (optional).
        data_dir: Root under which per-user librespot caches live (R9.3).
        max_sessions: Bounded per-user session-pool size (R8.1).
        session_idle_timeout_seconds: Per-user session idle timeout (R8.2).
        track_cache_max: Max cached (sub, track) audio entries (R8.3).
        track_cache_ttl_seconds: Cached-audio TTL (R8.3).
        host: Bind host for the HTTP server.
        port: Bind port for the HTTP server.
        expiry_skew_seconds: Skew applied when deciding token expiry (R2.2).
    """

    core_table: str = DEFAULT_CORE_TABLE
    source_creds_kms_key_id: str = ""
    region_name: str | None = None
    data_dir: str = DEFAULT_DATA_DIR
    max_sessions: int = DEFAULT_MAX_SESSIONS
    session_idle_timeout_seconds: float = DEFAULT_SESSION_IDLE_TIMEOUT
    track_cache_max: int = DEFAULT_TRACK_CACHE_MAX
    track_cache_ttl_seconds: float = DEFAULT_TRACK_CACHE_TTL
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    expiry_skew_seconds: float = DEFAULT_EXPIRY_SKEW_SECONDS

    @property
    def data_dir_path(self) -> Path:
        """The per-user cache root as a :class:`~pathlib.Path`."""
        return Path(self.data_dir)

    @classmethod
    def from_env(cls) -> SpotifyStreamSettings:
        """Build settings from environment variables.

        Raises:
            ValueError: If a numeric variable is malformed.
        """
        return cls(
            core_table=os.environ.get("HELLODJ_CORE_TABLE", DEFAULT_CORE_TABLE).strip()
            or DEFAULT_CORE_TABLE,
            source_creds_kms_key_id=os.environ.get(
                "HELLODJ_SOURCE_CREDS_KMS_KEY_ID", ""
            ).strip(),
            region_name=os.environ.get("AWS_REGION", "").strip() or None,
            data_dir=os.environ.get("DATA_DIR", DEFAULT_DATA_DIR).strip()
            or DEFAULT_DATA_DIR,
            max_sessions=_int_env("SPOTIFY_MAX_SESSIONS", DEFAULT_MAX_SESSIONS),
            session_idle_timeout_seconds=_float_env(
                "SPOTIFY_SESSION_IDLE_TIMEOUT", DEFAULT_SESSION_IDLE_TIMEOUT
            ),
            track_cache_max=_int_env(
                "SPOTIFY_TRACK_CACHE_MAX", DEFAULT_TRACK_CACHE_MAX
            ),
            track_cache_ttl_seconds=_float_env(
                "SPOTIFY_TRACK_CACHE_TTL", DEFAULT_TRACK_CACHE_TTL
            ),
            host=os.environ.get("SPOTIFY_STREAM_HOST", DEFAULT_HOST).strip()
            or DEFAULT_HOST,
            port=_int_env("SPOTIFY_STREAM_PORT", DEFAULT_PORT),
            expiry_skew_seconds=_float_env(
                "SPOTIFY_EXPIRY_SKEW_SECONDS", DEFAULT_EXPIRY_SKEW_SECONDS
            ),
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

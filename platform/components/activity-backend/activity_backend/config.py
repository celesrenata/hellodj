"""Runtime configuration for the activity-backend component.

Settings are read from the process environment so the component is configured
declaratively at deploy time (no self-hosted config store). Nothing here holds a
secret value; the CloudFront domain, S3 bucket, and hls-transcode endpoint are
references resolved at deploy time for the Beta/Gamma/Prod stages.

This module is pure/environment-driven and performs no network calls, so it can
be constructed and asserted in tests without any runtime dependency installed.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

__all__ = ["ActivityConfig"]

# The Activity is fronted by ALB/CloudFront at the ``/activity/`` path prefix
# (R18.2). Keep this in one place so route registration and the WS URL agree.
_DEFAULT_ROUTE_PREFIX = "/activity"

# In-cluster service DNS for the transcode component; overridable per stage.
_DEFAULT_TRANSCODE_URL = "http://hls-transcode:8080"

# Bind address for the aiohttp server inside the container.
_DEFAULT_HOST = "0.0.0.0"  # noqa: S104 - container-internal bind, fronted by ALB
_DEFAULT_PORT = 8090

_DEFAULT_HEARTBEAT_S = 30.0
_DEFAULT_STROKE_CAP = 500


def _env_float(source: dict[str, str], name: str, default: float) -> float:
    """Read a float from ``source``, falling back to ``default``."""
    raw = source.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(source: dict[str, str], name: str, default: int) -> int:
    """Read an int from ``source``, falling back to ``default``."""
    raw = source.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _normalize_prefix(prefix: str) -> str:
    """Return ``prefix`` with a leading slash and no trailing slash."""
    cleaned = "/" + prefix.strip().strip("/")
    return cleaned if cleaned != "/" else _DEFAULT_ROUTE_PREFIX


@dataclass(frozen=True)
class ActivityConfig:
    """Immutable runtime settings for the activity-backend.

    Attributes:
        route_prefix: HTTP/WS path prefix the Activity is served under
            (``/activity`` behind ALB/CloudFront — R18.2).
        host: Bind host for the aiohttp server.
        port: Bind port for the aiohttp server.
        transcode_base_url: Base URL of the hls-transcode component this
            backend emits transcode requests to (R18.4).
        cloudfront_domain: CloudFront distribution domain that fronts the S3
            HLS bucket; used to build viewer-facing HLS URLs (R18.2).
        hls_s3_bucket: Name of the S3 bucket HLS segments are written to and
            read from (CloudFront origin — R18.2, R18.4).
        hls_s3_prefix: Key prefix within the bucket for HLS objects.
        heartbeat_interval_s: WebSocket server heartbeat/ping interval.
        max_strokes_per_guild: Whiteboard stroke cap per guild (memory bound).
        aws_region: AWS region for any AWS SDK client (``None`` uses the boto3
            default resolution chain).
    """

    transcode_base_url: str = _DEFAULT_TRANSCODE_URL
    cloudfront_domain: str = ""
    hls_s3_bucket: str = ""
    hls_s3_prefix: str = "hls"
    route_prefix: str = _DEFAULT_ROUTE_PREFIX
    host: str = _DEFAULT_HOST
    port: int = _DEFAULT_PORT
    heartbeat_interval_s: float = _DEFAULT_HEARTBEAT_S
    max_strokes_per_guild: int = _DEFAULT_STROKE_CAP
    aws_region: str | None = None

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> ActivityConfig:
        """Build an :class:`ActivityConfig` from a process-environment mapping.

        Args:
            env: Mapping to read from; defaults to :data:`os.environ`. Passing an
                explicit mapping keeps the method pure and testable.

        Returns:
            A populated, immutable :class:`ActivityConfig`.
        """
        source = os.environ if env is None else env
        return cls(
            transcode_base_url=(
                source.get("HELLODJ_TRANSCODE_URL", _DEFAULT_TRANSCODE_URL).strip()
                or _DEFAULT_TRANSCODE_URL
            ),
            cloudfront_domain=source.get("HELLODJ_CLOUDFRONT_DOMAIN", "").strip(),
            hls_s3_bucket=source.get("HELLODJ_HLS_S3_BUCKET", "").strip(),
            hls_s3_prefix=(
                source.get("HELLODJ_HLS_S3_PREFIX", "hls").strip().strip("/")
                or "hls"
            ),
            route_prefix=_normalize_prefix(
                source.get("HELLODJ_ACTIVITY_ROUTE_PREFIX", _DEFAULT_ROUTE_PREFIX)
            ),
            host=source.get("HELLODJ_ACTIVITY_HOST", _DEFAULT_HOST).strip()
            or _DEFAULT_HOST,
            port=_env_int(source, "HELLODJ_ACTIVITY_PORT", _DEFAULT_PORT),
            heartbeat_interval_s=_env_float(
                source, "HELLODJ_ACTIVITY_HEARTBEAT_S", _DEFAULT_HEARTBEAT_S
            ),
            max_strokes_per_guild=_env_int(
                source, "HELLODJ_MAX_STROKES", _DEFAULT_STROKE_CAP
            ),
            aws_region=(source.get("AWS_REGION") or None),
        )

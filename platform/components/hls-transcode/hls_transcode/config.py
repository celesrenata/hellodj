"""Runtime configuration for the hls-transcode component.

Settings are read from the process environment so the component is configured
declaratively at deploy time (no self-hosted config store). Nothing here holds a
secret; the S3 bucket, CloudFront domain, and hybrid-GPU thresholds are
references/tunables resolved at deploy time for the Beta/Gamma/Prod stages.

This module is pure/environment-driven and performs no network calls, so it can
be constructed and asserted in tests without any runtime dependency installed.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from hellodj_platform_logic.types import HybridGpuThresholds

__all__ = ["TranscodeConfig"]

# Bind address for the aiohttp server inside the container; fronted intra-node
# by the activity-backend which calls /v1/transcode.
_DEFAULT_HOST = "0.0.0.0"  # noqa: S104 - container-internal bind
_DEFAULT_PORT = 8080

_DEFAULT_HLS_PREFIX = "hls"
_DEFAULT_METRICS_NAMESPACE = "HelloDJ/Transcode"

# Hybrid gas/electric controller defaults (Decision D3). Pressure is expressed
# as a fraction of CPU-transcode capacity in [0.0, ~N]; the GPU spins up when
# sustained pressure exceeds spin-up and scales to zero when sustained pressure
# falls below spin-down. spin_up must be strictly greater than spin_down.
_DEFAULT_SPIN_UP = 0.80
_DEFAULT_SPIN_DOWN = 0.30
_DEFAULT_SPIN_UP_WINDOW_S = 30.0
_DEFAULT_SPIN_DOWN_WINDOW_S = 120.0


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


@dataclass(frozen=True)
class TranscodeConfig:
    """Immutable runtime settings for the hls-transcode component.

    Attributes:
        host: Bind host for the aiohttp server.
        port: Bind port for the aiohttp server (the activity-backend targets
            this over intra-node loopback).
        hls_s3_bucket: S3 bucket HLS output is written to (CloudFront origin —
            R18.2, R18.4).
        hls_s3_prefix: Key prefix within the bucket for HLS objects.
        cloudfront_domain: CloudFront domain fronting the bucket; used to build
            viewer-facing playlist URLs.
        metrics_namespace: CloudWatch namespace for CPU/GPU pressure metrics
            (R16.4).
        gpu_available: Whether a GPU-capable node group is provisioned at all.
            When ``False`` the scheduler stays on the CPU path unconditionally
            (software-transcode-only deployment — R3.9).
        segment_duration_s: Target HLS segment duration in seconds.
        gpu_thresholds: Hybrid gas/electric controller thresholds/windows
            driving libx264 vs NVENC selection (Decision D3).
        aws_region: AWS region for AWS SDK clients (``None`` uses the boto3
            default resolution chain).
    """

    hls_s3_bucket: str = ""
    hls_s3_prefix: str = _DEFAULT_HLS_PREFIX
    cloudfront_domain: str = ""
    metrics_namespace: str = _DEFAULT_METRICS_NAMESPACE
    gpu_available: bool = False
    segment_duration_s: float = 2.0
    host: str = _DEFAULT_HOST
    port: int = _DEFAULT_PORT
    gpu_thresholds: HybridGpuThresholds = HybridGpuThresholds(
        spin_up_threshold=_DEFAULT_SPIN_UP,
        spin_down_threshold=_DEFAULT_SPIN_DOWN,
        spin_up_window_seconds=_DEFAULT_SPIN_UP_WINDOW_S,
        spin_down_window_seconds=_DEFAULT_SPIN_DOWN_WINDOW_S,
    )
    aws_region: str | None = None

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> TranscodeConfig:
        """Build a :class:`TranscodeConfig` from a process-environment mapping.

        Args:
            env: Mapping to read from; defaults to :data:`os.environ`. Passing an
                explicit mapping keeps the method pure and testable.

        Returns:
            A populated, immutable :class:`TranscodeConfig`.
        """
        source = os.environ if env is None else env
        gpu_available = source.get("HELLODJ_GPU_AVAILABLE", "").strip().lower() in (
            "1",
            "true",
            "yes",
            "on",
        )
        thresholds = HybridGpuThresholds(
            spin_up_threshold=_env_float(
                source, "HELLODJ_GPU_SPIN_UP", _DEFAULT_SPIN_UP
            ),
            spin_down_threshold=_env_float(
                source, "HELLODJ_GPU_SPIN_DOWN", _DEFAULT_SPIN_DOWN
            ),
            spin_up_window_seconds=_env_float(
                source, "HELLODJ_GPU_SPIN_UP_WINDOW_S", _DEFAULT_SPIN_UP_WINDOW_S
            ),
            spin_down_window_seconds=_env_float(
                source,
                "HELLODJ_GPU_SPIN_DOWN_WINDOW_S",
                _DEFAULT_SPIN_DOWN_WINDOW_S,
            ),
        )
        return cls(
            hls_s3_bucket=source.get("HELLODJ_HLS_S3_BUCKET", "").strip(),
            hls_s3_prefix=(
                source.get("HELLODJ_HLS_S3_PREFIX", _DEFAULT_HLS_PREFIX)
                .strip()
                .strip("/")
                or _DEFAULT_HLS_PREFIX
            ),
            cloudfront_domain=source.get(
                "HELLODJ_CLOUDFRONT_DOMAIN", ""
            ).strip().rstrip("/"),
            metrics_namespace=(
                source.get("HELLODJ_METRICS_NAMESPACE", _DEFAULT_METRICS_NAMESPACE)
                .strip()
                or _DEFAULT_METRICS_NAMESPACE
            ),
            gpu_available=gpu_available,
            segment_duration_s=_env_float(
                source, "HELLODJ_HLS_SEGMENT_S", 2.0
            ),
            host=source.get("HELLODJ_TRANSCODE_HOST", _DEFAULT_HOST).strip()
            or _DEFAULT_HOST,
            port=_env_int(source, "HELLODJ_TRANSCODE_PORT", _DEFAULT_PORT),
            gpu_thresholds=thresholds,
            aws_region=(source.get("AWS_REGION") or None),
        )

"""CloudWatch pressure-metric publisher for the Autoscaler (R16.4).

The Autoscaler adds/removes transcode-host capacity in response to measured
CPU and GPU pressure (Requirements 3.2, 3.3, 16.4). This module turns a
scheduler :class:`~hls_transcode.scheduler.PressureSnapshot` into CloudWatch
metric data and publishes it via ``put_metric_data``.

The CloudWatch client is *injected* so the publisher is fully unit-testable with
a fake, and the real boto3 client is created lazily so this module imports
cleanly without boto3 installed (R15.1). Building the metric-data payload is a
pure function (:func:`build_metric_data`), exercised directly in tests.

Requirements: 3.2, 3.3, 15.1, 16.4
"""

from __future__ import annotations

from typing import Any, Protocol

from .scheduler import PressureSnapshot

__all__ = [
    "CloudWatchClient",
    "PressureMetrics",
    "build_metric_data",
    "create_cloudwatch_client",
]

# Metric names published for the Autoscaler to alarm/scale on (R16.4).
_METRIC_CPU_PRESSURE = "CpuTranscodePressure"
_METRIC_GPU_ACTIVE = "GpuActive"
_METRIC_ACTIVE_JOBS = "ActiveTranscodeJobs"


class CloudWatchClient(Protocol):
    """Structural type for the subset of the boto3 CloudWatch client used."""

    def put_metric_data(self, **kwargs: Any) -> Any:
        """Publish a batch of metric data points."""
        ...


def create_cloudwatch_client(region_name: str | None = None) -> CloudWatchClient:
    """Create a real boto3 CloudWatch client (imported lazily).

    Args:
        region_name: Optional AWS region; ``None`` uses the boto3 default chain.

    Returns:
        A boto3 CloudWatch client implementing :class:`CloudWatchClient`.
    """
    import boto3  # local import so the module imports without boto3 present

    return boto3.client("cloudwatch", region_name=region_name)


def build_metric_data(
    snapshot: PressureSnapshot,
    *,
    dimensions: list[dict[str, str]] | None = None,
) -> list[dict[str, Any]]:
    """Build the CloudWatch ``MetricData`` payload for a pressure snapshot.

    Emits CPU transcode pressure (fraction), a GPU-active gauge (1/0), and the
    active-job count, all tagged with the same dimensions so the Autoscaler can
    key alarms per stage/component.

    Args:
        snapshot: The scheduler pressure snapshot.
        dimensions: Optional CloudWatch dimensions (e.g. stage, component).

    Returns:
        A list of metric-data dicts suitable for ``put_metric_data``.
    """
    dims = dimensions or []
    return [
        {
            "MetricName": _METRIC_CPU_PRESSURE,
            "Dimensions": dims,
            "Value": float(snapshot.cpu_pressure),
            "Unit": "None",
        },
        {
            "MetricName": _METRIC_GPU_ACTIVE,
            "Dimensions": dims,
            "Value": 1.0 if snapshot.gpu_active else 0.0,
            "Unit": "None",
        },
        {
            "MetricName": _METRIC_ACTIVE_JOBS,
            "Dimensions": dims,
            "Value": float(snapshot.active_jobs),
            "Unit": "Count",
        },
    ]


class PressureMetrics:
    """Publishes transcode pressure metrics to CloudWatch (R16.4).

    The client is injected (real one built via
    :func:`create_cloudwatch_client`); the publisher carries no AWS dependency
    at import time and is exercised in tests with a fake client that records
    ``put_metric_data`` calls.
    """

    def __init__(
        self,
        namespace: str,
        client: CloudWatchClient,
        *,
        dimensions: list[dict[str, str]] | None = None,
    ) -> None:
        """Initialise with the metric namespace and an injected client.

        Args:
            namespace: CloudWatch namespace (e.g. ``HelloDJ/Transcode``).
            client: The injected CloudWatch client.
            dimensions: Optional default dimensions applied to every metric.
        """
        self._namespace = namespace
        self._client = client
        self._dimensions = dimensions or []

    @property
    def namespace(self) -> str:
        """The CloudWatch namespace metrics are published under."""
        return self._namespace

    def publish(self, snapshot: PressureSnapshot) -> None:
        """Publish CPU/GPU pressure for one snapshot to CloudWatch (R16.4).

        Args:
            snapshot: The scheduler pressure snapshot to publish.
        """
        self._client.put_metric_data(
            Namespace=self._namespace,
            MetricData=build_metric_data(
                snapshot, dimensions=self._dimensions
            ),
        )

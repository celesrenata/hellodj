"""Env-driven bootstrap for the orchestrator's CloudWatch metrics (degraded-safe).

Builds the shared :class:`PlatformMetrics` emitter (``HelloDJ/Bot`` namespace)
and the :class:`StreamMetricsPublisher` daemon thread that publishes active
audio/video stream + connected-client gauges (R10.3). Mirrors
``watchdog_bootstrap`` / ``instance_bootstrap``: no boto3 import at module load,
degrade to a no-op on any failure so the health server always comes up.

Env:

* ``HELLODJ_STAGE`` / ``AWS_REGION``  — standard component env (namespace dims).
* ``HELLODJ_SESSION_TABLE``           — session table to sample (defaults to
  ``hellodj-session``).
* ``BOT_METRICS_INTERVAL``            — seconds between stream-gauge samples
  (default 60).

The DAX read/write instrumentation is applied separately by
``playback_bootstrap`` wrapping its ``SessionTable`` in
:class:`~playback_orchestrator.metrics.InstrumentedSessionTable`.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from hellodj_platform_logic.platform_metrics import PlatformMetrics

from .metrics import (
    BOT_METRICS_NAMESPACE,
    StreamMetricsPublisher,
)
from .persistence import SessionState

_LOG = logging.getLogger("playback_orchestrator.metrics_bootstrap")

_DEFAULT_SESSION_TABLE = "hellodj-session"

__all__ = ["build_bot_metrics", "start_stream_metrics_thread"]


def build_bot_metrics() -> PlatformMetrics:
    """Build the ``HelloDJ/Bot`` emitter from env (no-op without boto3/creds)."""
    return PlatformMetrics.from_env(
        BOT_METRICS_NAMESPACE, component="playback-orchestrator"
    )


def sampler_for_table(table: Any) -> Any | None:
    """Return a ``list_sessions`` callable over a ``SessionTable``, or ``None``.

    Extracted so the sampler logic is unit-testable with an injected table
    (backed by a fake ddb) — no boto3. Returns ``None`` when the table lacks the
    ``scan_sessions`` read enumeration.
    """
    scan = getattr(table, "scan_sessions", None)
    if not callable(scan):
        return None

    def list_sessions() -> list[SessionState]:
        states: list[SessionState] = []
        # Only the SESSION items carry voice-channel/current info; the scan is
        # key+state projected and read-only (the orchestrator stays the single
        # writer of session state).
        for item in scan():
            raw = item.get("state", {}) if isinstance(item, dict) else {}
            states.append(SessionState.from_dict(raw if isinstance(raw, dict) else {}))
        return states

    return list_sessions


def _build_session_sampler() -> Any | None:
    """Build a callable returning current session states, or ``None`` (degraded).

    Uses the shared ``SessionTable.scan_sessions`` to enumerate ``SESSION`` items
    and map them to :class:`SessionState`. Returns ``None`` on any failure so
    the publisher degrades to a no-op.
    """
    table_name = (
        os.getenv("HELLODJ_SESSION_TABLE", "").strip() or _DEFAULT_SESSION_TABLE
    )
    try:
        import boto3
        from hellodj_platform_logic.data_access import SessionTable

        ddb = boto3.resource(
            "dynamodb", region_name=os.getenv("AWS_REGION", "us-east-1")
        )
        table = SessionTable(ddb.Table(table_name))
    except Exception:  # noqa: BLE001 - degrade: no sampler
        return None

    return sampler_for_table(table)


def start_stream_metrics_thread() -> StreamMetricsPublisher:
    """Build + start the stream-gauge publisher on a daemon thread.

    Returns the publisher (its ``stop()`` is wired into the shutdown path). When
    metrics are unconfigured or the sampler can't be built, the returned
    publisher is disabled and ``start()`` is a logged no-op — the health server
    is unaffected.
    """
    metrics = build_bot_metrics()
    sampler = _build_session_sampler()
    interval = _interval_seconds()
    publisher = StreamMetricsPublisher(metrics, sampler, interval=interval)
    publisher.start()
    return publisher


def _interval_seconds() -> float:
    raw = os.getenv("BOT_METRICS_INTERVAL", "").strip()
    if not raw:
        return 60.0
    try:
        value = float(raw)
    except ValueError:
        return 60.0
    return value if value > 0 else 60.0

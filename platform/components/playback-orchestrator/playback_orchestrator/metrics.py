"""CloudWatch metrics for the playback-orchestrator (bot runtime KPIs).

Publishes the bot's operational KPIs into the per-stage ``HelloDJ/Bot`` CloudWatch
namespace (stage-dimensioned so Beta/Staging/Production never intermingle) using
the shared :class:`hellodj_platform_logic.platform_metrics.PlatformMetrics`
emitter. The per-stage ObservabilityStack reads this namespace for dashboards +
alarms (R10.3).

Two mechanisms:

* :class:`InstrumentedSessionTable` — a transparent wrapper around the shared
  DAX-fronted ``SessionTable`` that records every DAX read/write/error +
  latency (``DaxReads`` / ``DaxWrites`` / ``DaxErrors`` / ``DaxLatencyMs``).
* :class:`StreamMetricsPublisher` — a periodic gauge publisher (run on a daemon
  thread, mirroring the token watchdog) that samples the session store and
  publishes active audio streams, active video streams, and connected voice
  clients (``ActiveAudioStreams`` / ``ActiveVideoStreams`` / ``ConnectedClients``
  / ``ActiveSessions``).

Every publish is best-effort (the shared emitter swallows failures), so metrics
never break playback or the health server.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from hellodj_platform_logic.platform_metrics import PlatformMetrics

__all__ = [
    "BOT_METRICS_NAMESPACE",
    "InstrumentedSessionTable",
    "StreamMetricsPublisher",
    "count_streams",
]

_LOG = logging.getLogger("playback_orchestrator.metrics")

BOT_METRICS_NAMESPACE = "HelloDJ/Bot"

#: Default interval between stream-gauge samples (seconds).
_DEFAULT_PUBLISH_INTERVAL = 60.0


class InstrumentedSessionTable:
    """A DAX read/write/error-recording wrapper around ``SessionTable``.

    Delegates every attribute to the wrapped table, but records DAX access
    metrics for the hot-path methods (``get`` reads, ``put_state`` writes) to
    CloudWatch (``HelloDJ/Bot``). Unknown attributes pass through unchanged, so
    it is a drop-in for anything holding a ``SessionTable``.
    """

    _READ_METHODS = frozenset({"get", "batch_get", "query"})
    _WRITE_METHODS = frozenset({"put_state", "put", "delete", "update"})

    def __init__(self, table: Any, metrics: PlatformMetrics) -> None:
        self._table = table
        self._metrics = metrics

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._table, name)
        if not callable(attr):
            return attr
        if name in self._READ_METHODS:
            return self._wrap(name, attr, kind="read")
        if name in self._WRITE_METHODS:
            return self._wrap(name, attr, kind="write")
        return attr

    def _wrap(self, name: str, fn: Any, *, kind: str) -> Any:
        metrics = self._metrics

        def wrapper(*args: Any, **kwargs: Any) -> Any:
            start = time.perf_counter()
            try:
                result = fn(*args, **kwargs)
            except Exception:
                metrics.count("DaxErrors", dimensions={"Op": name})
                raise
            finally:
                elapsed_ms = (time.perf_counter() - start) * 1000.0
                metrics.gauge(
                    "DaxLatencyMs",
                    elapsed_ms,
                    unit="Milliseconds",
                    dimensions={"Op": name},
                )
            metric = "DaxReads" if kind == "read" else "DaxWrites"
            metrics.count(metric, dimensions={"Op": name})
            return result

        return wrapper


def count_streams(sessions: list[Any]) -> dict[str, int]:
    """Classify a list of session states into stream/client counts (pure).

    A session with a bound ``voice_channel_id`` counts as a connected client.
    Its ``current`` item's ``content_type`` classifies it as an audio or video
    stream (defaulting to audio when a track is playing without a type).

    Args:
        sessions: A list of objects exposing ``voice_channel_id`` and a
            ``current`` mapping (e.g. ``SessionState``).

    Returns:
        A mapping with ``ActiveSessions``, ``ConnectedClients``,
        ``ActiveAudioStreams``, and ``ActiveVideoStreams`` counts.
    """
    connected = 0
    audio = 0
    video = 0
    for s in sessions:
        if getattr(s, "voice_channel_id", None) is not None:
            connected += 1
        current = getattr(s, "current", None)
        if isinstance(current, dict) and current:
            ctype = str(current.get("content_type", "audio")).lower()
            if ctype == "video":
                video += 1
            else:
                audio += 1
    return {
        "ActiveSessions": len(sessions),
        "ConnectedClients": connected,
        "ActiveAudioStreams": audio,
        "ActiveVideoStreams": video,
    }


class StreamMetricsPublisher:
    """Periodically publishes active stream/client gauges to CloudWatch.

    Runs one sampling pass per :meth:`tick`; :meth:`start` spawns a daemon
    thread that ticks every ``interval`` seconds until :meth:`stop`. Sampling
    calls ``list_sessions()`` — an injected callable returning the current
    session states — so the publisher is testable without a live table and
    degrades to a no-op when no sampler is provided.
    """

    def __init__(
        self,
        metrics: PlatformMetrics,
        list_sessions: Any | None,
        *,
        interval: float = _DEFAULT_PUBLISH_INTERVAL,
    ) -> None:
        self._metrics = metrics
        self._list_sessions = list_sessions
        self._interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def enabled(self) -> bool:
        """Whether the publisher will actually sample + publish."""
        return (
            self._metrics.enabled
            and self._list_sessions is not None
            and self._interval > 0
        )

    def tick(self) -> dict[str, int] | None:
        """Sample the session store once and publish the stream gauges.

        Returns the published counts (for tests), or ``None`` when disabled or
        the sampler failed (best-effort — never raises).
        """
        if self._list_sessions is None or not self._metrics.enabled:
            return None
        try:
            sessions = list(self._list_sessions())
        except Exception:  # noqa: BLE001 - sampling must never crash the thread
            _LOG.debug("stream metrics sample failed")
            return None
        counts = count_streams(sessions)
        for name, value in counts.items():
            self._metrics.gauge(name, float(value), unit="Count")
        return counts

    def start(self) -> None:
        """Start the daemon sampling thread (no-op when disabled)."""
        if not self.enabled or self._thread is not None:
            if not self.enabled:
                _LOG.info("degraded: stream metrics publisher disabled")
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="stream-metrics", daemon=True
        )
        self._thread.start()
        _LOG.info(
            "stream metrics publisher started (interval=%ss, namespace=%s)",
            self._interval,
            self._metrics.namespace,
        )

    def stop(self) -> None:
        """Signal the sampling thread to stop (best-effort join)."""
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=self._interval + 1.0)
            self._thread = None

    def _run(self) -> None:
        while not self._stop.is_set():
            self.tick()
            self._stop.wait(self._interval)

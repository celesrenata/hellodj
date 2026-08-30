"""CloudWatch metrics instrumentation for the web-ui Flask app.

Wires the shared :class:`hellodj_platform_logic.platform_metrics.PlatformMetrics`
emitter into the request lifecycle so the per-stage ObservabilityStack dashboards
and alarms see live web-ui KPIs (R10.3). Published into the ``HelloDJ/WebUI``
namespace (stage-dimensioned so Beta/Staging/Production never intermingle):

* ``HttpRequests``     — one Count per response, dimensioned by ``Status`` class
  (``2xx``/``3xx``/``4xx``/``5xx``) and HTTP ``Method``.
* ``HttpServerErrors`` — Count of 5xx responses (server-side errors).
* ``HttpClientErrors`` — Count of 4xx responses.
* ``UnhandledExceptions`` — Count of exceptions that escaped a view (teardown
  with an error), i.e. server-side errors before a response is produced.
* ``RequestLatencyMs`` — per-request wall-clock latency (Milliseconds).

Database read/write/error metrics (``DbReads``/``DbWrites``/``DbErrors`` +
``DbLatencyMs``) are emitted by :class:`InstrumentedCoreTable`, a thin wrapper
around the shared ``CoreTable`` that records each DynamoDB operation. Every
publish is best-effort (the shared emitter swallows failures), so instrumentation
never breaks a request.
"""

from __future__ import annotations

import time
from typing import Any

from flask import Flask, g, request
from hellodj_platform_logic.platform_metrics import PlatformMetrics

__all__ = ["WEBUI_METRICS_NAMESPACE", "register_metrics", "InstrumentedCoreTable"]

WEBUI_METRICS_NAMESPACE = "HelloDJ/WebUI"


def _status_class(code: int) -> str:
    """Bucket an HTTP status code into a ``2xx``/``3xx``/``4xx``/``5xx`` class."""
    return f"{code // 100}xx"


def register_metrics(app: Flask, metrics: PlatformMetrics | None = None) -> PlatformMetrics:
    """Register request/response/error metric hooks on the Flask app.

    Args:
        app: The Flask application.
        metrics: Optional pre-built emitter (tests inject a fake-backed one);
            when omitted, one is built from the component env.

    Returns:
        The :class:`PlatformMetrics` instance registered on the app (also stored
        at ``app.extensions['metrics']``).
    """
    emitter = metrics or PlatformMetrics.from_env(
        WEBUI_METRICS_NAMESPACE, component="web-ui"
    )
    app.extensions["metrics"] = emitter

    @app.before_request
    def _start_timer() -> None:  # type: ignore[unused-ignore]
        g._metrics_start = time.perf_counter()

    @app.after_request
    def _record_response(response: Any) -> Any:  # type: ignore[unused-ignore]
        status = getattr(response, "status_code", 0) or 0
        method = request.method
        status_class = _status_class(status)
        emitter.count(
            "HttpRequests",
            dimensions={"Status": status_class, "Method": method},
        )
        if 500 <= status < 600:
            emitter.count("HttpServerErrors", dimensions={"Method": method})
        elif 400 <= status < 500:
            emitter.count("HttpClientErrors", dimensions={"Method": method})

        start = getattr(g, "_metrics_start", None)
        if start is not None:
            elapsed_ms = (time.perf_counter() - start) * 1000.0
            emitter.gauge("RequestLatencyMs", elapsed_ms, unit="Milliseconds")
        return response

    @app.teardown_request
    def _record_exception(exc: BaseException | None) -> None:  # type: ignore[unused-ignore]
        # An exception reaching teardown is a server-side error that may not
        # have produced a 5xx response yet — count it explicitly.
        if exc is not None:
            emitter.count(
                "UnhandledExceptions",
                dimensions={"Type": type(exc).__name__},
            )

    return emitter


class InstrumentedCoreTable:
    """A metrics-recording wrapper around the shared ``CoreTable``.

    Delegates every attribute to the wrapped table, but records DynamoDB read /
    write / error counts + latency to CloudWatch (``HelloDJ/WebUI``) for the
    common access methods. Unknown attributes pass through unchanged, so this is
    a drop-in for anything holding a ``CoreTable``. Read vs write is classified
    by method name; anything not classified is still latency-timed as an
    ``other`` op.
    """

    _READ_METHODS = frozenset(
        {"get_item", "query", "query_pk_prefix", "scan_entity", "batch_get"}
    )
    _WRITE_METHODS = frozenset(
        {"put_item", "update_item", "delete", "delete_item", "transact_write"}
    )

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
                metrics.count("DbErrors", dimensions={"Op": name})
                raise
            finally:
                elapsed_ms = (time.perf_counter() - start) * 1000.0
                metrics.gauge(
                    "DbLatencyMs",
                    elapsed_ms,
                    unit="Milliseconds",
                    dimensions={"Op": name},
                )
            metric = "DbReads" if kind == "read" else "DbWrites"
            metrics.count(metric, dimensions={"Op": name})
            return result

        return wrapper

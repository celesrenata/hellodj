"""Tests for the web-ui CloudWatch metrics middleware + DB instrumentation."""

from __future__ import annotations

from typing import Any

from flask import Flask
from hellodj_platform_logic.platform_metrics import PlatformMetrics

from metrics_middleware import (
    InstrumentedCoreTable,
    register_metrics,
)


class _FakeCloudWatch:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def put_metric_data(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)

    def metric_names(self) -> list[str]:
        names: list[str] = []
        for call in self.calls:
            names.extend(d["MetricName"] for d in call["MetricData"])
        return names


def _app_with_metrics() -> tuple[Flask, _FakeCloudWatch]:
    fake = _FakeCloudWatch()
    metrics = PlatformMetrics("HelloDJ/WebUI", fake, stage="beta", component="web-ui")
    app = Flask(__name__)

    @app.route("/ok")
    def ok():
        return "ok", 200

    @app.route("/missing")
    def missing():
        return "nope", 404

    @app.route("/boom")
    def boom():
        raise RuntimeError("kaboom")

    register_metrics(app, metrics)
    return app, fake


def test_2xx_request_publishes_httprequests_and_latency() -> None:
    app, fake = _app_with_metrics()
    app.test_client().get("/ok")
    names = fake.metric_names()
    assert "HttpRequests" in names
    assert "RequestLatencyMs" in names
    # No error metrics for a 200.
    assert "HttpServerErrors" not in names
    assert "HttpClientErrors" not in names


def test_4xx_request_publishes_client_error() -> None:
    app, fake = _app_with_metrics()
    app.test_client().get("/missing")
    names = fake.metric_names()
    assert "HttpClientErrors" in names
    assert "HttpServerErrors" not in names


def test_unhandled_exception_publishes_server_error_metrics() -> None:
    app, fake = _app_with_metrics()
    app.config["PROPAGATE_EXCEPTIONS"] = False
    app.test_client().get("/boom")
    names = fake.metric_names()
    # Flask turns the raised error into a 500 response AND teardown sees the exc.
    assert "HttpServerErrors" in names
    assert "UnhandledExceptions" in names


class _FakeTable:
    def get_item(self, **kwargs: Any) -> dict[str, Any]:
        return {"ok": True}

    def put_item(self, **kwargs: Any) -> None:
        return None

    def boom_write(self, **kwargs: Any) -> None:
        raise RuntimeError("ddb down")

    # A non-callable attribute passes through unchanged.
    table_name = "hellodj-core-beta"


def test_instrumented_core_table_counts_reads_and_writes() -> None:
    fake = _FakeCloudWatch()
    metrics = PlatformMetrics("HelloDJ/WebUI", fake, stage="beta")
    table = InstrumentedCoreTable(_FakeTable(), metrics)

    assert table.get_item(Key={"PK": "x"}) == {"ok": True}
    table.put_item(Item={"PK": "x"})
    # Passthrough of non-callable attribute.
    assert table.table_name == "hellodj-core-beta"

    names = fake.metric_names()
    assert "DbReads" in names
    assert "DbWrites" in names
    assert "DbLatencyMs" in names


def test_instrumented_core_table_counts_errors_and_reraises() -> None:
    fake = _FakeCloudWatch()
    metrics = PlatformMetrics("HelloDJ/WebUI", fake, stage="beta")
    # Classify boom_write as a write by adding it to the write set for the test.
    table = InstrumentedCoreTable(_FakeTable(), metrics)
    # boom_write isn't in the read/write sets → passes through un-instrumented;
    # verify the classified write path instead by wrapping delete.

    class _Boom:
        def delete(self, **kwargs: Any) -> None:
            raise RuntimeError("ddb down")

    t2 = InstrumentedCoreTable(_Boom(), metrics)
    try:
        t2.delete(Key={"PK": "x"})
        raise AssertionError("delete should have re-raised")
    except RuntimeError:
        pass
    assert "DbErrors" in fake.metric_names()
    # Unused local to keep the first table referenced.
    assert isinstance(table, InstrumentedCoreTable)

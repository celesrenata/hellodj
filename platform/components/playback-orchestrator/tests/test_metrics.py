"""Tests for the playback-orchestrator CloudWatch metrics (bot KPIs + DAX)."""

from __future__ import annotations

from typing import Any

from hellodj_platform_logic.platform_metrics import PlatformMetrics

from playback_orchestrator.metrics import (
    InstrumentedSessionTable,
    StreamMetricsPublisher,
    count_streams,
)
from playback_orchestrator.persistence import SessionState


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


def _metrics() -> tuple[PlatformMetrics, _FakeCloudWatch]:
    fake = _FakeCloudWatch()
    return PlatformMetrics("HelloDJ/Bot", fake, stage="beta"), fake


def test_count_streams_classifies_audio_video_and_clients() -> None:
    sessions = [
        SessionState(voice_channel_id=1, current={"content_type": "audio"}),
        SessionState(voice_channel_id=2, current={"content_type": "video"}),
        SessionState(voice_channel_id=3, current={"title": "x"}),  # defaults audio
        SessionState(voice_channel_id=None, current=None),  # idle, not connected
    ]
    counts = count_streams(sessions)
    assert counts == {
        "ActiveSessions": 4,
        "ConnectedClients": 3,
        "ActiveAudioStreams": 2,
        "ActiveVideoStreams": 1,
    }


def test_stream_publisher_tick_publishes_gauges() -> None:
    metrics, fake = _metrics()
    sessions = [
        SessionState(voice_channel_id=1, current={"content_type": "audio"}),
        SessionState(voice_channel_id=2, current={"content_type": "video"}),
    ]
    pub = StreamMetricsPublisher(metrics, lambda: sessions, interval=60)
    counts = pub.tick()
    assert counts["ActiveAudioStreams"] == 1
    assert counts["ActiveVideoStreams"] == 1
    names = fake.metric_names()
    for expected in (
        "ActiveSessions",
        "ConnectedClients",
        "ActiveAudioStreams",
        "ActiveVideoStreams",
    ):
        assert expected in names


def test_stream_publisher_disabled_without_sampler() -> None:
    metrics, _ = _metrics()
    pub = StreamMetricsPublisher(metrics, None, interval=60)
    assert pub.enabled is False
    assert pub.tick() is None
    pub.start()  # no-op, must not raise
    pub.stop()


def test_stream_publisher_tick_swallows_sampler_error() -> None:
    metrics, _ = _metrics()

    def _boom() -> list[SessionState]:
        raise RuntimeError("scan failed")

    pub = StreamMetricsPublisher(metrics, _boom, interval=60)
    assert pub.tick() is None  # error swallowed


class _FakeSessionTable:
    def get(self, pk: str, sk: str) -> dict[str, Any] | None:
        return {"pk": pk, "sk": sk}

    def put_state(self, pk: str, sk: str, mutator: Any) -> dict[str, Any]:
        return {"state": mutator({})}

    def boom_get_raises(self) -> None:
        raise RuntimeError("dax down")

    version = "v1"  # non-callable passthrough


def test_instrumented_session_table_counts_reads_writes() -> None:
    metrics, fake = _metrics()
    table = InstrumentedSessionTable(_FakeSessionTable(), metrics)

    table.get("GUILD#1", "SESSION")
    table.put_state("GUILD#1", "QUEUE", lambda _c: {"items": []})
    assert table.version == "v1"  # passthrough

    names = fake.metric_names()
    assert "DaxReads" in names
    assert "DaxWrites" in names
    assert "DaxLatencyMs" in names


def test_instrumented_session_table_counts_errors() -> None:
    metrics, fake = _metrics()

    class _Boom:
        def get(self, *_a: Any, **_k: Any) -> None:
            raise RuntimeError("dax down")

    table = InstrumentedSessionTable(_Boom(), metrics)
    try:
        table.get("GUILD#1", "SESSION")
        raise AssertionError("get should have re-raised")
    except RuntimeError:
        pass
    assert "DaxErrors" in fake.metric_names()


class _ScanFakeDdb:
    """Minimal ddb table honoring the SESSION-scan filter for an end-to-end test."""

    def __init__(self, items: list[dict[str, Any]]) -> None:
        self._items = items

    def scan(self, **kwargs: Any) -> dict[str, Any]:
        assert kwargs["FilterExpression"] == "SK = :sk"
        wanted = kwargs["ExpressionAttributeValues"][":sk"]
        rows = [it for it in self._items if it.get("SK") == wanted]
        return {"Items": rows}

    def get_item(self, **kwargs: Any) -> dict[str, Any]:  # pragma: no cover
        return {}

    def put_item(self, **kwargs: Any) -> dict[str, Any]:  # pragma: no cover
        return {}


def test_end_to_end_sampler_to_gauges_produces_expected_counts() -> None:
    # Prove the WHOLE bot path produces output: real SessionTable.scan_sessions
    # -> sampler_for_table -> StreamMetricsPublisher.tick -> published gauges.
    from hellodj_platform_logic.data_access import SessionTable

    from playback_orchestrator.metrics_bootstrap import sampler_for_table

    ddb = _ScanFakeDdb(
        [
            {
                "PK": "GUILD#1",
                "SK": "SESSION",
                "state": {"voice_channel_id": 10, "current": {"content_type": "audio"}},
            },
            {
                "PK": "GUILD#2",
                "SK": "SESSION",
                "state": {"voice_channel_id": 20, "current": {"content_type": "video"}},
            },
            {"PK": "GUILD#1", "SK": "QUEUE", "state": {"items": [1, 2]}},
        ]
    )
    table = SessionTable(ddb, None)
    sampler = sampler_for_table(table)
    assert sampler is not None

    metrics, fake = _metrics()
    pub = StreamMetricsPublisher(metrics, sampler, interval=60)
    counts = pub.tick()

    assert counts == {
        "ActiveSessions": 2,
        "ConnectedClients": 2,
        "ActiveAudioStreams": 1,
        "ActiveVideoStreams": 1,
    }
    # And the gauges were actually PUBLISHED to CloudWatch.
    names = fake.metric_names()
    for expected in (
        "ActiveSessions",
        "ConnectedClients",
        "ActiveAudioStreams",
        "ActiveVideoStreams",
    ):
        assert expected in names

"""Unit tests for the distributed search-cache accelerator.

Covers the three behaviors the accelerator wires into the bot's search:

1. Immediate serve on a cache hit (no provider dispatch).
2. Backfill of misses so another instance is served instantly.
3. Failure tracking: a track failing FAILURE_EVICTION_THRESHOLD times is
   evicted from every result the accelerator serves.
"""

from __future__ import annotations

from typing import Any

import pytest
from search.accelerator import (
    FAILURE_EVICTION_THRESHOLD,
    SearchCacheAccelerator,
)
from search.models import SearchResult


class FakeSearchCache:
    """In-memory stand-in for the shared ``SearchCacheTable`` (no boto3)."""

    def __init__(self) -> None:
        self.store: dict[str, dict[str, Any]] = {}
        self.put_calls = 0

    def get_results(self, query_key: str) -> dict[str, Any] | None:
        item = self.store.get(query_key)
        return dict(item) if item is not None else None

    def put(self, query_key: str, results: dict[str, Any], *, ttl: int | None = None):
        self.put_calls += 1
        self.store[query_key] = dict(results)
        return {"queryKey": query_key, "results": dict(results), "ttl": ttl}


def _track(provider: str, track_id: str, title: str = "Song") -> SearchResult:
    return SearchResult(
        title=title,
        artist="Artist",
        provider=provider,
        track_id=track_id,
    )


@pytest.fixture
def accel() -> tuple[SearchCacheAccelerator, FakeSearchCache]:
    table = FakeSearchCache()
    return SearchCacheAccelerator(table), table


class TestQueryKey:
    def test_key_normalizes_query_and_filters(self):
        k = SearchCacheAccelerator.query_key(
            "  Daft   Punk  ",
            provider_filter=None,
            content_type="tracks",
            sort_order="relevance",
        )
        assert k == "daft punk|all|tracks|relevance"

    def test_filters_produce_distinct_keys(self):
        base = {"content_type": "tracks", "sort_order": "relevance"}
        k1 = SearchCacheAccelerator.query_key("q", provider_filter="spotify", **base)
        k2 = SearchCacheAccelerator.query_key("q", provider_filter="tidal", **base)
        assert k1 != k2


class TestServeAndBackfill:
    def test_miss_returns_none(self, accel):
        acc, _ = accel
        assert acc.get("nothing here") is None

    def test_put_then_get_round_trip(self, accel):
        acc, _ = accel
        results = [_track("spotify", "a1"), _track("youtube", "b2")]
        acc.put("daft punk", results)

        served = acc.get("daft punk")
        assert served is not None
        assert [(r.provider, r.track_id) for r in served] == [
            ("spotify", "a1"),
            ("youtube", "b2"),
        ]

    def test_backfill_is_shared_across_instances(self):
        # Two accelerator instances over the SAME table (two bot pods).
        table = FakeSearchCache()
        pod_a = SearchCacheAccelerator(table)
        pod_b = SearchCacheAccelerator(table)

        pod_a.put("query", [_track("tidal", "t9")])
        served = pod_b.get("query")

        assert served is not None
        assert served[0].track_id == "t9"

    def test_empty_results_not_written(self, accel):
        acc, table = accel
        acc.put("query", [])
        assert table.put_calls == 0

    def test_filter_isolation(self, accel):
        acc, _ = accel
        acc.put("q", [_track("spotify", "s1")], provider_filter="spotify")
        assert acc.get("q", provider_filter="tidal") is None
        assert acc.get("q", provider_filter="spotify") is not None


class TestFailureEvictionThreshold:
    def test_default_threshold_is_three(self):
        assert FAILURE_EVICTION_THRESHOLD == 3

    def test_failures_increment_count(self, accel):
        acc, _ = accel
        assert acc.record_failure("youtube", "v1") == 1
        assert acc.record_failure("youtube", "v1") == 2
        assert acc.record_failure("youtube", "v1") == 3

    def test_track_evicted_after_three_failures(self, accel):
        acc, _ = accel
        acc.put("q", [_track("youtube", "v1"), _track("spotify", "s2")])

        # Two failures: still served.
        acc.record_failure("youtube", "v1")
        acc.record_failure("youtube", "v1")
        served = acc.get("q")
        assert served is not None
        assert {r.track_id for r in served} == {"v1", "s2"}

        # Third failure crosses the threshold: v1 is filtered out.
        acc.record_failure("youtube", "v1")
        served = acc.get("q")
        assert served is not None
        assert {r.track_id for r in served} == {"s2"}

    def test_all_tracks_evicted_returns_none(self, accel):
        acc, _ = accel
        acc.put("q", [_track("youtube", "v1")])
        for _ in range(FAILURE_EVICTION_THRESHOLD):
            acc.record_failure("youtube", "v1")
        # Filtering empties the entry → None so caller does a fresh fan-out.
        assert acc.get("q") is None

    def test_evicted_track_not_written_back(self, accel):
        acc, table = accel
        for _ in range(FAILURE_EVICTION_THRESHOLD):
            acc.record_failure("youtube", "v1")
        table.put_calls = 0
        # A subsequent backfill must drop the evicted track.
        acc.put("q", [_track("youtube", "v1"), _track("spotify", "s2")])
        served = acc.get("q")
        assert served is not None
        assert {r.track_id for r in served} == {"s2"}

    def test_eviction_is_per_track_not_per_query(self, accel):
        acc, _ = accel
        acc.put("query one", [_track("youtube", "v1")])
        acc.put("query two", [_track("youtube", "v1")])
        for _ in range(FAILURE_EVICTION_THRESHOLD):
            acc.record_failure("youtube", "v1")
        # Evicted from BOTH queries regardless of which one surfaced it.
        assert acc.get("query one") is None
        assert acc.get("query two") is None

    def test_empty_track_id_is_noop(self, accel):
        acc, table = accel
        assert acc.record_failure("youtube", "") == 0
        assert table.put_calls == 0


class TestBestEffortErrorHandling:
    def test_get_swallows_table_errors(self):
        class Boom:
            def get_results(self, query_key):
                raise RuntimeError("dynamo down")

            def put(self, *a, **k):
                raise RuntimeError("dynamo down")

        acc = SearchCacheAccelerator(Boom())
        assert acc.get("q") is None
        # put + record_failure never raise
        acc.put("q", [_track("spotify", "s1")])
        assert acc.record_failure("spotify", "s1") == 0

    def test_malformed_cached_track_dropped(self, accel):
        acc, table = accel
        key = SearchCacheAccelerator.query_key(
            "q", provider_filter=None, content_type="tracks", sort_order="relevance"
        )
        table.store[key] = {
            "tracks": [
                {"no_title": True},
                {"title": "ok", "artist": "A", "provider": "spotify", "track_id": "s1"},
            ]
        }
        served = acc.get("q")
        assert served is not None
        assert len(served) == 1
        assert served[0].title == "ok"

    def test_invalid_threshold_rejected(self):
        with pytest.raises(ValueError):
            SearchCacheAccelerator(FakeSearchCache(), failure_threshold=0)

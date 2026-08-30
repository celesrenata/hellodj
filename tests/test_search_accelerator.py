"""Unit tests for the provider-aware distributed search-cache accelerator.

Under the flatten-stage-topology layout the accelerator holds ONE cache table
per SEARCH provider (``hellodj-search-cache-<stage>-<provider>``) instead of a
single shared table. These tests exercise the provider-aware API:

1. Per-provider routing: a ``put`` of multi-provider results writes each
   provider's tracks into THAT provider's table; a ``get`` merges them back;
   ``record_failure``/eviction land in the correct provider's table.
2. Per-provider degradation: a provider absent from the map degrades to a
   miss/skip/no-op without raising; an empty map yields ``None`` (pure fan-out).
3. Serialization round-trip + failure-eviction threshold, adapted to the
   per-provider tables.

The accelerator is constructed with a MAP of provider -> in-memory fake table
(a small :class:`FakeSearchCache` implementing the ``SearchCacheLike`` seam:
``get_results``/``put``), so these tests never need boto3.
"""

from __future__ import annotations

from typing import Any

import pytest
from search.accelerator import (
    FAILURE_EVICTION_THRESHOLD,
    SearchCacheAccelerator,
    build_search_cache_accelerator,
)
from search.models import SearchResult

# The search providers exercised by these tests. This mirrors the shared
# SEARCH_CACHE_PROVIDERS list (spotify, tidal, youtube, youtube_music,
# soundcloud) but only the subset the tests actually touch is instantiated.
_PROVIDERS = ("spotify", "tidal", "youtube")


class FakeSearchCache:
    """In-memory stand-in for one provider's ``SearchCacheTable`` (no boto3)."""

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


def _make_tables(*providers: str) -> dict[str, FakeSearchCache]:
    """Build a provider -> fake-table map for the given providers."""
    return {p: FakeSearchCache() for p in (providers or _PROVIDERS)}


@pytest.fixture
def tables() -> dict[str, FakeSearchCache]:
    return _make_tables()


@pytest.fixture
def accel(tables) -> tuple[SearchCacheAccelerator, dict[str, FakeSearchCache]]:
    return SearchCacheAccelerator(tables), tables


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


class TestProviderSet:
    def test_providers_reflects_the_table_map(self, accel):
        acc, _ = accel
        assert set(acc.providers) == set(_PROVIDERS)

    def test_empty_map_has_no_providers(self):
        acc = SearchCacheAccelerator({})
        assert acc.providers == ()


class TestPerProviderRouting:
    def test_miss_returns_none(self, accel):
        acc, _ = accel
        assert acc.get("nothing here") is None

    def test_put_routes_each_track_to_its_provider_table(self, accel):
        acc, tables = accel
        acc.put("daft punk", [_track("spotify", "a1"), _track("youtube", "b2")])

        key = SearchCacheAccelerator.query_key(
            "daft punk",
            provider_filter=None,
            content_type="tracks",
            sort_order="relevance",
        )
        # Spotify's track landed ONLY in the spotify table; youtube's ONLY in
        # the youtube table; tidal was untouched.
        spotify_payload = tables["spotify"].store[key]
        youtube_payload = tables["youtube"].store[key]
        assert [t["track_id"] for t in spotify_payload["tracks"]] == ["a1"]
        assert [t["track_id"] for t in youtube_payload["tracks"]] == ["b2"]
        assert key not in tables["tidal"].store
        assert tables["tidal"].put_calls == 0

    def test_get_merges_slices_across_provider_tables(self, accel):
        acc, _ = accel
        acc.put("daft punk", [_track("spotify", "a1"), _track("youtube", "b2")])

        served = acc.get("daft punk")
        assert served is not None
        # Merged in the map's provider order (spotify, tidal, youtube).
        assert [(r.provider, r.track_id) for r in served] == [
            ("spotify", "a1"),
            ("youtube", "b2"),
        ]

    def test_provider_filter_reads_only_that_provider_table(self, accel):
        acc, _ = accel
        # An "all" fan-out resolved both a spotify and a tidal slice under the
        # unfiltered query key; separately a spotify-filtered query resolved a
        # spotify-only slice under the spotify key.
        acc.put("q", [_track("spotify", "s1"), _track("tidal", "t1")])
        acc.put("q", [_track("spotify", "s9")], provider_filter="spotify")

        # A spotify-filtered get reads ONLY the spotify table under the
        # spotify-scoped key — it must surface s9 and never the tidal slice.
        served = acc.get("q", provider_filter="spotify")
        assert served is not None
        assert [(r.provider, r.track_id) for r in served] == [("spotify", "s9")]

        # The tidal slice under the "all" key is untouched by the filtered read.
        assert acc.get("q", provider_filter="tidal") is None

    def test_backfill_is_shared_across_instances(self):
        # Two accelerator instances over the SAME provider tables (two pods).
        tables = _make_tables()
        pod_a = SearchCacheAccelerator(tables)
        pod_b = SearchCacheAccelerator(tables)

        pod_a.put("query", [_track("tidal", "t9")])
        served = pod_b.get("query")

        assert served is not None
        assert served[0].track_id == "t9"

    def test_empty_results_not_written(self, accel):
        acc, tables = accel
        acc.put("query", [])
        assert all(t.put_calls == 0 for t in tables.values())

    def test_filter_key_isolation(self, accel):
        acc, _ = accel
        acc.put("q", [_track("spotify", "s1")], provider_filter="spotify")
        # A tidal-filtered read looks under a DIFFERENT query key → miss.
        assert acc.get("q", provider_filter="tidal") is None
        assert acc.get("q", provider_filter="spotify") is not None


class TestPerProviderDegradation:
    def test_absent_provider_on_put_is_skipped_not_raised(self):
        # Only spotify has a table; a youtube track has nowhere to go.
        tables = _make_tables("spotify")
        acc = SearchCacheAccelerator(tables)
        acc.put("q", [_track("spotify", "s1"), _track("youtube", "y1")])

        served = acc.get("q")
        assert served is not None
        # Spotify accelerated; youtube degraded to fan-out (not stored/served).
        assert [(r.provider, r.track_id) for r in served] == [("spotify", "s1")]

    def test_absent_provider_filtered_get_is_a_miss(self):
        tables = _make_tables("spotify")
        acc = SearchCacheAccelerator(tables)
        # youtube has no table → filtered read degrades to a miss (fan-out).
        assert acc.get("q", provider_filter="youtube") is None

    def test_absent_provider_record_failure_is_noop(self):
        tables = _make_tables("spotify")
        acc = SearchCacheAccelerator(tables)
        # youtube absent → record_failure is a no-op returning 0, never raises.
        assert acc.record_failure("youtube", "y1") == 0

    def test_empty_map_is_pure_fanout(self):
        acc = SearchCacheAccelerator({})
        acc.put("q", [_track("spotify", "s1")])  # no-op
        assert acc.get("q") is None
        assert acc.record_failure("spotify", "s1") == 0


class TestBuildSearchCacheAccelerator:
    def test_none_when_no_prefix(self, monkeypatch):
        # Absent HELLODJ_SEARCH_CACHE_TABLE_PREFIX → None (pure fan-out).
        monkeypatch.delenv("HELLODJ_SEARCH_CACHE_TABLE_PREFIX", raising=False)
        assert build_search_cache_accelerator() is None

    def test_none_when_prefix_but_no_boto3(self, monkeypatch):
        # Prefix set but boto3 / hellodj_platform_logic absent in this env →
        # the lazy import fails and the builder degrades to None (pure fan-out).
        monkeypatch.setenv(
            "HELLODJ_SEARCH_CACHE_TABLE_PREFIX", "hellodj-search-cache-beta"
        )
        assert build_search_cache_accelerator() is None


class TestFailureEvictionThreshold:
    def test_default_threshold_is_three(self):
        assert FAILURE_EVICTION_THRESHOLD == 3

    def test_failures_increment_count_in_provider_table(self, accel):
        acc, tables = accel
        assert acc.record_failure("youtube", "v1") == 1
        assert acc.record_failure("youtube", "v1") == 2
        assert acc.record_failure("youtube", "v1") == 3
        # The failure counter lives in the youtube table only.
        assert tables["youtube"].put_calls >= 3
        fkey = SearchCacheAccelerator._failure_key("youtube", "v1")
        assert tables["youtube"].store[fkey]["count"] == 3
        assert fkey not in tables["spotify"].store

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

    def test_eviction_is_scoped_to_the_failing_provider(self, accel):
        acc, _ = accel
        # Same track_id under two DIFFERENT providers.
        acc.put("q", [_track("youtube", "dup"), _track("spotify", "dup")])
        for _ in range(FAILURE_EVICTION_THRESHOLD):
            acc.record_failure("youtube", "dup")

        served = acc.get("q")
        assert served is not None
        # Only the youtube "dup" is evicted; the spotify "dup" survives because
        # the failure counter lives in the youtube table, keyed (provider, id).
        assert [(r.provider, r.track_id) for r in served] == [("spotify", "dup")]

    def test_all_tracks_evicted_returns_none(self, accel):
        acc, _ = accel
        acc.put("q", [_track("youtube", "v1")])
        for _ in range(FAILURE_EVICTION_THRESHOLD):
            acc.record_failure("youtube", "v1")
        # Filtering empties the entry → None so caller does a fresh fan-out.
        assert acc.get("q") is None

    def test_evicted_track_not_written_back(self, accel):
        acc, tables = accel
        for _ in range(FAILURE_EVICTION_THRESHOLD):
            acc.record_failure("youtube", "v1")
        tables["youtube"].put_calls = 0
        tables["spotify"].put_calls = 0
        # A subsequent backfill must drop the evicted youtube track but keep s2.
        acc.put("q", [_track("youtube", "v1"), _track("spotify", "s2")])
        served = acc.get("q")
        assert served is not None
        assert {r.track_id for r in served} == {"s2"}
        # Nothing was written to the youtube table for this query key.
        key = SearchCacheAccelerator.query_key(
            "q", provider_filter=None, content_type="tracks", sort_order="relevance"
        )
        assert key not in tables["youtube"].store

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
        acc, tables = accel
        assert acc.record_failure("youtube", "") == 0
        assert all(t.put_calls == 0 for t in tables.values())


class TestBestEffortErrorHandling:
    def test_get_swallows_table_errors(self):
        class Boom:
            def get_results(self, query_key):
                raise RuntimeError("dynamo down")

            def put(self, *a, **k):
                raise RuntimeError("dynamo down")

        acc = SearchCacheAccelerator({"spotify": Boom()})
        assert acc.get("q") is None
        # put + record_failure never raise even when the table errors.
        acc.put("q", [_track("spotify", "s1")])
        assert acc.record_failure("spotify", "s1") == 0

    def test_malformed_cached_track_dropped(self, accel):
        acc, tables = accel
        key = SearchCacheAccelerator.query_key(
            "q", provider_filter=None, content_type="tracks", sort_order="relevance"
        )
        # Malformed + valid tracks written into the spotify table directly.
        tables["spotify"].store[key] = {
            "tracks": [
                {"no_title": True},
                {"title": "ok", "artist": "A", "provider": "spotify", "track_id": "s1"},
            ]
        }
        served = acc.get("q")
        assert served is not None
        assert len(served) == 1
        assert served[0].title == "ok"

    def test_serialization_round_trip_preserves_fields(self, accel):
        acc, _ = accel
        original = SearchResult(
            title="Around the World",
            artist="Daft Punk",
            album="Homework",
            release_year=1997,
            duration_ms=428000,
            artwork_url="http://art/aw.jpg",
            isrc="USQX99700012",
            provider="spotify",
            track_id="s-atw",
            variant_type=None,
            normalized_key="daft punk|around the world",
            has_music_video=True,
        )
        acc.put("atw", [original])
        served = acc.get("atw")
        assert served is not None and len(served) == 1
        assert served[0] == original

    def test_invalid_threshold_rejected(self):
        with pytest.raises(ValueError):
            SearchCacheAccelerator(_make_tables(), failure_threshold=0)

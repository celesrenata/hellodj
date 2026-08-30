"""Integration tests: UnifiedSearchEngine wired to a provider-aware accelerator.

Verifies the engine (a) serves an accelerator hit without dispatching any
provider search, (b) backfills the accelerator (per-provider) after a live
fan-out, and (c) routes ``record_track_failure`` to the correct provider table.

The accelerator is built from a MAP of provider -> in-memory fake table (the
``SearchCacheLike`` seam), so these tests never need boto3.
"""

from __future__ import annotations

from typing import Any

import pytest
from search.accelerator import SearchCacheAccelerator
from search.engine import UnifiedSearchEngine
from search.models import SearchResult


class FakeSearchCache:
    """In-memory stand-in for one provider's ``SearchCacheTable`` (no boto3)."""

    def __init__(self) -> None:
        self.store: dict[str, dict[str, Any]] = {}

    def get_results(self, query_key: str):
        item = self.store.get(query_key)
        return dict(item) if item is not None else None

    def put(self, query_key: str, results, *, ttl=None):
        self.store[query_key] = dict(results)
        return {"queryKey": query_key}


def _tables(*providers: str) -> dict[str, FakeSearchCache]:
    return {p: FakeSearchCache() for p in providers}


def _track(provider: str, track_id: str) -> SearchResult:
    return SearchResult(title="T", artist="A", provider=provider, track_id=track_id)


@pytest.mark.asyncio
async def test_accelerator_hit_skips_provider_dispatch(monkeypatch):
    tables = _tables("spotify", "tidal", "youtube")
    acc = SearchCacheAccelerator(tables)
    # Pre-warm the accelerator as if another instance had resolved this query.
    acc.put("daft punk", [_track("spotify", "s1")])

    engine = UnifiedSearchEngine(accelerator=acc)

    called = False

    async def _boom(*_a, **_k):
        nonlocal called
        called = True
        raise AssertionError("providers must not be dispatched on a cache hit")

    monkeypatch.setattr(engine, "_execute_search", _boom)

    results = await engine.search("daft punk")
    assert called is False
    assert [r.track_id for r in results] == ["s1"]


@pytest.mark.asyncio
async def test_miss_backfills_accelerator_into_provider_table(monkeypatch):
    from search.models import ProviderResult

    tables = _tables("spotify", "tidal", "youtube")
    acc = SearchCacheAccelerator(tables)
    engine = UnifiedSearchEngine(accelerator=acc)

    async def _fake_execute(query, providers, *, timeout_budget=None):
        return [ProviderResult(provider="spotify", results=[_track("spotify", "s1")])]

    monkeypatch.setattr(engine, "_execute_search", _fake_execute)

    results = await engine.search("new query")
    assert [r.track_id for r in results] == ["s1"]

    # The accelerator now holds the freshly-resolved results (shared with peers)
    # and they were routed into the SPOTIFY provider table, not the others.
    served = acc.get("new query")
    assert served is not None
    assert served[0].track_id == "s1"

    key = SearchCacheAccelerator.query_key(
        "new query", provider_filter=None, content_type="tracks", sort_order="relevance"
    )
    assert key in tables["spotify"].store
    assert key not in tables["tidal"].store
    assert key not in tables["youtube"].store


@pytest.mark.asyncio
async def test_record_track_failure_delegates_to_provider_table(monkeypatch):
    tables = _tables("spotify", "tidal", "youtube")
    acc = SearchCacheAccelerator(tables)
    engine = UnifiedSearchEngine(accelerator=acc)

    for _ in range(3):
        engine.record_track_failure("youtube", "v1")

    # The failure counter landed in the youtube table.
    fkey = SearchCacheAccelerator._failure_key("youtube", "v1")
    assert tables["youtube"].store[fkey]["count"] == 3
    assert fkey not in tables["spotify"].store

    acc.put("q", [_track("youtube", "v1")])
    # Evicted after the 3 recorded failures.
    assert acc.get("q") is None

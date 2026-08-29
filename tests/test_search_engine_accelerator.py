"""Integration tests: UnifiedSearchEngine wired to the accelerator.

Verifies the engine (a) serves an accelerator hit without dispatching any
provider search, and (b) backfills the accelerator after a live fan-out.
"""

from __future__ import annotations

from typing import Any

import pytest
from search.accelerator import SearchCacheAccelerator
from search.engine import UnifiedSearchEngine
from search.models import SearchResult


class FakeSearchCache:
    def __init__(self) -> None:
        self.store: dict[str, dict[str, Any]] = {}

    def get_results(self, query_key: str):
        item = self.store.get(query_key)
        return dict(item) if item is not None else None

    def put(self, query_key: str, results, *, ttl=None):
        self.store[query_key] = dict(results)
        return {"queryKey": query_key}


def _track(provider: str, track_id: str) -> SearchResult:
    return SearchResult(title="T", artist="A", provider=provider, track_id=track_id)


@pytest.mark.asyncio
async def test_accelerator_hit_skips_provider_dispatch(monkeypatch):
    table = FakeSearchCache()
    acc = SearchCacheAccelerator(table)
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
async def test_miss_backfills_accelerator(monkeypatch):
    from search.models import ProviderResult

    table = FakeSearchCache()
    acc = SearchCacheAccelerator(table)
    engine = UnifiedSearchEngine(accelerator=acc)

    async def _fake_execute(query, providers, *, timeout_budget=None):
        return [ProviderResult(provider="spotify", results=[_track("spotify", "s1")])]

    monkeypatch.setattr(engine, "_execute_search", _fake_execute)

    results = await engine.search("new query")
    assert [r.track_id for r in results] == ["s1"]

    # The accelerator now holds the freshly-resolved results (shared with peers).
    served = acc.get("new query")
    assert served is not None
    assert served[0].track_id == "s1"


@pytest.mark.asyncio
async def test_record_track_failure_delegates(monkeypatch):
    table = FakeSearchCache()
    acc = SearchCacheAccelerator(table)
    engine = UnifiedSearchEngine(accelerator=acc)

    for _ in range(3):
        engine.record_track_failure("youtube", "v1")

    acc.put("q", [_track("youtube", "v1")])
    # Evicted after the 3 recorded failures.
    assert acc.get("q") is None

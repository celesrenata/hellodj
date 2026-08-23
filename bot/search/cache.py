"""In-memory LRU result cache with TTL-based expiration.

Requirements: 7.1, 7.2, 7.3, 7.4, 7.5, 7.6, 18.6
"""

from __future__ import annotations

import re
from collections import OrderedDict

from .models import CacheEntry, SearchResult

_WHITESPACE_RE = re.compile(r"\s+")


class ResultCache:
    """LRU cache for search results with TTL expiration.

    - Max capacity: configurable (default 200 entries), LRU eviction
    - TTL: configurable (default 60 seconds), expired entries treated as misses
    - Cache key: normalized query + filter parameters for isolation
    - Storage: per-process in-memory (no external dependencies)
    """

    def __init__(self, capacity: int = 200, ttl: float = 60.0) -> None:
        self._capacity = capacity
        self._ttl = ttl
        self._store: OrderedDict[str, CacheEntry] = OrderedDict()

    def get(
        self,
        query: str,
        *,
        provider_filter: str | None = None,
        content_type: str = "tracks",
        sort_order: str = "relevance",
    ) -> list[SearchResult] | None:
        """Look up cached results for the given query and filters.

        Returns the cached result list on hit, or None on miss/expiry.
        On hit, the entry is moved to the end (most-recently-used).
        Expired entries are evicted on access.
        """
        key = self._make_key(query, provider_filter, content_type, sort_order)
        entry = self._store.get(key)
        if entry is None:
            return None
        if entry.is_expired(self._ttl):
            del self._store[key]
            return None
        # Move to end (most-recently-used)
        self._store.move_to_end(key)
        return entry.results

    def put(
        self,
        query: str,
        results: list[SearchResult],
        *,
        provider_filter: str | None = None,
        content_type: str = "tracks",
        sort_order: str = "relevance",
    ) -> None:
        """Store results in the cache, evicting LRU entries if at capacity."""
        key = self._make_key(query, provider_filter, content_type, sort_order)
        # If key already exists, remove it first so re-insertion goes to end
        if key in self._store:
            del self._store[key]
        # Evict LRU (oldest) entries if at capacity
        while len(self._store) >= self._capacity:
            self._store.popitem(last=False)
        self._store[key] = CacheEntry(results=results)

    def _make_key(
        self,
        query: str,
        provider_filter: str | None,
        content_type: str,
        sort_order: str,
    ) -> str:
        """Build a normalized cache key from query text and filter parameters.

        Normalization: lowercase, strip leading/trailing whitespace,
        collapse internal whitespace to a single space.

        Filter parameters are appended to ensure different filter combinations
        for the same query text produce distinct cache keys.
        """
        normalized_query = _WHITESPACE_RE.sub(" ", query.lower().strip())
        # Incorporate filters into the key for isolation (Req 18.6)
        return f"{normalized_query}|{provider_filter or 'all'}|{content_type}|{sort_order}"

"""Distributed search-cache accelerator over the ``hellodj-search-cache`` table.

The bot's :class:`~search.engine.UnifiedSearchEngine` fans a query out to
Spotify, Tidal, YouTube, and YouTube Music in parallel. That fan-out is the slow
path. This module puts the shared, DAX-fronted ``hellodj-search-cache`` hot
table (``hellodj_platform_logic.data_access.SearchCacheTable``) in FRONT of that
fan-out so every bot instance shares one accelerator cache:

* **Immediate serve on hit.** :meth:`SearchCacheAccelerator.get` returns the
  cached, deduplicated result list for a (query + filter) key without touching
  any provider.
* **Backfill on miss.** After the providers return, the engine calls
  :meth:`put` to store the freshly-resolved results so the next bot instance to
  ask the same question is served instantly.
* **Failure tracking + eviction.** A track that fails to play is recorded via
  :meth:`record_failure`. Once a track has failed
  :data:`FAILURE_EVICTION_THRESHOLD` (3) times it is treated as dead: it is
  filtered out of every cached result the accelerator serves, so no bot instance
  re-serves a track that has proven unplayable. Failure counts live in their own
  cache entries keyed by ``(provider, track_id)`` (independent of which query
  surfaced the track) and expire by the table's TTL, so a track that later
  starts working again naturally rejoins the cache once its counter lapses.

Design constraints mirrored from the rest of the bot's AWS wiring
(``playback.guild_credentials.build_dynamo_credential_resolver``):

* Nothing imports ``boto3`` or ``hellodj_platform_logic`` at module load. The
  :class:`SearchCacheTable` seam is injected, so this module stays importable in
  local dev / unit tests with neither present, and is exercised with in-memory
  fakes.
* :func:`build_search_cache_accelerator` wires the real table at bot startup and
  returns ``None`` on ANY construction failure (boto3 missing, no credentials,
  package unavailable) so the engine degrades to a pure provider fan-out — the
  accelerator is a pure optimization, never a hard dependency.
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from typing import Any, Protocol

from .models import SearchResult

log = logging.getLogger(__name__)

__all__ = [
    "FAILURE_EVICTION_THRESHOLD",
    "SearchCacheAccelerator",
    "SearchCacheLike",
    "build_search_cache_accelerator",
    "resolve_shared_accelerator",
]

#: A track that has failed to play this many times is evicted from the cache and
#: filtered out of every result the accelerator serves.
FAILURE_EVICTION_THRESHOLD = 3

#: Prefix for the standalone per-track failure-counter cache entries so they do
#: not collide with the query-result entries.
_FAILURE_KEY_PREFIX = "trackfail:"

#: The :class:`SearchResult` fields persisted in the cache. Kept explicit so a
#: future model field is not silently written/read; ``normalized_key`` and
#: ``variant_type`` are recomputable but cheap to store and preserve dedup order.
_RESULT_FIELDS = frozenset(SearchResult.__dataclass_fields__)


class SearchCacheLike(Protocol):
    """The subset of ``SearchCacheTable`` this accelerator depends on.

    Declared as a Protocol so the accelerator is unit-tested with an in-memory
    fake and never has to construct the real DynamoDB-backed table.
    """

    def get_results(self, query_key: str) -> dict[str, Any] | None: ...

    def put(
        self,
        query_key: str,
        results: dict[str, Any],
        *,
        ttl: int | None = None,
    ) -> Any: ...


class SearchCacheAccelerator:
    """Accelerator cache in front of the multi-provider search fan-out.

    Args:
        table: The shared ``hellodj-search-cache`` repository (or a fake
            implementing :class:`SearchCacheLike`).
        failure_threshold: Failures before a track is evicted (default 3).
    """

    def __init__(
        self,
        table: SearchCacheLike,
        *,
        failure_threshold: int = FAILURE_EVICTION_THRESHOLD,
    ) -> None:
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be >= 1")
        self._table = table
        self._failure_threshold = failure_threshold

    # -- query-key derivation ------------------------------------------------

    @staticmethod
    def query_key(
        query: str,
        *,
        provider_filter: str | None,
        content_type: str,
        sort_order: str,
    ) -> str:
        """Return the shared cache key for a query + filter combination.

        The normalization matches :class:`search.cache.ResultCache._make_key`
        (lowercased, whitespace-collapsed, filters appended) so the in-process
        LRU and the distributed accelerator agree on identity.
        """
        normalized = " ".join(query.lower().split())
        return f"{normalized}|{provider_filter or 'all'}|{content_type}|{sort_order}"

    @staticmethod
    def _track_key(provider: str, track_id: str) -> str:
        return f"{provider}\u0000{track_id}"

    @classmethod
    def _failure_key(cls, provider: str, track_id: str) -> str:
        return f"{_FAILURE_KEY_PREFIX}{cls._track_key(provider, track_id)}"

    # -- read path -----------------------------------------------------------

    def get(
        self,
        query: str,
        *,
        provider_filter: str | None = None,
        content_type: str = "tracks",
        sort_order: str = "relevance",
    ) -> list[SearchResult] | None:
        """Return cached results for a query, or ``None`` on a miss/error.

        Tracks that have crossed the failure threshold are filtered out of the
        returned list. If filtering empties an entry, ``None`` is returned so the
        caller performs a fresh provider fan-out rather than serving nothing.
        """
        key = self.query_key(
            query,
            provider_filter=provider_filter,
            content_type=content_type,
            sort_order=sort_order,
        )
        try:
            payload = self._table.get_results(key)
        except Exception as exc:  # noqa: BLE001 - accelerator is best-effort
            log.debug("search accelerator get failed for %r: %s", key, exc)
            return None

        if not payload:
            return None

        raw_tracks = payload.get("tracks")
        if not isinstance(raw_tracks, list):
            return None

        results: list[SearchResult] = []
        for raw in raw_tracks:
            result = _deserialize_result(raw)
            if result is None:
                continue
            if self._is_evicted(result.provider, result.track_id):
                continue
            results.append(result)

        return results or None

    # -- write path ----------------------------------------------------------

    def put(
        self,
        query: str,
        results: list[SearchResult],
        *,
        provider_filter: str | None = None,
        content_type: str = "tracks",
        sort_order: str = "relevance",
    ) -> None:
        """Backfill the accelerator with freshly-resolved provider results.

        Best-effort: any serialization or write error is logged and swallowed so
        a cache write can never break a search that already produced results.
        """
        if not results:
            return

        key = self.query_key(
            query,
            provider_filter=provider_filter,
            content_type=content_type,
            sort_order=sort_order,
        )
        payload = {
            "tracks": [
                _serialize_result(r)
                for r in results
                if not self._is_evicted(r.provider, r.track_id)
            ]
        }
        if not payload["tracks"]:
            return
        try:
            self._table.put(key, payload)
        except Exception as exc:  # noqa: BLE001 - accelerator is best-effort
            log.debug("search accelerator put failed for %r: %s", key, exc)

    # -- failure tracking + eviction ----------------------------------------

    def record_failure(self, provider: str, track_id: str) -> int:
        """Record one playback failure for a track; return its new count.

        When the count reaches :data:`FAILURE_EVICTION_THRESHOLD` the track is
        considered evicted — :meth:`get` and :meth:`put` filter it out from then
        on (until its failure-counter entry expires by TTL). Best-effort: returns
        ``0`` on any error without raising.
        """
        if not track_id:
            return 0
        fkey = self._failure_key(provider, track_id)
        try:
            current = self._table.get_results(fkey) or {}
            count = int(current.get("count", 0)) + 1
            self._table.put(fkey, {"count": count})
            if count >= self._failure_threshold:
                log.info(
                    "search accelerator: evicting track %s:%s after %d failures",
                    provider, track_id, count,
                )
            return count
        except Exception as exc:  # noqa: BLE001 - accelerator is best-effort
            log.debug(
                "search accelerator record_failure failed for %s:%s: %s",
                provider, track_id, exc,
            )
            return 0

    def _is_evicted(self, provider: str, track_id: str) -> bool:
        if not track_id:
            return False
        try:
            entry = self._table.get_results(self._failure_key(provider, track_id))
        except Exception as exc:  # noqa: BLE001 - treat lookup error as not-evicted
            log.debug(
                "search accelerator eviction check failed for %s:%s: %s",
                provider, track_id, exc,
            )
            return False
        if not entry:
            return False
        return int(entry.get("count", 0)) >= self._failure_threshold


def resolve_shared_accelerator() -> SearchCacheAccelerator | None:
    """Return the process-wide accelerator built at bot startup, or ``None``.

    Reached via a lazy ``import bot`` (mirroring how the cogs reach the shared
    entitlement resolver) so callers stay importable without the full bot
    package present (local dev / unit tests).
    """
    try:
        import bot as _bot_module  # type: ignore[import]

        return _bot_module.get_search_accelerator()
    except Exception:  # noqa: BLE001 - no shared accelerator available
        return None


def _serialize_result(result: SearchResult) -> dict[str, Any]:
    """Serialize a :class:`SearchResult` to a DynamoDB-safe mapping."""
    return asdict(result)


def _deserialize_result(raw: Any) -> SearchResult | None:
    """Rebuild a :class:`SearchResult` from a stored mapping, or ``None``."""
    if not isinstance(raw, dict):
        return None
    kwargs = {k: raw[k] for k in _RESULT_FIELDS if k in raw}
    if "title" not in kwargs:
        return None
    try:
        return SearchResult(**kwargs)
    except TypeError as exc:
        log.debug("search accelerator: dropping malformed cached track: %s", exc)
        return None


def build_search_cache_accelerator(
    *,
    table_name: str | None = None,
) -> SearchCacheAccelerator | None:
    """Wire a real ``SearchCacheTable``-backed accelerator, or ``None``.

    Lazily imports ``boto3`` and the shared ``hellodj_platform_logic`` package so
    this module stays importable where those are absent (local dev / unit tests).
    On ANY construction failure it logs and returns ``None`` so the engine runs a
    pure provider fan-out — the same non-fatal convention as
    :func:`playback.guild_credentials.build_dynamo_credential_resolver`.
    """
    try:
        import boto3  # lazy — only present/needed in the SaaS deployment
        from hellodj_platform_logic.data_access import (
            SEARCH_CACHE_TABLE_NAME,
            SearchCacheTable,
        )

        resolved_name = table_name or SEARCH_CACHE_TABLE_NAME
        ddb = boto3.resource("dynamodb")
        table = SearchCacheTable(ddb.Table(resolved_name))
        log.info(
            "search accelerator: hellodj-search-cache accelerator wired (table=%s)",
            resolved_name,
        )
        return SearchCacheAccelerator(table)
    except Exception as exc:  # noqa: BLE001 - non-fatal: pure fan-out fallback
        log.info(
            "search accelerator: unavailable (%s) — search falls back to a pure "
            "provider fan-out with the in-process LRU only", exc,
        )
        return None

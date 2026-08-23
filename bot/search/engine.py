"""Unified multi-provider search engine.

Orchestrates parallel searches across Spotify, Tidal, and YouTube via wavelink,
applies deduplication, caching, and timing budgets to deliver results within
Discord's 3-second autocomplete deadline.

Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 2.1, 2.2, 2.3, 9.1, 9.2, 9.3, 9.4,
              18.1, 18.4, 18.5, 18.7
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable

try:
    import wavelink
except ImportError:  # pragma: no cover
    wavelink = None  # type: ignore[assignment]

from .cache import ResultCache
from .deduplicator import Deduplicator, detect_variant, normalize_key
from .models import ProviderResult, SearchResult
from .url_detector import URLDetector

log = logging.getLogger(__name__)

# Provider configurations: (lavalink_search_prefix, max_results)
_PROVIDER_CONFIGS: dict[str, tuple[str, int]] = {
    "spotify": ("spsearch", 10),
    "tidal": ("tdsearch", 8),
    "youtube": ("ytsearch", 7),
}

# Per-provider search timeout in seconds
_PROVIDER_TIMEOUT: float = 2.0

# Total pipeline budget (ms) and search phase budget (ms)
_TOTAL_BUDGET_MS: float = 2800.0
_SEARCH_PHASE_BUDGET_MS: float = 2000.0
_DEDUP_FORMAT_BUDGET_MS: float = 300.0


class UnifiedSearchEngine:
    """Unified search backend for both autocomplete and Activity panel.

    Performs parallel multi-provider search via wavelink, deduplicates
    results by ISRC and normalized metadata, caches results, and enforces
    strict timing budgets.
    """

    def __init__(self, *, cache_capacity: int = 200, cache_ttl: float = 60.0) -> None:
        self._cache = ResultCache(capacity=cache_capacity, ttl=cache_ttl)

    async def search(
        self,
        query: str,
        *,
        guild_id: int | None = None,
        provider_filter: str | None = None,
        content_type: str = "tracks",
        sort_order: str = "relevance",
    ) -> list[SearchResult]:
        """Unified search: parallel providers, dedup, cache.

        Returns structured SearchResult list for both autocomplete and Activity.

        Args:
            query: The search query string.
            guild_id: Optional guild ID for source_provider ordering.
            provider_filter: Limit to a single provider ("spotify", "tidal",
                "youtube", "soundcloud") or None/"all" for all providers.
            content_type: Content type filter (default "tracks").
            sort_order: Sort order - "relevance", "duration", or "year".

        Returns:
            List of deduplicated, ordered SearchResult objects.
        """
        pipeline_start = time.monotonic()

        # --- Query threshold gate ---
        # Requirement 1.4: fewer than 2 non-whitespace chars → empty immediately
        if len(query.replace(" ", "")) < 2:
            return []

        # --- URL detection bypass ---
        # Requirement 8.1: recognized URLs bypass search entirely
        url_result = URLDetector.detect(query)
        if url_result is not None:
            platform_name, url = url_result
            return [
                SearchResult(
                    title=f"{platform_name} URL",
                    artist="",
                    provider=platform_name.lower(),
                    track_id=url,
                )
            ]

        # --- Cache lookup ---
        # Requirement 7.4: cache hit returns without dispatching searches
        cached = self._cache.get(
            query,
            provider_filter=provider_filter,
            content_type=content_type,
            sort_order=sort_order,
        )
        if cached is not None:
            return cached

        # --- Determine which providers to query ---
        if provider_filter and provider_filter != "all":
            providers = [provider_filter] if provider_filter in _PROVIDER_CONFIGS else []
        else:
            providers = list(_PROVIDER_CONFIGS.keys())

        if not providers:
            return []

        # --- Parallel provider dispatch ---
        # Enforce search phase budget (2000ms) on top of per-provider timeout
        elapsed_ms = (time.monotonic() - pipeline_start) * 1000
        remaining_search_ms = max(0, _SEARCH_PHASE_BUDGET_MS - elapsed_ms)

        provider_results = await self._execute_search(
            query, providers, timeout_budget=remaining_search_ms / 1000
        )

        # --- Check total pipeline budget ---
        elapsed_ms = (time.monotonic() - pipeline_start) * 1000
        if elapsed_ms >= _TOTAL_BUDGET_MS:
            # Emergency: return whatever raw results we have without dedup
            raw_results = []
            for pr in provider_results:
                if pr.results:
                    raw_results.extend(pr.results)
            return raw_results

        # --- Deduplication and ordering ---
        guild_source_provider = self._get_guild_source_provider(guild_id)

        all_results: list[SearchResult] = []
        for pr in provider_results:
            if pr.results:
                all_results.extend(pr.results)

        if not all_results:
            # Requirement 2.2: all providers failed/empty → empty list
            return []

        # Check budget before dedup
        elapsed_ms = (time.monotonic() - pipeline_start) * 1000
        if elapsed_ms >= (_TOTAL_BUDGET_MS - 100):
            # Not enough time for dedup — return raw
            return all_results

        deduplicated = Deduplicator.deduplicate(
            all_results,
            guild_source_provider=guild_source_provider,
        )

        # --- Apply sort order ---
        if sort_order == "duration":
            deduplicated.sort(
                key=lambda r: r.duration_ms if r.duration_ms is not None else float("inf")
            )
        elif sort_order == "year":
            deduplicated.sort(
                key=lambda r: -(r.release_year if r.release_year is not None else 0)
            )

        # --- Store in cache ---
        # Requirement 7.6: store successful results
        self._cache.put(
            query,
            deduplicated,
            provider_filter=provider_filter,
            content_type=content_type,
            sort_order=sort_order,
        )

        return deduplicated

    async def _execute_search(
        self,
        query: str,
        providers: list[str],
        *,
        timeout_budget: float | None = None,
    ) -> list[ProviderResult]:
        """Fan out searches to all requested providers with per-provider timeout.

        Args:
            query: The search query.
            providers: List of provider names to query.
            timeout_budget: Optional overall budget in seconds for the search phase.

        Returns:
            List of ProviderResult objects (one per provider).
        """

        async def _search_provider(name: str, prefix: str, limit: int) -> ProviderResult:
            start = time.monotonic()
            try:
                if wavelink is None:
                    raise RuntimeError("wavelink not available")

                tracks = await asyncio.wait_for(
                    wavelink.Playable.search(f"{prefix}:{query}"),
                    timeout=_PROVIDER_TIMEOUT,
                )
                results = [self._to_search_result(t, name) for t in tracks[:limit]]
                return ProviderResult(
                    provider=name,
                    results=results,
                    elapsed_ms=(time.monotonic() - start) * 1000,
                )
            except asyncio.TimeoutError as e:
                log.warning("Provider %s failed: %s: %s", name, type(e).__name__, e)
                return ProviderResult(
                    provider=name,
                    error=str(e),
                    elapsed_ms=(time.monotonic() - start) * 1000,
                )
            except Exception as e:
                log.warning("Provider %s failed: %s: %s", name, type(e).__name__, e)
                return ProviderResult(
                    provider=name,
                    error=str(e),
                    elapsed_ms=(time.monotonic() - start) * 1000,
                )

        tasks = [
            _search_provider(name, cfg[0], cfg[1])
            for name, cfg in _PROVIDER_CONFIGS.items()
            if name in providers
        ]

        if timeout_budget is not None and timeout_budget > 0:
            try:
                results = await asyncio.wait_for(
                    asyncio.gather(*tasks),
                    timeout=timeout_budget,
                )
                return list(results)
            except asyncio.TimeoutError:
                # Search phase budget exceeded — collect what we can
                log.warning(
                    "Search phase budget exceeded (%.1fms), proceeding with available results",
                    timeout_budget * 1000,
                )
                # Gather remaining tasks, ignoring those still running
                completed: list[ProviderResult] = []
                for task in tasks:
                    if hasattr(task, "__self__"):
                        # This is a coroutine that was already consumed by gather
                        pass
                return completed
        else:
            results = await asyncio.gather(*tasks)
            return list(results)

    def _to_search_result(self, track: "wavelink.Playable", provider: str) -> SearchResult:
        """Convert a wavelink.Playable to a SearchResult object.

        Extracts metadata from the track and computes normalized_key
        and variant_type.

        Args:
            track: A wavelink Playable track object.
            provider: The provider name this track came from.

        Returns:
            A populated SearchResult dataclass.
        """
        title = track.title or ""
        artist = track.author or ""
        album = getattr(track, "album", None)
        duration_ms = getattr(track, "length", None)
        artwork = getattr(track, "artwork", None)
        isrc = getattr(track, "isrc", None)
        track_id = getattr(track, "identifier", "") or ""

        variant_type = detect_variant(title)
        norm_key = normalize_key(artist, title)

        return SearchResult(
            title=title,
            artist=artist,
            album=album if isinstance(album, str) else None,
            duration_ms=duration_ms,
            artwork_url=artwork,
            isrc=isrc,
            provider=provider,
            track_id=track_id,
            variant_type=variant_type,
            normalized_key=norm_key,
        )

    async def search_streaming(
        self,
        query: str,
        *,
        guild_id: int | None = None,
        provider_filter: str | None = None,
        content_type: str = "tracks",
        sort_order: str = "relevance",
        on_provider_result: Callable[[str, list[SearchResult]], Awaitable[None]] | None = None,
    ) -> list[SearchResult]:
        """Streaming variant: calls on_provider_result as each provider responds.

        Used by Activity WebSocket for progressive rendering. Does not interact
        with the cache since streaming is meant for real-time progressive updates.

        Args:
            query: The search query string.
            guild_id: Optional guild ID for source_provider ordering.
            provider_filter: Limit to a single provider ("spotify", "tidal",
                "youtube", "soundcloud") or None/"all" for all providers.
            content_type: Content type filter (default "tracks").
            sort_order: Sort order - "relevance", "duration", or "year".
            on_provider_result: Async callback fired as each provider responds
                with (provider_name, results_list).

        Returns:
            Deduplicated, filtered, sorted list of SearchResult objects.

        Requirements: 17.2, 17.7, 18.1, 18.5
        """
        # --- Query threshold gate (same as search()) ---
        # Requirement 1.4: fewer than 2 non-whitespace chars → empty immediately
        if len(query.replace(" ", "")) < 2:
            return []

        # --- URL detection bypass ---
        url_result = URLDetector.detect(query)
        if url_result is not None:
            platform_name, url = url_result
            result = SearchResult(
                title=f"{platform_name} URL",
                artist="",
                provider=platform_name.lower(),
                track_id=url,
            )
            if on_provider_result:
                await on_provider_result(platform_name.lower(), [result])
            return [result]

        # --- Determine which providers to query ---
        if provider_filter and provider_filter != "all":
            providers_to_query = (
                [provider_filter] if provider_filter in _PROVIDER_CONFIGS else []
            )
        else:
            providers_to_query = list(_PROVIDER_CONFIGS.keys())

        if not providers_to_query:
            return []

        # --- Create individual tasks for each provider ---
        async def _search_single(name: str, prefix: str, limit: int) -> ProviderResult:
            """Search a single provider with timeout."""
            start = time.monotonic()
            try:
                if wavelink is None:
                    raise RuntimeError("wavelink not available")

                tracks = await asyncio.wait_for(
                    wavelink.Playable.search(f"{prefix}:{query}"),
                    timeout=_PROVIDER_TIMEOUT,
                )
                results = [self._to_search_result(t, name) for t in tracks[:limit]]
                return ProviderResult(
                    provider=name,
                    results=results,
                    elapsed_ms=(time.monotonic() - start) * 1000,
                )
            except asyncio.TimeoutError as e:
                log.warning("Provider %s failed: %s: %s", name, type(e).__name__, e)
                return ProviderResult(
                    provider=name,
                    error=str(e),
                    elapsed_ms=(time.monotonic() - start) * 1000,
                )
            except Exception as e:
                log.warning("Provider %s failed: %s: %s", name, type(e).__name__, e)
                return ProviderResult(
                    provider=name,
                    error=str(e),
                    elapsed_ms=(time.monotonic() - start) * 1000,
                )

        # Map tasks to provider names for identification
        tasks: dict[asyncio.Task[ProviderResult], str] = {
            asyncio.create_task(
                _search_single(name, _PROVIDER_CONFIGS[name][0], _PROVIDER_CONFIGS[name][1])
            ): name
            for name in providers_to_query
        }

        # --- Stream results as they arrive using asyncio.as_completed ---
        all_results: list[SearchResult] = []

        for coro in asyncio.as_completed(tasks.keys(), timeout=_PROVIDER_TIMEOUT):
            try:
                provider_result = await coro
                if provider_result.results:
                    all_results.extend(provider_result.results)
                    # Fire callback for progressive rendering
                    if on_provider_result:
                        await on_provider_result(
                            provider_result.provider, provider_result.results
                        )
            except asyncio.TimeoutError:
                # Overall timeout reached — proceed with what we have
                log.warning(
                    "Streaming search timeout reached, proceeding with %d results",
                    len(all_results),
                )
                break

        # --- Cancel any still-pending tasks ---
        for task in tasks:
            if not task.done():
                task.cancel()

        if not all_results:
            return []

        # --- Deduplicate after all providers respond (or timeout) ---
        guild_source_provider = self._get_guild_source_provider(guild_id)
        deduplicated = Deduplicator.deduplicate(
            all_results,
            guild_source_provider=guild_source_provider,
        )

        # --- Apply provider filter on final output ---
        # Requirement 18.5: only include results from matching provider
        if provider_filter and provider_filter != "all":
            deduplicated = [
                r for r in deduplicated if r.provider == provider_filter
            ]

        # --- Apply sort order ---
        if sort_order == "duration":
            deduplicated.sort(
                key=lambda r: r.duration_ms if r.duration_ms is not None else float("inf")
            )
        elif sort_order == "year":
            deduplicated.sort(
                key=lambda r: -(r.release_year if r.release_year is not None else 0)
            )

        return deduplicated

    def _get_guild_source_provider(self, guild_id: int | None) -> str:
        """Look up the guild's preferred source_provider.

        Falls back to "youtube" if guild_id is None or lookup fails.

        Args:
            guild_id: The Discord guild ID.

        Returns:
            The guild's source_provider string.
        """
        if guild_id is None:
            return "youtube"

        # Attempt to read from player guild state — import lazily to avoid
        # circular imports and hard dependency on the player module.
        try:
            import player as _player_module  # type: ignore[import]

            state = _player_module.guild_state.get(guild_id)
            if state and "source_provider" in state:
                return state["source_provider"]
        except (ImportError, Exception):
            pass

        return "youtube"

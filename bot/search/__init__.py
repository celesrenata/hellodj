"""Unified multi-provider search engine for HelloDJ."""

from .accelerator import (
    FAILURE_EVICTION_THRESHOLD,
    SearchCacheAccelerator,
    build_search_cache_accelerator,
)
from .cache import ResultCache
from .formatter import ChoiceFormatter
from .models import CacheEntry, ProviderResult, SearchResult, TrackGroup

__all__ = [
    "FAILURE_EVICTION_THRESHOLD",
    "CacheEntry",
    "ChoiceFormatter",
    "ProviderResult",
    "ResultCache",
    "SearchCacheAccelerator",
    "SearchResult",
    "TrackGroup",
    "UnifiedSearchEngine",
    "build_search_cache_accelerator",
]


def __getattr__(name: str):
    """Lazy import for UnifiedSearchEngine to avoid circular imports."""
    if name == "UnifiedSearchEngine":
        from .engine import UnifiedSearchEngine

        return UnifiedSearchEngine
    raise AttributeError(f"module 'bot.search' has no attribute {name!r}")

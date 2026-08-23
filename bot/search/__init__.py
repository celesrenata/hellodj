"""Unified multi-provider search engine for HelloDJ."""

from .cache import ResultCache
from .formatter import ChoiceFormatter
from .models import CacheEntry, ProviderResult, SearchResult, TrackGroup

__all__ = [
    "CacheEntry",
    "ChoiceFormatter",
    "ProviderResult",
    "ResultCache",
    "SearchResult",
    "TrackGroup",
    "UnifiedSearchEngine",
]


def __getattr__(name: str):
    """Lazy import for UnifiedSearchEngine to avoid circular imports."""
    if name == "UnifiedSearchEngine":
        from .engine import UnifiedSearchEngine

        return UnifiedSearchEngine
    raise AttributeError(f"module 'bot.search' has no attribute {name!r}")

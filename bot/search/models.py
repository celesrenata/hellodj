"""Data models for the unified search pipeline."""

from dataclasses import dataclass, field
from typing import Optional
import time


@dataclass
class SearchResult:
    """A single track result from any provider."""

    title: str
    artist: str
    album: str | None = None
    release_year: int | None = None
    duration_ms: int | None = None
    artwork_url: str | None = None
    isrc: str | None = None
    provider: str = ""  # "spotify", "tidal", "youtube", "soundcloud"
    track_id: str = ""  # Provider-specific ID
    variant_type: str | None = None  # "live", "remix", "acoustic", "music_video", or None
    normalized_key: str = ""  # Computed dedup key
    has_music_video: bool = False


@dataclass
class ProviderResult:
    """Raw results from a single provider before dedup."""

    provider: str
    results: list[SearchResult] = field(default_factory=list)
    error: str | None = None
    elapsed_ms: float = 0.0


@dataclass
class TrackGroup:
    """A canonical track across multiple providers (for Activity UI)."""

    primary: SearchResult  # Highest-priority provider's version
    variants: list[SearchResult] = field(default_factory=list)  # Same track from other providers
    available_providers: list[str] = field(default_factory=list)  # All providers that have it


@dataclass
class CacheEntry:
    """Time-stamped cache entry for the ResultCache."""

    results: list[SearchResult]
    created_at: float = field(default_factory=time.time)

    def is_expired(self, ttl: float) -> bool:
        """Check if this entry has exceeded the given TTL in seconds."""
        return (time.time() - self.created_at) > ttl

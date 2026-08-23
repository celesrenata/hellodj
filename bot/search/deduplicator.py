"""Deduplication utilities for unified search results.

Provides normalized key generation for metadata-based deduplication,
variant detection for distinguishing live/remix/acoustic versions,
and the Deduplicator class that performs ISRC-based deduplication
with provider priority and slot redistribution.
"""

from __future__ import annotations

import re

from .models import ProviderResult, SearchResult


# --- Normalization regex patterns ---

# Matches remaster annotations in various formats:
#   "- Remaster", "- Remastered 2011", "(Remastered)", "(Remastered 2011)", "[Remastered 2011]"
# Also handles en-dash and em-dash separators.
_REMASTER_RE = re.compile(
    r"\s*[-–—]\s*remaster(?:ed)?(?:\s+\d{4})?"
    r"|\s*\(remaster(?:ed)?(?:\s+\d{4})?\)"
    r"|\s*\[remaster(?:ed)?(?:\s+\d{4})?\]",
    re.IGNORECASE,
)

# Matches featuring credits in parentheses: "(feat. Artist)", "(ft. Artist)", "(feat Artist)"
_FEAT_RE = re.compile(r"\s*\((?:feat|ft)\.?\s+[^)]*\)", re.IGNORECASE)

# Matches trailing year patterns: "(2011)" or "[2011]"
_YEAR_SUFFIX_RE = re.compile(r"\s*[\(\[]\d{4}[\)\]]$")

# Collapses consecutive whitespace to a single space.
_WHITESPACE_RE = re.compile(r"\s+")

# --- Variant detection regex ---

# Matches variant keywords at word boundaries only.
# This ensures "Oliver" does not match "live" and "Premixed" does not match "remix".
_VARIANT_RE = re.compile(r"\b(live|remix|acoustic|music\s+video)\b", re.IGNORECASE)


def normalize_key(artist: str, title: str) -> str:
    """Generate a deterministic deduplication key from artist and title.

    Strips remaster annotations, featuring credits, and trailing year patterns
    from the title, then lowercases both fields, collapses whitespace, trims,
    and concatenates as ``{artist}:{title}``.

    Args:
        artist: The track artist name.
        title: The track title.

    Returns:
        A normalized string key in the format ``artist:title``.
    """
    title = _REMASTER_RE.sub("", title)
    title = _FEAT_RE.sub("", title)
    title = _YEAR_SUFFIX_RE.sub("", title)
    artist = _WHITESPACE_RE.sub(" ", artist.lower()).strip()
    title = _WHITESPACE_RE.sub(" ", title.lower()).strip()
    return f"{artist}:{title}"


def detect_variant(title: str) -> str | None:
    """Detect whether a track title indicates a variant version.

    Checks for the presence of "Live", "Remix", "Acoustic", or "Music Video"
    as whole words (word-boundary matched) in the title.

    Substring matches within larger words (e.g., "Oliver" containing "live",
    or "Premixed" containing "remix") do NOT trigger variant classification.

    Args:
        title: The track title to inspect.

    Returns:
        The variant type as a lowercase string (``"live"``, ``"remix"``,
        ``"acoustic"``, or ``"music_video"``), or ``None`` if no variant
        keyword is found.
    """
    match = _VARIANT_RE.search(title)
    if match:
        # Collapse internal whitespace before converting spaces to underscores
        variant = _WHITESPACE_RE.sub(" ", match.group(1).lower())
        return variant.replace(" ", "_")
    return None


# --- Provider priority ---

# Default priority order: Spotify > Tidal > YouTube > SoundCloud
_DEFAULT_PRIORITY = ["spotify", "tidal", "youtube", "soundcloud"]

# Default slot allocations per provider
_BASE_SLOTS: dict[str, int] = {"spotify": 10, "tidal": 8, "youtube": 7}


def _redistribute_slots(
    provider_results: list[ProviderResult],
    base_slots: dict[str, int] | None = None,
) -> dict[str, int]:
    """Proportionally redistribute unused slots from empty providers.

    When a provider returns zero results without error, its allocated slots
    are redistributed among providers that did return results. Fractional
    shares are rounded down, with the remainder assigned to the highest-
    priority provider. The total is capped at 25.

    Args:
        provider_results: List of raw provider results.
        base_slots: Slot allocation per provider. Defaults to
            ``{"spotify": 10, "tidal": 8, "youtube": 7}``.

    Returns:
        Updated slot allocation dictionary.
    """
    if base_slots is None:
        base_slots = dict(_BASE_SLOTS)

    successful = {pr.provider: pr for pr in provider_results if pr.results}
    empty = {pr.provider for pr in provider_results if not pr.results and pr.error is None}

    freed = sum(base_slots[p] for p in empty if p in base_slots)
    if freed == 0 or not successful:
        return base_slots

    priority_order = ["spotify", "tidal", "youtube"]
    active = [p for p in priority_order if p in successful]
    total_active_slots = sum(base_slots.get(p, 0) for p in active)

    if total_active_slots == 0:
        return base_slots

    new_slots = dict(base_slots)
    remaining = freed
    for p in active:
        share = int(freed * base_slots.get(p, 0) / total_active_slots)
        new_slots[p] = new_slots.get(p, 0) + share
        remaining -= share

    # Remainder to highest-priority active provider
    if remaining > 0 and active:
        new_slots[active[0]] += remaining

    # Cap at 25 total
    total = sum(new_slots.get(p, 0) for p in active)
    if total > 25:
        new_slots[active[-1]] -= total - 25

    return new_slots


class Deduplicator:
    """Removes duplicate search results across providers.

    Uses ISRC and normalized metadata keys to identify duplicates,
    preserves variant tracks as distinct entries, and respects guild
    source provider preference for priority ordering.
    """

    @staticmethod
    def deduplicate(
        results: list[SearchResult],
        *,
        guild_source_provider: str = "youtube",
    ) -> list[SearchResult]:
        """Remove duplicates from search results, preserving variants.

        For each result, a deduplication key is computed:
          - If ISRC is present and no variant: key = ISRC
          - If ISRC is present and variant: key = ``{isrc}:{variant_type}``
          - If ISRC is null: key = ``normalize_key(artist, title)``
            (with ``:variant_type`` appended if variant)

        Results are grouped by key. For each group, the highest-priority
        provider's version is retained. Priority is determined by the guild's
        source_provider (ranked first), then the default order:
        Spotify > Tidal > YouTube > SoundCloud.

        The retained result records ``available_providers`` information on the
        SearchResult's normalized_key isn't overwritten — the caller can use
        the returned list alongside the ``available_providers`` map for
        Activity UI Track_Groups.

        Args:
            results: Flat list of search results from all providers.
            guild_source_provider: The guild's preferred provider name
                (e.g., "spotify", "tidal", "youtube"). This provider is
                ranked highest in priority.

        Returns:
            Deduplicated list of SearchResult objects, ordered with the
            guild's preferred provider first, then by default priority.
        """
        if not results:
            return []

        # Build priority ranking: guild preference first, then default order
        priority = Deduplicator._build_priority(guild_source_provider)

        # Compute dedup keys and group
        groups: dict[str, list[SearchResult]] = {}
        for result in results:
            key = Deduplicator._compute_dedup_key(result)
            groups.setdefault(key, []).append(result)

        # For each group, pick the highest-priority provider's version
        deduplicated: list[SearchResult] = []
        available_providers_map: dict[str, list[str]] = {}

        for key, group in groups.items():
            # Sort group by priority (lowest index = highest priority)
            group.sort(key=lambda r: priority.get(r.provider, len(priority)))

            # Retain the highest-priority result
            primary = group[0]
            deduplicated.append(primary)

            # Record all providers that have this track
            providers_for_key = []
            seen_providers: set[str] = set()
            for r in group:
                if r.provider not in seen_providers:
                    providers_for_key.append(r.provider)
                    seen_providers.add(r.provider)
            available_providers_map[key] = providers_for_key

        # Order final results: guild source_provider first, then default priority
        deduplicated.sort(key=lambda r: priority.get(r.provider, len(priority)))

        return deduplicated

    @staticmethod
    def deduplicate_with_groups(
        results: list[SearchResult],
        *,
        guild_source_provider: str = "youtube",
    ) -> list[tuple[SearchResult, list[str]]]:
        """Deduplicate and return results with available_providers info.

        Same logic as ``deduplicate()`` but returns tuples of
        ``(primary_result, available_providers)`` for Activity UI
        Track_Group construction.

        Args:
            results: Flat list of search results from all providers.
            guild_source_provider: The guild's preferred provider name.

        Returns:
            List of tuples: (retained SearchResult, list of provider names
            that have this track).
        """
        if not results:
            return []

        priority = Deduplicator._build_priority(guild_source_provider)

        groups: dict[str, list[SearchResult]] = {}
        for result in results:
            key = Deduplicator._compute_dedup_key(result)
            groups.setdefault(key, []).append(result)

        result_with_providers: list[tuple[SearchResult, list[str]]] = []

        for key, group in groups.items():
            group.sort(key=lambda r: priority.get(r.provider, len(priority)))
            primary = group[0]

            providers_for_key: list[str] = []
            seen: set[str] = set()
            for r in group:
                if r.provider not in seen:
                    providers_for_key.append(r.provider)
                    seen.add(r.provider)

            result_with_providers.append((primary, providers_for_key))

        # Sort by priority
        result_with_providers.sort(
            key=lambda item: priority.get(item[0].provider, len(priority))
        )

        return result_with_providers

    @staticmethod
    def _build_priority(guild_source_provider: str) -> dict[str, int]:
        """Build provider priority mapping with guild preference first.

        Args:
            guild_source_provider: The guild's preferred provider.

        Returns:
            Dict mapping provider name to priority index (lower = higher priority).
        """
        order = [guild_source_provider] + [
            p for p in _DEFAULT_PRIORITY if p != guild_source_provider
        ]
        return {provider: idx for idx, provider in enumerate(order)}

    @staticmethod
    def _compute_dedup_key(result: SearchResult) -> str:
        """Compute the deduplication key for a search result.

        Key computation rules:
          - ISRC present, no variant: key = ISRC
          - ISRC present, variant: key = ``{isrc}:{variant_type}``
          - No ISRC, no variant: key = ``normalize_key(artist, title)``
          - No ISRC, variant: key = ``normalize_key(artist, title):{variant_type}``

        Args:
            result: The search result to compute a key for.

        Returns:
            The deduplication key string.
        """
        if result.isrc:
            base = result.isrc
        else:
            base = normalize_key(result.artist, result.title)

        if result.variant_type:
            return f"{base}:{result.variant_type}"
        return base

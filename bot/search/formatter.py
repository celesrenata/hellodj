"""Discord autocomplete choice formatting for unified search results."""

from __future__ import annotations

from discord import app_commands

from .models import SearchResult

# Provider icons for choice name prefix
PROVIDER_ICONS: dict[str, str] = {
    "spotify": "🟢",
    "tidal": "🔵",
    "youtube": "🔴",
    "soundcloud": "🟠",
}

# Short prefixes for value encoding
PROVIDER_PREFIXES: dict[str, str] = {
    "spotify": "sp",
    "tidal": "td",
    "youtube": "yt",
    "soundcloud": "sc",
}

# Reverse mapping: short prefix → lavalink search prefix
PREFIX_TO_LAVALINK: dict[str, str] = {
    "sp": "spsearch",
    "td": "tdsearch",
    "yt": "ytsearch",
    "sc": "scsearch",
}

# Max Discord autocomplete choice name/value length
_MAX_CHOICE_LEN = 100


def _format_duration(duration_ms: int) -> str:
    """Format milliseconds to M:SS or H:MM:SS string.

    - Duration < 60 minutes → M:SS (e.g., 3:45, 12:01)
    - Duration >= 60 minutes → H:MM:SS (e.g., 1:02:15)
    """
    total_seconds = duration_ms // 1000
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    seconds = total_seconds % 60

    if hours >= 1:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


class ChoiceFormatter:
    """Formats SearchResult objects into Discord autocomplete choices."""

    @staticmethod
    def format_choices(
        results: list[SearchResult],
        *,
        max_choices: int = 25,
    ) -> list[app_commands.Choice[str]]:
        """Convert SearchResults to Discord autocomplete choices.

        Each choice has:
        - name: ``{icon} {artist} - {title} ({M:SS})`` (≤100 chars)
        - value: ``{prefix}:{track_id}`` (≤100 chars)

        The title is truncated with "…" if the formatted name exceeds 100 chars.
        """
        choices: list[app_commands.Choice[str]] = []

        for result in results[:max_choices]:
            name = ChoiceFormatter._format_name(result)
            value = ChoiceFormatter.encode_value(result.provider, result.track_id)
            choices.append(app_commands.Choice(name=name, value=value))

        return choices

    @staticmethod
    def encode_value(provider: str, track_id: str) -> str:
        """Encode as '{prefix}:{track_id}', truncating track_id if > 100 chars total."""
        prefix = PROVIDER_PREFIXES.get(provider, provider)
        encoded = f"{prefix}:{track_id}"

        if len(encoded) > _MAX_CHOICE_LEN:
            # Truncate track_id so total is exactly 100 chars
            max_id_len = _MAX_CHOICE_LEN - len(prefix) - 1  # -1 for the colon
            encoded = f"{prefix}:{track_id[:max_id_len]}"

        return encoded

    @staticmethod
    def decode_value(value: str) -> tuple[str | None, str]:
        """Decode '{prefix}:{track_id}' → (lavalink_prefix, track_id).

        Returns (None, raw_value) if format unrecognized.
        """
        if ":" not in value:
            return (None, value)

        prefix, track_id = value.split(":", 1)

        if prefix not in PREFIX_TO_LAVALINK:
            return (None, value)

        return (PREFIX_TO_LAVALINK[prefix], track_id)

    @staticmethod
    def _format_name(result: SearchResult) -> str:
        """Build the formatted choice name, truncating title if needed."""
        icon = PROVIDER_ICONS.get(result.provider, "🔵")

        # Build duration suffix
        if result.duration_ms is not None:
            duration_str = f" ({_format_duration(result.duration_ms)})"
        else:
            duration_str = ""

        # Music video indicator
        video_indicator = " 🎬" if result.has_music_video else ""

        # Fixed parts: "{icon}{video_indicator} {artist} - " and " ({duration})"
        prefix_part = f"{icon}{video_indicator} {result.artist} - "
        suffix_part = duration_str

        # Available space for title
        available_for_title = _MAX_CHOICE_LEN - len(prefix_part) - len(suffix_part)

        if available_for_title < 1:
            # Edge case: artist name + duration alone exceed 100 chars
            # Truncate the whole thing
            full = f"{prefix_part}{result.title}{suffix_part}"
            return full[:_MAX_CHOICE_LEN]

        title = result.title
        if len(title) > available_for_title:
            # Truncate title and append "…"
            title = title[: available_for_title - 1] + "…"

        return f"{prefix_part}{title}{suffix_part}"

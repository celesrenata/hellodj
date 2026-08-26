"""Per-guild synced-lyrics overlay store (Requirement 6.2).

Holds the current synced-lyrics payload per guild so the overlay can be toggled
for everyone and so late-joining Activity clients receive the current lyrics on
connect. Lyric *resolution* (LRC/Genius providers) is out of scope for this
control-plane store; the store accepts already-parsed lines and manages the
overlay enable flag and track association.

Pure state with no runtime dependencies — testable in isolation.
"""

from __future__ import annotations

from .models import LyricsState

__all__ = ["LyricsStore", "parse_lrc"]


def parse_lrc(text: str) -> list[tuple[float, str]]:
    """Parse a minimal LRC document into ``(start_seconds, line)`` pairs.

    Supports the common ``[mm:ss.xx]`` timestamp form (one or more per line).
    Lines without a timestamp are ignored. The result is sorted by start time.

    Args:
        text: The raw LRC content.

    Returns:
        Ordered list of ``(start_seconds, text)`` tuples.
    """
    out: list[tuple[float, str]] = []
    for raw_line in text.splitlines():
        line = raw_line.rstrip("\n")
        stamps: list[float] = []
        rest = line
        while rest.startswith("["):
            close = rest.find("]")
            if close == -1:
                break
            tag = rest[1:close]
            seconds = _parse_timestamp(tag)
            if seconds is None:
                break
            stamps.append(seconds)
            rest = rest[close + 1 :]
        content = rest.strip()
        for start in stamps:
            out.append((start, content))
    out.sort(key=lambda pair: pair[0])
    return out


def _parse_timestamp(tag: str) -> float | None:
    """Parse an LRC ``mm:ss.xx`` timestamp into seconds, or ``None``."""
    if ":" not in tag:
        return None
    minutes_str, _, seconds_str = tag.partition(":")
    try:
        minutes = int(minutes_str)
        seconds = float(seconds_str)
    except ValueError:
        return None
    return minutes * 60 + seconds


class LyricsStore:
    """Per-guild lyrics overlay state manager."""

    def __init__(self) -> None:
        """Initialise an empty per-guild lyrics store."""
        self._states: dict[int, LyricsState] = {}

    def get(self, guild_id: int) -> LyricsState | None:
        """Return the lyrics state for a guild, if any."""
        return self._states.get(guild_id)

    def state_message(self, guild_id: int) -> dict | None:
        """Return the late-joiner ``lyrics`` message, or ``None``.

        ``None`` is returned when lyrics are disabled or absent, so the caller
        sends nothing to the joining client.
        """
        state = self._states.get(guild_id)
        if state is None or not state.enabled or not state.lines:
            return None
        return state.to_message()

    def set_lyrics(
        self,
        guild_id: int,
        track_key: str,
        lines: list[tuple[float, str]],
    ) -> LyricsState:
        """Set the current lyrics for a guild's track and enable the overlay."""
        state = self._states.setdefault(guild_id, LyricsState())
        state.track_key = track_key
        state.lines = list(lines)
        state.enabled = True
        return state

    def set_enabled(self, guild_id: int, enabled: bool) -> LyricsState:
        """Toggle the overlay for everyone in a guild."""
        state = self._states.setdefault(guild_id, LyricsState())
        state.enabled = enabled
        return state

    def clear(self, guild_id: int) -> None:
        """Drop lyrics state for a guild (e.g. on track change/skip)."""
        self._states.pop(guild_id, None)

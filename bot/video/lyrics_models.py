"""Data models for the synchronized lyrics overlay system.

Defines timed lyrics structures used by LyricsService, LRC parser,
beat-timing engine, and WebSocket broadcast.

Requirements: 6.4
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


SyncType = Literal["lrc_synced", "lrc_word", "beat_estimated"]


@dataclass
class TimedWord:
    """A single word with its timestamp for karaoke-style highlighting.

    Attributes:
        time_ms: Start time of the word in milliseconds from track start.
        text: The word text to display.
    """

    time_ms: int
    text: str

    def to_dict(self) -> dict:
        """Return a JSON-serializable dict for WebSocket payloads."""
        return {"time_ms": self.time_ms, "text": self.text}


@dataclass
class TimedLine:
    """A single lyrics line with its start timestamp and optional word-level timing.

    Attributes:
        time_ms: Start time of the line in milliseconds from track start.
        text: The full line text to display.
        words: Optional list of word-level timestamps for karaoke highlight.
            Present only when sync_type is "lrc_word".
    """

    time_ms: int
    text: str
    words: list[TimedWord] | None = None

    def to_dict(self) -> dict:
        """Return a JSON-serializable dict for WebSocket payloads."""
        result: dict = {"time_ms": self.time_ms, "text": self.text}
        if self.words is not None:
            result["words"] = [w.to_dict() for w in self.words]
        else:
            result["words"] = None
        return result


@dataclass
class TimedLyrics:
    """Complete timed lyrics payload for a track.

    Contains all timed lines (and optional word-level data) ready for
    WebSocket broadcast to Activity clients.

    Attributes:
        track_id: Identifier for the track (typically "artist:title").
        sync_type: How timing was determined — lrc_synced, lrc_word, or beat_estimated.
        duration_s: Total song duration in seconds.
        lines: Ordered list of timed lyric lines.
    """

    track_id: str
    sync_type: SyncType
    duration_s: float
    lines: list[TimedLine] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Return a JSON-serializable dict suitable for inclusion in WS messages."""
        return {
            "track_id": self.track_id,
            "sync_type": self.sync_type,
            "duration_s": self.duration_s,
            "lines": [line.to_dict() for line in self.lines],
        }

    def to_ws_message(self) -> dict:
        """Return a complete WebSocket message dict with type 'lyrics_data'."""
        return {"type": "lyrics_data", **self.to_dict()}


@dataclass
class LyricsState:
    """Per-guild server-side state for the lyrics overlay system.

    Tracks whether the overlay is enabled, which track's lyrics are loaded,
    and the current timed payload.

    Attributes:
        enabled: Whether the lyrics overlay is currently active for this guild.
        current_lyrics: The current timed lyrics payload, or None if unavailable.
        current_track_key: Cache key for the current track ("artist:title" lowercase).
    """

    enabled: bool = False
    current_lyrics: TimedLyrics | None = None
    current_track_key: str = ""

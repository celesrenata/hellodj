"""Typed domain models for the activity-backend component.

These pure dataclasses carry the server-authoritative state the WebSocket hub
and HTTP endpoints operate on. They have no runtime dependencies (no aiohttp,
no boto3), so they are fully unit-testable in isolation.

The :class:`PlaybackState` uses an anchor-based model (position + monotonic
anchor time) for jitter-free global sync, mirroring the legacy Activity
behavior so the client sync protocol is preserved through the re-platform
(Requirement 6.2).
"""

from __future__ import annotations

import dataclasses
import time

__all__ = [
    "PlaybackState",
    "VisualizerState",
    "LyricsState",
    "MediaType",
    "STROKE_TYPES",
]

#: Media currently driving the Activity for a guild.
MediaType = str  # "video" | "audio"

#: Whiteboard stroke primitive types accepted by the hub (R6.2 whiteboard).
STROKE_TYPES: frozenset[str] = frozenset(
    {
        "freehand",
        "line",
        "rect",
        "ellipse",
        "circle",
        "triangle",
        "star",
        "arrow",
        "text",
        "sticker",
    }
)


@dataclasses.dataclass
class PlaybackState:
    """Server-authoritative playback state for a guild.

    Anchor-based model for jitter-free sync:
        - ``anchor_position``: video position (seconds) at ``anchor_time``.
        - ``anchor_time``: ``time.monotonic()`` when the anchor was set.
        - ``playing``: whether position advances from the anchor.

    The anchor changes only on play/pause/seek — never on periodic ticks — so
    network jitter does not perturb global viewers.
    """

    playing: bool = True
    anchor_position: float = 0.0
    anchor_time: float = dataclasses.field(default_factory=time.monotonic)
    _epoch_offset: float = dataclasses.field(
        default_factory=lambda: time.time() - time.monotonic()
    )
    subtitle_lang: str | None = None
    audio_lang: str | None = None

    @property
    def anchor_time_wall(self) -> float:
        """Wall-clock equivalent of :attr:`anchor_time`."""
        return self.anchor_time + self._epoch_offset

    @property
    def position(self) -> float:
        """Current position computed from the anchor."""
        if self.playing:
            return self.anchor_position + (time.monotonic() - self.anchor_time)
        return self.anchor_position

    def seek_to(self, position: float) -> None:
        """Re-anchor to ``position`` (a seek)."""
        self.anchor_position = max(0.0, float(position))
        self.anchor_time = time.monotonic()

    def set_playing(self, playing: bool) -> None:
        """Toggle play/pause, freezing or resuming the anchor."""
        if playing and not self.playing:
            self.anchor_time = time.monotonic()
        elif not playing and self.playing:
            self.anchor_position = self.anchor_position + (
                time.monotonic() - self.anchor_time
            )
            self.anchor_time = time.monotonic()
        self.playing = playing

    def to_message(self, media_type: MediaType = "audio") -> dict:
        """Serialize the state for a late-joiner ``state`` WS message."""
        return {
            "type": "state",
            "media_type": media_type,
            "playing": self.playing,
            "anchor_position": self.anchor_position,
            "anchor_time_mono": self.anchor_time,
            "anchor_time": self.anchor_time_wall,
            "position": self.position,
            "timestamp": time.time(),
            "subtitle_lang": self.subtitle_lang,
            "audio_lang": self.audio_lang,
        }


@dataclasses.dataclass
class VisualizerState:
    """Per-guild audio-visualizer control state (R6.2 visualizer).

    ``engine`` is the selected visualizer engine (e.g. ``"drift"``,
    ``"audiovis"``, ``"dvd"``, or ``"off"``). ``hls_ready`` indicates the
    transcode pipeline has produced a playable HLS playlist for the engine, and
    ``playlist_url`` is the CloudFront URL clients load.
    """

    engine: str = "off"
    active: bool = False
    hls_ready: bool = False
    playlist_url: str | None = None
    config: dict = dataclasses.field(default_factory=dict)

    def to_message(self) -> dict:
        """Serialize for a ``visualizer`` WS message (late-joiner sync)."""
        return {
            "type": "visualizer",
            "engine": self.engine,
            "state": "active" if self.active else "inactive",
            "hls_ready": self.hls_ready,
            "playlist_url": self.playlist_url,
            "config": dict(self.config),
        }


@dataclasses.dataclass
class LyricsState:
    """Per-guild synced-lyrics overlay state (R6.2 synced lyrics).

    ``enabled`` toggles the overlay for everyone. ``lines`` is the ordered LRC
    payload as ``(start_seconds, text)`` pairs; ``track_key`` identifies the
    track the lyrics belong to so stale lyrics are not shown after a skip.
    """

    enabled: bool = False
    track_key: str | None = None
    lines: list[tuple[float, str]] = dataclasses.field(default_factory=list)

    def to_message(self) -> dict:
        """Serialize for a ``lyrics`` WS message."""
        return {
            "type": "lyrics",
            "enabled": self.enabled,
            "track_key": self.track_key,
            "lines": [[float(t), str(txt)] for t, txt in self.lines],
        }

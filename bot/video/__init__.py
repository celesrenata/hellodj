"""HelloDJ — Video streaming subsystem: data models, enums, and shared types."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Literal


class StreamState(Enum):
    """Lifecycle state of a per-guild video streaming session."""

    IDLE = "idle"
    RESOLVING = "resolving"
    BUFFERING = "buffering"
    STREAMING = "streaming"
    STOPPING = "stopping"
    ERROR = "error"


class Resolution(Enum):
    """Supported output resolutions as (width, height) tuples.

    Width values assume a 16:9 reference frame; the actual output width is
    computed from the source aspect ratio at transcode time.
    """

    RES_480P = (854, 480)
    RES_720P = (1280, 720)
    RES_1080P = (1920, 1080)
    RES_1440P = (2560, 1440)
    RES_2160P = (3840, 2160)

    @property
    def width(self) -> int:
        return self.value[0]

    @property
    def height(self) -> int:
        return self.value[1]

    @classmethod
    def from_height(cls, height: int) -> Resolution:
        """Return the Resolution whose height matches, or the closest one below.

        If the given height is below the smallest supported resolution, the
        smallest resolution is returned.
        """
        # Exact match first
        for res in cls:
            if res.height == height:
                return res

        # Find closest resolution at or below the requested height
        candidates = [r for r in cls if r.height <= height]
        if candidates:
            return max(candidates, key=lambda r: r.height)

        # Height is below the smallest — return smallest
        return min(cls, key=lambda r: r.height)


class SourceQuality(Enum):
    """YouTube source quality selection (pixel height for yt-dlp format filter)."""

    Q_360P = 360
    Q_480P = 480
    Q_720P = 720
    Q_1080P = 1080
    Q_1440P = 1440
    Q_2160P = 2160

    @property
    def height(self) -> int:
        return self.value


@dataclass
class FormatInfo:
    """A single available format option returned by yt-dlp format query."""

    height: int
    codec: str
    fps: float
    filesize_approx: int  # bytes
    format_id: str


@dataclass
class VideoSource:
    """Resolved video source ready for transcoding and streaming."""

    source_type: Literal["youtube", "upload", "url", "tidal"]
    file_path: str
    title: str
    duration_seconds: float  # 0 = unknown / live
    metadata: dict = field(default_factory=dict)
    audio_url: str | None = None
    cleanup_on_finish: bool = False


@dataclass
class SessionStatus:
    """Snapshot of a video streaming session's current state for the Activity API."""

    state: str  # StreamState value
    video_title: str | None
    video_duration: float  # Total duration in seconds (0 = unknown)
    elapsed_seconds: float  # How far into playback we are
    playlist_url: str | None  # Relative URL to playlist.m3u8
    queue_length: int
    session_id: str
    audio_tracks: list[dict] = field(default_factory=list)
    subtitles: list[dict] = field(default_factory=list)
    playing: bool = True
    uploader: str | None = None

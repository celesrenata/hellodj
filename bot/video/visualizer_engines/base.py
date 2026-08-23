"""VisualizerRenderer ABC and shared data classes for visualizer engines."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import AsyncIterator


@dataclass
class AudioFeatures:
    """Audio analysis frame from AudioFeatureBus.

    Attributes:
        fft: 512 magnitude bins from a 1024-sample FFT.
        beat: True if a beat was detected this frame.
        bpm: Current estimated BPM.
        band_energy: 7-band energy levels
            [sub_bass, bass, low_mid, mid, upper_mid, presence, brilliance].
        timestamp: Monotonic time of this frame (seconds).
    """

    fft: list[float]  # 512 bins (1024-sample FFT, magnitude only)
    beat: bool  # True if beat detected this frame
    bpm: float  # Current estimated BPM
    band_energy: list[float]  # [sub_bass, bass, low_mid, mid, upper_mid, presence, brilliance]
    timestamp: float  # Monotonic time of this frame


@dataclass
class TrackMetadata:
    """Current track information for visualizer display.

    Attributes:
        title: Track title.
        artist: Track artist name.
        artwork_url: URL to album/track artwork, or None.
        duration_ms: Total track duration in milliseconds.
        position_ms: Current playback position in milliseconds.
    """

    title: str
    artist: str
    artwork_url: str | None
    duration_ms: int
    position_ms: int


class VisualizerRenderer(ABC):
    """Abstract base for visualizer engine implementations.

    Engines fall into two categories:
    - Client-side (is_client_side=True): Send config to frontend, all rendering
      happens in the browser.
    - Server-rendered (is_client_side=False): Produce raw RGBA frames piped to
      the HLS transcode pipeline.
    """

    @abstractmethod
    async def initialize(self, metadata: TrackMetadata | None = None) -> None:
        """One-time setup. Load resources, configure shaders, etc."""
        ...

    @abstractmethod
    async def activate(self, metadata: TrackMetadata | None = None) -> None:
        """Start producing output (frames or client config)."""
        ...

    @abstractmethod
    async def suspend(self) -> None:
        """Pause rendering, release GPU resources. Must be resumable."""
        ...

    @abstractmethod
    async def resume(self, metadata: TrackMetadata | None = None) -> None:
        """Resume from suspended state with current metadata."""
        ...

    @abstractmethod
    async def stop(self) -> None:
        """Full shutdown. Release all resources."""
        ...

    @abstractmethod
    async def on_track_change(self, metadata: TrackMetadata) -> None:
        """Update displayed metadata. Called in any active/suspended state."""
        ...

    @property
    @abstractmethod
    def is_client_side(self) -> bool:
        """True if rendering happens entirely in the browser."""
        ...

    @property
    @abstractmethod
    def consumes_gpu_while_suspended(self) -> bool:
        """True if suspending still holds GPU allocations (e.g., GPU context)."""
        ...

    @property
    @abstractmethod
    def client_config(self) -> dict | None:
        """Config to send to frontend for client-side engines. None for server-rendered."""
        ...

    def on_audio_features(self, features: AudioFeatures) -> None:
        """Receive audio analysis data from AudioFeatureBus.

        Called synchronously at ~47fps. Store features for next render pass.
        Must be non-blocking. Default no-op for client-side engines.
        """
        pass

    async def render_frames(self) -> AsyncIterator[bytes]:
        """Yield raw RGBA frames for server-rendered engines.

        Each frame is width*height*4 bytes (RGBA). Frame rate is engine-controlled.
        Only called for server-rendered engines (is_client_side=False).
        """
        raise NotImplementedError(
            "Server-rendered engines must implement render_frames()"
        )
        yield b""  # pragma: no cover

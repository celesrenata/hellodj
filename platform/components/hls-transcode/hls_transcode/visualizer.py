"""Visualizer rendering hook point (R6.2).

The Discord Activity audio visualizer is preserved through the re-platform
(Requirement 6.2). On the legacy platform the visualizer engines rendered frames
with the GPU (EGL/OpenGL) and piped them into ffmpeg for HLS encoding. This
module defines the *interface* between a frame source (a visualizer engine) and
the transcode encoder, plus a deterministic stub source used for local
development and tests.

Keeping the visualizer behind a small :class:`FrameSource` protocol means the
concrete engine (native OpenGL, or an NVENC-assisted renderer on the warm GPU)
can be swapped without touching the scheduler/encoder wiring, and the transcode
pipeline stays testable without a GPU or ffmpeg. The renderer exposes the ffmpeg
input specification (raw-video pipe parameters) so the encoder can consume its
frames over stdin — the same "frames fed to the encoder" contract the legacy
pipeline used.

Requirements: 6.2
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

__all__ = ["FrameSpec", "FrameSource", "SolidColorFrameSource", "VisualizerRenderer"]


@dataclass(frozen=True)
class FrameSpec:
    """Geometry/format of the raw frames a visualizer produces.

    Attributes:
        width: Frame pixel width.
        height: Frame pixel height.
        framerate: Frames per second the source emits.
        pixel_format: Raw pixel format (ffmpeg name), e.g. ``rgba`` / ``rgb24``.
    """

    width: int = 1280
    height: int = 720
    framerate: int = 30
    pixel_format: str = "rgba"

    @property
    def bytes_per_pixel(self) -> int:
        """Number of bytes per pixel for :attr:`pixel_format`."""
        return 4 if self.pixel_format.lower() in ("rgba", "bgra") else 3

    @property
    def frame_size_bytes(self) -> int:
        """Total byte size of one raw frame."""
        return self.width * self.height * self.bytes_per_pixel

    def ffmpeg_input_args(self) -> list[str]:
        """Return ffmpeg args to read this raw-video stream from a pipe.

        The returned args precede ``-i <input>`` and tell ffmpeg how to
        interpret the raw frames the visualizer pipes in (the "frames fed to the
        encoder" contract).
        """
        return [
            "-f",
            "rawvideo",
            "-pixel_format",
            self.pixel_format,
            "-video_size",
            f"{self.width}x{self.height}",
            "-framerate",
            str(self.framerate),
        ]


class FrameSource(Protocol):
    """A source of raw visualizer frames fed to the encoder.

    Concrete implementations wrap a GPU/OpenGL visualizer engine. The transcode
    runtime pulls frames and writes them to the ffmpeg stdin pipe; here we only
    define the contract so the engine is swappable and the pipeline testable.
    """

    @property
    def spec(self) -> FrameSpec:
        """The geometry/format of frames this source emits."""
        ...

    def render_frame(self, audio_features: dict[str, float]) -> bytes:
        """Render one frame from the current audio features.

        Args:
            audio_features: Real-time audio features (beat, band energies, ...)
                driving the visualization.

        Returns:
            One raw frame of ``spec.frame_size_bytes`` bytes.
        """
        ...


class SolidColorFrameSource:
    """A deterministic stub frame source for local dev and tests.

    It renders a single solid-color frame whose brightness tracks a ``level``
    audio feature, so the transcode pipeline can be exercised end to end without
    a GPU. Real deployments substitute a GPU visualizer engine implementing the
    same :class:`FrameSource` protocol.
    """

    def __init__(self, spec: FrameSpec | None = None) -> None:
        """Initialise with an optional :class:`FrameSpec` (defaults 720p rgba)."""
        self._spec = spec or FrameSpec()

    @property
    def spec(self) -> FrameSpec:
        """The frame geometry/format this stub emits."""
        return self._spec

    def render_frame(self, audio_features: dict[str, float]) -> bytes:
        """Render one solid frame; grey level scales with the ``level`` feature."""
        level = audio_features.get("level", 0.0)
        clamped = 0.0 if level < 0.0 else 1.0 if level > 1.0 else level
        value = int(round(clamped * 255))
        if self._spec.bytes_per_pixel == 4:
            pixel = bytes((value, value, value, 255))
        else:
            pixel = bytes((value, value, value))
        return pixel * (self._spec.width * self._spec.height)


class VisualizerRenderer:
    """Adapts a :class:`FrameSource` to the encoder's raw-video input contract.

    The renderer exposes the ffmpeg input args for the source's frame format and
    proxies frame production, so the scheduler/encoder can wire a visualizer
    stream (input over the stdin pipe) exactly like the legacy pipeline, with the
    concrete engine swapped in behind the protocol (R6.2).
    """

    def __init__(self, source: FrameSource) -> None:
        """Initialise with the frame source (a GPU engine or the stub)."""
        self._source = source

    @property
    def spec(self) -> FrameSpec:
        """The frame geometry/format of the wrapped source."""
        return self._source.spec

    def ffmpeg_input_args(self) -> list[str]:
        """Return the ffmpeg input args for this visualizer's raw frames."""
        return self._source.spec.ffmpeg_input_args()

    def render_frame(self, audio_features: dict[str, float]) -> bytes:
        """Render one frame from ``audio_features`` via the wrapped source."""
        return self._source.render_frame(audio_features)

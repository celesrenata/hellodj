"""vgalizer audio visualizer integration — server-rendered.

Wraps the vgalizer tool to produce audio-reactive visual output.
When fully implemented, this engine will pipe audio features into vgalizer
and capture its rendered output as raw RGBA frames for the HLS pipeline.

Requirements: 5.2, 7.1
"""

from __future__ import annotations

from typing import AsyncIterator

from .base import TrackMetadata, VisualizerRenderer


class VgalizerEngine(VisualizerRenderer):
    """vgalizer audio visualizer — server-rendered (stub).

    This engine will integrate with vgalizer to produce audio-reactive
    visualizations. Currently raises NotImplementedError on render_frames().
    """

    def __init__(self) -> None:
        self._metadata: TrackMetadata | None = None

    # ------------------------------------------------------------------
    # Lifecycle — no-ops (stub)
    # ------------------------------------------------------------------

    async def initialize(self, metadata: TrackMetadata | None = None) -> None:
        """Store initial metadata. No resources to allocate yet."""
        self._metadata = metadata

    async def activate(self, metadata: TrackMetadata | None = None) -> None:
        """Activate with optional metadata update."""
        self._metadata = metadata or self._metadata

    async def suspend(self) -> None:
        """No-op — nothing to suspend."""

    async def resume(self, metadata: TrackMetadata | None = None) -> None:
        """Resume with optional metadata update."""
        self._metadata = metadata or self._metadata

    async def stop(self) -> None:
        """No-op — nothing to tear down."""

    async def on_track_change(self, metadata: TrackMetadata) -> None:
        """Update stored metadata."""
        self._metadata = metadata

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def is_client_side(self) -> bool:
        """False — this engine produces raw frames on the server."""
        return False

    @property
    def consumes_gpu_while_suspended(self) -> bool:
        """False — no GPU context held while suspended."""
        return False

    @property
    def client_config(self) -> None:
        """None — server-rendered engines don't send config to the frontend."""
        return None

    # ------------------------------------------------------------------
    # Frame rendering (not yet implemented)
    # ------------------------------------------------------------------

    async def render_frames(self) -> AsyncIterator[bytes]:
        """Yield raw RGBA frames — not yet implemented."""
        raise NotImplementedError("Engine 'vgalizer' is not yet implemented")
        yield b""  # pragma: no cover

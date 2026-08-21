"""DVD screensaver engine — client-side only, zero server rendering."""

from __future__ import annotations

from .base import TrackMetadata, VisualizerRenderer


class DVDEngine(VisualizerRenderer):
    """DVD screensaver — client-side only, zero server rendering.

    The server's role is minimal: store the bot avatar URL and current track
    metadata, then expose them via ``client_config`` for the WebSocket message.
    All rendering happens in the browser via CSS/JS.
    """

    def __init__(self, bot_avatar_url: str) -> None:
        self._avatar_url = bot_avatar_url
        self._metadata: TrackMetadata | None = None

    # ------------------------------------------------------------------
    # Lifecycle — all no-ops (no server rendering)
    # ------------------------------------------------------------------

    async def initialize(self, metadata: TrackMetadata | None = None) -> None:
        """Store initial metadata. No resources to allocate."""
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
        """Update stored metadata for the next client_config read."""
        self._metadata = metadata

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def is_client_side(self) -> bool:
        """Always True — all rendering happens in the browser."""
        return True

    @property
    def consumes_gpu_while_suspended(self) -> bool:
        """Always False — no server GPU usage whatsoever."""
        return False

    @property
    def client_config(self) -> dict:
        """Configuration payload sent to the frontend via WebSocket.

        Returns a dict with the bot avatar URL and current track info
        for the DVD screensaver to display.
        """
        return {
            "avatar_url": self._avatar_url,
            "track": {
                "title": self._metadata.title if self._metadata else "",
                "artist": self._metadata.artist if self._metadata else "",
            },
        }

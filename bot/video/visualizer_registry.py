"""HelloDJ — Per-guild VisualizerManager registry and event wiring.

Manages per-guild VisualizerManager instances, connecting WebSocketHub viewer
count changes and ActivityStreamer video lifecycle events to the appropriate
manager. Creates managers lazily on first event for a guild.

This module acts as the integration layer between:
- WebSocketHub (viewer count transitions → on_viewer_join / on_viewer_leave)
- ActivityStreamer (video start/end → on_video_start / on_video_end)
- VisualizerManager (state machine + rendering lifecycle)

Audio Independence (Req 8): This module MUST NOT import from player.py or
share any mutable state with the Lavalink audio playback pipeline.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from video.visualizer_manager import VisualizerManager

if TYPE_CHECKING:
    from video.ws_hub import WebSocketHub

log = logging.getLogger(__name__)


class VisualizerRegistry:
    """Per-guild VisualizerManager registry with WebSocketHub integration.

    Creates VisualizerManager instances lazily (on first viewer count change
    or video event for a guild) and wires them to the WebSocketHub's viewer
    count callback.

    Args:
        ws_hub: The WebSocket hub used for broadcasting and viewer tracking.
        bot_avatar_url: The bot's Discord avatar URL (passed to managers for DVD engine).
    """

    def __init__(self, ws_hub: WebSocketHub, bot_avatar_url: str = "") -> None:
        self._ws_hub = ws_hub
        self._bot_avatar_url = bot_avatar_url
        self._managers: dict[int, VisualizerManager] = {}

        # Wire the viewer count callback into WebSocketHub
        ws_hub.set_viewer_count_callback(self._on_viewer_count_change)

        log.info("VisualizerRegistry initialized and wired to WebSocketHub")

    def get_or_create(self, guild_id: int) -> VisualizerManager:
        """Get or lazily create a VisualizerManager for a guild.

        Args:
            guild_id: The guild to get the manager for.

        Returns:
            The VisualizerManager instance for the guild.
        """
        if guild_id not in self._managers:
            manager = VisualizerManager(
                guild_id=guild_id,
                ws_hub=self._ws_hub,
                bot_avatar_url=self._bot_avatar_url,
            )
            self._managers[guild_id] = manager
            log.debug("Created VisualizerManager for guild %d", guild_id)
        return self._managers[guild_id]

    def get(self, guild_id: int) -> VisualizerManager | None:
        """Get an existing VisualizerManager for a guild, or None.

        Unlike get_or_create, does NOT create a new manager if one doesn't exist.

        Args:
            guild_id: The guild to look up.

        Returns:
            The VisualizerManager instance, or None if not yet created.
        """
        return self._managers.get(guild_id)

    async def shutdown(self) -> None:
        """Shut down all managed VisualizerManager instances.

        Called during bot shutdown or cog unload to cleanly release resources.
        """
        log.info("VisualizerRegistry shutting down %d managers", len(self._managers))
        for guild_id, manager in self._managers.items():
            try:
                await manager.shutdown()
            except Exception:
                log.exception(
                    "Error shutting down VisualizerManager for guild %d", guild_id
                )
        self._managers.clear()

    async def remove(self, guild_id: int) -> None:
        """Shut down and remove the manager for a specific guild.

        Args:
            guild_id: The guild whose manager should be removed.
        """
        manager = self._managers.pop(guild_id, None)
        if manager is not None:
            await manager.shutdown()
            log.debug("Removed VisualizerManager for guild %d", guild_id)

    # ------------------------------------------------------------------
    # WebSocketHub viewer count callback
    # ------------------------------------------------------------------

    async def _on_viewer_count_change(
        self, guild_id: int, old_count: int, new_count: int
    ) -> None:
        """Handle viewer count transitions from WebSocketHub.

        Dispatches to the appropriate guild's VisualizerManager:
        - 0 → N: viewer joined → on_viewer_join()
        - N → 0: all viewers left → on_viewer_leave(viewer_count=0)

        Args:
            guild_id: The guild whose viewer count changed.
            old_count: Previous viewer count.
            new_count: New viewer count.
        """
        manager = self.get_or_create(guild_id)

        if old_count == 0 and new_count >= 1:
            log.debug(
                "Guild %d: first viewer joined (0 → %d) — notifying VisualizerManager",
                guild_id,
                new_count,
            )
            await manager.on_viewer_join()
        elif new_count == 0 and old_count > 0:
            log.debug(
                "Guild %d: all viewers left (%d → 0) — notifying VisualizerManager",
                guild_id,
                old_count,
            )
            await manager.on_viewer_leave(viewer_count=0)

    # ------------------------------------------------------------------
    # ActivityStreamer video lifecycle callbacks
    # ------------------------------------------------------------------

    async def on_video_start(self, guild_id: int) -> None:
        """Notify the guild's VisualizerManager that a video session started.

        Called by ActivityStreamer when a new video begins playing.
        Transitions the visualizer to DISABLED (video takes precedence).

        Args:
            guild_id: The guild where the video started.
        """
        manager = self.get_or_create(guild_id)
        await manager.on_video_start()

    async def on_video_end(self, guild_id: int) -> None:
        """Notify the guild's VisualizerManager that a video session ended.

        Called when the video session stops. Transitions the visualizer to
        IDLE_NO_VIEWERS (ready for viewers).

        Args:
            guild_id: The guild where the video ended.
        """
        manager = self.get_or_create(guild_id)
        await manager.on_video_end()

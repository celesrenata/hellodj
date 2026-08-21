"""Session registry for Video Activity sessions.

Provides a central, asyncio-safe registry of active ActivityStreamer instances
keyed by (guild_id, channel_id) composite key. This enables multiple simultaneous
video Activities in different channels of the same guild.

Supports grace periods so sessions remain alive briefly when all viewers
disconnect, allowing them to rejoin without losing playback.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bot.video.activity_streamer import ActivityStreamer

logger = logging.getLogger(__name__)

CompositeKey = tuple[int, int]  # (guild_id, channel_id)


class SessionRegistry:
    """Asyncio-safe registry of active Activity sessions.

    Uses a plain dict (safe under asyncio's cooperative multitasking model)
    keyed by (guild_id, channel_id) composite tuples. This enables multiple
    simultaneous video sessions across different channels in the same guild.
    """

    def __init__(self) -> None:
        self._sessions: dict[CompositeKey, ActivityStreamer] = {}
        self._grace_period_tasks: dict[CompositeKey, asyncio.Task[None]] = {}

    def register(self, guild_id: int, channel_id: int, streamer: ActivityStreamer) -> None:
        """Register an active session for a guild+channel.

        If a grace period is pending for this key, it is cancelled.
        """
        key: CompositeKey = (guild_id, channel_id)
        self.cancel_grace_period(guild_id, channel_id)
        self._sessions[key] = streamer
        logger.info("Registered session for guild %d channel %d", guild_id, channel_id)

    def unregister(self, guild_id: int, channel_id: int) -> None:
        """Remove a session from the registry.

        Also cancels any pending grace period task for the key.
        """
        key: CompositeKey = (guild_id, channel_id)
        self.cancel_grace_period(guild_id, channel_id)
        removed = self._sessions.pop(key, None)
        if removed is not None:
            logger.info("Unregistered session for guild %d channel %d", guild_id, channel_id)
        else:
            logger.debug(
                "Unregister called for guild %d channel %d but no session found",
                guild_id,
                channel_id,
            )

    def get(self, guild_id: int, channel_id: int) -> ActivityStreamer | None:
        """Return the active session for a guild+channel, or None."""
        return self._sessions.get((guild_id, channel_id))

    def get_by_guild(self, guild_id: int) -> list[tuple[int, ActivityStreamer]]:
        """Return all sessions for a guild as (channel_id, streamer) pairs."""
        return [
            (ch_id, streamer)
            for (g_id, ch_id), streamer in self._sessions.items()
            if g_id == guild_id
        ]

    def active_sessions(self) -> list[CompositeKey]:
        """Return a list of (guild_id, channel_id) tuples with active sessions."""
        return list(self._sessions.keys())

    async def start_grace_period(
        self, guild_id: int, channel_id: int, timeout: float = 30.0
    ) -> None:
        """Start a grace period for a session.

        After *timeout* seconds, the session is stopped and unregistered.
        If a grace period is already running for this key, it is replaced.
        """
        key: CompositeKey = (guild_id, channel_id)

        # Cancel any existing grace period first
        self.cancel_grace_period(guild_id, channel_id)

        streamer = self._sessions.get(key)
        if streamer is None:
            logger.debug(
                "Grace period requested for guild %d channel %d but no active session",
                guild_id,
                channel_id,
            )
            return

        logger.info(
            "Starting %.1fs grace period for guild %d channel %d",
            timeout,
            guild_id,
            channel_id,
        )

        task = asyncio.create_task(
            self._grace_period_coro(guild_id, channel_id, timeout),
            name=f"grace-period-{guild_id}-{channel_id}",
        )
        self._grace_period_tasks[key] = task

    def cancel_grace_period(self, guild_id: int, channel_id: int) -> None:
        """Cancel a pending grace period for a key, if one exists."""
        key: CompositeKey = (guild_id, channel_id)
        task = self._grace_period_tasks.pop(key, None)
        if task is not None and not task.done():
            task.cancel()
            logger.info(
                "Cancelled grace period for guild %d channel %d", guild_id, channel_id
            )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _grace_period_coro(
        self, guild_id: int, channel_id: int, timeout: float
    ) -> None:
        """Background coroutine that waits then tears down the session."""
        key: CompositeKey = (guild_id, channel_id)
        try:
            await asyncio.sleep(timeout)
        except asyncio.CancelledError:
            # Grace period was cancelled (viewer rejoined) — nothing to do
            return

        logger.info(
            "Grace period expired for guild %d channel %d — stopping session",
            guild_id,
            channel_id,
        )

        streamer = self._sessions.get(key)
        if streamer is not None:
            try:
                await streamer.stop()
            except Exception:
                logger.exception(
                    "Error stopping streamer during grace period for guild %d channel %d",
                    guild_id,
                    channel_id,
                )

        # Remove from registry (and clean up task reference)
        self._sessions.pop(key, None)
        self._grace_period_tasks.pop(key, None)

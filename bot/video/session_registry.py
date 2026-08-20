"""Session registry for Video Activity sessions.

Provides a central, asyncio-safe registry of active ActivityStreamer instances
keyed by guild_id. Supports grace periods so sessions remain alive briefly
when all viewers disconnect, allowing them to rejoin without losing playback.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bot.video.activity_streamer import ActivityStreamer

logger = logging.getLogger(__name__)


class SessionRegistry:
    """Thread-safe registry of active Activity sessions.

    Uses a plain dict (safe under asyncio's cooperative multitasking model)
    and asyncio.Task management for grace period timeouts.
    """

    def __init__(self) -> None:
        self._sessions: dict[int, ActivityStreamer] = {}
        self._grace_period_tasks: dict[int, asyncio.Task[None]] = {}

    def register(self, guild_id: int, streamer: ActivityStreamer) -> None:
        """Register an active session for a guild.

        If a grace period is pending for this guild, it is cancelled.
        """
        self.cancel_grace_period(guild_id)
        self._sessions[guild_id] = streamer
        logger.info("Registered session for guild %d", guild_id)

    def unregister(self, guild_id: int) -> None:
        """Remove a session from the registry.

        Also cancels any pending grace period task for the guild.
        """
        self.cancel_grace_period(guild_id)
        removed = self._sessions.pop(guild_id, None)
        if removed is not None:
            logger.info("Unregistered session for guild %d", guild_id)
        else:
            logger.debug("Unregister called for guild %d but no session found", guild_id)

    def get(self, guild_id: int) -> ActivityStreamer | None:
        """Return the active session for a guild, or None."""
        return self._sessions.get(guild_id)

    def active_sessions(self) -> list[int]:
        """Return a list of guild_ids with active sessions."""
        return list(self._sessions.keys())

    async def start_grace_period(self, guild_id: int, timeout: float = 30.0) -> None:
        """Start a grace period for a guild's session.

        After *timeout* seconds, the session is stopped and unregistered.
        If a grace period is already running for this guild, it is replaced.
        """
        # Cancel any existing grace period first
        self.cancel_grace_period(guild_id)

        streamer = self._sessions.get(guild_id)
        if streamer is None:
            logger.debug(
                "Grace period requested for guild %d but no active session", guild_id
            )
            return

        logger.info(
            "Starting %.1fs grace period for guild %d", timeout, guild_id
        )

        task = asyncio.create_task(
            self._grace_period_coro(guild_id, timeout),
            name=f"grace-period-{guild_id}",
        )
        self._grace_period_tasks[guild_id] = task

    def cancel_grace_period(self, guild_id: int) -> None:
        """Cancel a pending grace period for a guild, if one exists."""
        task = self._grace_period_tasks.pop(guild_id, None)
        if task is not None and not task.done():
            task.cancel()
            logger.info("Cancelled grace period for guild %d", guild_id)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _grace_period_coro(self, guild_id: int, timeout: float) -> None:
        """Background coroutine that waits then tears down the session."""
        try:
            await asyncio.sleep(timeout)
        except asyncio.CancelledError:
            # Grace period was cancelled (viewer rejoined) — nothing to do
            return

        logger.info("Grace period expired for guild %d — stopping session", guild_id)

        streamer = self._sessions.get(guild_id)
        if streamer is not None:
            try:
                await streamer.stop()
            except Exception:
                logger.exception(
                    "Error stopping streamer during grace period for guild %d",
                    guild_id,
                )

        # Remove from registry (and clean up task reference)
        self._sessions.pop(guild_id, None)
        self._grace_period_tasks.pop(guild_id, None)

"""Unified session registry for all playback sessions (audio and video).

Stores both Lavalink audio sessions and Discord Activity video sessions
under a composite key of (guild_id, channel_id), enabling simultaneous
sessions across multiple channels within the same guild.

Provides grace period management so sessions can survive brief disconnects
without losing playback state.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Awaitable, Callable, Literal

if TYPE_CHECKING:
    import wavelink

    from bot.video.activity_streamer import ActivityStreamer

logger = logging.getLogger(__name__)

__all__ = ["ChannelSession", "CompositeKey", "SessionRegistry"]

CompositeKey = tuple[int, int]  # (guild_id, channel_id)


@dataclass
class ChannelSession:
    """Represents an active playback session scoped to a specific voice channel."""

    guild_id: int
    channel_id: int
    session_type: Literal["audio", "video"]
    started_at: float = field(default_factory=time.time)
    # Audio-specific
    bot_instance_id: str | None = None
    player: wavelink.Player | None = None
    # Video-specific
    streamer: ActivityStreamer | None = None
    text_channel_id: int | None = None
    queue: list[dict] = field(default_factory=list)
    current: dict | None = None
    auto_resume: bool = True


class SessionRegistry:
    """Central registry of all active playback sessions.

    Uses a plain dict (safe under asyncio's cooperative multitasking model)
    and asyncio.Task management for grace period timeouts.

    Sessions are keyed by (guild_id, channel_id) composite tuples, enabling
    multiple simultaneous sessions across different channels in the same guild.
    """

    def __init__(self) -> None:
        self._sessions: dict[CompositeKey, ChannelSession] = {}
        self._grace_tasks: dict[CompositeKey, asyncio.Task[None]] = {}

    # ------------------------------------------------------------------
    # Core CRUD
    # ------------------------------------------------------------------

    def register(self, session: ChannelSession) -> None:
        """Register an active session.

        If a grace period is pending for this composite key, it is cancelled.
        """
        key: CompositeKey = (session.guild_id, session.channel_id)
        self.cancel_grace_period(session.guild_id, session.channel_id)
        self._sessions[key] = session
        logger.info(
            "Registered %s session for guild %d channel %d",
            session.session_type,
            session.guild_id,
            session.channel_id,
        )

    def unregister(self, guild_id: int, channel_id: int) -> None:
        """Remove a session from the registry.

        Also cancels any pending grace period task for the key.
        """
        key: CompositeKey = (guild_id, channel_id)
        self.cancel_grace_period(guild_id, channel_id)
        removed = self._sessions.pop(key, None)
        if removed is not None:
            logger.info(
                "Unregistered session for guild %d channel %d",
                guild_id,
                channel_id,
            )
        else:
            logger.debug(
                "Unregister called for guild %d channel %d but no session found",
                guild_id,
                channel_id,
            )

    def get(self, guild_id: int, channel_id: int) -> ChannelSession | None:
        """Return the active session for a composite key, or None."""
        return self._sessions.get((guild_id, channel_id))

    # ------------------------------------------------------------------
    # Query helpers
    # ------------------------------------------------------------------

    def get_by_guild(self, guild_id: int) -> list[ChannelSession]:
        """Return all sessions for a guild across all channels."""
        return [s for s in self._sessions.values() if s.guild_id == guild_id]

    def get_audio_sessions(self, guild_id: int) -> list[ChannelSession]:
        """Return all audio sessions for a guild."""
        return [
            s
            for s in self._sessions.values()
            if s.guild_id == guild_id and s.session_type == "audio"
        ]

    def get_video_sessions(self, guild_id: int) -> list[ChannelSession]:
        """Return all video sessions for a guild."""
        return [
            s
            for s in self._sessions.values()
            if s.guild_id == guild_id and s.session_type == "video"
        ]

    def active_keys(self) -> list[CompositeKey]:
        """Return a list of all active composite keys."""
        return list(self._sessions.keys())

    # ------------------------------------------------------------------
    # Grace period management
    # ------------------------------------------------------------------

    def start_grace_period(
        self,
        guild_id: int,
        channel_id: int,
        timeout: float,
        callback: Callable[[int, int], Awaitable[None]] | None = None,
    ) -> None:
        """Start a grace period for a session.

        After *timeout* seconds, the callback is invoked (if provided) and the
        session is unregistered. If a grace period is already running for this
        key, it is replaced.

        Parameters
        ----------
        guild_id:
            The guild ID of the session.
        channel_id:
            The channel ID of the session.
        timeout:
            Seconds to wait before unregistering.
        callback:
            Optional async callable invoked with (guild_id, channel_id) before
            unregistration. Use this to perform cleanup (e.g., stop a streamer,
            disconnect a player).
        """
        key: CompositeKey = (guild_id, channel_id)

        # Cancel any existing grace period first
        self.cancel_grace_period(guild_id, channel_id)

        session = self._sessions.get(key)
        if session is None:
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
            self._grace_period_coro(guild_id, channel_id, timeout, callback),
            name=f"grace-period-{guild_id}-{channel_id}",
        )
        self._grace_tasks[key] = task

    def cancel_grace_period(self, guild_id: int, channel_id: int) -> None:
        """Cancel a pending grace period for a key, if one exists."""
        key: CompositeKey = (guild_id, channel_id)
        task = self._grace_tasks.pop(key, None)
        if task is not None and not task.done():
            task.cancel()
            logger.info(
                "Cancelled grace period for guild %d channel %d",
                guild_id,
                channel_id,
            )

    def has_grace_period(self, guild_id: int, channel_id: int) -> bool:
        """Check whether a grace period is active for the given key."""
        key: CompositeKey = (guild_id, channel_id)
        task = self._grace_tasks.get(key)
        return task is not None and not task.done()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _grace_period_coro(
        self,
        guild_id: int,
        channel_id: int,
        timeout: float,
        callback: Callable[[int, int], Awaitable[None]] | None,
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

        if callback is not None:
            try:
                await callback(guild_id, channel_id)
            except Exception:
                logger.exception(
                    "Error in grace period callback for guild %d channel %d",
                    guild_id,
                    channel_id,
                )

        # Remove from registry (and clean up task reference)
        self._sessions.pop(key, None)
        self._grace_tasks.pop(key, None)

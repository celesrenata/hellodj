"""PlaybackRouter — Central dispatch for unified playback commands.

Routes all playback commands to the appropriate backend (Lavalink for audio,
Activity for video) based on content classification and session state.

Handles:
- Content classification → constraint checks → session creation or enqueue
- Dual-session tie-breaking by `started_at` timestamp
- Audio channel exclusivity enforcement
- Primary bot preference for first audio session
- Inactivity timeout (5 min no humans) triggers instance release
- Error responses for common failure modes
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Literal

from playback.classifier import ContentType, classify
from playback.content_filter import ContentFilter
from playback.queue_display import (
    QueuePaginationView,
    build_dual_queue_embed,
    build_queue_embed,
)
from playback.user_bans import UserBans

if TYPE_CHECKING:
    import discord
    from discord.ext import commands

    from playback.orchestrator import BotInstance, InstanceOrchestrator
    from playback.session_registry import ChannelSession, CompositeKey, SessionRegistry

log = logging.getLogger(__name__)

__all__ = ["PlaybackRouter"]

# Inactivity timeout: 5 minutes with no humans in the voice channel
_INACTIVITY_TIMEOUT_S = 300.0


class PlaybackRouter:
    """Routes playback commands to the appropriate backend.

    The router receives all unified playback commands and determines:
    1. Which voice channel the user is in
    2. Whether a session already exists for that channel
    3. What type of content is being requested
    4. Whether any constraints prevent the request (audio exclusivity, filters)

    Then it delegates to the correct backend (Lavalink or Activity).
    """

    def __init__(
        self,
        classifier: Any,  # ContentClassifier module (bot.playback.classifier)
        registry: SessionRegistry,
        orchestrator: InstanceOrchestrator,
        activity_backend: Any,
        *,
        primary_bot: commands.Bot | None = None,
        content_filter: ContentFilter | None = None,
        user_bans: UserBans | None = None,
    ) -> None:
        """Initialise the PlaybackRouter.

        Parameters
        ----------
        classifier:
            The content classification module (exposes `classify()`).
        registry:
            The SessionRegistry for looking up active sessions.
        orchestrator:
            The InstanceOrchestrator for multi-instance audio assignment.
        activity_backend:
            The Activity backend for video playback delegation.
        primary_bot:
            Optional reference to the primary bot (commands.Bot). Used to
            check voice state availability before assigning secondary instances.
            The primary bot is NOT managed by the orchestrator.
        content_filter:
            Optional per-guild content filter for blocking tracks.
        user_bans:
            Optional per-guild user ban list. When provided, banned users
            receive an ephemeral restriction message on all playback commands.
        """
        self._classifier = classifier
        self._registry = registry
        self._orchestrator = orchestrator
        self._activity_backend = activity_backend
        self._primary_bot = primary_bot
        self._content_filter = content_filter
        self._user_bans = user_bans
        # Inactivity timer tasks keyed by (guild_id, channel_id)
        self._inactivity_timers: dict[tuple[int, int], asyncio.Task[None]] = {}

    # ------------------------------------------------------------------
    # Public command handlers
    # ------------------------------------------------------------------

    async def play(
        self,
        interaction: discord.Interaction,
        query: str,
        *,
        mode: Literal["auto", "audio", "video", "music_video"] = "auto",
        attachment: discord.Attachment | None = None,
    ) -> None:
        """Classify content → resolve or create session → enqueue/play.

        Flow:
        1. Resolve user's voice channel (error if not in VC)
        2. Classify content type
        3. Check content filter
        4. If music_video: route directly to music video resolver
        5. If audio: check channel exclusivity constraints
        6. If session exists for same type: enqueue
        7. If no session: create new session
        8. If conflicting type: create new session (dual-session allowed)
        """
        # Ban check — must happen before ANY other logic
        guild_id = interaction.guild_id  # type: ignore[union-attr]
        user_id = interaction.user.id
        if self._check_user_banned(guild_id, user_id):
            await interaction.response.send_message(
                "You are restricted from using playback commands in this server.",
                ephemeral=True,
            )
            return

        channel_id = self._resolve_user_channel(interaction)
        if channel_id is None:
            await interaction.response.send_message(
                "Join a voice channel first.", ephemeral=True
            )
            return

        # music_video mode: bypass classifier, route directly to video cog's music_video handler
        if mode == "music_video":
            await self._handle_music_video_play(interaction, query, guild_id, channel_id)
            return

        # Classify the input
        attachment_ct = attachment.content_type if attachment else None
        result = classify(query, mode=mode, attachment_content_type=attachment_ct)

        if result is None:
            await interaction.response.send_message(
                "Could not determine content type. Try specifying `mode:audio` or `mode:video`.",
                ephemeral=True,
            )
            return

        # Check content filter before proceeding
        if self._content_filter is not None:
            rule = self._content_filter.check_track(
                guild_id,
                title=query,
                url=query if "://" in query else None,
            )
            if rule is not None:
                await interaction.response.send_message(
                    "This content is blocked in this server.", ephemeral=True
                )
                return

        content_type = result.content_type

        if content_type == ContentType.AUDIO:
            await self._handle_audio_play(interaction, query, guild_id, channel_id)
        else:
            await self._handle_video_play(interaction, query, guild_id, channel_id)

    async def skip(self, interaction: discord.Interaction) -> None:
        """Skip current track in the user's channel session."""
        # Ban check
        guild_id = interaction.guild_id  # type: ignore[union-attr]
        if self._check_user_banned(guild_id, interaction.user.id):
            await interaction.response.send_message(
                "You are restricted from using playback commands in this server.",
                ephemeral=True,
            )
            return

        session = await self._get_session_or_error(interaction)
        if session is None:
            return

        if session.session_type == "audio":
            await self._skip_audio(interaction, session)
        else:
            await self._skip_video(interaction, session)

    async def stop(self, interaction: discord.Interaction) -> None:
        """Stop playback and tear down the session."""
        # Ban check
        guild_id = interaction.guild_id  # type: ignore[union-attr]
        if self._check_user_banned(guild_id, interaction.user.id):
            await interaction.response.send_message(
                "You are restricted from using playback commands in this server.",
                ephemeral=True,
            )
            return

        session = await self._get_session_or_error(interaction)
        if session is None:
            return

        if session.session_type == "audio":
            await self._stop_audio(interaction, session)
        else:
            await self._stop_video(interaction, session)

    async def pause(self, interaction: discord.Interaction) -> None:
        """Toggle pause on the active session."""
        # Ban check
        guild_id = interaction.guild_id  # type: ignore[union-attr]
        if self._check_user_banned(guild_id, interaction.user.id):
            await interaction.response.send_message(
                "You are restricted from using playback commands in this server.",
                ephemeral=True,
            )
            return

        session = await self._get_session_or_error(interaction)
        if session is None:
            return

        if session.session_type == "audio":
            await self._pause_audio(interaction, session)
        else:
            await self._pause_video(interaction, session)

    async def queue(self, interaction: discord.Interaction) -> None:
        """Display queue for the active session."""
        # Ban check
        guild_id = interaction.guild_id  # type: ignore[union-attr]
        if self._check_user_banned(guild_id, interaction.user.id):
            await interaction.response.send_message(
                "You are restricted from using playback commands in this server.",
                ephemeral=True,
            )
            return

        session = await self._get_session_or_error(interaction)
        if session is None:
            return

        if session.session_type == "audio":
            await self._show_audio_queue(interaction, session)
        else:
            await self._show_video_queue(interaction, session)

    async def clear(self, interaction: discord.Interaction) -> None:
        """Clear the queue for the active session."""
        # Ban check
        guild_id = interaction.guild_id  # type: ignore[union-attr]
        if self._check_user_banned(guild_id, interaction.user.id):
            await interaction.response.send_message(
                "You are restricted from using playback commands in this server.",
                ephemeral=True,
            )
            return

        session = await self._get_session_or_error(interaction)
        if session is None:
            return

        if session.session_type == "audio":
            await self._clear_audio(interaction, session)
        else:
            await self._clear_video(interaction, session)

    # ------------------------------------------------------------------
    # Ban enforcement
    # ------------------------------------------------------------------

    def _check_user_banned(self, guild_id: int, user_id: int) -> bool:
        """Check whether a user is banned in the given guild.

        Returns False (no enforcement) when user_bans is None.
        """
        if self._user_bans is None:
            return False
        return self._user_bans.is_banned(guild_id, user_id)

    # ------------------------------------------------------------------
    # Session resolution
    # ------------------------------------------------------------------

    def _resolve_user_channel(self, interaction: discord.Interaction) -> int | None:
        """Extract channel_id from the interaction's voice state.

        Returns the channel ID if the user is in a voice channel,
        or None if not connected to voice.
        """
        member = interaction.user
        # interaction.user is a Member in guild contexts
        if hasattr(member, "voice") and member.voice is not None:  # type: ignore[union-attr]
            channel = member.voice.channel  # type: ignore[union-attr]
            if channel is not None:
                return channel.id
        return None

    def _resolve_session(
        self, guild_id: int, channel_id: int
    ) -> ChannelSession | None:
        """Look up active session by composite key.

        If both an audio and video session exist for the same channel,
        returns the one with the more recent `started_at` timestamp
        (dual-session tie-breaking per Property 13).
        """
        session = self._registry.get(guild_id, channel_id)
        if session is not None:
            return session

        # Check if there are multiple sessions for this channel
        # (audio + video dual-session scenario)
        guild_sessions = self._registry.get_by_guild(guild_id)
        channel_sessions = [
            s for s in guild_sessions if s.channel_id == channel_id
        ]

        if not channel_sessions:
            return None

        if len(channel_sessions) == 1:
            return channel_sessions[0]

        # Dual-session tie-breaking: most recent started_at wins
        return max(channel_sessions, key=lambda s: s.started_at)

    # ------------------------------------------------------------------
    # Primary bot availability check
    # ------------------------------------------------------------------

    def _check_primary_available(self, guild_id: int, channel_id: int) -> bool:
        """Check whether the primary bot can serve audio in the given channel.

        The primary bot is limited to one voice connection per guild (same
        as any other instance). Returns True if:
        - primary_bot is None (not configured — assume available for backward compat)
        - primary bot is not in any voice channel in this guild
        - primary bot is already in the target channel (can reuse)

        Returns False if the primary bot is connected to a different voice
        channel in the same guild.
        """
        if self._primary_bot is None:
            # No primary bot reference — assume available (backward compat)
            return True

        # Check the primary bot's voice_clients for this guild
        for vc in self._primary_bot.voice_clients:
            if vc.guild and vc.guild.id == guild_id:
                # Primary is connected somewhere in this guild
                if vc.channel and vc.channel.id == channel_id:
                    # It's in our target channel — can reuse
                    return True
                else:
                    # It's in a different channel — busy
                    return False

        # Primary is not connected to any VC in this guild — available
        return True

    # ------------------------------------------------------------------
    # Inactivity timer management
    # ------------------------------------------------------------------

    def start_inactivity_timer(self, guild_id: int, channel_id: int) -> None:
        """Start a 5-minute inactivity timer for the given channel.

        Called when a voice state update indicates the voice channel has no
        human users (only bots remaining). If a timer is already running for
        this channel, it is cancelled and restarted.

        On expiry, the session is stopped and the instance is released.
        """
        key = (guild_id, channel_id)

        # Cancel any existing timer for this key
        self.cancel_inactivity_timer(guild_id, channel_id)

        log.info(
            "Starting inactivity timer (%.0fs) for guild=%d channel=%d",
            _INACTIVITY_TIMEOUT_S,
            guild_id,
            channel_id,
        )

        task = asyncio.create_task(
            self._inactivity_expired(guild_id, channel_id),
            name=f"inactivity-{guild_id}-{channel_id}",
        )
        self._inactivity_timers[key] = task

    def cancel_inactivity_timer(self, guild_id: int, channel_id: int) -> None:
        """Cancel the inactivity timer for the given channel, if one exists.

        Called when a human user rejoins the voice channel, indicating the
        channel is no longer inactive.
        """
        key = (guild_id, channel_id)
        task = self._inactivity_timers.pop(key, None)
        if task is not None and not task.done():
            task.cancel()
            log.info(
                "Cancelled inactivity timer for guild=%d channel=%d",
                guild_id,
                channel_id,
            )

    async def _inactivity_expired(self, guild_id: int, channel_id: int) -> None:
        """Async callback invoked when the inactivity timer expires.

        Waits for the timeout duration, then stops the session and releases
        the associated bot instance.
        """
        try:
            await asyncio.sleep(_INACTIVITY_TIMEOUT_S)
        except asyncio.CancelledError:
            # Timer was cancelled (human rejoined) — nothing to do
            return

        key = (guild_id, channel_id)
        # Clean up timer reference
        self._inactivity_timers.pop(key, None)

        log.info(
            "Inactivity timeout expired for guild=%d channel=%d — releasing instance",
            guild_id,
            channel_id,
        )

        # Stop the session and release the instance
        session = self._registry.get(guild_id, channel_id)
        if session is not None and session.session_type == "audio":
            self._registry.unregister(guild_id, channel_id)
            await self._orchestrator.release_instance(guild_id, channel_id)
            log.info(
                "Released instance after inactivity: guild=%d channel=%d",
                guild_id,
                channel_id,
            )

    # ------------------------------------------------------------------
    # Audio play logic
    # ------------------------------------------------------------------

    async def _handle_audio_play(
        self,
        interaction: discord.Interaction,
        query: str,
        guild_id: int,
        channel_id: int,
    ) -> None:
        """Handle a play request classified as audio.

        Priority order:
        1. Existing instance in target channel → reuse (enqueue)
        2. Primary bot available → use primary
        3. Orchestrator assign_instance() → use secondary
        4. All occupied → error with channel list

        Enforces audio channel exclusivity per-instance.
        """
        # Check for existing audio sessions in this guild
        audio_sessions = self._registry.get_audio_sessions(guild_id)

        for existing in audio_sessions:
            if existing.channel_id == channel_id:
                # Same channel — enqueue to existing session
                await self._enqueue_audio(interaction, query, existing)
                return

            # Different channel — check if it's the same bot instance
            # that would serve this request (audio exclusivity)
            instance = self._orchestrator.get_instance_for_channel(
                guild_id, existing.channel_id
            )
            if instance is not None:
                # A secondary instance is busy in another channel
                # Try to find a different available instance
                available = self._orchestrator.available_count
                if available == 0 and not self._check_primary_available(guild_id, channel_id):
                    # No secondary instances and primary not available — report busy
                    channel_name = await self._get_channel_name(
                        interaction, existing.channel_id
                    )
                    await interaction.response.send_message(
                        f"Music is playing in **{channel_name}** — join that channel or wait for it to finish.",
                        ephemeral=True,
                    )
                    return
            else:
                # This session is served by the primary bot — check if primary
                # is in a different channel (busy)
                if not self._check_primary_available(guild_id, channel_id):
                    # Primary is busy in existing.channel_id
                    if self._orchestrator.available_count == 0:
                        channel_name = await self._get_channel_name(
                            interaction, existing.channel_id
                        )
                        await interaction.response.send_message(
                            f"Music is playing in **{channel_name}** — join that channel or wait for it to finish.",
                            ephemeral=True,
                        )
                        return

        # Check if all instances are occupied (including non-audio uses)
        primary_available = self._check_primary_available(guild_id, channel_id)
        if (
            self._orchestrator.available_count == 0
            and not primary_available
            and len(audio_sessions) > 0
        ):
            lines = []
            for s in audio_sessions:
                inst = self._orchestrator.get_instance_for_channel(guild_id, s.channel_id)
                ch_name = await self._get_channel_name(interaction, s.channel_id)
                inst_name = inst.display_name if inst else "Primary"
                lines.append(f"\u2022 {ch_name} \u2014 {inst_name}")
            msg = "All music slots are in use:\n" + "\n".join(lines)
            await interaction.response.send_message(msg, ephemeral=True)
            return

        # No conflict — start a new audio session.
        # Prefer primary bot if available, otherwise assign a secondary.
        await self._start_audio_session(
            interaction, query, guild_id, channel_id, use_primary=primary_available
        )

    async def _handle_video_play(
        self,
        interaction: discord.Interaction,
        query: str,
        guild_id: int,
        channel_id: int,
    ) -> None:
        """Handle a play request classified as video.

        Video is never blocked by audio sessions (Property 9).
        If a video session exists in the same channel, enqueue.
        Otherwise, create a new video session.
        """
        # Check for existing video session in this channel
        video_sessions = self._registry.get_video_sessions(guild_id)
        for existing in video_sessions:
            if existing.channel_id == channel_id:
                # Same channel — enqueue to existing video session
                await self._enqueue_video(interaction, query, existing)
                return

        # No existing video session in this channel — create new
        await self._start_video_session(interaction, query, guild_id, channel_id)

    async def _handle_music_video_play(
        self,
        interaction: discord.Interaction,
        query: str,
        guild_id: int,
        channel_id: int,
    ) -> None:
        """Handle a play request with mode=music_video.

        Delegates to the VideoCog's music_video handler which uses
        MusicVideoResolver → ActivityStreamer pipeline.
        """
        # Get the VideoCog and delegate to its music_video handler
        from cogs.video import VideoCog

        video_cog: VideoCog | None = interaction.client.get_cog("Video")  # type: ignore[assignment]
        if video_cog is None:
            await interaction.response.send_message(
                "❌ Video system is not available.", ephemeral=True
            )
            return

        # Synthesize the same call path as /video music_video
        await video_cog.video_music_video.callback(video_cog, interaction, query)

    # ------------------------------------------------------------------
    # Stub backend methods (to be wired in Task 12)
    # ------------------------------------------------------------------

    async def _start_audio_session(
        self,
        interaction: discord.Interaction,
        query: str,
        guild_id: int,
        channel_id: int,
        *,
        use_primary: bool = False,
    ) -> None:
        """Create a new audio session and begin playback.

        When *use_primary* is True, the primary bot's voice connection is used
        directly (no orchestrator assignment). Otherwise, a secondary instance
        is assigned via the orchestrator.

        Stub: assigns an instance via orchestrator (or uses primary), registers
        session, and acknowledges. Actual wavelink playback wired in integration task.
        """
        if use_primary:
            log.info(
                "Starting audio session (primary bot): guild=%d channel=%d query=%r",
                guild_id,
                channel_id,
                query,
            )
            # TODO(task-12): Create ChannelSession with primary bot, register,
            # connect voice via primary bot, play track
            await interaction.response.send_message(
                f"\U0001f3b5 Starting playback: {query}", ephemeral=False
            )
            return

        instance = await self._orchestrator.assign_instance(guild_id, channel_id)
        if instance is None:
            await interaction.response.send_message(
                "All music slots are in use.", ephemeral=True
            )
            return

        log.info(
            "Starting audio session: guild=%d channel=%d instance=%d query=%r",
            guild_id,
            channel_id,
            instance.index,
            query,
        )
        # TODO(task-12): Create ChannelSession, register, connect voice, play track
        await interaction.response.send_message(
            f"\U0001f3b5 Starting playback: {query}", ephemeral=False
        )

    async def _start_video_session(
        self,
        interaction: discord.Interaction,
        query: str,
        guild_id: int,
        channel_id: int,
    ) -> None:
        """Create a new video session and begin playback.

        Stub: delegates to activity_backend. Actual Activity launch
        wired in integration task.
        """
        log.info(
            "Starting video session: guild=%d channel=%d query=%r",
            guild_id,
            channel_id,
            query,
        )
        # TODO(task-12): Create ChannelSession, register, launch Activity
        await interaction.response.send_message(
            f"🎬 Starting video: {query}", ephemeral=False
        )

    async def _enqueue_audio(
        self,
        interaction: discord.Interaction,
        query: str,
        session: ChannelSession,
    ) -> None:
        """Enqueue a track to an existing audio session.

        Stub: appends to session queue and acknowledges.
        """
        log.info(
            "Enqueuing audio: guild=%d channel=%d query=%r",
            session.guild_id,
            session.channel_id,
            query,
        )
        # TODO(task-12): Resolve track, append to session.queue
        session.queue.append({"query": query})
        await interaction.response.send_message(
            f"🎵 Added to queue: {query}", ephemeral=False
        )

    async def _enqueue_video(
        self,
        interaction: discord.Interaction,
        query: str,
        session: ChannelSession,
    ) -> None:
        """Enqueue a video to an existing video session.

        Stub: appends to session queue and acknowledges.
        """
        log.info(
            "Enqueuing video: guild=%d channel=%d query=%r",
            session.guild_id,
            session.channel_id,
            query,
        )
        # TODO(task-12): Resolve video, append to session.queue
        session.queue.append({"query": query})
        await interaction.response.send_message(
            f"🎬 Added to queue: {query}", ephemeral=False
        )

    async def _skip_audio(
        self, interaction: discord.Interaction, session: ChannelSession
    ) -> None:
        """Skip the current audio track.

        Stub: acknowledges skip. Actual wavelink skip wired in integration.
        """
        log.info(
            "Skipping audio: guild=%d channel=%d",
            session.guild_id,
            session.channel_id,
        )
        # TODO(task-12): Call player.skip() or advance queue
        await interaction.response.send_message("⏭️ Skipped.", ephemeral=False)

    async def _skip_video(
        self, interaction: discord.Interaction, session: ChannelSession
    ) -> None:
        """Skip the current video.

        Stub: acknowledges skip. Actual Activity skip wired in integration.
        """
        log.info(
            "Skipping video: guild=%d channel=%d",
            session.guild_id,
            session.channel_id,
        )
        # TODO(task-12): Skip video via activity_backend
        await interaction.response.send_message("⏭️ Skipped.", ephemeral=False)

    async def _stop_audio(
        self, interaction: discord.Interaction, session: ChannelSession
    ) -> None:
        """Stop audio playback and tear down the session.

        Stub: unregisters session, cancels inactivity timer, and releases instance.
        """
        log.info(
            "Stopping audio: guild=%d channel=%d",
            session.guild_id,
            session.channel_id,
        )
        self.cancel_inactivity_timer(session.guild_id, session.channel_id)
        self._registry.unregister(session.guild_id, session.channel_id)
        await self._orchestrator.release_instance(session.guild_id, session.channel_id)
        # TODO(task-12): Disconnect player, cleanup
        await interaction.response.send_message("\u23f9\ufe0f Stopped.", ephemeral=False)

    async def _stop_video(
        self, interaction: discord.Interaction, session: ChannelSession
    ) -> None:
        """Stop video playback and tear down the session.

        Stub: unregisters session.
        """
        log.info(
            "Stopping video: guild=%d channel=%d",
            session.guild_id,
            session.channel_id,
        )
        self._registry.unregister(session.guild_id, session.channel_id)
        # TODO(task-12): Stop activity_backend streamer
        await interaction.response.send_message("⏹️ Stopped.", ephemeral=False)

    async def _pause_audio(
        self, interaction: discord.Interaction, session: ChannelSession
    ) -> None:
        """Toggle pause on audio playback.

        Stub: acknowledges pause toggle.
        """
        log.info(
            "Toggling pause (audio): guild=%d channel=%d",
            session.guild_id,
            session.channel_id,
        )
        # TODO(task-12): Toggle player.pause()
        await interaction.response.send_message("⏸️ Toggled pause.", ephemeral=False)

    async def _pause_video(
        self, interaction: discord.Interaction, session: ChannelSession
    ) -> None:
        """Toggle pause on video playback.

        Stub: acknowledges pause toggle.
        """
        log.info(
            "Toggling pause (video): guild=%d channel=%d",
            session.guild_id,
            session.channel_id,
        )
        # TODO(task-12): Toggle video pause via activity_backend
        await interaction.response.send_message("⏸️ Toggled pause.", ephemeral=False)

    async def _show_audio_queue(
        self, interaction: discord.Interaction, session: ChannelSession
    ) -> None:
        """Display the audio queue with embed and pagination."""
        await self._show_queue(interaction, session)

    async def _show_video_queue(
        self, interaction: discord.Interaction, session: ChannelSession
    ) -> None:
        """Display the video queue with embed and pagination."""
        await self._show_queue(interaction, session)

    async def _show_queue(
        self, interaction: discord.Interaction, session: ChannelSession
    ) -> None:
        """Display queue for a session, handling dual-queue mode.

        If both audio and video sessions are active in the same channel,
        shows both queues in separate embed sections (dual-queue mode).
        """
        guild_id = interaction.guild_id  # type: ignore[union-attr]
        channel_id = session.channel_id

        # Check for dual-session scenario (both audio + video in same channel)
        guild_sessions = self._registry.get_by_guild(guild_id)
        channel_sessions = [
            s for s in guild_sessions if s.channel_id == channel_id
        ]

        if len(channel_sessions) >= 2:
            # Dual-queue mode — find audio and video sessions
            audio_session = next(
                (s for s in channel_sessions if s.session_type == "audio"), None
            )
            video_session = next(
                (s for s in channel_sessions if s.session_type == "video"), None
            )

            if audio_session is not None and video_session is not None:
                embed = build_dual_queue_embed(audio_session, video_session)
                view = QueuePaginationView(
                    audio_session, second_session=video_session
                )
                await interaction.response.send_message(
                    embed=embed, view=view, ephemeral=False
                )
                return

        # Single-session mode
        embed = build_queue_embed(session)
        view = QueuePaginationView(session)
        await interaction.response.send_message(
            embed=embed, view=view, ephemeral=False
        )

    async def _clear_audio(
        self, interaction: discord.Interaction, session: ChannelSession
    ) -> None:
        """Clear the audio queue."""
        session.queue.clear()
        log.info(
            "Cleared audio queue: guild=%d channel=%d",
            session.guild_id,
            session.channel_id,
        )
        await interaction.response.send_message(
            "🗑️ Queue cleared.", ephemeral=False
        )

    async def _clear_video(
        self, interaction: discord.Interaction, session: ChannelSession
    ) -> None:
        """Clear the video queue."""
        session.queue.clear()
        log.info(
            "Cleared video queue: guild=%d channel=%d",
            session.guild_id,
            session.channel_id,
        )
        await interaction.response.send_message(
            "🗑️ Queue cleared.", ephemeral=False
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _get_session_or_error(
        self, interaction: discord.Interaction
    ) -> ChannelSession | None:
        """Resolve the user's session or send an appropriate error.

        Returns the resolved session, or None if an error was sent.
        """
        channel_id = self._resolve_user_channel(interaction)
        if channel_id is None:
            await interaction.response.send_message(
                "Join a voice channel first.", ephemeral=True
            )
            return None

        guild_id = interaction.guild_id  # type: ignore[union-attr]
        session = self._resolve_session(guild_id, channel_id)
        if session is None:
            await interaction.response.send_message(
                "No active session in your channel.", ephemeral=True
            )
            return None

        return session

    async def _get_channel_name(
        self, interaction: discord.Interaction, channel_id: int
    ) -> str:
        """Resolve a channel ID to its display name.

        Falls back to the raw ID if the channel can't be resolved.
        """
        guild = interaction.guild
        if guild is not None:
            channel = guild.get_channel(channel_id)
            if channel is not None:
                return channel.name
        return f"Channel {channel_id}"

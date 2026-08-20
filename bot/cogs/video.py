"""HelloDJ — Video cog: Discord Activity-based video streaming slash commands.

Provides the ``/video`` command group for streaming video into a voice channel
via a Discord Activity (embedded iframe). Supports YouTube, direct URLs,
queue management, and synchronized playback via HLS.

Commands
--------
- ``/video play <query>``  — Resolve a YouTube URL/search or direct video URL and start streaming.
- ``/video stop``          — Stop the current stream and close the Activity.
- ``/video skip``          — Skip to the next video in the queue.
- ``/video queue``         — Show the current video queue.
"""

from __future__ import annotations

import asyncio
import logging
import uuid

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from video import VideoSource
from video.activity_backend import ActivityBackend
from video.activity_launcher import ActivityLauncher, ActivityLaunchError
from video.activity_streamer import ActivityStreamer, QueueFullError
from video.gpu_probe import GPUProbe
from video.hls_cleanup import cleanup_orphaned_dirs
from video.session_registry import SessionRegistry
from video.sources import (
    URLDownloader,
    URLDownloaderError,
    YouTubeResolver,
    YouTubeResolverError,
    is_video_extension,
)

log = logging.getLogger(__name__)

# Grace period in seconds — how long to wait before stopping when all viewers leave
_GRACE_PERIOD_SECONDS: float = 30.0


class VideoCog(commands.Cog, name="Video"):
    """Video streaming commands — stream video to Discord via Activity."""

    video_group = app_commands.Group(
        name="video",
        description="Stream video into the voice channel via Discord Activity",
    )

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._gpu_probe = GPUProbe()
        self._registry = SessionRegistry()
        self._backend = ActivityBackend(self._registry)
        self._launcher: ActivityLauncher | None = None
        self._http_session: aiohttp.ClientSession | None = None

        # Per-guild instance_ids for token revocation on stop
        self._instance_ids: dict[int, str] = {}

    async def cog_load(self) -> None:
        """Probe GPU, start Activity backend, and prepare launcher on cog load."""
        await self._gpu_probe.probe()

        # Clean up orphaned HLS directories from previous sessions/crashes.
        # At startup there are no active sessions, so remove everything.
        cleanup_orphaned_dirs(active_sessions=set())

        # Start the Activity backend HTTP server
        await self._backend.start(port=8090)

        # Create aiohttp session and launcher for Discord API calls
        self._http_session = aiohttp.ClientSession()
        bot_token = self.bot.http.token
        self._launcher = ActivityLauncher(self._http_session, bot_token)

        log.info("VideoCog loaded: Activity backend started, launcher ready")

    async def cog_unload(self) -> None:
        """Stop all active sessions, backend, and clean up on cog unload."""
        # Stop all active sessions
        for guild_id in list(self._registry.active_sessions()):
            streamer = self._registry.get(guild_id)
            if streamer is not None:
                try:
                    await streamer.stop()
                except Exception as exc:
                    log.warning("Error stopping streamer on unload for guild %d: %s", guild_id, exc)
            self._registry.unregister(guild_id)

        # Revoke all tokens
        for guild_id, instance_id in self._instance_ids.items():
            self._backend.revoke_token(instance_id)
        self._instance_ids.clear()

        # Stop backend
        await self._backend.stop()

        # Close HTTP session
        if self._http_session is not None:
            await self._http_session.close()
            self._http_session = None

        self._launcher = None
        log.info("VideoCog unloaded: all sessions stopped, backend shut down")

    # ── Shared checks ──────────────────────────────────────

    def _check_voice(self, interaction: discord.Interaction) -> str | None:
        """Return an error message if the user is not in a voice channel, else None."""
        if not interaction.user.voice:  # type: ignore[union-attr]
            return "You must join a voice channel first."
        return None

    def _check_gpu(self) -> str | None:
        """Return an error message if GPU is unavailable, else None."""
        if not self._gpu_probe.gpu_available:
            return "Video streaming unavailable: Intel GPU device not detected."
        return None

    # ── /video play ────────────────────────────────────────

    @video_group.command(name="play", description="Play a YouTube video or URL in the voice channel Activity")
    @app_commands.describe(query="YouTube URL, search query, or direct video URL")
    async def video_play(self, interaction: discord.Interaction, query: str) -> None:
        # Pre-checks
        voice_err = self._check_voice(interaction)
        if voice_err:
            await interaction.response.send_message(voice_err, ephemeral=True)
            return

        gpu_err = self._check_gpu()
        if gpu_err:
            await interaction.response.send_message(gpu_err, ephemeral=True)
            return

        await interaction.response.defer()

        guild_id = interaction.guild_id
        assert guild_id is not None

        voice_channel = interaction.user.voice.channel  # type: ignore[union-attr]

        # Resolve the video source
        try:
            if is_video_extension(query):
                downloader = URLDownloader()
                source = await downloader.download(query)
            else:
                resolver = YouTubeResolver()
                source = await resolver.resolve(query)
        except YouTubeResolverError as exc:
            await interaction.followup.send(f"❌ YouTube error: {exc}", ephemeral=True)
            return
        except URLDownloaderError as exc:
            await interaction.followup.send(f"❌ URL error: {exc}", ephemeral=True)
            return
        except Exception as exc:
            log.error("Unexpected error resolving source in /video play: %s", exc, exc_info=True)
            await interaction.followup.send(
                "❌ An unexpected error occurred while resolving the video.",
                ephemeral=True,
            )
            return

        # Check if there's an active session for this guild
        streamer = self._registry.get(guild_id)

        if streamer is not None and streamer.is_active:
            # Session active — enqueue
            try:
                queue_len = streamer.enqueue(source)
                await interaction.followup.send(
                    f"📥 Added to queue (position {queue_len}): **{source.title}**"
                )
            except QueueFullError as exc:
                await interaction.followup.send(f"❌ {exc}", ephemeral=True)
            return

        # No active session — create streamer, launch Activity, start playback
        streamer = ActivityStreamer(guild_id=guild_id, channel_id=voice_channel.id)
        self._registry.register(guild_id, streamer)

        # Launch Discord Activity
        assert self._launcher is not None
        application_id = self.bot.user.id  # type: ignore[union-attr]

        try:
            await self._launcher.launch(voice_channel.id, application_id)
        except ActivityLaunchError as exc:
            self._registry.unregister(guild_id)
            await interaction.followup.send(
                f"❌ Failed to launch Activity: {exc.message}",
                ephemeral=True,
            )
            return

        # Register a token for the Activity frontend authentication
        instance_id = str(uuid.uuid4())
        self._backend.register_token(instance_id, guild_id)
        self._instance_ids[guild_id] = instance_id

        # Start playback
        try:
            await streamer.play(source)
        except Exception as exc:
            log.error("Error starting playback for guild %d: %s", guild_id, exc, exc_info=True)
            self._registry.unregister(guild_id)
            self._backend.revoke_token(instance_id)
            self._instance_ids.pop(guild_id, None)
            await interaction.followup.send(
                "❌ An error occurred while starting video playback.",
                ephemeral=True,
            )
            return

        # Send "Now Playing" embed
        embed = _build_now_playing_embed(source, len(streamer.queue))
        await interaction.followup.send(embed=embed)

    # ── /video stop ────────────────────────────────────────

    @video_group.command(name="stop", description="Stop the current video and close the Activity")
    async def video_stop(self, interaction: discord.Interaction) -> None:
        guild_id = interaction.guild_id
        assert guild_id is not None

        streamer = self._registry.get(guild_id)
        if streamer is None or not streamer.is_active:
            await interaction.response.send_message(
                "No video is currently streaming.", ephemeral=True
            )
            return

        await interaction.response.defer()

        try:
            await streamer.stop()
        except Exception as exc:
            log.error("Error stopping streamer for guild %d: %s", guild_id, exc, exc_info=True)
            await interaction.followup.send(
                "❌ An error occurred while stopping the stream.",
                ephemeral=True,
            )
            return

        # Close the Activity (best-effort)
        if self._launcher is not None:
            try:
                await self._launcher.close(streamer.channel_id)
            except Exception as exc:
                log.warning("Error closing Activity for guild %d: %s", guild_id, exc)

        # Unregister session and revoke token
        self._registry.unregister(guild_id)
        instance_id = self._instance_ids.pop(guild_id, None)
        if instance_id:
            self._backend.revoke_token(instance_id)

        await interaction.followup.send("⏹️ Video stream stopped and Activity closed.")

    # ── /video skip ────────────────────────────────────────

    @video_group.command(name="skip", description="Skip to the next video in the queue")
    async def video_skip(self, interaction: discord.Interaction) -> None:
        guild_id = interaction.guild_id
        assert guild_id is not None

        streamer = self._registry.get(guild_id)
        if streamer is None or not streamer.is_active:
            await interaction.response.send_message(
                "No video is currently streaming.", ephemeral=True
            )
            return

        await interaction.response.defer()

        had_queue = len(streamer.queue) > 0

        try:
            await streamer.skip()
        except Exception as exc:
            log.error("Error skipping for guild %d: %s", guild_id, exc, exc_info=True)
            await interaction.followup.send(
                "❌ An error occurred while skipping.",
                ephemeral=True,
            )
            return

        if had_queue and streamer.is_active and streamer.source:
            # Skipped to next — send Now Playing
            embed = _build_now_playing_embed(streamer.source, len(streamer.queue))
            await interaction.followup.send("⏭️ Skipped!", embed=embed)
        else:
            # Queue was empty, session stopped
            # Clean up Activity
            if self._launcher is not None:
                try:
                    await self._launcher.close(streamer.channel_id)
                except Exception as exc:
                    log.warning("Error closing Activity after skip for guild %d: %s", guild_id, exc)

            self._registry.unregister(guild_id)
            instance_id = self._instance_ids.pop(guild_id, None)
            if instance_id:
                self._backend.revoke_token(instance_id)

            await interaction.followup.send("⏭️ Skipped! Queue is empty — Activity closed.")

    # ── /video queue ───────────────────────────────────────

    @video_group.command(name="queue", description="Show the current video queue")
    async def video_queue(self, interaction: discord.Interaction) -> None:
        guild_id = interaction.guild_id
        assert guild_id is not None

        streamer = self._registry.get(guild_id)

        current_source = streamer.source if streamer and streamer.is_active else None
        queue = list(streamer.queue) if streamer and streamer.is_active else []

        embed = _build_queue_embed(current_source, queue)
        await interaction.response.send_message(embed=embed)

    # ── Voice state listener (grace period) ────────────────

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        """Handle grace period when all viewers leave the voice channel."""
        # Ignore bot's own voice state changes
        if member.bot:
            return

        guild_id = member.guild.id
        streamer = self._registry.get(guild_id)
        if streamer is None or not streamer.is_active:
            return

        channel_id = streamer.channel_id

        # Someone left the voice channel where the Activity is running
        if before.channel and before.channel.id == channel_id:
            # Check if voice channel is now empty of human users
            channel = before.channel
            human_members = [m for m in channel.members if not m.bot]
            if not human_members:
                # All humans left — start grace period
                await self._registry.start_grace_period(guild_id, _GRACE_PERIOD_SECONDS)

        # Someone joined the voice channel where the Activity is running
        if after.channel and after.channel.id == channel_id:
            # Cancel grace period if someone rejoined
            self._registry.cancel_grace_period(guild_id)


# ── Embed builders ─────────────────────────────────────────


def _build_now_playing_embed(source: VideoSource, queue_length: int) -> discord.Embed:
    """Build a 'Now Playing' embed for the current video."""
    embed = discord.Embed(
        title="🎬 Now Playing",
        description=source.title,
        color=discord.Color.purple(),
    )
    if source.duration_seconds > 0:
        minutes = int(source.duration_seconds // 60)
        seconds = int(source.duration_seconds % 60)
        embed.add_field(name="Duration", value=f"{minutes}:{seconds:02d}")
    if queue_length > 0:
        embed.add_field(name="Queue", value=f"{queue_length} video(s) up next")
    return embed


def _build_queue_embed(source: VideoSource | None, queue: list[VideoSource]) -> discord.Embed:
    """Build a queue embed showing current playback and upcoming videos."""
    embed = discord.Embed(title="📋 Video Queue", color=discord.Color.blue())
    if source:
        embed.add_field(name="Now Playing", value=source.title, inline=False)
    if queue:
        lines = [f"{i + 1}. {s.title}" for i, s in enumerate(queue[:20])]
        if len(queue) > 20:
            lines.append(f"... and {len(queue) - 20} more")
        embed.add_field(name="Up Next", value="\n".join(lines), inline=False)
    elif not source:
        embed.description = "Queue is empty. Use `/video play` to add videos."
    return embed


# ── Cog setup ─────────────────────────────────────────────


async def setup(bot: commands.Bot) -> None:
    """Register the Video cog."""
    await bot.add_cog(VideoCog(bot))

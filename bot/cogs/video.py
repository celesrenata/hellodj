"""HelloDJ — Video cog: Discord Activity-based video streaming slash commands.

Provides the ``/video`` command group for streaming video into a voice channel
via a Discord Activity (embedded iframe). Supports YouTube, direct URLs,
queue management, and synchronized playback via HLS.

Commands
--------
- ``/video play <query>``  — Resolve a YouTube URL/search or direct video URL and start streaming.
- ``/video stop``          — Stop the current stream and close the Activity.
- ``/video skip``          — Skip to the next video in the queue.
- ``/video previous``      — Go back to the previously played video.
- ``/video queue``         — Show the current video queue.
"""

from __future__ import annotations

import asyncio
import logging
import time

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from video import VideoSource
from video.activity_backend import ActivityBackend
from video.ws_hub import PlaybackState
from video.activity_launcher import ActivityLauncher, ActivityLaunchError
from video.activity_streamer import ActivityStreamer, QueueFullError, TransitionDeniedError
from video.gpu_probe import GPUProbe
from video.hls_cleanup import cleanup_orphaned_dirs
from video.session_registry import SessionRegistry
from video.source_router import classify_input, SourceType
from video.sources import (
    URLDownloader,
    URLDownloaderError,
    YouTubeResolver,
    YouTubeResolverError,
    is_video_extension,
)
from video.tidal_resolver import TidalResolver, TidalResolverError
from video.upload_handler import UploadHandler, UploadHandlerError

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
        # All dicts keyed by (guild_id, channel_id) composite key
        self._now_playing_messages: dict[tuple[int, int], discord.Message] = {}
        self._seek_bar_tasks: dict[tuple[int, int], asyncio.Task] = {}
        self._activity_urls: dict[tuple[int, int], str] = {}  # (guild_id, channel_id) → Activity invite URL

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
        # Cancel all seek bar update tasks
        for task in self._seek_bar_tasks.values():
            task.cancel()
        self._seek_bar_tasks.clear()
        self._now_playing_messages.clear()

        # Stop all active sessions
        for guild_id, channel_id in list(self._registry.active_sessions()):
            streamer = self._registry.get(guild_id, channel_id)
            if streamer is not None:
                try:
                    await streamer.stop()
                except Exception as exc:
                    log.warning(
                        "Error stopping streamer on unload for guild %d channel %d: %s",
                        guild_id, channel_id, exc,
                    )
            # Disconnect WebSocket clients for this guild
            await self._backend.ws_hub.disconnect_all(guild_id)
            self._registry.unregister(guild_id, channel_id)

        # Stop backend
        await self._backend.stop()

        # Close HTTP session
        if self._http_session is not None:
            await self._http_session.close()
            self._http_session = None

        self._launcher = None
        log.info("VideoCog unloaded: all sessions stopped, backend shut down")

    # ── Legacy deprecation helpers ────────────────────────

    # Replacement mapping: legacy command → unified equivalent
    _LEGACY_REPLACEMENTS: dict[str, str] = {
        "play": "/play <query> mode:video",
        "stop": "/stop",
        "skip": "/skip",
        "previous": "/skip (with unified queue logic)",
        "queue": "/queue",
    }

    async def _check_legacy_allowed(self, interaction: discord.Interaction, command_name: str) -> bool:
        """Check if legacy /video commands are allowed.

        Returns True if the command should proceed (with deprecation notice appended later).
        Returns False if the command was rejected (already sent error message).
        """
        from playback.instance_config import is_legacy_video_enabled

        replacement = self._LEGACY_REPLACEMENTS.get(command_name, "/play")

        if not is_legacy_video_enabled():
            # Globally disabled — reject with replacement listing
            await interaction.response.send_message(
                f"The `/video` commands have been removed. Use `{replacement}` instead.",
                ephemeral=True,
            )
            return False

        # Check guild-specific immediate migration
        guild_id = interaction.guild_id
        if guild_id is not None:
            from guild_settings import get_setting

            immediate_migration = get_setting(guild_id, "unified_playback_immediate", False)
            if immediate_migration:
                await interaction.response.send_message(
                    f"Legacy `/video` commands are disabled for this server. "
                    f"Use `{replacement}` instead.",
                    ephemeral=True,
                )
                return False

        # Transition period active — proceed with deprecation notice
        return True

    def _deprecation_notice(self, command_name: str) -> str:
        """Return the deprecation notice string for a given legacy command."""
        replacement = self._LEGACY_REPLACEMENTS.get(command_name, "/play")
        return f"\n⚠️ This command is deprecated. Use `{replacement}` instead."

    # ── Shared checks ──────────────────────────────────────

    def _check_voice(self, interaction: discord.Interaction) -> str | None:
        """Return an error message if the user is not in a voice channel, else None."""
        if not interaction.user.voice:  # type: ignore[union-attr]
            return "You must join a voice channel first."
        return None

    def _check_same_channel(self, interaction: discord.Interaction) -> str | None:
        """Return an error if the user is not in the same channel as the video Activity.

        Returns None if the check passes or there's no active session (nothing to guard).
        """
        user_voice = interaction.user.voice  # type: ignore[union-attr]
        if not user_voice or not user_voice.channel:
            return "You must join a voice channel first."

        guild_id = interaction.guild_id
        assert guild_id is not None
        channel_id = user_voice.channel.id

        # Check if there's a session in ANY channel for this guild that isn't the user's channel
        for ch_id, streamer in self._registry.get_by_guild(guild_id):
            if streamer.is_active and ch_id != channel_id:
                channel = interaction.guild.get_channel(ch_id)
                channel_name = channel.name if channel else f"ID {ch_id}"
                return (
                    f"The video Activity is in **{channel_name}** — "
                    f"you need to be in that channel to control it."
                )
        return None

    def _check_gpu(self) -> str | None:
        """Return an error message if GPU is unavailable, else None."""
        if not self._gpu_probe.gpu_available:
            return "Video streaming unavailable: Intel GPU device not detected."
        return None

    def _get_user_channel(self, interaction: discord.Interaction) -> int | None:
        """Extract the user's voice channel ID from an interaction.

        Returns None if the user is not in a voice channel.
        """
        user_voice = interaction.user.voice  # type: ignore[union-attr]
        if user_voice and user_voice.channel:
            return user_voice.channel.id
        return None

    # ── /video play ────────────────────────────────────────

    @video_group.command(name="play", description="Play a video in the voice channel Activity")
    @app_commands.describe(
        query="YouTube URL/search, Tidal URL, tidal:search, or direct video URL",
        attachment="Upload a video file directly",
    )
    async def video_play(
        self,
        interaction: discord.Interaction,
        query: str | None = None,
        attachment: discord.Attachment | None = None,
    ) -> None:
        # Legacy deprecation check — must be first
        if not await self._check_legacy_allowed(interaction, "play"):
            return

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

        # Determine source type
        has_attachment = attachment is not None

        # Attachment takes priority (Req 10.4)
        if has_attachment:
            source_type: SourceType = "upload"
        else:
            if query is None:
                await interaction.followup.send(
                    "Provide a URL, search query, or file attachment.", ephemeral=True
                )
                return
            source_type = classify_input(query)

        # Resolve the video source
        try:
            match source_type:
                case "upload":
                    handler = UploadHandler()
                    source = await handler.process(attachment, interaction.user.display_name)
                case "youtube_url" | "youtube_search":
                    resolver = YouTubeResolver()
                    source = await resolver.resolve(query)
                case "tidal_url":
                    tidal = TidalResolver()
                    source = await tidal.resolve_url(query)
                case "tidal_search":
                    tidal = TidalResolver()
                    search_query = query[len("tidal:"):].strip()
                    source = await tidal.search(search_query)
                case "general_url":
                    try:
                        downloader = URLDownloader()
                        source = await asyncio.wait_for(downloader.download(query), timeout=10.0)
                    except (URLDownloaderError, asyncio.TimeoutError):
                        # Fallback to YouTube search (Req 8.6)
                        resolver = YouTubeResolver()
                        source = await resolver.resolve(query)
        except TidalResolverError as exc:
            await interaction.followup.send(f"❌ Tidal error: {exc}", ephemeral=True)
            return
        except UploadHandlerError as exc:
            await interaction.followup.send(f"❌ Upload error: {exc}", ephemeral=True)
            return
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

        # Check if there's an active session for this guild+channel
        streamer = self._registry.get(guild_id, voice_channel.id)

        if streamer is not None and streamer.is_active:
            # Session active — enqueue
            try:
                queue_len = streamer.enqueue(source)
                await interaction.followup.send(
                    f"📥 Added to queue (position {queue_len}): **{source.title}**"
                    + self._deprecation_notice("play")
                )
            except QueueFullError as exc:
                await interaction.followup.send(f"❌ {exc}", ephemeral=True)
            return

        # No active session — create streamer, launch Activity, start playback
        streamer = ActivityStreamer(guild_id=guild_id, channel_id=voice_channel.id, ws_hub=self._backend.ws_hub)
        self._registry.register(guild_id, voice_channel.id, streamer)

        # Launch Discord Activity
        assert self._launcher is not None
        application_id = self.bot.user.id  # type: ignore[union-attr]

        try:
            invite_data = await self._launcher.launch(voice_channel.id, application_id)
        except ActivityLaunchError as exc:
            self._registry.unregister(guild_id, voice_channel.id)
            await interaction.followup.send(
                f"❌ Failed to launch Activity: {exc.message}",
                ephemeral=True,
            )
            return

        # Build the Activity invite URL
        invite_code = invite_data.get("code", "")
        activity_url = f"https://discord.gg/{invite_code}" if invite_code else None

        # Start playback
        try:
            await streamer.play(source)
        except Exception as exc:
            log.error("Error starting playback for guild %d: %s", guild_id, exc, exc_info=True)
            self._registry.unregister(guild_id, voice_channel.id)
            await interaction.followup.send(
                "❌ An error occurred while starting video playback.",
                ephemeral=True,
            )
            return

        # Initialize WebSocketHub playback state for this guild
        self._backend.ws_hub.set_state(
            guild_id,
            PlaybackState(playing=True, position=0.0, last_update=time.monotonic()),
        )

        # Send "Now Playing" embed with control buttons
        embed = _build_now_playing_embed(source, len(streamer.queue), activity_url=activity_url, elapsed_seconds=0.0)
        msg = await interaction.followup.send(embed=embed, view=VideoControlView(self), wait=True)
        key = (guild_id, voice_channel.id)
        self._now_playing_messages[key] = msg
        if activity_url:
            self._activity_urls[key] = activity_url
        self._start_seek_bar_update(key)

        # Send deprecation notice
        await interaction.followup.send(self._deprecation_notice("play"), ephemeral=True)

    # ── /video stop ────────────────────────────────────────

    @video_group.command(name="stop", description="Stop the current video and close the Activity")
    async def video_stop(self, interaction: discord.Interaction) -> None:
        # Legacy deprecation check — must be first
        if not await self._check_legacy_allowed(interaction, "stop"):
            return

        guild_id = interaction.guild_id
        assert guild_id is not None

        err = self._check_same_channel(interaction)
        if err:
            await interaction.response.send_message(err, ephemeral=True)
            return

        channel_id = self._get_user_channel(interaction)
        assert channel_id is not None
        key = (guild_id, channel_id)

        streamer = self._registry.get(guild_id, channel_id)
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

        # Disconnect all WebSocket clients for this guild
        await self._backend.ws_hub.disconnect_all(guild_id)

        # Stop seek bar updates
        self._stop_seek_bar_update(key)

        # Close the Activity (best-effort)
        if self._launcher is not None:
            try:
                await self._launcher.close(streamer.channel_id)
            except Exception as exc:
                log.warning("Error closing Activity for guild %d: %s", guild_id, exc)

        # Unregister session
        self._registry.unregister(guild_id, channel_id)

        await interaction.followup.send(
            "⏹️ Video stream stopped and Activity closed."
            + self._deprecation_notice("stop")
        )

    # ── /video skip ────────────────────────────────────────

    @video_group.command(name="skip", description="Skip to the next video in the queue")
    async def video_skip(self, interaction: discord.Interaction) -> None:
        # Legacy deprecation check — must be first
        if not await self._check_legacy_allowed(interaction, "skip"):
            return

        guild_id = interaction.guild_id
        assert guild_id is not None

        err = self._check_same_channel(interaction)
        if err:
            await interaction.response.send_message(err, ephemeral=True)
            return

        channel_id = self._get_user_channel(interaction)
        assert channel_id is not None
        key = (guild_id, channel_id)

        streamer = self._registry.get(guild_id, channel_id)
        if streamer is None or not streamer.is_active:
            await interaction.response.send_message(
                "No video is currently streaming.", ephemeral=True
            )
            return

        await interaction.response.defer()

        had_queue = len(streamer.queue) > 0

        try:
            await streamer.skip()
        except TransitionDeniedError:
            await interaction.followup.send(
                "⏳ Can't do that right now — video is loading.", ephemeral=True
            )
            return
        except Exception as exc:
            log.error("Error skipping for guild %d: %s", guild_id, exc, exc_info=True)
            # Attempt recovery: try next item in queue
            recovery_ok = await self._attempt_skip_recovery(streamer, guild_id, channel_id)
            if recovery_ok:
                if streamer.is_active and streamer.source:
                    embed = _build_now_playing_embed(streamer.source, len(streamer.queue))
                    await interaction.followup.send("⏭️ Skipped (recovered)!", embed=embed)
                else:
                    await interaction.followup.send(
                        "❌ Playback failed — session stopped.", ephemeral=True
                    )
            else:
                await interaction.followup.send(
                    "❌ Playback failed — session stopped.", ephemeral=True
                )
            return

        if had_queue and streamer.is_active and streamer.source:
            # Skipped to next — send Now Playing with seek bar
            new_state = PlaybackState(playing=True, position=0.0, last_update=time.monotonic())
            self._backend.ws_hub.set_state(guild_id, new_state)
            await self._backend.ws_hub.broadcast_from_bot(guild_id, {
                "type": "state",
                "playing": True,
                "position": 0.0,
                "timestamp": time.time(),
                "subtitle_lang": None,
                "audio_lang": None,
            })
            embed = _build_now_playing_embed(streamer.source, len(streamer.queue), elapsed_seconds=0.0)
            msg = await interaction.followup.send("⏭️ Skipped!", embed=embed, view=VideoControlView(self), wait=True)
            self._now_playing_messages[key] = msg
            self._start_seek_bar_update(key)
            # Send deprecation notice
            await interaction.followup.send(self._deprecation_notice("skip"), ephemeral=True)
        else:
            # Queue was empty, session stopped
            self._stop_seek_bar_update(key)
            # Disconnect WebSocket clients
            await self._backend.ws_hub.disconnect_all(guild_id)
            # Clean up Activity
            if self._launcher is not None:
                try:
                    await self._launcher.close(streamer.channel_id)
                except Exception as exc:
                    log.warning("Error closing Activity after skip for guild %d: %s", guild_id, exc)

            self._registry.unregister(guild_id, channel_id)

            await interaction.followup.send(
                "⏭️ Skipped! Queue is empty — Activity closed."
                + self._deprecation_notice("skip")
            )

    # ── /video previous ───────────────────────────────────

    @video_group.command(name="previous", description="Go back to the previously played video")
    async def video_previous(self, interaction: discord.Interaction) -> None:
        # Legacy deprecation check — must be first
        if not await self._check_legacy_allowed(interaction, "previous"):
            return

        guild_id = interaction.guild_id
        assert guild_id is not None

        err = self._check_same_channel(interaction)
        if err:
            await interaction.response.send_message(err, ephemeral=True)
            return

        channel_id = self._get_user_channel(interaction)
        assert channel_id is not None
        key = (guild_id, channel_id)

        streamer = self._registry.get(guild_id, channel_id)
        if streamer is None or not streamer.is_active:
            await interaction.response.send_message(
                "No video is currently streaming.", ephemeral=True
            )
            return

        await interaction.response.defer()

        try:
            result = await streamer.previous()
        except TransitionDeniedError:
            await interaction.followup.send(
                "⏳ Can't do that right now — video is loading.", ephemeral=True
            )
            return
        except Exception as exc:
            log.error("Error going to previous for guild %d: %s", guild_id, exc, exc_info=True)
            # Attempt recovery: try next item in queue
            recovery_ok = await self._attempt_skip_recovery(streamer, guild_id, channel_id)
            if recovery_ok:
                if streamer.is_active and streamer.source:
                    embed = _build_now_playing_embed(streamer.source, len(streamer.queue))
                    await interaction.followup.send("⏮️ Recovered!", embed=embed)
                else:
                    await interaction.followup.send(
                        "❌ Playback failed — session stopped.", ephemeral=True
                    )
            else:
                await interaction.followup.send(
                    "❌ Playback failed — session stopped.", ephemeral=True
                )
            return

        if not result:
            await interaction.followup.send("⏮️ No previous video available.", ephemeral=True)
            return

        # Success — now playing previous video
        if streamer.is_active and streamer.source:
            new_state = PlaybackState(playing=True, position=0.0, last_update=time.monotonic())
            self._backend.ws_hub.set_state(guild_id, new_state)
            await self._backend.ws_hub.broadcast_from_bot(guild_id, {
                "type": "state",
                "playing": True,
                "position": 0.0,
                "timestamp": time.time(),
                "subtitle_lang": None,
                "audio_lang": None,
            })
            embed = _build_now_playing_embed(streamer.source, len(streamer.queue), elapsed_seconds=0.0)
            msg = await interaction.followup.send("⏮️ Playing previous video!", embed=embed, view=VideoControlView(self), wait=True)
            self._now_playing_messages[key] = msg
            self._start_seek_bar_update(key)
            # Send deprecation notice
            await interaction.followup.send(self._deprecation_notice("previous"), ephemeral=True)
        else:
            await interaction.followup.send(
                "⏮️ Went back to previous video."
                + self._deprecation_notice("previous")
            )

    @video_group.command(name="last", description="Go back to the previously played video (alias for /video previous)")
    async def video_last(self, interaction: discord.Interaction) -> None:
        """Alias for /video previous."""
        await self.video_previous.callback(self, interaction)

    # ── /video queue ───────────────────────────────────────

    @video_group.command(name="queue", description="Show the current video queue")
    async def video_queue(self, interaction: discord.Interaction) -> None:
        # Legacy deprecation check — must be first
        if not await self._check_legacy_allowed(interaction, "queue"):
            return

        guild_id = interaction.guild_id
        assert guild_id is not None

        channel_id = self._get_user_channel(interaction)
        # For queue, allow viewing even if not in a voice channel — show nothing
        if channel_id is not None:
            streamer = self._registry.get(guild_id, channel_id)
        else:
            streamer = None

        current_source = streamer.source if streamer and streamer.is_active else None
        queue = list(streamer.queue) if streamer and streamer.is_active else []

        embed = _build_queue_embed(current_source, queue)
        await interaction.response.send_message(
            embed=embed,
            content=self._deprecation_notice("queue"),
        )

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

        # Someone left a voice channel — check if there's a session in that channel
        if before.channel:
            channel_id = before.channel.id
            streamer = self._registry.get(guild_id, channel_id)
            if streamer is not None and streamer.is_active:
                # Check if voice channel is now empty of human users
                human_members = [m for m in before.channel.members if not m.bot]
                if not human_members:
                    # All humans left — start grace period
                    await self._registry.start_grace_period(
                        guild_id, channel_id, _GRACE_PERIOD_SECONDS
                    )

        # Someone joined a voice channel — cancel grace period if there's a session there
        if after.channel:
            channel_id = after.channel.id
            streamer = self._registry.get(guild_id, channel_id)
            if streamer is not None and streamer.is_active:
                self._registry.cancel_grace_period(guild_id, channel_id)


    # ── Recovery helper ───────────────────────────────────

    async def _attempt_skip_recovery(self, streamer: ActivityStreamer, guild_id: int, channel_id: int) -> bool:
        """Attempt to recover from a playback error by trying the next queue item.

        If the queue is also empty, stops the session and returns False.
        Returns True if recovery succeeded (next item is now playing).
        """
        key = (guild_id, channel_id)
        if streamer.queue:
            next_source = streamer.queue.pop(0)
            log.info(
                "Attempting playback recovery for guild=%d channel=%d with next queue item: %s",
                guild_id,
                channel_id,
                next_source.title,
            )
            try:
                await streamer._play_source(next_source)
                if streamer.is_active:
                    return True
            except Exception as exc:
                log.error(
                    "Recovery also failed for guild %d: %s", guild_id, exc, exc_info=True
                )

        # Recovery failed or queue empty — stop session
        log.warning("Playback recovery failed for guild %d channel %d — stopping session", guild_id, channel_id)
        try:
            await streamer.stop()
        except Exception:
            pass

        # Disconnect WebSocket clients
        await self._backend.ws_hub.disconnect_all(guild_id)

        # Stop seek bar updates
        self._stop_seek_bar_update(key)

        if self._launcher is not None:
            try:
                await self._launcher.close(streamer.channel_id)
            except Exception:
                pass

        self._registry.unregister(guild_id, channel_id)
        return False

    # ── Seek bar update background task ───────────────────

    def _start_seek_bar_update(self, key: tuple[int, int]) -> None:
        """Start (or restart) the periodic seek bar update task for a session."""
        if key in self._seek_bar_tasks:
            self._seek_bar_tasks[key].cancel()
        task = asyncio.create_task(self._update_seek_bar_loop(key))
        self._seek_bar_tasks[key] = task

    def _stop_seek_bar_update(self, key: tuple[int, int]) -> None:
        """Cancel the seek bar update task and clean up message reference."""
        if key in self._seek_bar_tasks:
            self._seek_bar_tasks[key].cancel()
            del self._seek_bar_tasks[key]
        self._now_playing_messages.pop(key, None)
        self._activity_urls.pop(key, None)

    async def _update_seek_bar_loop(self, key: tuple[int, int]) -> None:
        """Background loop that edits the Now Playing embed every 30s with an updated seek bar."""
        guild_id, channel_id = key
        try:
            while True:
                await asyncio.sleep(30)
                streamer = self._registry.get(guild_id, channel_id)
                msg = self._now_playing_messages.get(key)
                if streamer is None or not streamer.is_active or msg is None:
                    break

                # Get elapsed from WebSocketHub state if available, fall back to streamer
                ws_state = self._backend.ws_hub.get_state(guild_id)
                if ws_state is not None:
                    # Compute current position accounting for time since last update
                    if ws_state.playing:
                        elapsed = ws_state.position + (time.monotonic() - ws_state.last_update)
                    else:
                        elapsed = ws_state.position
                else:
                    elapsed = streamer.get_elapsed_seconds()

                duration = streamer.source.duration_seconds if streamer.source else 0.0

                # Clamp elapsed to [0, duration]
                if elapsed < 0:
                    elapsed = 0.0
                if duration > 0 and elapsed > duration:
                    elapsed = duration

                # Rebuild embed with updated seek bar
                activity_url = self._activity_urls.get(key)
                embed = _build_now_playing_embed(streamer.source, len(streamer.queue), activity_url=activity_url, elapsed_seconds=elapsed)

                try:
                    await msg.edit(embed=embed)
                except (discord.NotFound, discord.HTTPException):
                    # Message was deleted or we can't edit it
                    break
        except asyncio.CancelledError:
            pass
        finally:
            # Clean up references if we exited naturally
            self._seek_bar_tasks.pop(key, None)


# ── Embed builders ─────────────────────────────────────────


def _build_seek_bar(elapsed_seconds: float, duration_seconds: float) -> str:
    """Build a text-based seek bar for the Now Playing embed.

    Format: ▬🔘▬▬▬▬▬▬▬▬ 0:30 / 4:24
    10 segments, indicator at position floor(elapsed/duration * 10).
    """

    def fmt_time(seconds: float) -> str:
        s = int(max(0, seconds))
        h = s // 3600
        m = (s % 3600) // 60
        ss = s % 60
        if h > 0:
            return f"{h}:{m:02d}:{ss:02d}"
        return f"{m}:{ss:02d}"

    elapsed_str = fmt_time(elapsed_seconds)

    if duration_seconds <= 0:
        return f"{'▬' * 10} {elapsed_str} / ???"

    duration_str = fmt_time(duration_seconds)

    # Calculate position (0-9)
    pos = int(elapsed_seconds / duration_seconds * 10)
    pos = max(0, min(9, pos))

    # Build bar
    bar = "▬" * pos + "🔘" + "▬" * (9 - pos)
    return f"{bar} {elapsed_str} / {duration_str}"


def _build_now_playing_embed(source: VideoSource, queue_length: int, *, activity_url: str | None = None, elapsed_seconds: float = 0.0) -> discord.Embed:
    """Build a 'Now Playing' embed for the current video."""
    seek_bar = _build_seek_bar(elapsed_seconds, source.duration_seconds)

    # Source-type-aware title formatting
    # Note: For Tidal, source.title is already "Artist — Title" from TidalResolver.
    # The match is left explicit for clarity and future source types.
    title_text = source.title

    embed = discord.Embed(
        title="🎬 Now Playing",
        description=f"{title_text}\n{seek_bar}",
        color=discord.Color.purple(),
    )

    # Upload attribution in footer (Req 6.1)
    if source.source_type == "upload":
        uploader = source.metadata.get("uploader", "Unknown")
        embed.set_footer(text=f"Uploaded by {uploader}")

    if queue_length > 0:
        embed.add_field(name="Queue", value=f"{queue_length} video(s) up next")
    if activity_url:
        install_url = "https://discord.com/oauth2/authorize?client_id=1534778518137995325"
        embed.add_field(
            name="Activity",
            value=f"[Join Activity]({activity_url}) • [Install Activity]({install_url})",
            inline=False,
        )
    return embed


class VideoControlView(discord.ui.View):
    """Playback control buttons for the Now Playing embed.

    Row 0: ⏮ (previous) • ⏪ (seek -10s) • ⏯ (play/pause) • ⏩ (seek +10s) • ⏭ (skip) • 🚫 (stop)

    The ⏯ button broadcasts play/pause via the WebSocketHub to all Activity
    clients. The ⏪/⏩ buttons compute a new position from the hub state and
    broadcast a seek event.
    """

    def __init__(self, cog: VideoCog) -> None:
        super().__init__(timeout=300)
        self._cog = cog

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return True

    @discord.ui.button(label="⏮", style=discord.ButtonStyle.secondary, custom_id="video_previous", row=0)
    async def previous_video(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        """Go back to the previously played video."""
        guild_id = interaction.guild_id
        assert guild_id is not None

        channel_id = self._cog._get_user_channel(interaction)
        if channel_id is None:
            await interaction.response.send_message("You must be in a voice channel.", ephemeral=True)
            return
        key = (guild_id, channel_id)

        streamer = self._cog._registry.get(guild_id, channel_id)
        if streamer is None or not streamer.is_active:
            await interaction.response.send_message("No video is currently streaming.", ephemeral=True)
            return

        await interaction.response.defer()

        try:
            result = await streamer.previous()
        except TransitionDeniedError:
            await interaction.followup.send(
                "⏳ Can't do that right now — video is loading.", ephemeral=True
            )
            return
        except Exception as exc:
            log.error("Error going to previous (button) for guild %d: %s", guild_id, exc, exc_info=True)
            # Attempt recovery
            recovery_ok = await self._cog._attempt_skip_recovery(streamer, guild_id, channel_id)
            if recovery_ok:
                if streamer.is_active and streamer.source:
                    embed = _build_now_playing_embed(streamer.source, len(streamer.queue))
                    await interaction.followup.send("⏮ Recovered!", embed=embed, view=VideoControlView(self._cog))
                else:
                    await interaction.followup.send("❌ Playback failed — session stopped.", ephemeral=True)
                    self._disable_all()
                    if interaction.message:
                        await interaction.message.edit(view=self)
            else:
                await interaction.followup.send("❌ Playback failed — session stopped.", ephemeral=True)
                self._disable_all()
                if interaction.message:
                    await interaction.message.edit(view=self)
            return

        if not result:
            await interaction.followup.send("⏮ No previous video available.", ephemeral=True)
            return

        # Success
        if streamer.is_active and streamer.source:
            import time as _time
            new_state = PlaybackState(playing=True, position=0.0, last_update=_time.monotonic())
            self._cog._backend.ws_hub.set_state(guild_id, new_state)
            await self._cog._backend.ws_hub.broadcast_from_bot(guild_id, {
                "type": "state",
                "playing": True,
                "position": 0.0,
                "timestamp": _time.time(),
                "subtitle_lang": None,
                "audio_lang": None,
            })
            embed = _build_now_playing_embed(streamer.source, len(streamer.queue), elapsed_seconds=0.0)
            msg = await interaction.followup.send("⏮ Playing previous video!", embed=embed, view=VideoControlView(self._cog), wait=True)
            self._cog._now_playing_messages[key] = msg
            self._cog._start_seek_bar_update(key)
        else:
            await interaction.followup.send("⏮ Went back to previous video.")

    @discord.ui.button(label="⏪", style=discord.ButtonStyle.secondary, custom_id="video_seek_back", row=0)
    async def seek_back(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        """Seek back 10 seconds and broadcast to all Activity clients."""
        import time as _time

        guild_id = interaction.guild_id
        assert guild_id is not None

        ws_hub = self._cog._backend.ws_hub
        state = ws_hub.get_state(guild_id)
        if state is None:
            await interaction.response.send_message("No sync state available.", ephemeral=True)
            return

        new_pos = max(0.0, state.position - 10.0)
        state.position = new_pos
        state.last_update = _time.monotonic()
        ws_hub.set_state(guild_id, state)

        msg = {"type": "seek", "position": new_pos, "timestamp": _time.time()}
        await ws_hub.broadcast_from_bot(guild_id, msg)
        await interaction.response.send_message(f"⏪ Seeked back to {int(new_pos)}s", ephemeral=True)

    @discord.ui.button(label="⏯", style=discord.ButtonStyle.primary, custom_id="video_pause", row=0)
    async def pause_resume(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        """Toggle play/pause and broadcast to all Activity clients via WebSocketHub."""
        import time as _time

        guild_id = interaction.guild_id
        assert guild_id is not None

        ws_hub = self._cog._backend.ws_hub
        state = ws_hub.get_state(guild_id)
        if state is None:
            await interaction.response.send_message("No sync state available.", ephemeral=True)
            return

        if state.playing:
            # Pause
            state.playing = False
            state.last_update = _time.monotonic()
            msg = {"type": "pause", "position": state.position, "timestamp": _time.time()}
        else:
            # Resume
            state.playing = True
            state.last_update = _time.monotonic()
            msg = {"type": "play", "position": state.position, "timestamp": _time.time()}

        ws_hub.set_state(guild_id, state)
        await ws_hub.broadcast_from_bot(guild_id, msg)

        emoji = "⏸" if not state.playing else "▶"
        label = "Paused" if not state.playing else "Resumed"
        await interaction.response.send_message(f"{emoji} {label}", ephemeral=True)

    @discord.ui.button(label="⏩", style=discord.ButtonStyle.secondary, custom_id="video_seek_forward", row=0)
    async def seek_forward(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        """Seek forward 10 seconds and broadcast to all Activity clients."""
        import time as _time

        guild_id = interaction.guild_id
        assert guild_id is not None

        ws_hub = self._cog._backend.ws_hub
        state = ws_hub.get_state(guild_id)
        if state is None:
            await interaction.response.send_message("No sync state available.", ephemeral=True)
            return

        new_pos = state.position + 10.0
        state.position = new_pos
        state.last_update = _time.monotonic()
        ws_hub.set_state(guild_id, state)

        msg = {"type": "seek", "position": new_pos, "timestamp": _time.time()}
        await ws_hub.broadcast_from_bot(guild_id, msg)
        await interaction.response.send_message(f"⏩ Seeked forward to {int(new_pos)}s", ephemeral=True)

    @discord.ui.button(label="⏭", style=discord.ButtonStyle.secondary, custom_id="video_skip", row=0)
    async def skip_video(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        """Skip to the next video in the queue."""
        guild_id = interaction.guild_id
        assert guild_id is not None

        channel_id = self._cog._get_user_channel(interaction)
        if channel_id is None:
            await interaction.response.send_message("You must be in a voice channel.", ephemeral=True)
            return
        key = (guild_id, channel_id)

        streamer = self._cog._registry.get(guild_id, channel_id)
        if streamer is None or not streamer.is_active:
            await interaction.response.send_message("No video is currently streaming.", ephemeral=True)
            return

        await interaction.response.defer()
        had_queue = len(streamer.queue) > 0

        try:
            await streamer.skip()
        except TransitionDeniedError:
            await interaction.followup.send(
                "⏳ Can't do that right now — video is loading.", ephemeral=True
            )
            return
        except Exception as exc:
            log.error("Error skipping (button) for guild %d: %s", guild_id, exc, exc_info=True)
            # Attempt recovery
            recovery_ok = await self._cog._attempt_skip_recovery(streamer, guild_id, channel_id)
            if recovery_ok:
                if streamer.is_active and streamer.source:
                    embed = _build_now_playing_embed(streamer.source, len(streamer.queue))
                    await interaction.followup.send("⏭ Skipped (recovered)!", embed=embed, view=VideoControlView(self._cog))
                else:
                    await interaction.followup.send("❌ Playback failed — session stopped.", ephemeral=True)
                    self._disable_all()
                    if interaction.message:
                        await interaction.message.edit(view=self)
            else:
                await interaction.followup.send("❌ Playback failed — session stopped.", ephemeral=True)
                self._disable_all()
                if interaction.message:
                    await interaction.message.edit(view=self)
            return

        if had_queue and streamer.is_active and streamer.source:
            import time as _time
            new_state = PlaybackState(playing=True, position=0.0, last_update=_time.monotonic())
            self._cog._backend.ws_hub.set_state(guild_id, new_state)
            await self._cog._backend.ws_hub.broadcast_from_bot(guild_id, {
                "type": "state",
                "playing": True,
                "position": 0.0,
                "timestamp": _time.time(),
                "subtitle_lang": None,
                "audio_lang": None,
            })
            embed = _build_now_playing_embed(streamer.source, len(streamer.queue), elapsed_seconds=0.0)
            msg = await interaction.followup.send("⏭ Skipped!", embed=embed, view=VideoControlView(self._cog), wait=True)
            self._cog._now_playing_messages[key] = msg
            self._cog._start_seek_bar_update(key)
        else:
            self._cog._stop_seek_bar_update(key)
            await self._cog._backend.ws_hub.disconnect_all(guild_id)
            if self._cog._launcher is not None:
                try:
                    await self._cog._launcher.close(streamer.channel_id)
                except Exception:
                    pass
            self._cog._registry.unregister(guild_id, channel_id)
            await interaction.followup.send("⏭ Queue empty — stopped.")
            self._disable_all()
            if interaction.message:
                await interaction.message.edit(view=self)

    @discord.ui.button(label="🚫", style=discord.ButtonStyle.danger, custom_id="video_stop", row=1)
    async def stop_video(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        """Stop the current video and close the Activity."""
        guild_id = interaction.guild_id
        assert guild_id is not None

        channel_id = self._cog._get_user_channel(interaction)
        if channel_id is None:
            await interaction.response.send_message("You must be in a voice channel.", ephemeral=True)
            return
        key = (guild_id, channel_id)

        streamer = self._cog._registry.get(guild_id, channel_id)
        if streamer is None or not streamer.is_active:
            await interaction.response.send_message("No video is currently streaming.", ephemeral=True)
            return

        await interaction.response.defer()
        await streamer.stop()

        # Disconnect all WebSocket clients for this guild
        await self._cog._backend.ws_hub.disconnect_all(guild_id)

        # Stop seek bar updates
        self._cog._stop_seek_bar_update(key)

        if self._cog._launcher is not None:
            try:
                await self._cog._launcher.close(channel_id)
            except Exception:
                pass

        self._cog._registry.unregister(guild_id, channel_id)
        await interaction.followup.send("⏹️ Video stopped.")
        self._disable_all()
        if interaction.message:
            await interaction.message.edit(view=self)

    def _disable_all(self) -> None:
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True


def _build_queue_embed(source: VideoSource | None, queue: list[VideoSource]) -> discord.Embed:
    """Build a queue embed showing current playback and upcoming videos."""
    embed = discord.Embed(title="📋 Video Queue", color=discord.Color.blue())
    if source:
        now_playing_text = source.title
        if source.source_type == "upload":
            uploader = source.metadata.get("uploader", "Unknown")
            now_playing_text += f" (uploaded by {uploader})"
        embed.add_field(name="Now Playing", value=now_playing_text, inline=False)
    if queue:
        lines: list[str] = []
        for i, s in enumerate(queue[:20]):
            line = f"{i + 1}. {s.title}"
            if s.source_type == "upload":
                uploader = s.metadata.get("uploader", "Unknown")
                line += f" (uploaded by {uploader})"
            lines.append(line)
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

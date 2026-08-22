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

# URL patterns that indicate a playlist (should use allow_playlist=True)
_PLAYLIST_PATTERNS = (
    "/playlist",       # YouTube, Spotify, Tidal playlists
    "/album",          # Spotify/Tidal album URLs
    "/sets/",          # SoundCloud sets (playlists)
    "?list=",          # YouTube ?list= parameter
    "&list=",          # YouTube &list= parameter
)


def _is_playlist_url(url: str) -> bool:
    """Heuristic: detect if a URL points to a playlist/album rather than a single track."""
    lower = url.lower()
    return any(pat in lower for pat in _PLAYLIST_PATTERNS)


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
        mode: Literal["auto", "audio", "video", "music_video", "album"] = "auto",
    ) -> None:
        """Classify content → resolve or create session → enqueue/play.

        Flow:
        1. Resolve user's voice channel (error if not in VC)
        2. Classify content type
        3. Check content filter
        4. If music_video: route to video cog's music_video handler
        5. If album: route to music cog's album flow
        6. If audio: check channel exclusivity constraints
        7. If session exists for same type: enqueue
        8. If no session: create new session
        9. If conflicting type: create new session (dual-session allowed)
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

        # music_video mode: bypass classifier, route directly to video cog
        if mode == "music_video":
            await self._handle_music_video_play(interaction, query, guild_id, channel_id)
            return

        # album mode: bypass classifier, route to music cog's album flow
        if mode == "album":
            await self._handle_album_play(interaction, query, guild_id, channel_id)
            return

        # Classify the input
        result = classify(query, mode=mode)

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

        session = await self._get_session_or_error_or_player(interaction)
        if session is None:
            return

        if session == "audio_player":
            await self._skip_audio_direct(interaction)
        elif session == "video_session":
            await self._skip_video_direct(interaction)
        elif session.session_type == "audio":
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

        session = await self._get_session_or_error_or_player(interaction)
        if session is None:
            return

        if session == "audio_player":
            await self._stop_audio_direct(interaction)
        elif session == "video_session":
            await self._stop_video_direct(interaction)
        elif session.session_type == "audio":
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

        session = await self._get_session_or_error_or_player(interaction)
        if session is None:
            return

        if session == "audio_player":
            await self._pause_audio_direct(interaction)
        elif session == "video_session":
            await self._pause_video(interaction, session)  # video pause handled by Activity
        elif session.session_type == "audio":
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

        session = await self._get_session_or_error_or_player(interaction)
        if session is None:
            return

        if session == "audio_player":
            await self._show_audio_queue_direct(interaction)
        elif session.session_type == "audio":
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

        session = await self._get_session_or_error_or_player(interaction)
        if session is None:
            return

        if session == "audio_player":
            await self._clear_audio_direct(interaction)
        elif session.session_type == "audio":
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

        If a video session is active, the track is queued to the unified
        queue instead of starting a new audio session.
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

    # ------------------------------------------------------------------
    # Audio backend — delegates to Music cog's player/wavelink system
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

        Delegates to the Music cog's internal helpers which handle wavelink
        connection, track resolution, and queue management via player.py.
        """
        log.info(
            "Starting audio session: guild=%d channel=%d query=%r use_primary=%s",
            guild_id,
            channel_id,
            query,
            use_primary,
        )

        # Get the Music cog and delegate
        music_cog = interaction.client.get_cog("Music")  # type: ignore[union-attr]
        if music_cog is None:
            log.error("Music cog not loaded — cannot start audio session")
            await interaction.response.send_message(
                "❌ Music system is not available.", ephemeral=True
            )
            return

        # Route: playlist URL → _play_playlist, other URL → _play_link, search → _play_song
        is_url = query.startswith("http://") or query.startswith("https://")
        if is_url:
            if _is_playlist_url(query):
                await music_cog._play_playlist(interaction, query)
            else:
                await music_cog._play_link(interaction, query)
        else:
            await music_cog._play_song(interaction, query)

    async def _start_video_session(
        self,
        interaction: discord.Interaction,
        query: str,
        guild_id: int,
        channel_id: int,
    ) -> None:
        """Create a new video session — delegates to VideoCog.video_play."""
        log.info(
            "Starting video session: guild=%d channel=%d query=%r",
            guild_id,
            channel_id,
            query,
        )

        video_cog = interaction.client.get_cog("Video")  # type: ignore[union-attr]
        if video_cog is None:
            await interaction.response.send_message(
                "❌ Video system is not available.", ephemeral=True
            )
            return

        await video_cog.video_play(interaction, query)

    async def _handle_music_video_play(
        self,
        interaction: discord.Interaction,
        query: str,
        guild_id: int,
        channel_id: int,
    ) -> None:
        """Handle /play type:music_video — search for videos and show a picker.

        For URLs, starts immediately. For text queries, searches for video
        results (Tidal first when source_provider is tidal, then YouTube)
        and shows a selection dropdown.
        """
        import player

        await interaction.response.defer()

        is_url = query.startswith("http://") or query.startswith("https://")

        if is_url:
            # Direct URL — skip picker, start/queue immediately
            await self._music_video_enqueue_or_start(interaction, query, guild_id, channel_id)
            return

        # Text search — try provider-specific video search, fall back to YouTube
        await interaction.followup.send("🔄 Searching for music videos…", ephemeral=True)

        state = player.get_state(guild_id)
        source_provider = state.get("source_provider", "youtube")
        log.info("Music video search: guild=%d source_provider=%r query=%r", guild_id, source_provider, query)

        results = await self._search_music_videos(query, source_provider=source_provider)
        if not results:
            await interaction.followup.send("No music videos found for that query.")
            return

        # Show picker
        import discord as _discord
        from cogs.music import SearchSelectView

        # Convert yt-dlp results to the format SearchSelectView expects
        picker_results = []
        for r in results[:10]:
            picker_results.append({
                "title": r.get("title", "Unknown"),
                "author": r.get("uploader") or r.get("channel") or "",
                "duration": (r.get("duration") or 0) * 1000,  # seconds → ms
                "url": r.get("url") or r.get("webpage_url") or "",
                "webpage_url": r.get("url") or r.get("webpage_url") or "",
            })

        async def on_pick(info: dict, picker: _discord.Interaction):
            video_url = info.get("webpage_url") or info.get("url", "")
            title = info.get("title", query)
            await picker.response.edit_message(
                content=f"🎬 Loading: **{title}**…", view=None
            )
            await self._music_video_enqueue_or_start(
                interaction, video_url, guild_id, channel_id, title=title
            )

        view = SearchSelectView(picker_results, interaction.user.id, on_pick, guild_id=interaction.guild_id)
        msg = await interaction.followup.send("Select a music video:", view=view)
        view.message = msg

    async def _search_music_videos(self, query: str, *, source_provider: str = "youtube") -> list[dict]:
        """Search for music video results (metadata only, no download).

        When source_provider is "tidal", tries Tidal video search first.
        Always falls back to YouTube via yt-dlp --flat-playlist.
        Returns up to 5 results in a unified format.
        """
        import asyncio as _asyncio
        import json

        # Try Tidal video search first when provider is tidal
        if source_provider == "tidal":
            log.info("Music video search: trying Tidal v2 video search for %r", query)
            try:
                tidal_results = await self._search_tidal_videos(query)
                if tidal_results:
                    log.info("Music video search: Tidal returned %d results for %r", len(tidal_results), query)
                    # Supplement with YouTube results for more options
                    yt_results = await self._search_youtube_videos(query)
                    # Combine: Tidal first, then YouTube (deduplicated by title roughly)
                    combined = tidal_results + yt_results
                    return combined[:10]
            except Exception as exc:
                log.debug("Tidal video search failed for %r: %s", query, exc)

        # YouTube search (primary for non-tidal providers, fallback for tidal)
        return await self._search_youtube_videos(query)

    async def _search_youtube_videos(self, query: str) -> list[dict]:
        """Search YouTube for music video results via yt-dlp --flat-playlist."""
        import asyncio as _asyncio
        import json

        search_query = f"ytsearch5:{query} official music video"
        args = [
            "yt-dlp",
            "--flat-playlist",
            "--dump-json",
            "--no-download",
            search_query,
        ]

        try:
            process = await _asyncio.create_subprocess_exec(
                *args,
                stdout=_asyncio.subprocess.PIPE,
                stderr=_asyncio.subprocess.PIPE,
            )
            stdout, stderr = await _asyncio.wait_for(
                process.communicate(), timeout=15
            )
        except _asyncio.TimeoutError:
            try:
                process.kill()
                await process.wait()
            except (ProcessLookupError, OSError):
                pass
            log.warning("Music video search timed out for %r", query)
            return []
        except (FileNotFoundError, OSError) as exc:
            log.warning("Music video search failed (yt-dlp not found?): %s", exc)
            return []

        if process.returncode != 0:
            log.debug("Music video search returned non-zero for %r", query)
            return []

        results = []
        for line in stdout.decode(errors="replace").strip().splitlines():
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
                results.append(entry)
            except json.JSONDecodeError:
                continue

        return results

    async def _search_tidal_videos(self, query: str) -> list[dict]:
        """Search Tidal's v2 API for music videos (metadata only).

        Uses the same JSON:API pattern as album search but with
        include=videos,videos.artists. Returns up to 5 results.
        """
        import aiohttp
        import re as _re
        from credentials import creds
        from urllib.parse import quote

        token = creds.get("tidal.access_token", "")
        if not token:
            return []

        encoded = quote(query)
        url = (
            f"https://openapi.tidal.com/v2/searchResults"
            f"?filter%5Bquery%5D={encoded}"
            f"&countryCode=US"
            f"&include=videos,videos.artists"
        )
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.api+json",
        }

        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
                async with session.get(url, headers=headers) as resp:
                    if resp.status != 200:
                        log.debug("Tidal v2 video search returned %d", resp.status)
                        return []
                    data = await resp.json()
        except Exception as exc:
            log.debug("Tidal v2 video search network error: %s", exc)
            return []

        included = data.get("included", [])

        # Build artist lookup
        artist_map: dict[str, str] = {}
        for resource in included:
            if resource.get("type") == "artists":
                aid = resource.get("id", "")
                name = resource.get("attributes", {}).get("name", "")
                if aid and name:
                    artist_map[aid] = name

        # Extract video results
        results = []
        for resource in included:
            if resource.get("type") != "videos":
                continue
            attrs = resource.get("attributes", {})
            video_id = resource.get("id", "")
            title = attrs.get("title", "")
            if not title or not video_id:
                continue

            # Resolve artist names from relationships
            rels = resource.get("relationships", {})
            artist_refs = rels.get("artists", {}).get("data", [])
            artist_names = [artist_map.get(ref.get("id", ""), "") for ref in artist_refs]
            artist_names = [n for n in artist_names if n]
            artist = ", ".join(artist_names)

            # Parse ISO 8601 duration (PT3M24S) to seconds
            duration_str = attrs.get("duration", "")
            duration = 0
            if duration_str:
                m = _re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", duration_str)
                if m:
                    duration = int(m.group(1) or 0) * 3600 + int(m.group(2) or 0) * 60 + int(m.group(3) or 0)

            video_url = f"https://tidal.com/browse/video/{video_id}"

            results.append({
                "title": title,
                "uploader": artist,
                "channel": artist,
                "duration": duration,
                "url": video_url,
                "webpage_url": video_url,
            })

            if len(results) >= 5:
                break

        return results

    async def _music_video_enqueue_or_start(
        self,
        interaction: discord.Interaction,
        video_url: str,
        guild_id: int,
        channel_id: int,
        *,
        title: str | None = None,
    ) -> None:
        """Enqueue or start a music video by URL (after user has picked)."""
        import player

        state = player.get_state(guild_id)
        voice_channel = interaction.client.get_channel(channel_id)

        if voice_channel:
            state["voice_channel"] = voice_channel
        state["text_channel"] = interaction.channel

        entry = {
            "type": "music_video",
            "query": video_url,
            "title": f"🎬 {title or video_url}",
        }

        current = state.get("current")
        p = player.get_player(guild_id)
        is_playing = p and p.connected and (p.playing or p.paused)

        if not is_playing and not current:
            state["current"] = entry
            player.persist(guild_id)
            await player._start_video_from_queue(guild_id, entry)
            await interaction.followup.send(f"🎬 Starting music video: **{title or video_url}**")
        else:
            state.setdefault("queue", []).append(entry)
            player.persist(guild_id)
            pos = len(state["queue"])
            await interaction.followup.send(
                f"🎬 Music video queued (position {pos}): **{title or video_url}**"
            )

    async def _handle_album_play(
        self,
        interaction: discord.Interaction,
        query: str,
        guild_id: int,
        channel_id: int,
    ) -> None:
        """Handle /play type:album — delegates to Music cog's album flow."""
        music_cog = interaction.client.get_cog("Music")  # type: ignore[union-attr]
        if music_cog is None:
            await interaction.response.send_message(
                "❌ Music system is not available.", ephemeral=True
            )
            return
        await music_cog._play_album(interaction, query)

    async def _enqueue_audio(
        self,
        interaction: discord.Interaction,
        query: str,
        session: ChannelSession,
    ) -> None:
        """Enqueue a track to an existing audio session.

        Delegates to Music cog — player.py handles queueing when already playing.
        """
        log.info(
            "Enqueuing audio: guild=%d channel=%d query=%r",
            session.guild_id,
            session.channel_id,
            query,
        )

        music_cog = interaction.client.get_cog("Music")  # type: ignore[union-attr]
        if music_cog is None:
            await interaction.response.send_message(
                "❌ Music system is not available.", ephemeral=True
            )
            return

        is_url = query.startswith("http://") or query.startswith("https://")
        if is_url:
            if _is_playlist_url(query):
                await music_cog._play_playlist(interaction, query)
            else:
                await music_cog._play_link(interaction, query)
        else:
            await music_cog._play_song(interaction, query)

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
        """Skip the current audio track via wavelink player.stop()."""
        import player

        log.info(
            "Skipping audio: guild=%d channel=%d",
            session.guild_id,
            session.channel_id,
        )
        p = player.get_player(session.guild_id)
        if p and p.connected:
            await p.stop()  # triggers on_wavelink_track_end → plays next
            await interaction.response.send_message("⏭️ Skipped.", ephemeral=False)
        else:
            await interaction.response.send_message(
                "Nothing is playing right now.", ephemeral=True
            )

    async def _skip_video(
        self, interaction: discord.Interaction, session: ChannelSession
    ) -> None:
        """Skip the current video — delegates to VideoCog."""
        video_cog = interaction.client.get_cog("Video")  # type: ignore[union-attr]
        if video_cog is not None:
            await video_cog.video_skip(interaction)
        else:
            await interaction.response.send_message("⏭️ Skipped.", ephemeral=False)

    async def _stop_audio(
        self, interaction: discord.Interaction, session: ChannelSession
    ) -> None:
        """Stop audio playback, disconnect player, and tear down the session."""
        import player

        log.info(
            "Stopping audio: guild=%d channel=%d",
            session.guild_id,
            session.channel_id,
        )
        self.cancel_inactivity_timer(session.guild_id, session.channel_id)
        self._registry.unregister(session.guild_id, session.channel_id)
        await self._orchestrator.release_instance(session.guild_id, session.channel_id)

        # Disconnect the wavelink player
        p = player.get_player(session.guild_id)
        if p and p.connected:
            await p.disconnect()

        # Clear player state
        state = player.get_state(session.guild_id)
        state["current"] = None
        state["queue"] = []
        state["player"] = None
        player.persist(session.guild_id)

        await interaction.response.send_message("\u23f9\ufe0f Stopped.", ephemeral=False)

    async def _stop_video(
        self, interaction: discord.Interaction, session: ChannelSession
    ) -> None:
        """Stop video playback — delegates to VideoCog."""
        self._registry.unregister(session.guild_id, session.channel_id)
        video_cog = interaction.client.get_cog("Video")  # type: ignore[union-attr]
        if video_cog is not None:
            await video_cog.video_stop(interaction)
        else:
            await interaction.response.send_message("⏹️ Stopped.", ephemeral=False)

    async def _pause_audio(
        self, interaction: discord.Interaction, session: ChannelSession
    ) -> None:
        """Toggle pause on audio playback via wavelink."""
        import player

        log.info(
            "Toggling pause (audio): guild=%d channel=%d",
            session.guild_id,
            session.channel_id,
        )
        p = player.get_player(session.guild_id)
        if p and p.connected:
            if p.paused:
                await p.pause(False)
                await interaction.response.send_message("▶️ Resumed.", ephemeral=False)
            else:
                await p.pause(True)
                await interaction.response.send_message("⏸️ Paused.", ephemeral=False)
        else:
            await interaction.response.send_message(
                "Nothing is playing right now.", ephemeral=True
            )

    async def _pause_video(
        self, interaction: discord.Interaction, session: ChannelSession
    ) -> None:
        """Toggle pause on video — not yet supported, acknowledge."""
        await interaction.response.send_message(
            "⏸️ Video pause/resume is controlled via the Activity player.", ephemeral=True
        )

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

    async def _get_session_or_error_or_player(
        self, interaction: discord.Interaction
    ) -> ChannelSession | str | None:
        """Resolve the user's session, or fall back to active wavelink player or video session.

        Returns:
        - A ChannelSession if found in the registry.
        - The string "audio_player" if no session but a wavelink player is active.
        - The string "video_session" if a video Activity session is active.
        - None if an error message was sent (user not in VC, nothing playing).
        """
        import player

        channel_id = self._resolve_user_channel(interaction)
        if channel_id is None:
            await interaction.response.send_message(
                "Join a voice channel first.", ephemeral=True
            )
            return None

        guild_id = interaction.guild_id  # type: ignore[union-attr]
        session = self._resolve_session(guild_id, channel_id)
        if session is not None:
            return session

        # No session in registry — check if there's an active wavelink player
        p = player.get_player(guild_id)
        if p and p.connected:
            return "audio_player"

        # Check if there's an active video session (Activity streaming)
        video_cog = interaction.client.get_cog("Video")  # type: ignore[union-attr]
        if video_cog is not None:
            streamer = video_cog._registry.get(guild_id, channel_id)
            if streamer is not None and streamer.is_active:
                return "video_session"

        await interaction.response.send_message(
            "Nothing is playing in your channel.", ephemeral=True
        )
        return None

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

    # ------------------------------------------------------------------
    # Direct player.py methods (bypass session registry)
    # ------------------------------------------------------------------

    async def _skip_audio_direct(self, interaction: discord.Interaction) -> None:
        """Skip audio via player.py — no session registry needed."""
        import player

        guild_id = interaction.guild_id  # type: ignore[union-attr]
        p = player.get_player(guild_id)
        if p and p.connected:
            await p.stop()  # triggers on_wavelink_track_end → plays next
            await interaction.response.send_message("⏭️ Skipped.", ephemeral=False)
        else:
            await interaction.response.send_message(
                "Nothing is playing right now.", ephemeral=True
            )

    async def _stop_audio_direct(self, interaction: discord.Interaction) -> None:
        """Stop audio and disconnect via player.py — no session registry needed."""
        import player

        guild_id = interaction.guild_id  # type: ignore[union-attr]
        p = player.get_player(guild_id)
        if p and p.connected:
            await p.disconnect()

        state = player.get_state(guild_id)
        state["current"] = None
        state["queue"] = []
        state["player"] = None
        player.persist(guild_id)

        await interaction.response.send_message("\u23f9\ufe0f Stopped.", ephemeral=False)

    async def _pause_audio_direct(self, interaction: discord.Interaction) -> None:
        """Toggle pause via player.py — no session registry needed."""
        import player

        guild_id = interaction.guild_id  # type: ignore[union-attr]
        p = player.get_player(guild_id)
        if p and p.connected:
            if p.paused:
                await p.pause(False)
                await interaction.response.send_message("▶️ Resumed.", ephemeral=False)
            else:
                await p.pause(True)
                await interaction.response.send_message("⏸️ Paused.", ephemeral=False)
        else:
            await interaction.response.send_message(
                "Nothing is playing right now.", ephemeral=True
            )

    async def _show_audio_queue_direct(self, interaction: discord.Interaction) -> None:
        """Show queue via player.py state — no session registry needed."""
        import player
        from cogs.music import QueuePaginatedView

        guild_id = interaction.guild_id  # type: ignore[union-attr]
        view = QueuePaginatedView(guild_id)
        await interaction.response.send_message(
            embed=view._embed(), view=view, ephemeral=False
        )

    async def _clear_audio_direct(self, interaction: discord.Interaction) -> None:
        """Clear the audio queue via player.py — no session registry needed."""
        import player

        guild_id = interaction.guild_id  # type: ignore[union-attr]
        state = player.get_state(guild_id)
        player.clear_queue(state)
        player.persist(guild_id)
        await interaction.response.send_message("🗑️ Queue cleared.", ephemeral=False)

    async def _skip_video_direct(self, interaction: discord.Interaction) -> None:
        """Skip video via VideoCog — no PlaybackRouter session registry needed."""
        video_cog = interaction.client.get_cog("Video")  # type: ignore[union-attr]
        if video_cog is not None:
            await video_cog.video_skip(interaction)
        else:
            await interaction.response.send_message("❌ Video system unavailable.", ephemeral=True)

    async def _stop_video_direct(self, interaction: discord.Interaction) -> None:
        """Stop video via VideoCog — no PlaybackRouter session registry needed."""
        video_cog = interaction.client.get_cog("Video")  # type: ignore[union-attr]
        if video_cog is not None:
            await video_cog.video_stop(interaction)
        else:
            await interaction.response.send_message("❌ Video system unavailable.", ephemeral=True)

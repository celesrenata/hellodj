"""HelloDJ — Unified Playback cog: single command surface for audio and video.

Registers the unified slash commands (/play, /upload, /skip, /stop, /pause,
/queue, /clear) and delegates all logic to the PlaybackRouter. The router
handles content classification, session resolution, backend dispatch, and
error responses.

Commands
--------
- ``/play <query> [type]``  — Play audio or video (auto-detected or forced).
- ``/upload <attachment>``  — Upload a file to play as video.
- ``/skip``   — Skip the current track/video in the user's channel session.
- ``/stop``   — Stop playback and tear down the session.
- ``/pause``  — Toggle pause on the active session.
- ``/queue``  — Display the queue for the active session.
- ``/clear``  — Clear the queue for the active session.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Literal

import discord
from discord import app_commands
from discord.ext import commands

from search import UnifiedSearchEngine
from search.formatter import ChoiceFormatter

if TYPE_CHECKING:
    from bot.playback.router import PlaybackRouter

log = logging.getLogger(__name__)

__all__ = ["PlaybackCog", "setup"]


class PlaybackCog(commands.Cog, name="Playback"):
    """Unified playback commands — audio and video through one interface."""

    def __init__(self, bot: commands.Bot, router: PlaybackRouter) -> None:
        self.bot = bot
        self.router = router
        self._search_engine = UnifiedSearchEngine()

    @app_commands.command(name="play", description="Play a song or video")
    @app_commands.describe(
        query="Song name, URL, or video link",
        mode="Force playback type (default: auto-detect)",
    )
    @app_commands.rename(mode="type")
    async def play(
        self,
        interaction: discord.Interaction,
        query: str,
        mode: Literal["auto", "audio", "video", "music_video", "album"] = "auto",
    ) -> None:
        """Play audio or video content — auto-detected or forced via mode."""
        # Attempt to decode autocomplete selection value
        lavalink_prefix, track_id = ChoiceFormatter.decode_value(query)

        if lavalink_prefix is not None:
            # Value was encoded from autocomplete — construct the proper query for the router
            if lavalink_prefix == "ytsearch":
                # YouTube: pass the full URL directly to router (it handles YouTube URLs natively)
                resolved_query = f"https://www.youtube.com/watch?v={track_id}"
            elif lavalink_prefix == "spsearch":
                # Spotify: pass as spotify track URL
                resolved_query = f"https://open.spotify.com/track/{track_id}"
            elif lavalink_prefix == "tdsearch":
                # Tidal: pass as tidal track URL
                resolved_query = f"https://tidal.com/track/{track_id}"
            else:
                # SoundCloud or unknown: fall through to router with raw query
                resolved_query = query

            await self.router.play(interaction, resolved_query, mode=mode)
            return

        # Fall through to router for raw text queries or failed decodes
        await self.router.play(interaction, query, mode=mode)

    @play.autocomplete("query")
    async def play_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        """Autocomplete handler for the /play command query parameter."""
        import asyncio

        try:
            results = await asyncio.wait_for(
                self._search_engine.search(
                    current,
                    guild_id=interaction.guild_id,
                ),
                timeout=2.5,
            )
            # URL detection: engine returns a single result with track_id as URL
            if len(results) == 1 and results[0].track_id.startswith("http"):
                url_result = results[0]
                name = f"🔗 {url_result.title}"[:100]
                value = url_result.track_id[:100]
                return [app_commands.Choice(name=name, value=value)]
            return ChoiceFormatter.format_choices(results)
        except asyncio.TimeoutError:
            log.warning("Autocomplete search timed out for query=%r", current[:30])
            return []
        except Exception:
            log.exception("Autocomplete search failed")
            return []

    @app_commands.command(name="upload", description="Upload a video or audio file to play")
    @app_commands.describe(attachment="File to play (audio or video)")
    async def upload(
        self,
        interaction: discord.Interaction,
        attachment: discord.Attachment,
    ) -> None:
        """Upload and play a file via the video Activity."""
        video_cog = self.bot.get_cog("Video")
        if video_cog is not None:
            await video_cog.video_play(interaction, query=None, attachment=attachment)
        else:
            await interaction.response.send_message(
                "❌ Video system not available for file uploads.", ephemeral=True
            )

    @app_commands.command(name="skip", description="Skip the current track or entire playlist")
    @app_commands.describe(mode="What to skip (default: current song)")
    @app_commands.choices(mode=[
        app_commands.Choice(name="Song", value="song"),
        app_commands.Choice(name="Playlist (clear remaining)", value="playlist"),
    ])
    async def skip(self, interaction: discord.Interaction, mode: str = "song") -> None:
        """Skip the current track, or skip the entire active playlist."""
        if mode == "playlist":
            import player
            state = player.get_state(interaction.guild_id)
            active_pl = state.get("active_playlist")
            if not active_pl:
                await interaction.response.send_message(
                    "No active playlist to skip.", ephemeral=True
                )
                return
            # Remove only tracks tagged with this playlist from the queue
            queue = state["queue"]
            remaining = [t for t in queue if t.get("_from_playlist", "").casefold() != active_pl.casefold()]
            removed_count = len(queue) - len(remaining)
            state["queue"] = remaining
            state["active_playlist"] = None
            # Skip the current track (which is from this playlist)
            player_obj = player.get_player(interaction.guild_id)
            if player_obj and player_obj.connected and player_obj.playing:
                await player_obj.stop()
            player.persist(interaction.guild_id)
            await interaction.response.send_message(
                f"⏭ Skipped playlist **{active_pl}** — removed {removed_count} track(s) from queue."
            )
            return
        await self.router.skip(interaction)

    @app_commands.command(name="stop", description="Stop playback")
    async def stop(self, interaction: discord.Interaction) -> None:
        """Stop playback and tear down the session."""
        await self.router.stop(interaction)

    @app_commands.command(name="pause", description="Pause or resume playback")
    async def pause(self, interaction: discord.Interaction) -> None:
        """Toggle pause on the active session."""
        await self.router.pause(interaction)

    @app_commands.command(name="queue", description="Show the playback queue")
    async def queue(self, interaction: discord.Interaction) -> None:
        """Display the queue for the active session."""
        await self.router.queue(interaction)

    @app_commands.command(name="clear", description="Clear the playback queue")
    async def clear(self, interaction: discord.Interaction) -> None:
        """Clear the queue for the active session."""
        await self.router.clear(interaction)


async def setup(bot: commands.Bot) -> None:
    """Load the Playback cog.

    Retrieves the PlaybackRouter from ``bot.playback_router`` if available
    (wired in Task 12). If not yet wired, creates a minimal placeholder
    router so the cog can still load and register commands.
    """
    router = getattr(bot, "playback_router", None)

    if router is None:
        # Placeholder: create a router with minimal dependencies.
        # This allows the cog to load and register slash commands before
        # the full wiring is done in Task 12.
        from playback.classifier import classify
        from playback.session_registry import SessionRegistry

        log.warning(
            "PlaybackCog: bot.playback_router not set — loading with placeholder router. "
            "Full wiring happens in Task 12."
        )

        # Lazy import to avoid circular deps at module level
        from playback.router import PlaybackRouter

        # Create a minimal router with stub dependencies
        registry = SessionRegistry()

        # Placeholder orchestrator (no-op for now)
        class _StubOrchestrator:
            available_count = 0

            async def assign_instance(self, guild_id: int, channel_id: int):
                return None

            async def release_instance(self, guild_id: int, channel_id: int):
                pass

            def get_instance_for_channel(self, guild_id: int, channel_id: int):
                return None

        router = PlaybackRouter(
            classifier=classify,
            registry=registry,
            orchestrator=_StubOrchestrator(),  # type: ignore[arg-type]
            activity_backend=None,
        )

    await bot.add_cog(PlaybackCog(bot, router))
    log.info("PlaybackCog loaded")

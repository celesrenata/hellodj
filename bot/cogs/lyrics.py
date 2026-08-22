"""HelloDJ — Lyrics cog: fetch and display song lyrics from Genius API."""

import logging
from typing import Literal

import discord
from discord import app_commands
from discord.ext import commands

import player
from video.genius_provider import GeniusProvider
from video.lyrics_service import get_lyrics_service

log = logging.getLogger(__name__)


class LyricsPaginatedView(discord.ui.View):
    """Paginated view for multi-page lyrics."""

    def __init__(self, pages: list[str]):
        super().__init__(timeout=120)
        self.pages = pages
        self.page = 0

    def _embed(self) -> discord.Embed:
        current = self.pages[self.page]
        embed = discord.Embed(
            title="📝 HelloDJ Lyrics",
            description=current[:2000],
            colour=discord.Colour.blurple(),
        )
        embed.set_footer(text=f"Page {self.page + 1}/{len(self.pages)}")
        return embed

    @discord.ui.button(label="◀ Prev", style=discord.ButtonStyle.secondary, custom_id="l_prev")
    async def prev_page(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if self.page > 0:
            self.page -= 1
        await interaction.response.edit_message(embed=self._embed(), view=self)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary, custom_id="l_next")
    async def next_page(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if self.page < len(self.pages) - 1:
            self.page += 1
        await interaction.response.edit_message(embed=self._embed(), view=self)


class Lyrics(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        from config import cfg
        self.client_id = cfg("genius.client_id", "")
        self.client_secret = cfg("genius.client_secret", "")
        self.access_token = cfg("genius.access_token", "")
        self.api_key = cfg("genius.api_key", "")
        self._genius = GeniusProvider(self.access_token or self.api_key)

    async def _ensure_activity(self, interaction: discord.Interaction) -> str | None:
        """Ensure a Discord Activity is running for the user's voice channel.

        Returns the Activity invite URL, or None if launch failed.
        If an Activity is already active, returns the existing URL.
        """
        guild_id = interaction.guild_id
        voice_channel = interaction.user.voice.channel

        video_cog = self.bot.get_cog("Video")
        if video_cog is None:
            return None

        # Check if Activity already exists for this guild
        for key, url in video_cog._activity_urls.items():
            if key[0] == guild_id:
                return url

        # No Activity running — launch one
        if video_cog._launcher is None:
            return None

        try:
            application_id = self.bot.user.id
            invite_data = await video_cog._launcher.launch(voice_channel.id, application_id)
            invite_code = invite_data.get("code", "")
            activity_url = f"https://discord.gg/{invite_code}" if invite_code else None
            if activity_url:
                video_cog._activity_urls[(guild_id, voice_channel.id)] = activity_url
            return activity_url
        except Exception as exc:
            log.warning("Failed to launch Activity for lyrics overlay: %s", exc)
            return None

    @app_commands.command(name="lyrics", description="Fetch lyrics for the current song")
    @app_commands.describe(overlay="Toggle lyrics overlay for all Activity viewers")
    async def lyrics(
        self,
        interaction: discord.Interaction,
        overlay: Literal["on", "off"] | None = None,
    ):
        state = player.get_state(interaction.guild.id)
        current = state.get("current")

        if overlay is not None:
            # Broadcast overlay control
            if overlay == "on":
                if not current:
                    await interaction.response.send_message(
                        "Nothing is playing right now.", ephemeral=True
                    )
                    return

                if not interaction.user.voice:
                    await interaction.response.send_message(
                        "You must join a voice channel first.", ephemeral=True
                    )
                    return

                await interaction.response.defer(ephemeral=True)

                # Ensure an Activity is running
                activity_url = await self._ensure_activity(interaction)

                # Enable overlay + trigger lyrics fetch
                lyrics_svc = get_lyrics_service(interaction.guild.id)
                lyrics_svc.enabled = True

                # Pull metadata from current track entry
                artist = current.get("author", "")
                title = current.get("title", "")
                duration_ms = current.get("duration", 0)

                await lyrics_svc.fetch_and_broadcast(artist, title, duration_ms)

                # Broadcast enable to all Activity viewers
                await lyrics_svc._ws_hub.broadcast_from_bot(
                    interaction.guild.id, {"type": "lyrics_overlay_enable"}
                )

                # Build response with Activity link
                msg = "🎤 Lyrics overlay enabled for all viewers."
                if activity_url:
                    install_url = "https://discord.com/oauth2/authorize?client_id=1534778518137995325"
                    msg += f"\n[Join Activity]({activity_url}) • [Install Activity]({install_url})"
                await interaction.followup.send(msg, ephemeral=True)

            else:  # overlay == "off"
                lyrics_svc = get_lyrics_service(interaction.guild.id)
                lyrics_svc.enabled = False

                # Broadcast disable to all Activity viewers
                await lyrics_svc._ws_hub.broadcast_from_bot(
                    interaction.guild.id, {"type": "lyrics_overlay_disable"}
                )
                await interaction.response.send_message(
                    "Lyrics overlay disabled.", ephemeral=True
                )
            return

        # Default behavior: embed lyrics in chat (unchanged)
        if not current:
            await interaction.response.send_message("Nothing is playing right now.")
            return

        song_title = current.get("title", "")
        artist = current.get("author", "")

        if not self.access_token and not self.api_key:
            await interaction.response.send_message(
                "Genius API not configured. Set `GENIUS_CLIENT_ID`, `GENIUS_CLIENT_SECRET`, "
                "and `GENIUS_ACCESS_TOKEN` in `.env` (or the legacy `GENIUS_API_KEY`).",
                ephemeral=True,
            )
            return

        await interaction.response.defer()

        try:
            lyrics_text = await self._genius.fetch(song_title, artist)
            if not lyrics_text:
                await interaction.followup.send(
                    f"No lyrics found for **{song_title}** by {artist}."
                )
                return

            # Split into pages of max 2000 chars
            pages = []
            remaining = lyrics_text
            while remaining:
                pages.append(remaining[:2000])
                remaining = remaining[2000:]

            if len(pages) == 1:
                embed = discord.Embed(
                    title=f"📝 {song_title}",
                    description=lyrics_text[:2000],
                    colour=discord.Colour.blurple(),
                )
                await interaction.followup.send(embed=embed)
            else:
                view = LyricsPaginatedView(pages)
                await interaction.followup.send(embed=view._embed(), view=view)

        except Exception as exc:
            log.error("HelloDJ lyrics fetch failed: %s", exc)
            await interaction.followup.send("Could not fetch lyrics.")


async def setup(bot: commands.Bot):
    await bot.add_cog(Lyrics(bot))

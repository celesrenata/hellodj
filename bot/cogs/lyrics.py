"""HelloDJ — Lyrics cog: fetch and display song lyrics from Genius API."""

import logging
import os

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

import player

log = logging.getLogger(__name__)

GENIUS_API_URL = "https://api.genius.com"


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

    @app_commands.command(name="lyrics", description="Fetch lyrics for the current song")
    async def lyrics(self, interaction: discord.Interaction):
        state = player.get_state(interaction.guild.id)
        current = state.get("current")
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
            lyrics_text = await self._fetch_lyrics(song_title, artist)
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

    async def _fetch_lyrics(self, title: str, artist: str) -> str | None:
        """Search Genius API for a song and return its lyrics text."""
        # The access token is used as the Bearer token for API calls. Fall back to the
        # legacy GENIUS_API_KEY if GENIUS_ACCESS_TOKEN is empty, for backward compatibility.
        bearer_token = self.access_token or self.api_key
        headers = {"Authorization": f"Bearer {bearer_token}"}
        params = {"q": f"{title} {artist}"}

        async with aiohttp.ClientSession() as session:
            # Search for the song
            async with session.get(
                f"{GENIUS_API_URL}/search",
                headers=headers,
                params=params,
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()

            hits = data.get("response", {}).get("hits", [])
            if not hits:
                return None

            # Take the first hit
            song = hits[0]["result"]
            song_url = song.get("url")
            if not song_url:
                return None

            # Fetch the lyrics page and extract lyrics
            async with session.get(song_url) as page_resp:
                if page_resp.status != 200:
                    return None
                html = await page_resp.text()

            # Simple extraction: look for lyrics in HTML
            # Genius doesn't provide a plain-text API, so we scrape
            import re
            # Find content between the lyrics container tags
            match = re.search(
                r'<div class="lyrics">(.*?)</div>',
                html,
                re.DOTALL,
            )
            if not match:
                # Alternative: look for the lyrics in the page's metadata
                return self._extract_from_html(html)

            # Clean HTML tags
            text = re.sub(r"<[^>]+>", "", match.group(1))
            text = text.replace("\\n", "\n").replace("<br>", "\n").strip()
            # Decode HTML entities
            text = text.replace("&", "&").replace("<", "<").replace(">", ">")
            return text

    def _extract_from_html(self, html: str) -> str | None:
        """Fallback extraction from raw HTML."""
        import re
        # Look for the lyrics in script tags or raw content
        # This is a best-effort fallback
        match = re.search(
            r'data-lyrics="(.*?)"',
            html,
            re.DOTALL,
        )
        if match:
            text = match.group(1)
            text = text.replace("\\n", "\n").replace("<br>", "\n").strip()
            return text
        return None


async def setup(bot: commands.Bot):
    await bot.add_cog(Lyrics(bot))

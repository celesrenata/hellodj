"""HelloDJ — Info cog: /info dashboard showing current playback state."""

import logging
import time

import discord
from discord import app_commands
from discord.ext import commands

import player

log = logging.getLogger(__name__)


class Info(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="info", description="Show HelloDJ playback information")
    async def info_cmd(self, interaction: discord.Interaction):
        state = player.get_state(interaction.guild.id)
        player_obj = player.get_player(interaction.guild.id)

        embed = discord.Embed(
            title="🎵 HelloDJ — Playback Information",
            colour=discord.Colour.blurple(),
        )

        # Current track
        current = state.get("current")
        if current:
            embed.add_field(
                name="Now Playing",
                value=f"**{current.get('title', 'Unknown')}**\n{current.get('author', 'Unknown Artist')}",
                inline=False,
            )

        # Player state — wavelink 3.5 uses properties, not methods
        if player_obj:
            if player_obj.playing:
                embed.add_field(name="Status", value="▶️ Playing", inline=True)
            elif player_obj.paused:
                embed.add_field(name="Status", value="⏸️ Paused", inline=True)
            else:
                embed.add_field(name="Status", value="⏹️ Stopped", inline=True)

            # Ping / latency
            latency = round(self.bot.latency * 1000)  # ms
            embed.add_field(name="Latency", value=f"{latency}ms", inline=True)

        # Queue
        queue_len = len(state["queue"])
        embed.add_field(name="Queue", value=f"{queue_len} track(s)", inline=True)

        # Repeat mode
        repeat = state.get("repeat_mode", "off")
        embed.add_field(name="Repeat", value=f"**{repeat}**", inline=True)

        # Autoplay
        autoplay = state.get("autoplay_enabled", False)
        embed.add_field(name="Autoplay", value="ON" if autoplay else "OFF", inline=True)

        # Genres
        genres = state.get("autoplay_genres", [])
        if genres:
            embed.add_field(
                name="Genres",
                value=", ".join(genres),
                inline=False,
            )

        # Source provider
        provider = state.get("source_provider", "youtube")
        embed.add_field(name="Source", value=f"**{provider}**", inline=True)

        # Next songs
        queue = state["queue"]
        if queue:
            next_songs = "\n".join(
                f"{i + 1}. **{t.get('title', 'Unknown')}**"
                for i, t in enumerate(queue[:3])
            )
            if len(queue) > 3:
                next_songs += f"\n…and {len(queue) - 3} more"
            embed.add_field(
                name="Up Next",
                value=next_songs,
                inline=False,
            )

        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(Info(bot))

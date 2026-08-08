"""HelloDJ — Filters cog: audio effects via Lavalink's built-in EQ/filter API.

Uses wavelink 3.5's Filters API directly (no dismusic dependency).
"""

import logging

import discord
from discord import app_commands
from discord.ext import commands
import wavelink

import player

log = logging.getLogger(__name__)


class Filters(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    filter_group = app_commands.Group(name="filter", description="Apply audio filters to HelloDJ playback")

    # ── Bassboost ───────────────────────────────────────────

    @filter_group.command(name="bassboost", description="Boost low-end frequencies")
    @app_commands.choices(level=[
        app_commands.Choice(name="Low", value="low"),
        app_commands.Choice(name="Moderate", value="moderate"),
        app_commands.Choice(name="Strong", value="strong"),
    ])
    async def bassboost(self, interaction: discord.Interaction, level: str = "moderate"):
        player_obj = player.get_player(interaction.guild.id)
        if not player_obj:
            await interaction.response.send_message("HelloDJ is not connected to voice.")
            return

        # Lavalink EQ: boost 60-200Hz bands (15 bands total, 0-14)
        eq_levels = {
            "low": [0.0, 0.05, 0.1, 0.05, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
            "moderate": [0.0, 0.1, 0.15, 0.1, 0.05, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
            "strong": [0.0, 0.15, 0.25, 0.15, 0.1, 0.05, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
        }

        gains = eq_levels.get(level, eq_levels["moderate"])
        bands = [(i, g) for i, g in enumerate(gains)]

        # Build filters: set equalizer, reset others
        filters = player_obj.filters
        filters.equalizer.set(bands=bands)
        filters.timescale.reset()
        filters.rotation.reset()
        await player_obj.set_filters(filters)

        state = player.get_state(interaction.guild.id)
        state["filters"]["bassboost"] = {"level": level, "gains": gains}
        player.persist(interaction.guild.id)

        await interaction.response.send_message(f"HelloDJ bassboost **{level}** applied.")

    # ── Nightcore ───────────────────────────────────────────

    @filter_group.command(name="nightcore", description="Speed up tempo and shift pitch upward")
    async def nightcore(self, interaction: discord.Interaction):
        player_obj = player.get_player(interaction.guild.id)
        if not player_obj:
            await interaction.response.send_message("HelloDJ is not connected to voice.")
            return

        # Lavalink timescale filter: speed 1.25x, pitch shift
        filters = player_obj.filters
        filters.timescale.set(speed=1.25, pitch=1.25, rate=1.0)
        filters.equalizer.reset()
        filters.rotation.reset()
        await player_obj.set_filters(filters)

        state = player.get_state(interaction.guild.id)
        state["filters"]["nightcore"] = {"speed": 1.25, "pitch": 1.25}
        player.persist(interaction.guild.id)

        await interaction.response.send_message("HelloDJ nightcore filter applied.")

    # ── 8D ──────────────────────────────────────────────────

    @filter_group.command(name="8d", description="Apply spatial panning (left/right oscillation)")
    async def eightd(self, interaction: discord.Interaction):
        player_obj = player.get_player(interaction.guild.id)
        if not player_obj:
            await interaction.response.send_message("HelloDJ is not connected to voice.")
            return

        # Lavalink rotation filter: oscillate pan at 0.2 Hz
        filters = player_obj.filters
        filters.rotation.set(rotation_hz=0.2)
        filters.equalizer.reset()
        filters.timescale.reset()
        await player_obj.set_filters(filters)

        state = player.get_state(interaction.guild.id)
        state["filters"]["8d"] = {"rotation": 0.2}
        player.persist(interaction.guild.id)

        await interaction.response.send_message("HelloDJ 8D filter applied.")

    # ── Equalizer ───────────────────────────────────────────

    @filter_group.command(name="equalizer", description="Fine-tune specific frequency bands")
    @app_commands.describe(
        band1="20Hz  (-1.0 to 1.0)",
        band2="60Hz  (-1.0 to 1.0)",
        band3="100Hz  (-1.0 to 1.0)",
        band4="140Hz  (-1.0 to 1.0)",
        band5="200Hz  (-1.0 to 1.0)",
        band6="400Hz  (-1.0 to 1.0)",
        band7="800Hz  (-1.0 to 1.0)",
        band8="1.6kHz  (-1.0 to 1.0)",
        band9="3.2kHz  (-1.0 to 1.0)",
        band10="6.4kHz  (-1.0 to 1.0)",
    )
    async def equalizer(
        self,
        interaction: discord.Interaction,
        band1: float = 0.0,
        band2: float = 0.0,
        band3: float = 0.0,
        band4: float = 0.0,
        band5: float = 0.0,
        band6: float = 0.0,
        band7: float = 0.0,
        band8: float = 0.0,
        band9: float = 0.0,
        band10: float = 0.0,
    ):
        player_obj = player.get_player(interaction.guild.id)
        if not player_obj:
            await interaction.response.send_message("HelloDJ is not connected to voice.")
            return

        # Lavalink has 15 bands (0-14). Map our 10 bands to Lavalink's bands.
        gains = [0.0] * 15
        user_gains = [band1, band2, band3, band4, band5, band6, band7, band8, band9, band10]
        for i, g in enumerate(user_gains):
            if i < 10:
                gains[i] = max(-1.0, min(1.0, g))

        bands = [(i, g) for i, g in enumerate(gains)]
        filters = player_obj.filters
        filters.equalizer.set(bands=bands)
        filters.timescale.reset()
        filters.rotation.reset()
        await player_obj.set_filters(filters)

        state = player.get_state(interaction.guild.id)
        state["filters"]["equalizer"] = {"gains": gains}
        player.persist(interaction.guild.id)

        await interaction.response.send_message("HelloDJ equalizer applied with custom band levels.")

    # ── Filter reset ────────────────────────────────────────

    @app_commands.command(name="filter_reset", description="Reset all audio filters to default")
    async def filter_reset(self, interaction: discord.Interaction):
        player_obj = player.get_player(interaction.guild.id)
        if not player_obj:
            await interaction.response.send_message("HelloDJ is not connected to voice.")
            return

        # Reset all filters using wavelink 3.5 API
        filters = player_obj.filters
        filters.reset()
        await player_obj.set_filters(filters)

        state = player.get_state(interaction.guild.id)
        state["filters"] = {}
        player.persist(interaction.guild.id)

        await interaction.response.send_message("HelloDJ all filters reset to default.")


async def setup(bot: commands.Bot):
    await bot.add_cog(Filters(bot))

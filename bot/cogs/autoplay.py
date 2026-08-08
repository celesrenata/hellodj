"""HelloDJ — Autoplay cog: auto-recommendation engine with genre management.

When the queue empties and autoplay is enabled, the bot automatically adds
recommended tracks based on genres or the current track's metadata.
If a user manually enqueues a song, autoplay pauses until that song finishes.
"""

import logging

import discord
from discord import app_commands
from discord.ext import commands

import player
import session

log = logging.getLogger(__name__)


class Autoplay(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ── Autoplay toggle ─────────────────────────────────────

    @app_commands.command(name="autoplay", description="Toggle automatic song recommendations")
    async def autoplay_toggle(self, interaction: discord.Interaction):
        state = player.get_state(interaction.guild.id)
        enabled = not state.get("autoplay_enabled", False)
        state["autoplay_enabled"] = enabled
        player.persist(interaction.guild.id)

        status = "ON" if enabled else "OFF"
        await interaction.response.send_message(f"HelloDJ autoplay: **{status}**")

    # ── Genre management group ──────────────────────────────

    genre_group = app_commands.Group(
        name="genre", description="Manage autoplay genres",
        parent=app_commands.Group(
            name="autoplay",
            description="HelloDJ autoplay commands",
        ),
    )

    @genre_group.command(name="add", description="Add a genre for autoplay recommendations")
    @app_commands.describe(genre="Genre name (e.g. pop, rock, electronic)")
    async def genre_add(self, interaction: discord.Interaction, genre: str):
        state = player.get_state(interaction.guild.id)
        genres = state.get("autoplay_genres", [])
        genre_lower = genre.lower().strip()

        if genre_lower not in genres:
            genres.append(genre_lower)
            state["autoplay_genres"] = genres
            player.persist(interaction.guild.id)

        await interaction.response.send_message(
            f"HelloDJ genre **{genre_lower}** added. Current genres: {', '.join(genres) or 'none'}",
            ephemeral=True,
        )

    @genre_group.command(name="remove", description="Remove a genre from autoplay")
    @app_commands.describe(genre="Genre to remove")
    async def genre_remove(self, interaction: discord.Interaction, genre: str):
        state = player.get_state(interaction.guild.id)
        genres = state.get("autoplay_genres", [])
        genre_lower = genre.lower().strip()

        if genre_lower in genres:
            genres.remove(genre_lower)
            state["autoplay_genres"] = genres
            player.persist(interaction.guild.id)

        await interaction.response.send_message(
            f"HelloDJ genre **{genre_lower}** removed. Current genres: {', '.join(genres) or 'none'}",
            ephemeral=True,
        )

    @genre_group.command(name="clear", description="Clear all autoplay genres")
    async def genre_clear(self, interaction: discord.Interaction):
        state = player.get_state(interaction.guild.id)
        state["autoplay_genres"] = []
        player.persist(interaction.guild.id)
        await interaction.response.send_message(
            "HelloDJ all genres cleared. Autoplay will use song-based recommendations.",
            ephemeral=True,
        )

    @genre_group.command(name="list", description="Show current autoplay genres")
    async def genre_list(self, interaction: discord.Interaction):
        state = player.get_state(interaction.guild.id)
        genres = state.get("autoplay_genres", [])
        if genres:
            await interaction.response.send_message(
                f"HelloDJ current genres: {', '.join(genres)}", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                "No genres set. HelloDJ will recommend based on the current song.", ephemeral=True
            )


async def setup(bot: commands.Bot):
    await bot.add_cog(Autoplay(bot))

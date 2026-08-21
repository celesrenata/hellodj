"""HelloDJ — Visualizer cog: the /visualizer slash command.

Provides the ``/visualizer`` command for configuring the per-guild
visualizer engine. Supports autocomplete for valid engine values and
persists the selection via guild_settings.
"""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

import guild_settings


class VisualizerCog(commands.Cog, name="Visualizer"):
    """Per-guild visualizer engine configuration."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # --- autocomplete ---

    async def _engine_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """Return matching engine choices from VALID_VISUALIZER_ENGINES."""
        current_lower = current.casefold()
        return [
            app_commands.Choice(name=engine, value=engine)
            for engine in sorted(guild_settings.VALID_VISUALIZER_ENGINES)
            if current_lower in engine.casefold()
        ][:25]

    # --- slash command ---

    @app_commands.command(name="visualizer", description="Set the visualizer engine for this server")
    @app_commands.describe(engine="Visualizer engine to use (e.g. dvd, projectm, off)")
    @app_commands.autocomplete(engine=_engine_autocomplete)
    async def visualizer(self, interaction: discord.Interaction, engine: str) -> None:
        """Set the visualizer engine for the current guild."""
        guild_id = interaction.guild_id

        try:
            guild_settings.set_visualizer_engine(guild_id, engine)
        except ValueError:
            valid = ", ".join(sorted(guild_settings.VALID_VISUALIZER_ENGINES))
            await interaction.response.send_message(
                f"❌ Invalid engine `{engine}`. Valid options: {valid}",
                ephemeral=True,
            )
            return

        # Build confirmation embed
        embed = discord.Embed(
            title="🎨 Visualizer Updated",
            color=discord.Color.blurple(),
        )
        embed.add_field(name="Engine", value=f"`{engine}`", inline=True)

        if engine == "off":
            embed.description = "The visualizer has been disabled. It will stop once the VisualizerManager is active."
        else:
            embed.description = f"Visualizer engine set to **{engine}** for this server."

        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(VisualizerCog(bot))

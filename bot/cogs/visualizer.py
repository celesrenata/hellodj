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
            embed.description = "The visualizer has been disabled."
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        embed.description = f"Visualizer engine set to **{engine}** for this server."

        # Launch Activity if not already running and user is in voice
        activity_url = None
        if interaction.user.voice:
            activity_url = await self._ensure_activity(interaction)

        if activity_url:
            install_url = "https://discord.com/oauth2/authorize?client_id=1534778518137995325"
            embed.add_field(
                name="Activity",
                value=f"[Join Activity]({activity_url}) • [Install Activity]({install_url})",
                inline=False,
            )

        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def _ensure_activity(self, interaction: discord.Interaction) -> str | None:
        """Ensure a Discord Activity is running for the user's voice channel.

        Returns the Activity invite URL, or None if launch failed.
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
            import logging
            logging.getLogger(__name__).warning("Failed to launch Activity for visualizer: %s", exc)
            return None


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(VisualizerCog(bot))

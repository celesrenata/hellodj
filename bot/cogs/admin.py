"""Admin cog: system administration commands for the bot."""

import logging
import os
import sys

import discord
from discord import app_commands
from discord.ext import commands

import player
import session
import blacklist as _blacklist

log = logging.getLogger(__name__)

# Reference the shared blacklist dict
blacklist = _blacklist.blacklist


class Admin(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def _is_admin(self, interaction: discord.Interaction) -> bool:
        """Check if user has administrator permission or is the bot owner."""
        if self.bot.owner_id is not None and interaction.user.id == self.bot.owner_id:
            return True
        if interaction.guild:
            perms = interaction.user.guild_permissions
            return perms.administrator
        return False

    @app_commands.command(name="restart", description="Soft reboot the bot application")
    async def restart(self, interaction: discord.Interaction):
        if not self._is_admin(interaction):
            await interaction.response.send_message(
                "You need Administrator permission to use this command.", ephemeral=True
            )
            return

        await interaction.response.send_message("🔄 Restarting bot...")
        os._exit(42)

    @app_commands.command(name="kill", description="Safely shut down the bot process (Admin only)")
    async def kill(self, interaction: discord.Interaction):
        if not self._is_admin(interaction):
            await interaction.response.send_message(
                "You need Administrator permission to use this command.", ephemeral=True
            )
            return

        await interaction.response.send_message("🛑 Shutting down...")
        sys.exit(0)

    @app_commands.command(name="revoke", description="Prevent a user from using the bot")
    @app_commands.describe(user="User to revoke access from")
    async def revoke(self, interaction: discord.Interaction, user: discord.User):
        if not self._is_admin(interaction):
            await interaction.response.send_message(
                "You need Administrator permission to use this command.", ephemeral=True
            )
            return

        gid = interaction.guild.id
        if gid not in blacklist:
            blacklist[gid] = []

        if user.id not in blacklist[gid]:
            blacklist[gid].append(user.id)

        await interaction.response.send_message(
            f"Revoked **{user.name}** from using the bot.", ephemeral=True
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(Admin(bot))

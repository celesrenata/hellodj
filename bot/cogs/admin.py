"""HelloDJ — Admin cog: system administration commands for the bot."""

import logging
import os
import sys

import discord
from discord import app_commands
from discord.ext import commands

import player
import session
import blacklist as _blacklist
import allowlist as _allowlist
import guild_settings as _guild_settings
import oauth_store

log = logging.getLogger(__name__)

# Reference the shared dicts
blacklist = _blacklist.blacklist
allowlist = _allowlist.allowlist


class Admin(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def _is_admin(self, interaction: discord.Interaction) -> bool:
        """Check if user has administrator permission or is a bot/oauth owner."""
        if self.bot.owner_id is not None and interaction.user.id == self.bot.owner_id:
            return True
        # OAuth-bound owner/admin (from data/oauth.json, written by web-ui)
        if oauth_store.is_bound_admin(interaction.user.id):
            return True
        if interaction.guild:
            perms = interaction.user.guild_permissions
            return perms.administrator
        return False

    @app_commands.command(name="restart", description="Soft reboot HelloDJ")
    async def restart(self, interaction: discord.Interaction):
        if not self._is_admin(interaction):
            await interaction.response.send_message(
                "You need Administrator permission to use this command.", ephemeral=True
            )
            return

        await interaction.response.send_message("🔄 HelloDJ restarting...")
        os._exit(42)

    @app_commands.command(name="kill", description="Safely shut down HelloDJ (Admin only)")
    async def kill(self, interaction: discord.Interaction):
        if not self._is_admin(interaction):
            await interaction.response.send_message(
                "You need Administrator permission to use this command.", ephemeral=True
            )
            return

        await interaction.response.send_message("🛑 HelloDJ shutting down...")
        sys.exit(0)

    @app_commands.command(name="revoke", description="Prevent a user from using HelloDJ")
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
            _blacklist.save()

        await interaction.response.send_message(
            f"Revoked **{user.name}** from using HelloDJ.", ephemeral=True
        )

    @app_commands.command(
        name="blacklist",
        description="Reload the blacklist from data/blacklist.json (Admin only)",
    )
    async def blacklist_reload(self, interaction: discord.Interaction):
        """Reload the shared blacklist from data/blacklist.json (written by the
        web UI) so web-UI blacklist edits take effect on the running bot."""
        if not self._is_admin(interaction):
            await interaction.response.send_message(
                "You need Administrator permission to use this command.", ephemeral=True
            )
            return

        _blacklist.reload()
        await interaction.response.send_message(
            "🔄 Blacklist reloaded from data/blacklist.json.", ephemeral=True
        )

    # ── /restrict command ────────────────────────────────────

    @app_commands.command(
        name="restrict",
        description="Restrict a user: add to disallow list OR remove from allow list",
    )
    @app_commands.describe(
        user="User to restrict",
        action="Action to perform: add_to_disallow (blacklist) or remove_from_allow",
    )
    @app_commands.choices(action=[
        app_commands.Choice(name="Add to disallow list (blacklist)", value="add_to_disallow"),
        app_commands.Choice(name="Remove from allow list", value="remove_from_allow"),
    ])
    async def restrict(
        self,
        interaction: discord.Interaction,
        user: discord.User,
        action: str,
    ):
        if not self._is_admin(interaction):
            await interaction.response.send_message(
                "You need Administrator permission to use this command.", ephemeral=True
            )
            return

        gid = interaction.guild.id
        uid = user.id

        if action == "add_to_disallow":
            # Add user to the guild's blacklist (disallow list)
            if gid not in blacklist:
                blacklist[gid] = []
            if uid not in blacklist[gid]:
                blacklist[gid].append(uid)
            _blacklist.save()
            await interaction.response.send_message(
                f"Added **{user.name}** to the disallow list (blacklist).", ephemeral=True
            )
        elif action == "remove_from_allow":
            # Remove user from the allow list
            _allowlist.remove(gid, uid)
            await interaction.response.send_message(
                f"Removed **{user.name}** from the allow list.", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                "Unknown action. Use add_to_disallow or remove_from_allow.", ephemeral=True
            )

    # ── /allow command ───────────────────────────────────────

    @app_commands.command(
        name="allow",
        description="Allow a user: remove from disallow list OR add to allow list",
    )
    @app_commands.describe(
        user="User to allow",
        action="Action to perform: remove_from_disallow or add_to_allow",
    )
    @app_commands.choices(action=[
        app_commands.Choice(name="Remove from disallow list (blacklist)", value="remove_from_disallow"),
        app_commands.Choice(name="Add to allow list", value="add_to_allow"),
    ])
    async def allow(
        self,
        interaction: discord.Interaction,
        user: discord.User,
        action: str,
    ):
        if not self._is_admin(interaction):
            await interaction.response.send_message(
                "You need Administrator permission to use this command.", ephemeral=True
            )
            return

        gid = interaction.guild.id
        uid = user.id

        if action == "remove_from_disallow":
            # Remove user from the guild's blacklist (disallow list)
            ids = blacklist.get(gid)
            if ids and uid in ids:
                ids.remove(uid)
                _blacklist.save()
            await interaction.response.send_message(
                f"Removed **{user.name}** from the disallow list (blacklist).", ephemeral=True
            )
        elif action == "add_to_allow":
            # Add user to the allow list
            _allowlist.add(gid, uid)
            await interaction.response.send_message(
                f"Added **{user.name}** to the allow list.", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                "Unknown action. Use remove_from_disallow or add_to_allow.", ephemeral=True
            )

    # ── /restrict_mode command ───────────────────────────────
    # Intentional naming exception: kept as a single underscore-compound
    # top-level command. Converting to a space-group (/restrict mode) would
    # collide with the existing /restrict command (admin.py:104), so this
    # keeps the underscore name for a low-risk, non-breaking scheme.

    @app_commands.command(
        name="restrict_mode",
        description="Set the guild restriction mode: restrictive (default) or allow_all",
    )
    @app_commands.describe(
        mode="Restriction mode: restrictive (block blacklisted users) or allow_all (only allow listed users)"
    )
    @app_commands.choices(mode=[
        app_commands.Choice(name="Restrictive (block blacklisted users, default)", value="restrictive"),
        app_commands.Choice(name="Allow-all (only allow listed users)", value="allow_all"),
    ])
    async def restrict_mode(
        self,
        interaction: discord.Interaction,
        mode: str,
    ):
        if not self._is_admin(interaction):
            await interaction.response.send_message(
                "You need Administrator permission to use this command.", ephemeral=True
            )
            return

        gid = interaction.guild.id
        _guild_settings.set_guild_mode(gid, mode)

        mode_label = "Restrictive" if mode == "restrictive" else "Allow-all"
        description = (
            "Blacklisted users will be blocked. All other users can use the bot."
            if mode == "restrictive"
            else "Only allow-listed users can use the bot. Everyone else is blocked."
        )
        await interaction.response.send_message(
            f"✅ Guild mode set to **{mode_label}**.\n{description}",
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(Admin(bot))

"""HelloDJ — Unified /hellodj admin command group.

Registers the ``/hellodj`` application command group with subcommands for
bot health, guild settings, content filtering, user moderation, and instance
management.

Command tree:
    /hellodj ping                      — Bot latency + Lavalink + instance health
    /hellodj status                    — Active sessions in this guild
    /hellodj settings                  — Guild configuration display
    /hellodj block artist <name>       — Block an artist (Manage Guild)
    /hellodj block track <url>         — Block a track URL (Manage Guild)
    /hellodj block domain <pattern>    — Block a domain pattern (Manage Guild)
    /hellodj block keyword <word>      — Block a keyword (Manage Guild)
    /hellodj block list                — List all block rules (Manage Guild)
    /hellodj unblock <rule_id>         — Remove a block rule (Manage Guild)
    /hellodj ban <user>                — Ban user from playback (Manage Guild)
    /hellodj unban <user>              — Unban user (Manage Guild)
    /hellodj ban list                  — List banned users (Manage Guild)
    /hellodj instances                 — View instance assignments (Manage Guild)
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

if TYPE_CHECKING:
    from playback.content_filter import ContentFilter
    from playback.user_bans import UserBans

log = logging.getLogger(__name__)


# ── Top-level /hellodj group ────────────────────────────────────────────────────


class HelloDJGroup(app_commands.Group):
    """Parent group for /hellodj commands."""

    def __init__(self) -> None:
        super().__init__(name="hellodj", description="HelloDJ administration commands")


# ── Block subgroup (/hellodj block) ─────────────────────────────────────────────


class BlockGroup(app_commands.Group):
    """Subgroup for /hellodj block commands."""

    def __init__(self) -> None:
        super().__init__(
            name="block",
            description="Content filter commands",
            parent=None,  # Will be attached to parent in cog
        )


# ── The Cog ─────────────────────────────────────────────────────────────────────


class AdminPanel(commands.Cog, name="AdminPanel"):
    """Unified /hellodj admin panel cog."""

    hellodj = HelloDJGroup()
    block = app_commands.Group(
        name="block",
        description="Block content from being played in this guild",
        parent=hellodj,
    )

    def __init__(
        self,
        bot: commands.Bot,
        *,
        content_filter: ContentFilter | None = None,
        user_bans: UserBans | None = None,
    ) -> None:
        self.bot = bot
        self.content_filter = content_filter
        self.user_bans = user_bans

    # ── /hellodj ping ───────────────────────────────────────────────────────────

    @hellodj.command(name="ping", description="Check bot latency, Lavalink status, and instance health")
    async def ping(self, interaction: discord.Interaction) -> None:
        """Respond with bot latency, Lavalink connection status, and instance health."""
        ws_latency = round(self.bot.latency * 1000)

        # Lavalink status
        lavalink_status = "Disconnected"
        try:
            import wavelink

            nodes = wavelink.Pool.nodes
            if nodes:
                # wavelink.Pool.nodes is a dict of node_id -> Node
                connected_count = sum(
                    1 for n in nodes.values() if n.status == wavelink.NodeStatus.CONNECTED
                )
                total = len(nodes)
                if connected_count > 0:
                    lavalink_status = f"Connected ({connected_count}/{total} nodes)"
                else:
                    lavalink_status = f"Disconnected (0/{total} nodes)"
            else:
                lavalink_status = "No nodes configured"
        except Exception:
            lavalink_status = "Unknown (wavelink unavailable)"

        # Instance health
        instance_info = "N/A (orchestrator not loaded)"
        try:
            from playback.instance_config import get_instance_count, get_instance_credentials

            count = get_instance_count()
            if count > 0:
                configured = sum(
                    1 for i in range(count) if get_instance_credentials(i) is not None
                )
                instance_info = f"{configured}/{count} configured"
            else:
                instance_info = "Single instance (no secondaries)"
        except Exception:
            instance_info = "Unable to query"

        embed = discord.Embed(
            title="🏓 Pong!",
            color=discord.Color.green() if ws_latency < 200 else discord.Color.orange(),
        )
        embed.add_field(name="Bot Latency", value=f"{ws_latency}ms", inline=True)
        embed.add_field(name="Lavalink", value=lavalink_status, inline=True)
        embed.add_field(name="Instances", value=instance_info, inline=True)

        await interaction.response.send_message(embed=embed)

    # ── /hellodj status ─────────────────────────────────────────────────────────

    @hellodj.command(name="status", description="View active playback sessions in this guild")
    async def status(self, interaction: discord.Interaction) -> None:
        """Display active sessions in this guild."""
        if not interaction.guild:
            await interaction.response.send_message(
                "This command can only be used in a server.", ephemeral=True
            )
            return

        guild_id = interaction.guild.id

        # Try to get sessions from the session registry if available
        sessions_info: list[str] = []

        # Check existing player module's guild_state for audio sessions
        try:
            import player

            state = player.guild_state.get(guild_id)
            if state and state.get("voice_channel"):
                vc = state["voice_channel"]
                queue_len = len(state.get("queue", []))
                current = state.get("current")
                track_title = current.get("title", "Unknown") if current else "Nothing"
                sessions_info.append(
                    f"🎵 **{vc.name}** — Audio | Now: {track_title} | Queue: {queue_len}"
                )
        except Exception:
            pass

        # Check for video/activity sessions (future: SessionRegistry)
        # For now, just report what we can find
        try:
            from video.session_registry import get_all_sessions

            for session in get_all_sessions():
                if session.get("guild_id") == guild_id:
                    ch_id = session.get("channel_id")
                    channel = interaction.guild.get_channel(ch_id) if ch_id else None
                    ch_name = channel.name if channel else f"Channel {ch_id}"
                    sessions_info.append(f"🎬 **{ch_name}** — Video")
        except Exception:
            pass

        if not sessions_info:
            embed = discord.Embed(
                title="📊 Guild Status",
                description="No active playback sessions in this guild.",
                color=discord.Color.greyple(),
            )
        else:
            embed = discord.Embed(
                title="📊 Guild Status",
                description="\n".join(sessions_info),
                color=discord.Color.blue(),
            )

        embed.set_footer(text=f"Guild: {interaction.guild.name}")
        await interaction.response.send_message(embed=embed)

    # ── /hellodj settings ───────────────────────────────────────────────────────

    @hellodj.command(name="settings", description="Display guild configuration")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def settings(self, interaction: discord.Interaction) -> None:
        """Show current guild configuration as an embed."""
        if not interaction.guild:
            await interaction.response.send_message(
                "This command can only be used in a server.", ephemeral=True
            )
            return

        guild_id = interaction.guild.id

        # Gather settings from various sources
        source_provider = "youtube"
        repeat_mode = "off"

        try:
            import player

            state = player.guild_state.get(guild_id)
            if state:
                source_provider = state.get("source_provider", "youtube")
                repeat_mode = state.get("repeat_mode", "off")
        except Exception:
            pass

        # Legacy video toggle
        legacy_video = True
        try:
            from playback.instance_config import is_legacy_video_enabled

            legacy_video = is_legacy_video_enabled()
        except Exception:
            pass

        # Content filter count
        filter_count = 0
        if self.content_filter:
            filter_count = len(self.content_filter.list_rules(guild_id))

        # Guild restriction mode
        restriction_mode = "restrictive"
        try:
            import guild_settings as _gs

            restriction_mode = _gs.get_guild_mode(guild_id)
        except Exception:
            pass

        embed = discord.Embed(
            title="⚙️ Guild Settings",
            color=discord.Color.blue(),
        )
        embed.add_field(name="Source Provider", value=source_provider, inline=True)
        embed.add_field(name="Repeat Mode", value=repeat_mode, inline=True)
        embed.add_field(name="Restriction Mode", value=restriction_mode, inline=True)
        embed.add_field(
            name="Legacy Video Commands",
            value="Enabled" if legacy_video else "Disabled",
            inline=True,
        )
        embed.add_field(name="Content Filter Rules", value=str(filter_count), inline=True)
        embed.set_footer(text=f"Guild: {interaction.guild.name} ({guild_id})")

        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── /hellodj block artist ───────────────────────────────────────────────────

    @block.command(name="artist", description="Block an artist from being played")
    @app_commands.describe(name="Artist name to block (case-insensitive)")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def block_artist(self, interaction: discord.Interaction, name: str) -> None:
        """Block tracks by a specific artist."""
        if not self.content_filter:
            await interaction.response.send_message(
                "Content filter is not available.", ephemeral=True
            )
            return

        rule_id = await self.content_filter.add_rule(
            interaction.guild_id, "artist", name, interaction.user.id
        )
        await interaction.response.send_message(
            f"✅ Blocked artist **{name}**.\nRule ID: `{rule_id}`",
            ephemeral=True,
        )

    # ── /hellodj block track ────────────────────────────────────────────────────

    @block.command(name="track", description="Block a specific track URL")
    @app_commands.describe(url="Track URL to block")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def block_track(self, interaction: discord.Interaction, url: str) -> None:
        """Block a specific track by URL."""
        if not self.content_filter:
            await interaction.response.send_message(
                "Content filter is not available.", ephemeral=True
            )
            return

        rule_id = await self.content_filter.add_rule(
            interaction.guild_id, "track", url, interaction.user.id
        )
        await interaction.response.send_message(
            f"✅ Blocked track URL.\nRule ID: `{rule_id}`",
            ephemeral=True,
        )

    # ── /hellodj block domain ───────────────────────────────────────────────────

    @block.command(name="domain", description="Block a domain pattern (glob-style)")
    @app_commands.describe(pattern="Domain pattern to block (e.g., *.example.com)")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def block_domain(self, interaction: discord.Interaction, pattern: str) -> None:
        """Block content from a domain pattern."""
        if not self.content_filter:
            await interaction.response.send_message(
                "Content filter is not available.", ephemeral=True
            )
            return

        rule_id = await self.content_filter.add_rule(
            interaction.guild_id, "domain", pattern, interaction.user.id
        )
        await interaction.response.send_message(
            f"✅ Blocked domain pattern **{pattern}**.\nRule ID: `{rule_id}`",
            ephemeral=True,
        )

    # ── /hellodj block keyword ──────────────────────────────────────────────────

    @block.command(name="keyword", description="Block tracks containing a keyword in title")
    @app_commands.describe(word="Keyword to block (case-insensitive substring match)")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def block_keyword(self, interaction: discord.Interaction, word: str) -> None:
        """Block tracks whose title contains a keyword."""
        if not self.content_filter:
            await interaction.response.send_message(
                "Content filter is not available.", ephemeral=True
            )
            return

        rule_id = await self.content_filter.add_rule(
            interaction.guild_id, "keyword", word, interaction.user.id
        )
        await interaction.response.send_message(
            f"✅ Blocked keyword **{word}**.\nRule ID: `{rule_id}`",
            ephemeral=True,
        )

    # ── /hellodj block list ─────────────────────────────────────────────────────

    @block.command(name="list", description="List all content filter rules for this guild")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def block_list(self, interaction: discord.Interaction) -> None:
        """Display all active filter rules for this guild."""
        if not self.content_filter:
            await interaction.response.send_message(
                "Content filter is not available.", ephemeral=True
            )
            return

        rules = self.content_filter.list_rules(interaction.guild_id)

        if not rules:
            await interaction.response.send_message(
                "No content filter rules set for this guild.", ephemeral=True
            )
            return

        embed = discord.Embed(
            title="🚫 Content Filter Rules",
            color=discord.Color.red(),
        )

        # Group by type for readability
        type_emoji = {"artist": "🎤", "track": "🎵", "domain": "🌐", "keyword": "🔤"}

        lines: list[str] = []
        for rule in rules[:25]:  # Cap at 25 to fit embed limits
            emoji = type_emoji.get(rule["type"], "❓")
            value = rule["value"]
            if len(value) > 50:
                value = value[:47] + "..."
            lines.append(
                f"{emoji} **{rule['type']}**: {value}\n"
                f"   ID: `{rule['id']}` | By: <@{rule['added_by']}>"
            )

        embed.description = "\n".join(lines)

        if len(rules) > 25:
            embed.set_footer(text=f"Showing 25 of {len(rules)} rules")
        else:
            embed.set_footer(text=f"{len(rules)} rule(s) total")

        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── /hellodj unblock ────────────────────────────────────────────────────────

    @hellodj.command(name="unblock", description="Remove a content filter rule by its ID")
    @app_commands.describe(rule_id="The rule ID to remove (from /hellodj block list)")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def unblock(self, interaction: discord.Interaction, rule_id: str) -> None:
        """Remove a content filter rule."""
        if not self.content_filter:
            await interaction.response.send_message(
                "Content filter is not available.", ephemeral=True
            )
            return

        removed = await self.content_filter.remove_rule(interaction.guild_id, rule_id)

        if removed:
            await interaction.response.send_message(
                f"✅ Removed filter rule `{rule_id}`.", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                f"❌ No rule found with ID `{rule_id}` in this guild.", ephemeral=True
            )

    # ── /hellodj ban ────────────────────────────────────────────────────────────

    @hellodj.command(name="ban", description="Ban a user from using playback commands")
    @app_commands.describe(user="User to ban from the bot")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def ban(self, interaction: discord.Interaction, user: discord.User) -> None:
        """Ban a user from playback in this guild."""
        if not self.user_bans:
            await interaction.response.send_message(
                "User ban system is not available.", ephemeral=True
            )
            return

        newly_banned = await self.user_bans.ban_user(
            interaction.guild_id, user.id, interaction.user.id
        )

        if newly_banned:
            await interaction.response.send_message(
                f"✅ Banned **{user.display_name}** from using playback commands.",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                f"**{user.display_name}** is already banned.", ephemeral=True
            )

    # ── /hellodj unban ──────────────────────────────────────────────────────────

    @hellodj.command(name="unban", description="Unban a user from playback commands")
    @app_commands.describe(user="User to unban")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def unban(self, interaction: discord.Interaction, user: discord.User) -> None:
        """Restore a user's ability to use playback commands."""
        if not self.user_bans:
            await interaction.response.send_message(
                "User ban system is not available.", ephemeral=True
            )
            return

        removed = await self.user_bans.unban_user(interaction.guild_id, user.id)

        if removed:
            await interaction.response.send_message(
                f"✅ Unbanned **{user.display_name}**.", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                f"**{user.display_name}** is not banned.", ephemeral=True
            )

    # ── /hellodj ban list (handled as 'banlist' to avoid collision) ──────────────

    @hellodj.command(name="banlist", description="List all banned users in this guild")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def ban_list(self, interaction: discord.Interaction) -> None:
        """Display all banned users for this guild."""
        if not self.user_bans:
            await interaction.response.send_message(
                "User ban system is not available.", ephemeral=True
            )
            return

        bans = self.user_bans.list_bans(interaction.guild_id)

        if not bans:
            await interaction.response.send_message(
                "No users are banned in this guild.", ephemeral=True
            )
            return

        embed = discord.Embed(
            title="🔨 Banned Users",
            color=discord.Color.dark_red(),
        )

        lines: list[str] = []
        for entry in bans[:25]:  # Cap at 25 to fit embed limits
            user_id = entry["user_id"]
            banned_by = entry.get("banned_by", "Unknown")
            banned_at = entry.get("banned_at", "Unknown")
            # Truncate ISO timestamp to date
            if isinstance(banned_at, str) and "T" in banned_at:
                banned_at = banned_at.split("T")[0]
            lines.append(f"<@{user_id}> — banned by <@{banned_by}> on {banned_at}")

        embed.description = "\n".join(lines)

        if len(bans) > 25:
            embed.set_footer(text=f"Showing 25 of {len(bans)} bans")
        else:
            embed.set_footer(text=f"{len(bans)} ban(s) total")

        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── /hellodj instances ──────────────────────────────────────────────────────

    @hellodj.command(name="instances", description="View bot instance assignments")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def instances(self, interaction: discord.Interaction) -> None:
        """Show configured bot instances and their current assignments."""
        try:
            from playback.instance_config import (
                get_instance_count,
                get_instance_credentials,
            )
        except ImportError:
            await interaction.response.send_message(
                "Instance configuration module is not available.", ephemeral=True
            )
            return

        count = get_instance_count()

        if count == 0:
            embed = discord.Embed(
                title="🤖 Bot Instances",
                description="No secondary instances configured.\nOnly the primary bot instance is active.",
                color=discord.Color.greyple(),
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        embed = discord.Embed(
            title="🤖 Bot Instances",
            color=discord.Color.purple(),
        )

        # Primary instance
        embed.add_field(
            name="Primary Instance",
            value=f"**{self.bot.user.display_name if self.bot.user else 'HelloDJ'}**\nStatus: Online",
            inline=False,
        )

        # Secondary instances
        for i in range(count):
            cred = get_instance_credentials(i)
            if cred:
                name = cred.get("name", f"Instance #{i + 2}")
                # We can't know assignment status without the orchestrator
                # but we can show they're configured
                embed.add_field(
                    name=f"Instance {i + 1}",
                    value=f"**{name}**\nApp ID: `{cred.get('app_id', 'N/A')}`\nStatus: Configured",
                    inline=True,
                )
            else:
                embed.add_field(
                    name=f"Instance {i + 1}",
                    value="Not configured",
                    inline=True,
                )

        embed.set_footer(text=f"{count} secondary instance(s) configured")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── Error handlers ──────────────────────────────────────────────────────────

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        """Handle permission errors for the /hellodj group."""
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message(
                "You need **Manage Server** permission to use this command.",
                ephemeral=True,
            )
        else:
            log.error("AdminPanel command error: %s", error, exc_info=error)
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "An error occurred. Please try again later.", ephemeral=True
                )


# ── Extension setup ─────────────────────────────────────────────────────────────


async def setup(bot: commands.Bot) -> None:
    """Load the AdminPanel cog with content filter and user bans."""
    # Initialize content filter
    content_filter: ContentFilter | None = None
    try:
        from playback.content_filter import ContentFilter

        content_filter = ContentFilter()
    except Exception as exc:
        log.warning("AdminPanel: could not initialize ContentFilter (%s)", exc)

    # Initialize user bans
    user_bans: UserBans | None = None
    try:
        from playback.user_bans import UserBans

        user_bans = UserBans()
    except Exception as exc:
        log.warning("AdminPanel: could not initialize UserBans (%s)", exc)

    await bot.add_cog(AdminPanel(bot, content_filter=content_filter, user_bans=user_bans))

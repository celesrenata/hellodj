"""HelloDJ — Help cog: paginated /help listing every available slash command.

Commands are fetched dynamically from the bot's command tree for the current
guild, grouped into logical sections (Music / Filters / Utility), and split
into pages of at most 25 entries. A small paginated view with ⬅️/➡️ buttons
navigates the pages.
"""

import logging

import discord
from discord import app_commands
from discord.ext import commands

log = logging.getLogger(__name__)

MAX_PER_PAGE = 25


class HelpPageView(discord.ui.View):
    """Pagination view for the /help embed pages."""

    def __init__(self, pages: list[discord.Embed]):
        super().__init__(timeout=300)
        self.pages = pages
        self.page = 0

    async def _update_page(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(embed=self.pages[self.page], view=self)

    @discord.ui.button(label="⬅️", style=discord.ButtonStyle.secondary, custom_id="help_prev")
    async def prev(self, interaction: discord.Interaction, _button: discord.ui.Button):
        self.page = max(0, self.page - 1)
        await self._update_page(interaction)

    @discord.ui.button(label="➡️", style=discord.ButtonStyle.secondary, custom_id="help_next")
    async def next(self, interaction: discord.Interaction, _button: discord.ui.Button):
        self.page = min(len(self.pages) - 1, self.page + 1)
        await self._update_page(interaction)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # Any guild member may page through the command list.
        return True


class Help(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="help", description="List all HelloDJ slash commands (paginated)")
    async def help_cmd(self, interaction: discord.Interaction):
        """Fetch every slash command for the guild, group it logically, and show
        a paginated list capped at 25 entries per page."""
        commands = await self._collect_commands(interaction)
        if not commands:
            await interaction.response.send_message(
                "No slash commands are available in this guild yet.",
                ephemeral=True,
            )
            return

        lines = []
        for name, desc in commands:
            if desc is None:
                # Section header (e.g. "🎵 Music")
                lines.append(f"\n**{name}**")
            else:
                lines.append(f"**/{name}** — {desc}")
        pages = self._chunk(lines, MAX_PER_PAGE)
        embeds = [self._make_embed(pages, i) for i in range(len(pages))]

        view = HelpPageView(embeds)
        await interaction.response.send_message(embed=embeds[0], view=view)

    async def _collect_commands(self, interaction: discord.Interaction) -> list[tuple[str, str | None]]:
        """Return a grouped, ordered list of ``(name, description)`` pairs.

        Command names are prefixed with their parent group (e.g. a ``song``
        subcommand of the ``play`` group shows as ``play song``). Section
        headers are tuples with a ``None`` description.
        """
        tree = self.bot.tree
        guild = interaction.guild
        raw = []
        for cmd in tree.get_commands(guild=guild):
            if isinstance(cmd, app_commands.Group):
                for sub in cmd.commands:
                    name = f"{cmd.name} {sub.name}"
                    raw.append((name, sub.description or "No description"))
            else:
                raw.append((cmd.name, cmd.description or "No description"))

        # Deduplicate while preserving order.
        seen: set[str] = set()
        dedup = []
        for name, desc in raw:
            if name not in seen:
                seen.add(name)
                dedup.append((name, desc))

        return self._group(dedup)

    @staticmethod
    def _group(items: list[tuple[str, str]]) -> list[tuple[str, str | None]]:
        """Order commands into logical sections: Music, Filters, Utility.

        Grouping is best-effort by command name; anything unrecognized is
        placed under Utility so the list stays complete.
        """
        music_keys = {
            "play", "add", "remove", "delete", "move", "shuffle", "repeat",
            "pause", "resume", "skip", "stop", "np", "nowplaying", "link",
            "source", "remote", "join", "leave", "l", "disconnect", "fuckoff",
            "sleep", "crossfade", "continue", "playlist", "autoplay", "queue",
        }
        filter_keys = {"filter", "filter_reset", "tune"}
        utility_keys = {"help", "info", "ping", "metrics", "admin", "blacklist"}

        music, filters, utility = [], [], []
        for name, desc in items:
            root = name.split(" ", 1)[0]
            if root in music_keys:
                music.append((name, desc))
            elif root in filter_keys:
                filters.append((name, desc))
            else:
                utility.append((name, desc))

        out: list[tuple[str, str | None]] = []
        if music:
            out.append(("🎵 Music", None))
            out.extend(music)
        if filters:
            out.append(("🎚️ Filters", None))
            out.extend(filters)
        if utility:
            out.append(("🛠️ Utility", None))
            out.extend(utility)
        return out

    def _make_embed(self, pages: list[list[str]], idx: int) -> discord.Embed:
        embed = discord.Embed(
            title=f"🎵 HelloDJ Commands ({idx + 1}/{len(pages)})",
            colour=discord.Colour.blurple(),
        )
        body = "\n".join(pages[idx])
        embed.description = body
        embed.set_footer(text="Use /help for this list • ⬅️ / ➡️ to page")
        return embed

    @staticmethod
    def _chunk(lines: list[str], size: int) -> list[list[str]]:
        return [lines[i:i + size] for i in range(0, len(lines), size)]


async def setup(bot: commands.Bot):
    await bot.add_cog(Help(bot))

"""Enhanced /remote command — comprehensive playback control panel.

Provides a rich embed (current track, queue preview, volume, repeat/shuffle state)
with a persistent view containing transport controls, volume, shuffle, autoplay,
like, stop, and external links (Dashboard, top.gg upvote).

Persistent: timeout=None, fixed custom_ids prefixed with 'hellodj:remote:',
registered in setup_hook via bot.add_view(EnhancedRemoteView()).

Auto-updates on track change via the on_track_start callback chain.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

import player

if TYPE_CHECKING:
    pass

log = logging.getLogger(__name__)

# ── Dashboard URL ────────────────────────────────────────────────────────────
DASHBOARD_URL = "https://hellodj.celestium.life/player"
TOP_GG_URL_TEMPLATE = "https://top.gg/bot/{bot_id}/vote"


# ── Helper functions ─────────────────────────────────────────────────────────

def _progress_bar(position_ms: int, duration_ms: int, width: int = 12) -> str:
    """Build a text progress bar."""
    if duration_ms <= 0:
        return "▬" * width
    ratio = max(0.0, min(1.0, position_ms / duration_ms))
    filled = int(ratio * width)
    filled = max(0, min(filled, width - 1))
    bar = ["▬"] * width
    bar[filled] = "🔘"
    return "".join(bar)


def _fmt_duration_ms(ms: int) -> str:
    """Format milliseconds as M:SS or H:MM:SS."""
    if ms <= 0:
        return "0:00"
    total_secs = ms // 1000
    hours, remainder = divmod(total_secs, 3600)
    mins, secs = divmod(remainder, 60)
    if hours > 0:
        return f"{hours}:{mins:02d}:{secs:02d}"
    return f"{mins}:{secs:02d}"


def _get_position_ms(guild_id: int) -> int:
    """Get current playback position in ms from the wavelink player."""
    p = player.get_player(guild_id)
    if p is None:
        return 0
    return getattr(p, "position", 0) or 0


def _build_remote_embed(
    guild_id: int,
    user: discord.User | discord.Member | None = None,
) -> discord.Embed:
    """Build the enhanced /remote embed with track info, queue preview, and state."""
    state = player.get_state(guild_id)
    current = state.get("current")

    if not current:
        return _build_idle_embed(user)

    # Track info
    title = current.get("title") or "Unknown"
    author = current.get("author") or "Unknown Artist"
    duration = current.get("duration") or 0
    artwork = current.get("artwork_url")
    url = current.get("webpage_url") or current.get("url") or current.get("uri")

    # Position / progress
    position = _get_position_ms(guild_id)
    if duration > 0:
        progress = f"`{_progress_bar(position, duration)}`  {_fmt_duration_ms(position)} / {_fmt_duration_ms(duration)}"
    else:
        progress = "🔴 LIVE"

    # Build embed
    embed = discord.Embed(
        title=title,
        url=url if url else None,
        colour=discord.Colour.blurple(),
    )
    embed.add_field(name="Artist", value=author, inline=True)
    embed.add_field(name="Duration", value=_fmt_duration_ms(duration) if duration > 0 else "LIVE", inline=True)

    # Volume
    p = player.get_player(guild_id)
    volume_pct = int((getattr(p, "volume", 1.0) if p else 1.0) * 100)
    embed.add_field(name="Volume", value=f"{volume_pct}%", inline=True)

    # Repeat / Shuffle / Autoplay state
    repeat_mode = state.get("repeat_mode", "off")
    autoplay = state.get("autoplay_enabled", False)
    repeat_icon = {"off": "➡️", "one": "🔂", "all": "🔁"}.get(repeat_mode, "➡️")
    state_line = f"{repeat_icon} Repeat: {repeat_mode.capitalize()} • 🎲 AutoPlay: {'ON' if autoplay else 'OFF'}"
    embed.add_field(name="State", value=state_line, inline=False)

    # Progress bar
    embed.description = progress

    # Queue preview (next 5)
    queue = state.get("queue", [])
    if queue:
        preview_lines = []
        for i, entry in enumerate(queue[:5], 1):
            t = entry.get("title") or "Unknown"
            a = entry.get("author") or ""
            d = _fmt_duration_ms(entry.get("duration") or 0)
            line = f"`{i}.` **{t}**"
            if a:
                line += f" — {a}"
            line += f" [{d}]"
            preview_lines.append(line)
        if len(queue) > 5:
            preview_lines.append(f"*…and {len(queue) - 5} more*")
        embed.add_field(name="Up Next", value="\n".join(preview_lines), inline=False)

    # Artwork
    if artwork:
        embed.set_thumbnail(url=artwork)

    # Author (user who invoked)
    if user:
        embed.set_author(
            name=user.display_name,
            icon_url=user.display_avatar.url if user.display_avatar else None,
        )

    embed.set_footer(text="HelloDJ Enhanced Remote • Buttons persist across restarts")
    return embed


def _build_idle_embed(user: discord.User | discord.Member | None = None) -> discord.Embed:
    """Build the idle/not-playing embed."""
    embed = discord.Embed(
        title="🎵 HelloDJ — Not Playing",
        description=(
            "Nothing is playing right now.\n\n"
            "Use `/play` to start a track, or open the "
            f"[Dashboard]({DASHBOARD_URL}) to queue songs from the web."
        ),
        colour=discord.Colour.greyple(),
    )
    if user:
        embed.set_author(
            name=user.display_name,
            icon_url=user.display_avatar.url if user.display_avatar else None,
        )
    embed.set_footer(text="HelloDJ Enhanced Remote")
    return embed


# ── Persistent View ──────────────────────────────────────────────────────────

class EnhancedRemoteView(discord.ui.View):
    """Comprehensive remote control view with persistent buttons.

    Layout:
      Row 0: ⏮ Previous | ⏯ Pause/Resume | ⏭ Skip | 🔉 Vol- | 🔊 Vol+
      Row 1: 🔀 Shuffle | 🔄 AutoPlay | ❤️ Like | ⏹️ Stop | 🌐 Dashboard (link)
      Row 2: ⬆️ Upvote on top.gg (link)
    """

    def __init__(self) -> None:
        super().__init__(timeout=None)

    # ── Row 0: Transport + Volume ────────────────────────────

    @discord.ui.button(
        label="⏮", style=discord.ButtonStyle.secondary,
        custom_id="hellodj:remote:prev", row=0,
    )
    async def prev_track(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        guild_id = interaction.guild_id
        if not guild_id:
            return
        from playback.unified_controls import unified_previous
        await unified_previous(guild_id)
        await self._update_embed(interaction)

    @discord.ui.button(
        label="⏯", style=discord.ButtonStyle.primary,
        custom_id="hellodj:remote:pause", row=0,
    )
    async def pause_resume(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        guild_id = interaction.guild_id
        if not guild_id:
            return
        p = player.get_player(guild_id)
        if p:
            if p.paused:
                await p.pause(False)
            elif p.playing:
                await p.pause(True)
        await self._update_embed(interaction)

    @discord.ui.button(
        label="⏭", style=discord.ButtonStyle.secondary,
        custom_id="hellodj:remote:skip", row=0,
    )
    async def skip_track(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        guild_id = interaction.guild_id
        if not guild_id:
            return
        from playback.unified_controls import unified_skip
        await unified_skip(guild_id)
        # Small delay to let the track change propagate
        await asyncio.sleep(0.3)
        await self._update_embed(interaction)

    @discord.ui.button(
        label="🔉", style=discord.ButtonStyle.secondary,
        custom_id="hellodj:remote:vol_down", row=0,
    )
    async def volume_down(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        guild_id = interaction.guild_id
        if not guild_id:
            return
        p = player.get_player(guild_id)
        if p:
            new_vol = max(0.0, (getattr(p, "volume", 1.0) or 1.0) - 0.10)
            await p.set_volume(new_vol)
        await self._update_embed(interaction)

    @discord.ui.button(
        label="🔊", style=discord.ButtonStyle.secondary,
        custom_id="hellodj:remote:vol_up", row=0,
    )
    async def volume_up(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        guild_id = interaction.guild_id
        if not guild_id:
            return
        p = player.get_player(guild_id)
        if p:
            new_vol = min(1.0, (getattr(p, "volume", 1.0) or 1.0) + 0.10)
            await p.set_volume(new_vol)
        await self._update_embed(interaction)

    # ── Row 1: Shuffle, AutoPlay, Like, Stop, Dashboard ──────

    @discord.ui.button(
        label="🔀 Shuffle", style=discord.ButtonStyle.secondary,
        custom_id="hellodj:remote:shuffle", row=1,
    )
    async def shuffle_toggle(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        guild_id = interaction.guild_id
        if not guild_id:
            return
        import random
        state = player.get_state(guild_id)
        queue = state.get("queue", [])
        if queue:
            random.shuffle(queue)
            player.persist(guild_id)
        await self._update_embed(interaction)

    @discord.ui.button(
        label="🔄 AutoPlay", style=discord.ButtonStyle.secondary,
        custom_id="hellodj:remote:autoplay", row=1,
    )
    async def autoplay_toggle(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        guild_id = interaction.guild_id
        if not guild_id:
            return
        state = player.get_state(guild_id)
        enabled = not state.get("autoplay_enabled", False)
        state["autoplay_enabled"] = enabled
        player.persist(guild_id)
        await self._update_embed(interaction)

    @discord.ui.button(
        label="❤️ Like", style=discord.ButtonStyle.success,
        custom_id="hellodj:remote:like", row=1,
    )
    async def like_track(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        guild_id = interaction.guild_id
        if not guild_id:
            return
        state = player.get_state(guild_id)
        current = state.get("current")
        if not current:
            await interaction.response.send_message(
                "Nothing is playing to add to a playlist.", ephemeral=True
            )
            return

        # Add to "Liked Songs" playlist (auto-create if needed via add_track)
        import storage
        liked_name = "Liked Songs"
        title = current.get("title", "current track")
        try:
            await storage.add_track(guild_id, liked_name, current)
        except Exception as exc:
            log.warning("Like button failed for guild %d: %s", guild_id, exc)
            await interaction.response.send_message(
                f"Could not add to playlist: {exc}", ephemeral=True
            )
            return
        await interaction.response.send_message(
            f"❤️ Added **{title}** to **{liked_name}**.", ephemeral=True
        )

    @discord.ui.button(
        label="⏹️ Stop", style=discord.ButtonStyle.danger,
        custom_id="hellodj:remote:stop", row=1,
    )
    async def stop_playback(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        guild_id = interaction.guild_id
        if not guild_id:
            return
        p = player.get_player(guild_id)
        if p:
            state = player.get_state(guild_id)
            state["queue"] = []
            state["current"] = None
            await p.stop()
            player.persist(guild_id)
        await self._update_embed(interaction)

    # ── Row 2: Link buttons (Dashboard + top.gg) ────────────
    # Link buttons are added in __init__ since they don't use callbacks
    # and discord.py @button decorator doesn't support style=link properly.

    # top.gg button is added dynamically by the cog's _build_view() since
    # we need the bot application ID at runtime for the URL.

    # ── Embed updater ────────────────────────────────────────

    async def _update_embed(self, interaction: discord.Interaction) -> None:
        """Edit the message with the updated embed, acknowledging the interaction."""
        guild_id = interaction.guild_id
        if not guild_id:
            await interaction.response.defer()
            return
        embed = _build_remote_embed(guild_id, user=interaction.user)
        try:
            await interaction.response.edit_message(embed=embed)
        except discord.HTTPException:
            # Fallback: defer if edit fails
            try:
                await interaction.response.defer()
            except discord.HTTPException:
                pass


# ── Cog ──────────────────────────────────────────────────────────────────────

class Remote(commands.Cog):
    """Enhanced /remote command with full playback controls and persistent view."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Track remote messages per guild for auto-update on track change
        self._remote_messages: dict[int, discord.Message] = {}

    async def cog_load(self) -> None:
        """Chain into the track-start callback for auto-updating the remote embed."""
        original_callback = player._on_track_start_callback

        async def _remote_track_start(guild_id: int, metadata: dict) -> None:
            # Forward to original callback first
            if original_callback is not None:
                try:
                    await original_callback(guild_id, metadata)
                except Exception:
                    pass
            # Auto-update our remote message
            await self._on_track_change(guild_id)

        player.set_on_track_start_callback(_remote_track_start)

    async def _on_track_change(self, guild_id: int) -> None:
        """Update the remote embed when a track changes."""
        msg = self._remote_messages.get(guild_id)
        if msg is None:
            return
        embed = _build_remote_embed(guild_id)
        view = self._build_view()
        try:
            await msg.edit(embed=embed, view=view)
        except discord.NotFound:
            # Message was deleted
            self._remote_messages.pop(guild_id, None)
        except discord.HTTPException as exc:
            log.debug("Failed to auto-update remote embed for guild %d: %s", guild_id, exc)

    def _build_view(self) -> EnhancedRemoteView:
        """Build the view with link buttons (Dashboard + top.gg) included."""
        view = EnhancedRemoteView()
        # Add Dashboard link button
        dashboard_button = discord.ui.Button(
            label="🌐 Dashboard",
            style=discord.ButtonStyle.link,
            url=DASHBOARD_URL,
            row=2,
        )
        view.add_item(dashboard_button)
        # Add top.gg upvote link button
        bot_id = self.bot.user.id if self.bot.user else 0
        topgg_url = TOP_GG_URL_TEMPLATE.format(bot_id=bot_id)
        topgg_button = discord.ui.Button(
            label="⬆️ Upvote on top.gg",
            style=discord.ButtonStyle.link,
            url=topgg_url,
            row=2,
        )
        view.add_item(topgg_button)
        return view

    @app_commands.command(
        name="remote",
        description="Show the enhanced now-playing control panel with full playback controls",
    )
    async def remote(self, interaction: discord.Interaction) -> None:
        guild_id = interaction.guild_id
        if not guild_id:
            await interaction.response.send_message(
                "This command can only be used in a server.", ephemeral=True
            )
            return

        user = interaction.user
        embed = _build_remote_embed(guild_id, user=user)
        view = self._build_view()

        # Delete previous remote message for this guild
        old_msg = self._remote_messages.get(guild_id)
        if old_msg:
            try:
                await old_msg.delete()
            except (discord.NotFound, discord.HTTPException):
                pass

        await interaction.response.send_message(embed=embed, view=view)
        msg = await interaction.original_response()
        self._remote_messages[guild_id] = msg


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Remote(bot))

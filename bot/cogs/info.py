"""HelloDJ — Info cog: /info dashboard showing current playback state."""

import logging
import time

import discord
from discord import app_commands
from discord.ext import commands

import metrics as _metrics
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

    @app_commands.command(name="ping", description="Show HelloDJ latency / websocket ping")
    async def ping(self, interaction: discord.Interaction):
        """Report the bot's websocket latency cleanly."""
        latency = round(self.bot.latency * 1000)  # ms
        embed = discord.Embed(
            title="🏓 HelloDJ — Ping",
            colour=discord.Colour.blurple(),
        )
        embed.add_field(name="Latency", value=f"{latency}ms", inline=True)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="metrics", description="Show AI usage metrics (LLM/STT/TTS)")
    @app_commands.describe(period="Time period: today, week, month, or all")
    @app_commands.choices(period=[
        app_commands.Choice(name="Today", value="today"),
        app_commands.Choice(name="This week", value="week"),
        app_commands.Choice(name="This month", value="month"),
        app_commands.Choice(name="All time", value="all"),
    ])
    async def metrics_cmd(
        self,
        interaction: discord.Interaction,
        period: str = "today",
    ):
        """Show aggregated AI usage metrics for the requested period."""
        try:
            summary = await _metrics.metrics.get_summary(period)
        except Exception as exc:
            log.warning("Could not read metrics: %s", exc)
            await interaction.response.send_message(
                "⚠️ Metrics are not available right now.",
                ephemeral=True,
            )
            return

        llm = summary.get("llm", {})
        stt = summary.get("stt", {})
        tts = summary.get("tts", {})
        wake = summary.get("wakeword", {})

        label = {
            "today": "Today",
            "week": "This Week",
            "month": "This Month",
            "all": "All Time",
        }.get(period, "Today")

        embed = discord.Embed(
            title=f"📊 HelloDJ — AI Usage Metrics ({label})",
            colour=discord.Colour.blurple(),
        )

        llm_calls = llm.get("calls", 0)
        llm_tokens = llm.get("tokens", 0)
        llm_input = llm.get("input_tokens", 0)
        llm_output = llm.get("output_tokens", 0)
        avg_latency = llm.get("avg_latency_ms", 0)
        embed.add_field(
            name="🧠 LLM (chat)",
            value=f"**{llm_calls}** calls\n"
                  f"{llm_tokens:,} tokens\n"
                  f"↘ {llm_input:,} in / ↗ {llm_output:,} out\n"
                  f"⏱ avg {avg_latency:.0f} ms",
            inline=True,
        )

        models = llm.get("models", {})
        if models:
            model_lines = "\n".join(
                f"• **{m}** — {b.get('calls', 0)} calls, {b.get('tokens', 0):,} tok"
                for m, b in models.items()
            )
            embed.add_field(
                name="Models",
                value=model_lines,
                inline=False,
            )

        stt_calls = stt.get("calls", 0)
        stt_ms = stt.get("duration_ms", 0)
        embed.add_field(
            name="🎙️ STT",
            value=f"**{stt_calls}** calls\n{duration_text(stt_ms)}",
            inline=True,
        )

        tts_calls = tts.get("calls", 0)
        tts_chars = tts.get("chars", 0)
        embed.add_field(
            name="🔊 TTS",
            value=f"**{tts_calls}** calls\n{tts_chars:,} chars",
            inline=True,
        )

        wake_dets = wake.get("detections", 0)
        embed.add_field(
            name="🫨 Wake Word",
            value=f"**{wake_dets}** detections",
            inline=True,
        )

        # Per-engine breakdowns for STT / TTS.
        stt_engines = stt.get("engines", {})
        tts_engines = tts.get("engines", {})
        if stt_engines:
            engine_lines = "\n".join(
                f"• **{eng}** — {b.get('calls', 0)} calls"
                for eng, b in stt_engines.items()
            )
            embed.add_field(
                name="STT Engines",
                value=engine_lines or "—",
                inline=False,
            )
        if tts_engines:
            engine_lines = "\n".join(
                f"• **{eng}** — {b.get('calls', 0)} calls"
                for eng, b in tts_engines.items()
            )
            embed.add_field(
                name="TTS Engines",
                value=engine_lines or "—",
                inline=False,
            )

        await interaction.response.send_message(embed=embed)


def duration_text(ms: float) -> str:
    """Format milliseconds as a short human-readable duration."""
    if ms <= 0:
        return "0s"
    seconds = ms / 1000.0
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes = seconds / 60.0
    if minutes < 60:
        return f"{minutes:.1f}m"
    hours = minutes / 60.0
    return f"{hours:.1f}h"


async def setup(bot: commands.Bot):
    await bot.add_cog(Info(bot))

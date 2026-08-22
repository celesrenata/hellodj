"""HelloDJ — Music cog: voice playback and queue commands — builds on wavelink 3.5.

Platform support
----------------
HelloDJ resolves and plays tracks through Lavalink (wavelink 3.5). The
following streaming platforms are supported, depending on the Lavalink
plugins configured in ``lavalink/application.yml``:

* **YouTube** — default source; video + audio.
* **YouTube Music** — audio-only, higher quality than YouTube.
* **SoundCloud** — audio-only.
* **Spotify** — metadata lookup via ``spsearch:`` prefix (audio via YouTube).
* **TIDAL** — metadata lookup via ``tdsearch:`` prefix; falls back to YouTube
  when no Tidal video is available.
* **Apple Music** — not directly supported; use YouTube/YouTube Music.
* **Deezer** — not directly supported; use YouTube/YouTube Music.
* **Bandcamp** — not directly supported; use YouTube/YouTube Music.

Use ``/source`` to switch the preferred provider at runtime.
"""

import asyncio
import logging
import os
import re
import time

import discord
from discord import app_commands
from discord.ext import commands
import wavelink
from wavelink import Playable, TrackSource

log = logging.getLogger(__name__)

import player
import session
import sounds
import whosampled
import artist_info
import guild_settings as _guild_settings
import sleep_settings as _sleep_settings
import file_handler
from debug import get_debug_logger

dbg = get_debug_logger("music")


# ── Duration parser (for /sleep) ──────────────────────────
# Accepts mixed numeric+unit tokens and bare numbers:
#   "1h 2m 3s", "1 hour", "2 minutes", "3 seconds", "1h 30m",
#   "90", "1.5m"  (bare number is treated as seconds).
# Returns total seconds, or 0 when nothing parseable is found.

_UNIT_SECONDS = {
    "h": 3600, "hr": 3600, "hour": 3600, "hours": 3600,
    "m": 60, "min": 60, "minute": 60, "minutes": 60,
    "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
}

_DURATION_TOKEN_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*"
    r"(h(?:ours?)?|m(?:in(?:utes?)?)?|s(?:ec(?:onds?)?)?)?"
    r"\b"
)


def parse_duration(text: str) -> int:
    """Parse a human duration string into seconds. Returns 0 on failure."""
    if not text or not isinstance(text, str):
        return 0
    text = text.strip().lower()
    if not text:
        return 0

    total = 0.0
    matched = False
    for m in _DURATION_TOKEN_RE.finditer(text):
        number = float(m.group(1))
        unit = m.group(2)
        if unit is None:
            # A bare number is treated as seconds.
            total += number
        else:
            total += number * _UNIT_SECONDS.get(unit, 1)
        matched = True

    if not matched:
        return 0
    return max(0, int(total))


class SaveQueueView(discord.ui.View):
    def __init__(self, invoker_id: int):
        super().__init__(timeout=30)
        self.invoker_id = invoker_id
        self.save = True

    async def _guard(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message(
                "Only the person who ran the command can answer.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Save queue", style=discord.ButtonStyle.success)
    async def save_btn(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if not await self._guard(interaction):
            return
        self.save = True
        self.stop()
        await interaction.response.defer()

    @discord.ui.button(label="Discard", style=discord.ButtonStyle.secondary)
    async def discard_btn(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if not await self._guard(interaction):
            return
        self.save = False
        self.stop()
        await interaction.response.defer()


class QueuePaginatedView(discord.ui.View):
    def __init__(self, guild_id: int, page_size: int = 10):
        super().__init__(timeout=120)
        self.guild_id = guild_id
        self.page_size = page_size
        self.page = 0

    def _embed(self) -> discord.Embed:
        state = player.get_state(self.guild_id)
        current = state.get("current")
        items = player.get_queue_page(state, self.page, self.page_size)
        total_pages = max(1, (len(state["queue"]) + self.page_size - 1) // self.page_size)

        embed = discord.Embed(title="🎶 HelloDJ Queue", colour=discord.Colour.blurple())
        if current:
            embed.add_field(name="Now Playing", value=f"**{current.get('title', 'Unknown')}**", inline=False)

        if items:
            start = self.page * self.page_size
            lines = [f"{start + i + 1}. **{item.get('title', 'Unknown')}**" for i, item in enumerate(items)]
            embed.add_field(name=f"Up Next  (Page {self.page + 1}/{total_pages})", value="\n".join(lines), inline=False)
        else:
            embed.add_field(name="Up Next", value="Empty", inline=False)

        embed.set_footer(text=f"{len(state['queue'])} track(s) total — HelloDJ")
        return embed

    @discord.ui.button(label="◀ Prev", style=discord.ButtonStyle.secondary, custom_id="q_prev")
    async def prev_page(self, interaction: discord.Interaction, _button: discord.ui.Button):
        state = player.get_state(self.guild_id)
        total_pages = max(1, (len(state["queue"]) + self.page_size - 1) // self.page_size)
        if self.page > 0:
            self.page -= 1
        await interaction.response.edit_message(embed=self._embed(), view=self)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary, custom_id="q_next")
    async def next_page(self, interaction: discord.Interaction, _button: discord.ui.Button):
        state = player.get_state(self.guild_id)
        total_pages = max(1, (len(state["queue"]) + self.page_size - 1) // self.page_size)
        if self.page < total_pages - 1:
            self.page += 1
        await interaction.response.edit_message(embed=self._embed(), view=self)


class SearchSelectView(discord.ui.View):
    """Dropdown of search results."""

    def __init__(self, results: list[dict], invoker_id: int, on_pick):
        super().__init__(timeout=60)
        self.results = results
        self.invoker_id = invoker_id
        self.on_pick = on_pick
        self.message: discord.Message | None = None

        options = []
        for i, info in enumerate(results):
            artist = (info.get("author") or "").strip()
            song = (info.get("title") or "Unknown").strip()
            album = (info.get("album") or "").strip()
            duration = info.get("duration") or 0
            total_secs = int(duration) // 1000
            mins, secs = divmod(total_secs, 60)

            # Clean up YouTube "Topic" channels: "Artist - Topic" -> "Artist"
            if artist.endswith(" - Topic"):
                artist = artist[:-8].strip()

            # Label: song title (max 100 chars)
            label = song[:100]

            # Description: "by Artist — 3:15" or "by Artist • Album — 3:15"
            parts = []
            if artist:
                parts.append(f"by {artist}")
            if album:
                parts.append(album)
            time_str = f"{mins}:{secs:02d}"
            desc = " • ".join(parts) + f" — {time_str}" if parts else time_str
            desc = desc[:100]

            options.append(discord.SelectOption(label=label, value=str(i), description=desc))

        select = discord.ui.Select(placeholder="Choose a song…", options=options)
        select.callback = self._on_select
        self.add_item(select)

        cancel = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary)
        cancel.callback = self._on_cancel
        self.add_item(cancel)

    async def _on_cancel(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message("Only the person who searched can cancel.", ephemeral=True)
            return
        await interaction.response.edit_message(content="Search cancelled.", view=None)
        self.stop()

    async def _on_select(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message("Only the person who searched can pick a song.", ephemeral=True)
            return
        idx = int(interaction.data["values"][0])
        info = self.results[idx]
        await self.on_pick(info, interaction)
        self.stop()

    async def on_timeout(self) -> None:
        if self.message:
            try:
                await self.message.edit(content="Search timed out.", view=None)
            except discord.HTTPException:
                pass


class AlbumSelectView(discord.ui.View):
    """Dropdown of album search results showing artist, year, track count, and total duration."""

    def __init__(self, results: list[dict], invoker_id: int, on_pick):
        super().__init__(timeout=60)
        self.results = results
        self.invoker_id = invoker_id
        self.on_pick = on_pick
        self.message: discord.Message | None = None

        options = []
        for i, info in enumerate(results[:25]):  # Discord max 25 options
            name = (info.get("name") or info.get("title") or "Unknown Album").strip()
            artist = (info.get("artist") or info.get("author") or "").strip()
            year = info.get("year") or ""
            track_count = info.get("track_count") or 0
            total_duration_ms = info.get("total_duration") or 0

            # Format total duration as M:SS or H:MM:SS
            total_secs = int(total_duration_ms) // 1000
            hours, remainder = divmod(total_secs, 3600)
            mins, secs = divmod(remainder, 60)
            if hours > 0:
                time_str = f"{hours}:{mins:02d}:{secs:02d}"
            else:
                time_str = f"{mins}:{secs:02d}"

            # Label: album name (max 100 chars)
            label = name[:100]

            # Description: "Artist • 2024 • 12 tracks • 45:30"
            parts = []
            if artist:
                parts.append(artist[:40])
            if year:
                parts.append(str(year))
            if track_count:
                parts.append(f"{track_count} tracks")
            parts.append(time_str)
            desc = " • ".join(parts)
            desc = desc[:100]

            options.append(discord.SelectOption(label=label, value=str(i), description=desc))

        if not options:
            options.append(discord.SelectOption(label="No results", value="none"))

        select = discord.ui.Select(placeholder="Choose an album…", options=options)
        select.callback = self._on_select
        self.add_item(select)

        cancel = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary)
        cancel.callback = self._on_cancel
        self.add_item(cancel)

    async def _on_cancel(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message("Only the person who searched can cancel.", ephemeral=True)
            return
        await interaction.response.edit_message(content="Album search cancelled.", view=None)
        self.stop()

    async def _on_select(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message("Only the person who searched can pick an album.", ephemeral=True)
            return
        idx = int(interaction.data["values"][0])
        if idx >= len(self.results):
            await interaction.response.edit_message(content="Invalid selection.", view=None)
            self.stop()
            return
        info = self.results[idx]
        await self.on_pick(info, interaction)
        self.stop()

    async def on_timeout(self) -> None:
        if self.message:
            try:
                await self.message.edit(content="Album search timed out.", view=None)
            except discord.HTTPException:
                pass


# ── /remote control panel ─────────────────────────────────
# An all-in-one button panel: Pause⏸️ / Play▶️ / Skip⏭️ /
# Lyrics🎤 / Samples💿 / Queue📄, plus a filter dropdown
# (vaporwave, 8bit, 8d, bassboost, nightcore, equalizer, 808,
# reset). Any guild member may press the buttons (interaction_check
# allows the invoking user or any guild member).

class RemoteControlView(discord.ui.View):
    def __init__(self, guild_id: int = 0, bot: commands.Bot | None = None):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.bot = bot
        self.message: discord.Message | None = None

        # ── Transport buttons ─────────────────────────────
        pause_btn = discord.ui.Button(
            label="⏸️ Pause", style=discord.ButtonStyle.primary, custom_id="rc_pause"
        )
        pause_btn.callback = self._on_pause
        self.add_item(pause_btn)

        play_btn = discord.ui.Button(
            label="▶️ Play", style=discord.ButtonStyle.success, custom_id="rc_play"
        )
        play_btn.callback = self._on_play
        self.add_item(play_btn)

        skip_btn = discord.ui.Button(
            label="⏭️ Skip", style=discord.ButtonStyle.secondary, custom_id="rc_skip"
        )
        skip_btn.callback = self._on_skip
        self.add_item(skip_btn)

        lyrics_btn = discord.ui.Button(
            label="🎤 Lyrics", style=discord.ButtonStyle.secondary, custom_id="rc_lyrics"
        )
        lyrics_btn.callback = self._on_lyrics
        self.add_item(lyrics_btn)

        samples_btn = discord.ui.Button(
            label="💿 Samples", style=discord.ButtonStyle.secondary, custom_id="rc_samples"
        )
        samples_btn.callback = self._on_samples
        self.add_item(samples_btn)

        queue_btn = discord.ui.Button(
            label="📄 Queue", style=discord.ButtonStyle.secondary, custom_id="rc_queue"
        )
        queue_btn.callback = self._on_queue
        self.add_item(queue_btn)

        # ── Filter dropdown ───────────────────────────────
        filter_options = [
            discord.SelectOption(label="vaporwave", value="vaporwave",
                                 description="Slowed, mellow vaporwave vibe"),
            discord.SelectOption(label="8bit", value="8bit",
                                 description="Arcade 8-bit chiptune vibe (distortion + tremolo + vibrato + EQ)"),
            discord.SelectOption(label="8d", value="8d",
                                 description="Spatial panning (left/right oscillation)"),
            discord.SelectOption(label="bassboost", value="bassboost",
                                 description="Boost low-end frequencies"),
            discord.SelectOption(label="nightcore", value="nightcore",
                                 description="Speed up tempo and shift pitch upward"),
            discord.SelectOption(label="equalizer", value="equalizer",
                                 description="Fine-tune specific frequency bands"),
            discord.SelectOption(label="808", value="808",
                                 description="Play the 808 cowbell as a sound effect"),
            discord.SelectOption(label="reset", value="reset",
                                 description="Reset all audio filters to default"),
        ]
        self._filter_select = discord.ui.Select(
            placeholder="🎚️ Audio filter…", options=filter_options, custom_id="rc_filter"
        )
        self._filter_select.callback = self._on_filter
        self.add_item(self._filter_select)

    # Any guild member may use the control panel.
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return True

    async def on_timeout(self) -> None:
        if self.message:
            try:
                await self.message.edit(content="Remote control timed out.", view=None)
            except discord.HTTPException:
                pass

    async def _defer(self, interaction: discord.Interaction) -> None:
        if interaction.response.is_done():
            return
        try:
            await interaction.response.defer()
        except discord.HTTPException:
            pass

    async def _on_pause(self, interaction: discord.Interaction) -> None:
        p = player.get_player(self.guild_id)
        if p:
            if p.paused:
                await p.pause(False)
            elif p.playing:
                await p.pause(True)
        await self._defer(interaction)

    async def _on_play(self, interaction: discord.Interaction) -> None:
        p = player.get_player(self.guild_id)
        if p:
            if p.paused:
                await p.pause(False)
            elif not p.playing:
                await player._play_next_from_queue(self.guild_id)
        await self._defer(interaction)

    async def _on_skip(self, interaction: discord.Interaction) -> None:
        # Check if a video session is active — skip the video instead
        video_cog = self.bot.get_cog("Video")
        if video_cog is not None:
            voice = interaction.user.voice  # type: ignore[union-attr]
            channel_id = voice.channel.id if voice and voice.channel else None
            if channel_id:
                streamer = video_cog._registry.get(self.guild_id, channel_id)
                if streamer is not None and streamer.is_active:
                    try:
                        await streamer.skip()
                        await self._defer(interaction)
                    except Exception:
                        await self._defer(interaction)
                    return

        # Fall back to audio skip
        p = player.get_player(self.guild_id)
        if p:
            await p.stop()
        await self._defer(interaction)

    async def _on_lyrics(self, interaction: discord.Interaction) -> None:
        state = player.get_state(self.guild_id)
        current = state.get("current")
        if not current:
            await interaction.response.send_message("No song is currently playing.", ephemeral=True)
            return
        title = current.get("title") or "Unknown"
        artist = current.get("author") or ""
        # Delegate to the Lyrics cog if it is loaded.
        lyrics_cog = self.bot.get_cog("Lyrics")
        if lyrics_cog is None:
            await interaction.response.send_message(
                f"**{title}** — lyrics lookup is unavailable (lyrics cog not loaded).",
                ephemeral=True,
            )
            return
        try:
            await lyrics_cog.lyrics(interaction)
        except Exception as exc:
            log.error("Remote lyrics failed: %s", exc)
            await interaction.response.send_message(
                f"Could not fetch lyrics for **{title}**.", ephemeral=True
            )

    async def _on_samples(self, interaction: discord.Interaction) -> None:
        state = player.get_state(self.guild_id)
        current = state.get("current")
        if not current:
            await interaction.response.send_message("No song is currently playing.", ephemeral=True)
            return
        title = current.get("title") or "Unknown"
        artist = current.get("author") or ""
        query = f"{title} {artist}".strip()
        await interaction.response.defer(ephemeral=True)
        try:
            result = await whosampled.search(query)
        except Exception as exc:
            log.error("Remote samples lookup failed: %s", exc)
            await interaction.followup.send(f"WhoSampled lookup failed: {exc}", ephemeral=True)
            return
        if result.blocked or result.error:
            await interaction.followup.send(
                result.error or "WhoSampled is unavailable.", ephemeral=True
            )
            return
        description, fields = whosampled.to_embed_fields(result)
        embed = discord.Embed(
            title="🔍 WhoSampled Lookup",
            description=description or f"Query: **{query}**",
            colour=discord.Colour.blurple(),
        )
        for name, value in fields:
            embed.add_field(name=name, value=value, inline=False)
        if result.track_url:
            embed.set_footer(text="Source: WhoSampled")
            embed.url = result.track_url
        await interaction.followup.send(embed=embed, ephemeral=True)

    async def _on_queue(self, interaction: discord.Interaction) -> None:
        view = QueuePaginatedView(self.guild_id)
        await interaction.response.send_message(embed=view._embed(), view=view, ephemeral=True)

    # ── Filter dropdown handler ───────────────────────────
    async def _on_filter(self, interaction: discord.Interaction) -> None:
        choice = interaction.data["values"][0]
        p = player.get_player(self.guild_id)
        if not p:
            await interaction.response.send_message(
                "HelloDJ is not connected to voice.", ephemeral=True
            )
            return
        await self._apply_filter(interaction, p, choice)

    async def _apply_filter(self, interaction: discord.Interaction, p, choice: str) -> None:
        """Replicate the Filters cog logic for the selected preset."""
        filters = p.filters

        if choice == "reset":
            filters.reset()
            await p.set_filters(filters)
            state = player.get_state(self.guild_id)
            state["filters"] = {}
            player.persist(self.guild_id)
            await interaction.response.send_message(
                "HelloDJ all filters reset to default.", ephemeral=True
            )
            return

        if choice == "bassboost":
            gains = [0.0, 0.1, 0.15, 0.1, 0.05, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
            bands = [{"band": i, "gain": g} for i, g in enumerate(gains)]
            filters.equalizer.set(bands=bands)
            filters.timescale.reset()
            filters.rotation.reset()
            filters.low_pass.reset()
            await p.set_filters(filters)
            state = player.get_state(self.guild_id)
            state["filters"]["bassboost"] = {"level": "moderate", "gains": gains}
            player.persist(self.guild_id)
            await interaction.response.send_message(
                "HelloDJ bassboost **moderate** applied.", ephemeral=True
            )
            return

        if choice == "nightcore":
            filters.timescale.set(speed=1.25, pitch=1.25, rate=1.0)
            filters.equalizer.reset()
            filters.rotation.reset()
            filters.low_pass.reset()
            await p.set_filters(filters)
            state = player.get_state(self.guild_id)
            state["filters"]["nightcore"] = {"speed": 1.25, "pitch": 1.25}
            player.persist(self.guild_id)
            await interaction.response.send_message(
                "HelloDJ nightcore filter applied.", ephemeral=True
            )
            return

        if choice == "8d":
            filters.rotation.set(rotation_hz=0.5)
            filters.equalizer.reset()
            filters.timescale.reset()
            filters.low_pass.reset()
            await p.set_filters(filters)
            state = player.get_state(self.guild_id)
            state["filters"]["8d"] = {"rotation": 0.5}
            player.persist(self.guild_id)
            await interaction.response.send_message(
                "HelloDJ 8D filter applied (rotation 0.5 Hz).", ephemeral=True
            )
            return

        if choice == "vaporwave":
            gains = [0.15, 0.15, 0.15, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
            bands = [{"band": i, "gain": g} for i, g in enumerate(gains)]
            filters.timescale.set(speed=0.85, pitch=0.9, rate=0.85)
            filters.equalizer.set(bands=bands)
            filters.rotation.reset()
            filters.low_pass.reset()
            await p.set_filters(filters)
            state = player.get_state(self.guild_id)
            state["filters"]["vaporwave"] = {
                "speed": 0.85, "pitch": 0.9, "rate": 0.85, "gains": gains,
            }
            player.persist(self.guild_id)
            await interaction.response.send_message(
                "HelloDJ vaporwave filter applied (0.85x, pitch 0.9x).", ephemeral=True
            )
            return

        if choice == "8bit":
            # Arcade/chiptune guitar-pedal chain, synced with filters.py:
            # distortion (scale) + tremolo + vibrato + timescale + equalizer,
            # with the muffling low-pass removed. Treble-emphasis / mid-boost
            # EQ so the arcade tone CUTS THROUGH rather than muffles.
            gains = [0, 0.05, 0.1, 0.2, 0.25, 0.3, 0.25, 0.2, 0.15, 0.1, 0.05, 0, -0.1, -0.2, -0.3]
            bands = [{"band": i, "gain": g} for i, g in enumerate(gains)]
            filters.distortion.set(scale=2.0)
            filters.tremolo.set(frequency=16.0, depth=0.6)
            filters.vibrato.set(frequency=12.0, depth=0.4)
            filters.timescale.set(speed=1.0, pitch=1.1, rate=1.0)
            filters.equalizer.set(bands=bands)
            filters.low_pass.reset()
            filters.rotation.reset()
            filters.karaoke.reset()
            filters.channel_mix.reset()
            await p.set_filters(filters)
            state = player.get_state(self.guild_id)
            state["filters"]["8bit"] = {
                "gains": gains,
                "speed": 1.0, "pitch": 1.1, "rate": 1.0,
                "distortion_scale": 2.0,
                "tremolo": {"frequency": 16.0, "depth": 0.6},
                "vibrato": {"frequency": 12.0, "depth": 0.4},
            }
            player.persist(self.guild_id)
            await interaction.response.send_message(
                "HelloDJ 8-bit arcade filter applied (distortion + tremolo + vibrato + EQ).",
                ephemeral=True
            )
            return

        if choice == "808":
            await interaction.response.defer(ephemeral=True)
            path = await sounds.ensure_preset(sounds.DEFAULT_PRESET)
            if not path:
                await interaction.followup.send(
                    "Could not load the 808 cowbell sample. Check the logs.", ephemeral=True
                )
                return
            ok = await sounds.play_sound(p, path, volume=100)
            if ok:
                await interaction.followup.send(
                    "🔊 808 cowbell played.", ephemeral=True
                )
            else:
                await interaction.followup.send(
                    "Could not play the 808 cowbell. Check the logs.", ephemeral=True
                )
            return

        if choice == "equalizer":
            from cogs.equalizer_view import EqualizerView, _build_eq_embed
            eq_view = EqualizerView(self.guild_id)
            embed = _build_eq_embed(eq_view.gains, eq_view.selected_band)
            await interaction.response.send_message(embed=embed, view=eq_view, ephemeral=True)
            return

        await interaction.response.send_message(f"Unknown filter: {choice}", ephemeral=True)


class Music(commands.Cog):
    # ── Command groups ──────────────────────────────────────
    chime_group = app_commands.Group(
        name="chime",
        description="Configure join/leave chime sounds",
    )

    # NOTE: play_group REMOVED — unified /play is now handled by PlaybackCog.
    # The underlying _play_*_flow helpers are preserved for the router to call.

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # /sleep — track the last time a non-bot human was present in each
        # guild's voice channel, so the monitor can disconnect after the
        # configured idle timeout.
        self._sleep_last_active: dict[int, float] = {}
        self._sleep_task: asyncio.Task | None = None

    # ── Cog lifecycle ───────────────────────────────────────

    async def cog_load(self) -> None:
        """Start the /sleep voice-activity monitor loop."""
        self._sleep_task = asyncio.ensure_future(self._sleep_monitor_loop())

    async def cog_unload(self) -> None:
        """Cancel the /sleep monitor loop on unload."""
        if self._sleep_task and not self._sleep_task.done():
            self._sleep_task.cancel()
            self._sleep_task = None

    async def _sleep_monitor_loop(self) -> None:
        """Every 30s, check guilds with a sleep timeout and disconnect when the
        voice channel has had no non-bot humans for >= the configured timeout."""
        try:
            while True:
                await asyncio.sleep(30)
                await self._check_sleep_guilds()
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            log.error("sleep monitor loop error: %s", exc)

    async def _check_sleep_guilds(self) -> None:
        now = time.time()
        for guild in list(self.bot.guilds):
            timeout = _sleep_settings.get_sleep_timeout(guild.id)
            if timeout <= 0:
                continue
            player_obj = player.get_player(guild.id)
            if not player_obj or not player_obj.connected:
                continue
            vc_channel = player_obj.channel
            if not vc_channel:
                continue
            human_members = [m for m in vc_channel.members if not m.bot]
            if human_members:
                self._sleep_last_active[guild.id] = now
                continue
            last = self._sleep_last_active.get(guild.id)
            if last is None:
                self._sleep_last_active[guild.id] = now
                continue
            if now - last >= timeout:
                await self._sleep_disconnect(guild.id)
                self._sleep_last_active.pop(guild.id, None)

    async def _sleep_disconnect(self, guild_id: int) -> None:
        """Auto-leave the voice channel and park/save any playing queue."""
        state = player.get_state(guild_id)
        player_obj = player.get_player(guild_id)
        had_content = bool(state.get("current")) or bool(state["queue"])
        if had_content:
            # park() internally snapshots the live queue/current, so save it
            # BEFORE clearing the in-memory state.
            await player.park(guild_id)
        player.clear_queue(state)
        state["current"] = None
        if player_obj and player_obj.connected:
            try:
                await player_obj.disconnect()
            except Exception as exc:
                log.warning("sleep: disconnect failed: %s", exc)
        text_ch = state.get("text_channel")
        if text_ch:
            try:
                if had_content:
                    await text_ch.send(
                        "😴 HelloDJ: nobody was listening for the idle timeout — "
                        "I disconnected and saved the queue. Use `/continue` to resume."
                    )
                else:
                    await text_ch.send(
                        "😴 HelloDJ: nobody was listening for the idle timeout — I disconnected."
                    )
            except Exception as exc:
                log.warning("sleep: could not notify text channel: %s", exc)

    # ── Same-channel guard ─────────────────────────────────

    def _check_same_channel(self, interaction: discord.Interaction) -> str | None:
        """Verify the invoking user is in the same voice channel as the bot.

        Returns an error message if:
          - The user is not in any voice channel, OR
          - The bot is connected but in a DIFFERENT voice channel.
        Returns None if the check passes (user is in the bot's channel, or bot
        is not connected anywhere — in which case there's nothing to guard).
        """
        user_voice = interaction.user.voice
        if not user_voice or not user_voice.channel:
            return "You need to be in a voice channel first."

        player_obj = player.get_player(interaction.guild.id)
        if player_obj and player_obj.connected and player_obj.channel:
            if user_voice.channel.id != player_obj.channel.id:
                return (
                    f"HelloDJ is active in **{player_obj.channel.name}** — "
                    f"you need to be in that channel to control playback."
                )
        return None

    # ── Connection ──────────────────────────────────────────

    @app_commands.command(name="join", description="Join your current voice channel")
    async def join(self, interaction: discord.Interaction):
        if not interaction.user.voice:
            await interaction.response.send_message("You need to be in a voice channel first.")
            return
        await interaction.response.defer()
        channel = interaction.user.voice.channel
        state = player.get_state(interaction.guild.id)
        state["voice_channel"] = channel
        state["text_channel"] = interaction.channel

        # Create wavelink Player for this guild (HybridPlayer when voice_recv is present)
        player_obj = await player.connect_player(channel)
        state["player"] = player_obj

        await interaction.followup.send(f"HelloDJ joined **{channel.name}**.")

    # ── Play group ──────────────────────────────────────────

    async def _resolve_tracks(self, query: str, provider: str) -> list:
        """Resolve a query to a list of tracks using the configured provider.

        Tidal→YouTube fallback: when the provider is Tidal and no tracks are
        found (or no video is available), fall back to YouTube search.
        """
        source_map = {
            "youtube": TrackSource.YouTube,
            "youtube_music": TrackSource.YouTubeMusic,
            "soundcloud": TrackSource.SoundCloud,
            "spotify": "spsearch",
            "tidal": "tidal",
        }
        source = source_map.get(provider, TrackSource.YouTube)

        if provider == "tidal":
            # Tidal: use tdsearch: prefix for metadata lookup via LavasRC.
            # Pass source=None so wavelink doesn't prepend ytsearch:/ytmsearch:.
            # If no Tidal results, fall back to YouTube.
            tidal_query = f"tdsearch:{query}" if not query.startswith("http") else query
            try:
                tracks = await Playable.search(tidal_query, source=None)
            except Exception as exc:
                log.warning("Tidal search failed for %r: %s", query, exc)
                tracks = None
            if not tracks:
                log.info("Tidal returned no results for %r — falling back to YouTube", query)
                tracks = await Playable.search(query, source=TrackSource.YouTube)
            return tracks

        tracks = await Playable.search(query, source=source)
        if not tracks:
            log.info("Provider %r returned no results for %r — falling back to YouTube", provider, query)
            tracks = await Playable.search(query, source=TrackSource.YouTube)
        return tracks

    async def _ensure_player(self, interaction: discord.Interaction) -> wavelink.Player:
        """Ensure a wavelink player is connected for this guild.

        If a video session is active, skips the voice connect — the track
        will be queued and played after the video ends.
        """
        voice_channel = interaction.user.voice.channel
        guild_id = interaction.guild.id
        state = player.get_state(guild_id)
        state["voice_channel"] = voice_channel
        state["text_channel"] = interaction.channel
        state["persist_enabled"] = True

        # Don't connect during video playback — just return existing (or None-safe stub)
        if player._is_video_active(guild_id):
            player_obj = state.get("player")
            if player_obj:
                return player_obj
            # No player exists — create a minimal stub that won't actually connect
            # The enqueue_and_start guard will prevent playback anyway
            return None  # type: ignore[return-value]

        player_obj = state.get("player")
        if not player_obj or not player_obj.connected:
            player_obj = await player.connect_player(voice_channel)
            state["player"] = player_obj
        return player_obj

    # ── /play group ─────────────────────────────────────────
    # NOTE: Slash command registrations REMOVED — unified /play is handled by
    # PlaybackCog. These methods are kept as internal helpers callable from
    # the PlaybackRouter (Task 12 wiring).

    async def _play_song(self, interaction: discord.Interaction, query: str):
        """Internal: search-and-pick flow. Called by PlaybackRouter."""
        if not interaction.user.voice:
            await interaction.response.send_message("You need to be in a voice channel first.")
            return
        await self._play_search_flow(interaction, query)

    async def _play_link(self, interaction: discord.Interaction, url: str):
        """Internal: direct URL play flow. Called by PlaybackRouter."""
        if not interaction.user.voice:
            await interaction.response.send_message("You need to be in a voice channel first.")
            return
        await self._play_url_flow(interaction, url, allow_playlist=False)

    async def _play_album(self, interaction: discord.Interaction, query: str):
        """Internal: album play flow. Called by PlaybackRouter."""
        if not interaction.user.voice:
            await interaction.response.send_message("You need to be in a voice channel first.")
            return
        await self._play_album_flow(interaction, query)

    async def _play_playlist(self, interaction: discord.Interaction, url: str):
        """Internal: playlist play flow. Called by PlaybackRouter."""
        if not interaction.user.voice:
            await interaction.response.send_message("You need to be in a voice channel first.")
            return
        await self._play_url_flow(interaction, url, allow_playlist=True)

    async def _play_video(
        self,
        interaction: discord.Interaction,
        url: str = "",
        file: discord.Attachment | None = None,
    ):
        """Internal: video/file upload play flow. Called by PlaybackRouter."""
        if not interaction.user.voice:
            await interaction.response.send_message("You need to be in a voice channel first.")
            return

        # Upload path (no URL): play a local audio/video attachment.
        if not url:
            attachments = getattr(interaction, "attachments", None) or []
            attachment = attachments[0] if attachments else None
            if attachment is None:
                await interaction.response.send_message(
                    "Please attach an audio or video file, or provide a video URL.",
                    ephemeral=True,
                )
                return
            await interaction.response.defer()
            try:
                player_obj = await self._ensure_player(interaction)
            except Exception as exc:
                log.error("Play video (upload) failed to ensure player: %s", exc)
                await interaction.followup.send("Could not join voice to play that file.")
                return
            try:
                info = await file_handler.process_upload(attachment, player_obj, interaction.channel)
                played = await file_handler.play_uploaded_file(
                    interaction.guild.id, player_obj, info["playable_path"], info["title"]
                )
            except Exception as exc:
                log.error("Play video (upload) failed: %s", exc)
                await interaction.followup.send(f"Could not play that file: {exc}")
                return
            if played:
                await interaction.followup.send(f"HelloDJ is playing **{info['title']}**.")
            else:
                await interaction.followup.send("Could not play that file — check the logs.")
            return

        # URL path: same resolution as /play link.
        await self._play_url_flow(interaction, url, allow_playlist=False)

    async def _play_music_video(self, interaction: discord.Interaction, query: str):
        """Internal: music video play flow. Called by PlaybackRouter."""
        if not interaction.user.voice:
            await interaction.response.send_message("You need to be in a voice channel first.")
            return
        if query.startswith("http://") or query.startswith("https://"):
            await self._play_url_flow(interaction, query, allow_playlist=False)
            return
        await self._play_search_flow(interaction, query, label="music video")

    # ── Private play flows (shared by the /play group) ─────

    async def _play_search_flow(self, interaction: discord.Interaction, query: str, label: str = "song") -> None:
        """Search for a track and either queue a single result or show a dropdown."""
        await interaction.response.defer()
        await interaction.followup.send("🔄 Searching…", ephemeral=True)
        try:
            await self._ensure_player(interaction)
            state = player.get_state(interaction.guild.id)
            provider = state.get("source_provider", "youtube")
            is_url = query.startswith("http://") or query.startswith("https://")

            tracks = await self._resolve_tracks(query, provider)
            if not tracks:
                await interaction.followup.send("No results found.")
                return

            # For search queries, show a selection dropdown (top 10).
            if not is_url and len(tracks) > 1:
                results = player._search_entries(tracks, provider)[:10]

                async def on_pick(info: dict, picker: discord.Interaction):
                    title = info.get("title") or "Unknown"
                    author = info.get("author") or "Unknown artist"
                    duration_ms = info.get("duration") or 0
                    await player.add_track(state, interaction.guild.id, info)
                    p = player.get_player(interaction.guild.id)

                    # Respond to the interaction IMMEDIATELY (before slow resolve)
                    if p and p.connected and not p.playing and not p.paused:
                        embed = discord.Embed(
                            title="✅ Selected & playing",
                            description=f"**{title}**",
                            colour=discord.Colour.blurple(),
                        )
                        embed.add_field(name="🎤 Artist", value=author, inline=True)
                        embed.add_field(
                            name="⏱️ Length",
                            value=player._fmt_duration_ms(duration_ms),
                            inline=True,
                        )
                        embed.add_field(name="🎚️ Position", value="Now playing", inline=True)
                    else:
                        queue_len = len(state["queue"])
                        pos = queue_len
                        time_to_play_ms = 0
                        if p and p.playing:
                            cur_len = int(p.current.length or 0)
                            if cur_len > player._DURATION_MAX_MS:
                                cur_len = 0
                            remaining = max(0, cur_len - int(p.position or 0))
                            time_to_play_ms += remaining
                        for item in state["queue"][:-1]:
                            time_to_play_ms += int(item.get("duration") or 0)
                        embed = discord.Embed(
                            title="✅ Added to queue",
                            description=f"**{title}**",
                            colour=discord.Colour.blurple(),
                        )
                        embed.add_field(name="🎤 Artist", value=author, inline=True)
                        embed.add_field(
                            name="⏱️ Length",
                            value=player._fmt_duration_ms(duration_ms),
                            inline=True,
                        )
                        embed.add_field(
                            name="🎚️ Position",
                            value=f"#{pos} in queue",
                            inline=True,
                        )
                        embed.add_field(
                            name="⏳ Time to play",
                            value=player._fmt_duration_ms(time_to_play_ms),
                            inline=False,
                        )
                    await picker.response.edit_message(embed=embed, view=None)

                    # Start playback AFTER responding (can be slow for Spotify)
                    if p and p.connected and not p.playing and not p.paused:
                        await player._play_next_from_queue(interaction.guild.id)
                    elif p and not p.connected:
                        vc = state.get("voice_channel")
                        if vc:
                            try:
                                new_player = await player.connect_player(vc)
                                state["player"] = new_player
                                await player._play_next_from_queue(interaction.guild.id)
                            except Exception as exc:
                                log.error("on_pick reconnect failed: %s", exc)

                view = SearchSelectView(results, interaction.user.id, on_pick)
                prompt = f"Select a {label}:"
                msg = await interaction.followup.send(prompt, view=view)
                view.message = msg
                return

            # Direct URL or single result
            track = tracks[0]
            info = player._track_entry(track, provider)
            await player.add_track(state, interaction.guild.id, info)
            await self._start_if_idle(interaction.guild.id)
            await interaction.followup.send(f"HelloDJ added to queue: **{info['title']}**")

        except (wavelink.LavalinkLoadException, wavelink.NodeException) as exc:
            log.error("Play %s failed (%s): %s", label, type(exc).__name__, exc)
            severity = getattr(exc, "severity", None)
            if severity == "fault":
                cause = getattr(exc, "cause", None)
                detail = f" ({cause})" if cause else ""
                await interaction.followup.send(
                    f"Could not play that {label} — the music source failed{detail}. "
                    "If this is a YouTube request, the source may be temporarily unavailable "
                    "or blocking automated requests."
                )
            elif severity == "noMatches":
                await interaction.followup.send(f"Could not play that {label} — the music source returned no results.")
            else:
                await interaction.followup.send(
                    f"Could not play that {label} — the music source returned no results or was unavailable."
                )
        except Exception as exc:
            log.error("Play %s failed (%s): %s", label, type(exc).__name__, exc)
            await interaction.followup.send(f"Could not play: {exc}")

    async def _start_if_idle(self, guild_id: int) -> None:
        """Start playback from the queue if the player is connected and idle.

        ``player.add_track`` only appends the resolved entry to the queue and
        persists it — it never starts playback. /play link (and the direct-URL
        branch of /play song) must mirror the dropdown path (``on_pick`` →
        ``player._play_next_from_queue``) so a freshly-connected, idle player
        actually starts playing instead of leaving the track queued forever.
        """
        p = player.get_player(guild_id)
        if p and p.connected and not p.playing:
            # Unpause if the player was left in paused state after a stop/track-end
            if p.paused:
                try:
                    await p.pause(False)
                except Exception:
                    pass
            await player._play_next_from_queue(guild_id)
            await player._play_next_from_queue(guild_id)
        elif p and not p.connected:
            # Player exists but lost connection — re-evaluate state. Log for
            # diagnosis so we can catch the race condition.
            log.warning(
                "_start_if_idle: player exists but not connected (guild=%s "
                "connected=%s playing=%s paused=%s) — attempting reconnect & play",
                guild_id, p.connected, p.playing, p.paused,
            )
            state = player.get_state(guild_id)
            vc = state.get("voice_channel")
            if vc:
                try:
                    new_player = await player.connect_player(vc)
                    state["player"] = new_player
                    await player._play_next_from_queue(guild_id)
                except Exception as exc:
                    log.error("_start_if_idle reconnect failed guild=%s: %s", guild_id, exc)
        else:
            # Log the state so we can diagnose why playback didn't start.
            log.info(
                "_start_if_idle: skipping (guild=%s p=%s connected=%s playing=%s paused=%s)",
                guild_id,
                p is not None,
                getattr(p, "connected", None) if p else None,
                getattr(p, "playing", None) if p else None,
                getattr(p, "paused", None) if p else None,
            )

    async def _play_url_flow(self, interaction: discord.Interaction, url: str, *, allow_playlist: bool = False) -> None:
        """Resolve a direct URL (track/video) or playlist and queue it."""
        if not (url.startswith("http://") or url.startswith("https://")):
            await interaction.response.send_message("Please provide a valid http(s) URL.", ephemeral=True)
            return
        await interaction.response.defer()
        await interaction.followup.send("🔄 Resolving…", ephemeral=True)
        try:
            await self._ensure_player(interaction)
            state = player.get_state(interaction.guild.id)
            provider = state.get("source_provider", "youtube")

            result = await Playable.search(url, source=TrackSource.YouTube)

            if isinstance(result, wavelink.Playlist):
                tracks = result.tracks
                if not tracks:
                    await interaction.followup.send("That playlist has no tracks.")
                    return
                for track in tracks:
                    info = player._track_entry(track, provider)
                    await player.add_track(state, interaction.guild.id, info)
                await self._start_if_idle(interaction.guild.id)
                await interaction.followup.send(
                    f"HelloDJ added **{len(tracks)}** tracks from **{result.name}** to the queue."
                )
            elif isinstance(result, list):
                # For a direct track link, take only the first result; for a
                # playlist URL, queue every returned track.
                items = result if allow_playlist else [result[0]]
                for track in items:
                    info = player._track_entry(track, provider)
                    await player.add_track(state, interaction.guild.id, info)
                await self._start_if_idle(interaction.guild.id)
                if allow_playlist:
                    await interaction.followup.send(f"HelloDJ added **{len(result)}** tracks to the queue.")
                else:
                    await interaction.followup.send(f"HelloDJ added to queue: **{info['title']}**")
            else:
                info = player._track_entry(result, provider)
                await player.add_track(state, interaction.guild.id, info)
                await self._start_if_idle(interaction.guild.id)
                await interaction.followup.send(f"HelloDJ added to queue: **{info['title']}**")

        except (wavelink.LavalinkLoadException, wavelink.NodeException) as exc:
            log.error("Play URL failed (%s): %s", type(exc).__name__, exc)
            await interaction.followup.send("Could not play that URL — the music source failed or was unavailable.")
        except Exception as exc:
            log.error("Play URL failed (%s): %s", type(exc).__name__, exc)
            await interaction.followup.send(f"Could not play that URL: {exc}")

    async def _play_album_flow(self, interaction: discord.Interaction, query: str) -> None:
        """Resolve an album (URL or search) and show a selection dropdown or queue directly."""
        await interaction.response.defer()
        await interaction.followup.send("🔄 Searching for albums…", ephemeral=True)
        try:
            await self._ensure_player(interaction)
            state = player.get_state(interaction.guild.id)
            provider = state.get("source_provider", "youtube")

            is_url = query.startswith("http://") or query.startswith("https://")

            # For URLs, load directly (no selection needed)
            if is_url:
                result = await Playable.search(query)
                if isinstance(result, wavelink.Playlist):
                    tracks = result.tracks
                    if not tracks:
                        await interaction.followup.send("That album/playlist has no tracks.")
                        return
                    for track in tracks:
                        info = player._track_entry(track, provider)
                        await player.add_track(state, interaction.guild.id, info)
                    await self._start_if_idle(interaction.guild.id)
                    await interaction.followup.send(
                        f"HelloDJ added **{len(tracks)}** tracks from **{result.name}** to the queue."
                    )
                elif isinstance(result, list) and result:
                    for track in result:
                        info = player._track_entry(track, provider)
                        await player.add_track(state, interaction.guild.id, info)
                    await self._start_if_idle(interaction.guild.id)
                    await interaction.followup.send(f"HelloDJ added **{len(result)}** tracks to the queue.")
                else:
                    await interaction.followup.send("Could not load that album URL.")
                return

            # For text searches, use provider-specific album search
            album_results = await self._search_albums(query, provider)

            if not album_results:
                await interaction.followup.send("No albums found for that query.")
                return

            # Show selection dropdown
            async def on_album_pick(album_info: dict, picker: discord.Interaction):
                album_url = album_info.get("url")
                album_name = album_info.get("name") or "Unknown Album"
                if not album_url:
                    await picker.response.edit_message(
                        content=f"Could not load **{album_name}** — no URL available.", view=None
                    )
                    return

                await picker.response.edit_message(
                    content=f"🔄 Loading **{album_name}**…", view=None
                )

                try:
                    result = await Playable.search(album_url)
                    tracks = []
                    if isinstance(result, wavelink.Playlist):
                        tracks = result.tracks
                    elif isinstance(result, list):
                        tracks = result

                    if not tracks:
                        await picker.followup.send(f"**{album_name}** has no playable tracks.")
                        return

                    for track in tracks:
                        info = player._track_entry(track, provider)
                        await player.add_track(state, interaction.guild.id, info)
                    await self._start_if_idle(interaction.guild.id)
                    await picker.followup.send(
                        f"HelloDJ added **{len(tracks)}** tracks from **{album_name}** to the queue."
                    )
                except Exception as exc:
                    log.error("Album load failed for %s: %s", album_url, exc)
                    await picker.followup.send(f"Could not load **{album_name}**: {exc}")

            view = AlbumSelectView(album_results, interaction.user.id, on_album_pick)
            msg = await interaction.followup.send("Select an album:", view=view)
            view.message = msg

        except (wavelink.LavalinkLoadException, wavelink.NodeException) as exc:
            log.error("Play album failed (%s): %s", type(exc).__name__, exc)
            await interaction.followup.send("Could not load that album — the music source failed or was unavailable.")
        except Exception as exc:
            log.error("Play album failed (%s): %s", type(exc).__name__, exc)
            await interaction.followup.send(f"Could not load that album: {exc}")

    async def _search_albums(self, query: str, provider: str) -> list[dict]:
        """Search for albums across providers and return structured results.

        Each result dict has: name, artist, year, track_count, total_duration, url
        """
        results = []

        # Provider-specific album search prefixes
        search_queries = []
        if provider == "spotify":
            search_queries.append(("spsearch", f"spsearch:{query}", "spotify"))
        elif provider == "tidal":
            # Use the direct Tidal v2 album search for accurate results
            import tidal as _tidal_mod
            try:
                tidal_albums = await _tidal_mod.search_albums(query, limit=10)
                if tidal_albums:
                    return tidal_albums
            except Exception as exc:
                log.debug("Tidal direct album search failed: %s", exc)
            # Fall back to track-based grouping
            search_queries.append(("tidal", f"tdsearch:{query}", "tidal"))
        elif provider == "youtube_music":
            search_queries.append(("ytmsearch", f"ytmsearch:{query} album", "youtube_music"))
        elif provider == "youtube":
            search_queries.append(("ytsearch", f"ytsearch:{query} album", "youtube"))
        elif provider == "soundcloud":
            search_queries.append(("scsearch", f"scsearch:{query}", "soundcloud"))

        # Always try multiple providers for better results (but limit to avoid API quota)
        # Only add fallbacks if primary returned no results (lazy evaluation below)
        fallback_queries = []

        seen_names = set()
        for _prefix, sq, src in search_queries:
            try:
                result = await Playable.search(sq, source=None)
                if isinstance(result, wavelink.Playlist):
                    # Direct playlist result
                    name = result.name or "Unknown Album"
                    if name.lower() in seen_names:
                        continue
                    seen_names.add(name.lower())

                    tracks = result.tracks or []
                    total_dur = sum(getattr(t, "length", 0) or 0 for t in tracks)
                    if total_dur > player._DURATION_MAX_MS:
                        total_dur = 0
                    artist = getattr(tracks[0], "author", "") if tracks else ""
                    # Try to get year from extras
                    year = ""
                    if tracks:
                        extras = getattr(tracks[0], "extras", None)
                        if extras and hasattr(extras, "get"):
                            year = extras.get("albumYear", "") or ""

                    results.append({
                        "name": name,
                        "artist": artist,
                        "year": year,
                        "track_count": len(tracks),
                        "total_duration": total_dur,
                        "url": getattr(result, "uri", None) or getattr(result, "url", None) or "",
                        "source": src,
                    })
                elif isinstance(result, list) and result:
                    # For search results that return tracks, try to group by album
                    # Extract unique album info from the track list
                    album_map: dict[str, dict] = {}
                    for track in result[:20]:
                        extras = getattr(track, "extras", None)
                        album_name = ""
                        album_url = ""
                        if extras and hasattr(extras, "get"):
                            album_name = extras.get("albumName", "") or ""
                            album_url = extras.get("albumUrl", "") or ""
                        if not album_name:
                            raw = getattr(track, "raw_data", None)
                            if raw and isinstance(raw, dict):
                                pi = raw.get("pluginInfo", {})
                                album_name = pi.get("albumName", "") or ""
                                album_url = pi.get("albumUrl", "") or ""
                        if not album_name:
                            continue
                        key = album_name.lower()
                        if key in seen_names:
                            continue
                        if key not in album_map:
                            artist = getattr(track, "author", "") or ""
                            year = ""
                            if extras and hasattr(extras, "get"):
                                year = extras.get("albumYear", "") or ""
                            album_map[key] = {
                                "name": album_name,
                                "artist": artist,
                                "year": year,
                                "track_count": 0,
                                "total_duration": 0,
                                "url": album_url,
                                "source": src,
                            }
                        length = getattr(track, "length", 0) or 0
                        if length <= player._DURATION_MAX_MS:
                            album_map[key]["total_duration"] += length
                        album_map[key]["track_count"] += 1

                    # Skip expensive album pre-load — use search result sample count.
                    # Loading each album URL hammers the Spotify API quota.

                    for key, album_info in album_map.items():
                        seen_names.add(key)
                        results.append(album_info)

            except Exception as exc:
                log.debug("Album search with %s failed: %s", sq, exc)

        # If primary provider returned nothing, try fallbacks
        if not results and fallback_queries:
            for _prefix, sq, src in fallback_queries:
                try:
                    result = await Playable.search(sq, source=None)
                    if isinstance(result, list) and result:
                        album_map: dict[str, dict] = {}
                        for track in result[:10]:
                            extras = getattr(track, "extras", None)
                            album_name = ""
                            album_url = ""
                            if extras and hasattr(extras, "get"):
                                album_name = extras.get("albumName", "") or ""
                                album_url = extras.get("albumUrl", "") or ""
                            if not album_name:
                                continue
                            key = album_name.lower()
                            if key in seen_names:
                                continue
                            if key not in album_map:
                                artist = getattr(track, "author", "") or ""
                                album_map[key] = {
                                    "name": album_name,
                                    "artist": artist,
                                    "year": "",
                                    "track_count": 0,
                                    "total_duration": 0,
                                    "url": album_url,
                                    "source": src,
                                }
                            album_map[key]["track_count"] += 1
                        for key, album_info in album_map.items():
                            seen_names.add(key)
                            results.append(album_info)
                except Exception as exc:
                    log.debug("Album fallback search with %s failed: %s", sq, exc)
                if results:
                    break

        # Post-filter: keep only results where ALL query words appear in name+artist
        query_words = [w.lower() for w in query.split() if len(w) > 1]
        if query_words and results:
            filtered = [
                r for r in results
                if all(
                    w in (r.get("name", "") + " " + r.get("artist", "")).lower()
                    for w in query_words
                )
            ]
            if filtered:
                results = filtered

        return results[:10]

    # ── Album ───────────────────────────────────────────────

    # ── Album (internal helper) ────────────────────────────

    async def _album(self, interaction: discord.Interaction, query: str):
        """Internal: album play flow (standalone). Called by PlaybackRouter."""
        if not interaction.user.voice:
            await interaction.response.send_message("You need to be in a voice channel first.")
            return
        await self._play_album_flow(interaction, query)

    # ── Pause / Resume (internal helpers) ──────────────────

    async def _do_pause(self, interaction: discord.Interaction):
        """Internal: pause playback. Called by PlaybackRouter."""
        err = self._check_same_channel(interaction)
        if err:
            await interaction.response.send_message(err, ephemeral=True)
            return
        player_obj = player.get_player(interaction.guild.id)
        if player_obj and player_obj.playing:
            await player_obj.pause(True)
            await interaction.response.send_message("HelloDJ paused.")
        else:
            await interaction.response.send_message("HelloDJ: Nothing is playing.")

    async def _do_resume(self, interaction: discord.Interaction):
        """Internal: resume playback. Called by PlaybackRouter."""
        err = self._check_same_channel(interaction)
        if err:
            await interaction.response.send_message(err, ephemeral=True)
            return
        player_obj = player.get_player(interaction.guild.id)
        if player_obj and player_obj.paused:
            await player_obj.pause(False)
            await interaction.response.send_message("HelloDJ resumed.")
        else:
            await interaction.response.send_message("HelloDJ: Nothing is paused.")

    # ── Skip (internal helper) ─────────────────────────────

    async def _do_skip(self, interaction: discord.Interaction):
        """Internal: skip current track. Called by PlaybackRouter."""
        err = self._check_same_channel(interaction)
        if err:
            await interaction.response.send_message(err, ephemeral=True)
            return
        player_obj = player.get_player(interaction.guild.id)
        if player_obj and (player_obj.playing or player_obj.paused):
            await player_obj.stop()
            await interaction.response.send_message("HelloDJ skipped.")
        else:
            await interaction.response.send_message("HelloDJ: Nothing to skip.")

    # ── Stop / Clear (internal helpers) ───────────────────

    async def _do_stop(self, interaction: discord.Interaction):
        """Internal: stop playback and offer save. Called by PlaybackRouter."""
        err = self._check_same_channel(interaction)
        if err:
            await interaction.response.send_message(err, ephemeral=True)
            return
        gid = interaction.guild.id
        state = player.get_state(gid)
        had_content = bool(state.get("current")) or bool(state["queue"])
        snap = player._snapshot(state)
        state["persist_enabled"] = False
        player.clear_queue(state)
        player_obj = player.get_player(gid)
        if player_obj and (player_obj.playing or player_obj.paused):
            await player_obj.stop()
        state["current"] = None

        if not had_content:
            await session.clear(gid)
            await interaction.response.send_message("HelloDJ stopped and cleared the queue.")
            return

        view = SaveQueueView(interaction.user.id)
        await interaction.response.send_message(
            "HelloDJ stopped. Save this queue so you can `/continue` later?", view=view
        )
        await view.wait()
        if view.save:
            await session.save_guild(gid, auto_resume=False, **snap)
            msg = "HelloDJ saved — use `/continue` to resume this queue."
        else:
            await session.clear(gid)
            msg = "HelloDJ stopped and cleared the queue."
        await interaction.edit_original_response(content=msg, view=None)

    # NOTE: clear_group REMOVED — unified /clear is now handled by PlaybackCog.
    # Internal helpers preserved for router delegation.

    async def _do_clear_music(self, interaction: discord.Interaction):
        """Internal: clear music queue without save prompt. Called by PlaybackRouter."""
        err = self._check_same_channel(interaction)
        if err:
            await interaction.response.send_message(err, ephemeral=True)
            return
        gid = interaction.guild.id
        state = player.get_state(gid)
        state["persist_enabled"] = False
        player.clear_queue(state)
        player_obj = player.get_player(gid)
        if player_obj and (player_obj.playing or player_obj.paused):
            await player_obj.stop()
        state["current"] = None
        await session.clear(gid)
        await interaction.response.send_message("HelloDJ cleared the music queue.")

    async def _do_clear_video(self, interaction: discord.Interaction):
        """Internal: clear video queue. Called by PlaybackRouter."""
        gid = interaction.guild.id

        user_voice = interaction.user.voice
        if not user_voice or not user_voice.channel:
            await interaction.response.send_message(
                "You need to be in a voice channel first.", ephemeral=True
            )
            return

        video_cog = self.bot.get_cog("Video")
        if video_cog is None:
            await interaction.response.send_message(
                "Video cog is not loaded.", ephemeral=True
            )
            return

        streamer = video_cog._registry.get(gid, user_voice.channel.id)
        if streamer is None or not streamer.is_active:
            await interaction.response.send_message(
                "No active video session to clear.", ephemeral=True
            )
            return

        # Verify user is in the same channel as the video activity
        if user_voice.channel.id != streamer.channel_id:
            channel = interaction.guild.get_channel(streamer.channel_id)
            channel_name = channel.name if channel else f"ID {streamer.channel_id}"
            await interaction.response.send_message(
                f"The video Activity is in **{channel_name}** — "
                f"you need to be in that channel to control it.",
                ephemeral=True,
            )
            return

        await interaction.response.defer()
        try:
            await streamer.stop()
        except Exception as exc:
            log.error("Error stopping video streamer during clear video for guild %d: %s", gid, exc)

        # Disconnect WebSocket clients
        await video_cog._backend.ws_hub.disconnect_all(gid)

        # Stop seek bar updates
        video_cog._stop_seek_bar_update((gid, user_voice.channel.id))

        # Close the Activity (best-effort)
        if video_cog._launcher is not None:
            try:
                await video_cog._launcher.close(streamer.channel_id)
            except Exception as exc:
                log.warning("Error closing Activity during clear video for guild %d: %s", gid, exc)

        video_cog._registry.unregister(gid, user_voice.channel.id)
        await interaction.followup.send("HelloDJ cleared the video queue and stopped the Activity.")

    # ── Queue display (internal helper) ───────────────────

    async def _do_queue(self, interaction: discord.Interaction, view_type: str = "simple"):
        """Internal: show current queue. Called by PlaybackRouter."""
        state = player.get_state(interaction.guild.id)
        current = state.get("current")
        items = state["queue"]

        if view_type == "paginated":
            view = QueuePaginatedView(interaction.guild.id)
            await interaction.response.send_message(embed=view._embed(), view=view)
            return

        if not current and not items:
            await interaction.response.send_message("HelloDJ queue is empty.")
            return
        lines = []
        if current:
            lines.append(f"HelloDJ now playing: **{current.get('title', 'Unknown')}**")
        lines += [f"{i + 1}. **{item.get('title', 'Unknown')}**" for i, item in enumerate(items)]
        await interaction.response.send_message("\n".join(lines))

    # ── Now playing ─────────────────────────────────────────

    async def _nowplaying(self, interaction: discord.Interaction) -> None:
        """Show the current song, its length, the queue size, and the song's
        position within the queue (e.g. ``2/12``).
        """
        state = player.get_state(interaction.guild.id)
        current = state.get("current")
        if not current:
            await interaction.response.send_message(
                "Nothing is playing right now.",
                ephemeral=True,
            )
            return

        title = current.get("title") or "Unknown"
        author = current.get("author") or "Unknown artist"
        duration_ms = current.get("duration") or 0
        queue_len = len(state.get("queue") or [])
        # Position is 1-based: the current song is #1 in the queue.
        position = 1
        total = queue_len + 1
        url = current.get("webpage_url")

        embed = discord.Embed(
            title="🎶 Now Playing",
            description=f"**{title}**",
            colour=discord.Colour.blurple(),
        )
        embed.add_field(name="🎤 Artist", value=author, inline=False)
        embed.add_field(name="⏱️ Length", value=player._fmt_duration_ms(duration_ms), inline=True)
        embed.add_field(name="🎚️ Position", value=f"Now playing: {position}/{total}", inline=True)
        embed.add_field(name="📃 Queue", value=f"{queue_len} track(s) up next", inline=True)
        if url:
            embed.add_field(name="🔗 Link", value=url, inline=False)
            embed.url = url

        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="np", description="Show the current song, its length, and queue position")
    async def np(self, interaction: discord.Interaction):
        await self._nowplaying(interaction)

    @app_commands.command(name="nowplaying", description="Alias for /np — current song, length, and queue position")
    async def nowplaying(self, interaction: discord.Interaction):
        await self._nowplaying(interaction)

    @app_commands.command(name="link", description="Copy the direct link to the currently playing song")
    async def link(self, interaction: discord.Interaction):
        state = player.get_state(interaction.guild.id)
        current = state.get("current")
        if not current:
            await interaction.response.send_message("Nothing is playing right now.", ephemeral=True)
            return
        url = current.get("uri") or current.get("url") or current.get("webpage_url")
        if not url:
            await interaction.response.send_message("No direct link available for this track.", ephemeral=True)
            return
        await interaction.response.send_message(f"🔗 {url}", ephemeral=True)

    # ── Add (append without interrupting) ───────────────────

    @app_commands.command(name="add", description="Add a song to the queue without interrupting playback")
    @app_commands.describe(query="URL or search terms")
    async def add(self, interaction: discord.Interaction, query: str):
        if not interaction.user.voice:
            await interaction.response.send_message("You need to be in a voice channel first.")
            return
        await interaction.response.defer()
        await interaction.followup.send("🔄 Searching…", ephemeral=True)

        state = player.get_state(interaction.guild.id)
        state["voice_channel"] = interaction.user.voice.channel
        state["text_channel"] = interaction.channel

        provider = state.get("source_provider", "youtube")
        is_url = query.startswith("http://") or query.startswith("https://")

        try:
            tracks = await self._resolve_tracks(query, provider)
            if not tracks:
                await interaction.followup.send("No results found.")
                return

            if not is_url and len(tracks) > 1:
                results = player._search_entries(tracks, provider)[:10]

                async def on_pick(info: dict, picker: discord.Interaction):
                    title = info.get("title") or "Unknown"
                    await player.add_track(state, interaction.guild.id, info)
                    await picker.response.edit_message(
                        content=f"HelloDJ added to queue (#{len(state['queue'])}): **{title}**", view=None
                    )

                view = SearchSelectView(results, interaction.user.id, on_pick)
                msg = await interaction.followup.send("Select a song to add:", view=view)
                view.message = msg
                return

            track = tracks[0]
            info = player._track_entry(track, provider)
            await player.add_track(state, interaction.guild.id, info)
            await interaction.followup.send(f"HelloDJ added to queue (#{len(state['queue'])}): **{info['title']}**")

        except Exception as exc:
            await interaction.followup.send(f"Could not add: {exc}")

    # ── Remove ──────────────────────────────────────────────

    @app_commands.command(name="remove", description="Remove a track from the queue by index (1-based)")
    @app_commands.describe(index="Track number to remove (1 = first, 2 = second, …)")
    async def remove(self, interaction: discord.Interaction, index: int):
        state = player.get_state(interaction.guild.id)
        if not state["queue"]:
            await interaction.response.send_message("HelloDJ queue is empty.")
            return
        if index < 1 or index > len(state["queue"]):
            await interaction.response.send_message(
                f"Invalid index {index}. Queue has {len(state['queue'])} track(s) (1–{len(state['queue'])})."
            )
            return
        removed = player.remove_from_queue(state, index - 1)
        if removed is None:
            await interaction.response.send_message(f"Invalid index. Queue has {len(state['queue'])} track(s).")
            return
        player.persist(interaction.guild.id)
        await interaction.response.send_message(f"HelloDJ removed **{removed.get('title', 'Unknown')}** from the queue.")

    @app_commands.command(name="delete", description="Alias for /remove")
    @app_commands.describe(index="Track number to delete (1-based)")
    async def delete(self, interaction: discord.Interaction, index: int):
        await self.remove.callback(self, interaction, index)

    # ── Move ────────────────────────────────────────────────

    @app_commands.command(name="move", description="Move a track to a new position in the queue")
    @app_commands.describe(from_index="Current position (1-based)", to_index="Target position (1-based)")
    async def move(self, interaction: discord.Interaction, from_index: int, to_index: int):
        state = player.get_state(interaction.guild.id)
        if not state["queue"]:
            await interaction.response.send_message("HelloDJ queue is empty.")
            return
        ok = player.move_in_queue(state, from_index - 1, to_index - 1)
        if not ok:
            await interaction.response.send_message(f"Invalid indices. Queue has {len(state['queue'])} track(s).")
            return
        player.persist(interaction.guild.id)
        await interaction.response.send_message("HelloDJ track moved.")

    # ── Shuffle ─────────────────────────────────────────────

    @app_commands.command(name="shuffle", description="Randomize the order of tracks in the queue")
    async def shuffle(self, interaction: discord.Interaction):
        state = player.get_state(interaction.guild.id)
        if not state["queue"]:
            await interaction.response.send_message("HelloDJ queue is empty.")
            return
        player.shuffle_queue(state)
        player.persist(interaction.guild.id)
        await interaction.response.send_message("HelloDJ queue shuffled.")

    # ── Repeat ──────────────────────────────────────────────

    @app_commands.command(name="repeat", description="Toggle repeat mode")
    @app_commands.choices(mode=[
        app_commands.Choice(name="Off", value="off"),
        app_commands.Choice(name="Single Track", value="single"),
        app_commands.Choice(name="Full Queue", value="queue"),
    ])
    async def repeat(self, interaction: discord.Interaction, mode: str = ""):
        state = player.get_state(interaction.guild.id)
        if mode:
            player.set_repeat(state, mode)
        else:
            modes = ["off", "single", "queue"]
            current = state["repeat_mode"]
            next_mode = modes[(modes.index(current) + 1) % len(modes)] if current in modes else "off"
            player.set_repeat(state, next_mode)
            mode = next_mode
        player.persist(interaction.guild.id)
        await interaction.response.send_message(f"HelloDJ repeat: **{mode}**")

    # ── Source ──────────────────────────────────────────────

    @app_commands.command(name="source", description="Set the preferred streaming source/provider")
    @app_commands.choices(provider=[
        app_commands.Choice(name="YouTube", value="youtube"),
        app_commands.Choice(name="YouTube Music", value="youtube_music"),
        app_commands.Choice(name="SoundCloud", value="soundcloud"),
        app_commands.Choice(name="Spotify", value="spotify"),
        app_commands.Choice(name="Tidal", value="tidal"),
    ])
    async def source(self, interaction: discord.Interaction, provider: str):
        state = player.get_state(interaction.guild.id)
        state["source_provider"] = provider
        player.persist(interaction.guild.id)
        await interaction.response.send_message(f"HelloDJ source set to **{provider}**.")

    # ── Leave + aliases ─────────────────────────────────────

    @app_commands.command(name="leave", description="Disconnect HelloDJ from voice")
    async def leave(self, interaction: discord.Interaction):
        err = self._check_same_channel(interaction)
        if err:
            await interaction.response.send_message(err, ephemeral=True)
            return
        gid = interaction.guild.id
        state = player.get_state(gid)
        player_obj = player.get_player(gid)
        if not player_obj or not player_obj.connected:
            await interaction.response.send_message("HelloDJ is not in a voice channel.")
            return

        had_content = bool(state.get("current")) or bool(state["queue"])
        snap = player._snapshot(state)
        state["persist_enabled"] = False
        player.clear_queue(state)
        state["current"] = None
        await player_obj.disconnect()
        state["player"] = None

        if not had_content:
            await session.clear(gid)
            state["persist_enabled"] = True
            await interaction.response.send_message("HelloDJ disconnected.")
            return

        view = SaveQueueView(interaction.user.id)
        await interaction.response.send_message(
            "HelloDJ disconnected. Save this queue so you can `/continue` later?", view=view
        )
        await view.wait()
        if view.save:
            await session.save_guild(gid, auto_resume=False, **snap)
            text = "HelloDJ saved — use `/continue` to resume this queue."
        else:
            await session.clear(gid)
            text = "HelloDJ queue discarded."
        state["persist_enabled"] = True
        msg = await interaction.original_response()
        await msg.edit(content=text, view=None)

    @app_commands.command(name="fuckoff", description="Disconnect HelloDJ from voice (alias for /leave)")
    async def fuckoff(self, interaction: discord.Interaction):
        await self.leave.callback(self, interaction)

    @app_commands.command(name="l", description="Alias for /leave")
    async def l_cmd(self, interaction: discord.Interaction):
        await self.leave.callback(self, interaction)

    @app_commands.command(name="disconnect", description="Alias for /leave")
    async def disconnect(self, interaction: discord.Interaction):
        await self.leave.callback(self, interaction)

    # ── /remote control panel ───────────────────────────────

    @app_commands.command(
        name="remote",
        description="Show or refresh the unified now-playing control panel",
    )
    async def remote(self, interaction: discord.Interaction):
        player_obj = player.get_player(interaction.guild.id)
        if not player_obj or not player_obj.connected:
            await interaction.response.send_message("HelloDJ is not connected to voice.", ephemeral=True)
            return
        state = player.get_state(interaction.guild.id)
        current = state.get("current")
        old_msg = state.get("now_playing_msg")
        view = player.NowPlayingView(interaction.guild.id)
        embed = player.build_now_playing_embed_from_entry(current) if current else discord.Embed(
            title="🎵 HelloDJ — Now Playing",
            description="Nothing is playing yet. Use /play to start a song — the buttons below will control it.",
            colour=discord.Colour.blurple(),
        ).set_footer(text="HelloDJ — Use the buttons below to control playback")

        # Delete the old remote panel if it exists
        if old_msg:
            try:
                await old_msg.delete()
            except (discord.NotFound, discord.HTTPException):
                pass

        # Send a fresh one
        await interaction.response.send_message(embed=embed, view=view)
        state["now_playing_msg"] = await interaction.original_response()

    # ── /sleep auto-leave ───────────────────────────────────

    @app_commands.command(
        name="sleep",
        description="Auto-leave voice when no one is listening for a duration (max 2 hours). Use 0 to disable.",
    )
    @app_commands.describe(
        time="Idle timeout (max 2 hours), e.g. 1h 2m 3s, 1 hour, 2 minutes, 3 seconds, or bare seconds. 0 = disable.",
    )
    async def sleep(self, interaction: discord.Interaction, time: str = ""):
        gid = interaction.guild.id
        seconds = parse_duration(time)
        if seconds == 0:
            _sleep_settings.clear_sleep_timeout(gid)
            await interaction.response.send_message(
                "😴 HelloDJ auto-leave **disabled**. It will stay in voice until you tell it to leave.",
                ephemeral=True,
            )
            return
        # Cap the idle timeout at 2 hours (7200s); inform the user if clamped.
        cap_applied = False
        if seconds > 7200:
            seconds = 7200
            cap_applied = True
        _sleep_settings.set_sleep_timeout(gid, seconds)
        mins, secs = divmod(seconds, 60)
        if mins >= 60:
            hours, mins = divmod(mins, 60)
            pretty = f"{hours}h {mins}m {secs}s"
        elif mins > 0:
            pretty = f"{mins}m {secs}s"
        else:
            pretty = f"{secs}s"
        cap_note = (
            " _(capped at the 2-hour maximum)_" if cap_applied else ""
        )
        await interaction.response.send_message(
            f"😴 HelloDJ will auto-leave **{pretty}** after the voice channel is empty.{cap_note}",
            ephemeral=True,
        )

    # ── /crossfade ──────────────────────────────────────────

    @app_commands.command(
        name="crossfade",
        description="Fade tracks into one another. Give seconds to enable, 0 to disable.",
    )
    @app_commands.describe(
        seconds="Crossfade duration in seconds (e.g. 5, 8). 0 disables crossfade.",
    )
    async def crossfade(self, interaction: discord.Interaction, seconds: float = 0.0):
        gid = interaction.guild.id
        cf = max(0.0, seconds)
        player.set_crossfade(player.get_state(gid), cf)
        player.persist(gid)
        await session.save_guild(gid, **player._snapshot(player.get_state(gid)))
        if cf <= 0:
            player.reset_crossfade(gid)
            await interaction.response.send_message(
                "🎚️ HelloDJ crossfade **disabled**.", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                f"🎚️ HelloDJ crossfade enabled: **{cf:.1f}s** fade between tracks.", ephemeral=True
            )

    # ── /save (alias /grab) ─────────────────────────────────

    @app_commands.command(
        name="save",
        description="DM the current song details (title, artist, duration, source, link) to you",
    )
    async def save(self, interaction: discord.Interaction):
        await self._save_song(interaction)

    @app_commands.command(
        name="grab",
        description="Alias for /save — DM the current song details to you",
    )
    async def grab(self, interaction: discord.Interaction):
        await self._save_song(interaction)

    async def _save_song(self, interaction: discord.Interaction) -> None:
        state = player.get_state(interaction.guild.id)
        current = state.get("current")
        if not current:
            await interaction.response.send_message("No song is currently playing.", ephemeral=True)
            return
        title = current.get("title") or "Unknown"
        author = current.get("author") or "Unknown artist"
        duration_ms = current.get("duration") or 0
        url = current.get("webpage_url")
        provider = state.get("source_provider", "youtube")

        embed = discord.Embed(
            title="🎵 Current Song",
            description=f"**{title}**",
            colour=discord.Colour.blurple(),
        )
        embed.add_field(name="🎤 Artist", value=author, inline=True)
        embed.add_field(name="⏱️ Duration", value=player._fmt_duration_ms(duration_ms), inline=True)
        embed.add_field(name="📡 Source", value=provider, inline=True)
        if url:
            embed.add_field(name="🔗 Link", value=url, inline=False)
            embed.url = url
        embed.set_footer(text=f"Saved by {interaction.user.display_name}")

        try:
            await interaction.user.send(embed=embed)
        except discord.Forbidden:
            await interaction.response.send_message(
                "I couldn't DM you — please allow direct messages from bots.", ephemeral=True
            )
            return
        except Exception as exc:
            log.error("Save DM failed: %s", exc)
            await interaction.response.send_message("Could not send the DM.", ephemeral=True)
            return
        await interaction.response.send_message("✅ Check your DMs — song saved!", ephemeral=True)

    # ── /whosat (alias /whosthis) ───────────────────────────

    @app_commands.command(
        name="whosat",
        description="Show artist bio/trivia for the current song or a given artist",
    )
    @app_commands.describe(artist="Artist name (optional — uses the current song if omitted)")
    async def whosat(self, interaction: discord.Interaction, artist: str = ""):
        await self._whosat(interaction, artist)

    @app_commands.command(
        name="whosthis",
        description="Alias for /whosat — show artist bio/trivia",
    )
    @app_commands.describe(artist="Artist name (optional — uses the current song if omitted)")
    async def whosthis(self, interaction: discord.Interaction, artist: str = ""):
        await self._whosat(interaction, artist)

    async def _whosat(self, interaction: discord.Interaction, artist: str = "") -> None:
        await interaction.response.defer(ephemeral=True)
        if not artist:
            state = player.get_state(interaction.guild.id)
            current = state.get("current")
            if current:
                artist = current.get("author") or ""
        if not artist:
            await interaction.followup.send(
                "No artist given and no song is currently playing. Try `/whosat artist: <name>`.",
                ephemeral=True,
            )
            return

        result = await artist_info.lookup(artist)
        if result.error:
            embed = discord.Embed(
                title="🎤 Artist Lookup",
                description=result.error,
                colour=discord.Colour.orange(),
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        embed = discord.Embed(
            title=f"🎤 {result.name or artist}",
            description=result.bio or "No bio available.",
            colour=discord.Colour.blurple(),
        )
        if result.wikipedia_url:
            embed.set_footer(text="Source: MusicBrainz + Wikipedia")
            embed.url = result.wikipedia_url
        else:
            embed.set_footer(text="Source: MusicBrainz")
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── Samples (WhoSampled) ────────────────────────────────

    @app_commands.command(name="samples", description="Look up what samples a song uses (WhoSampled)")
    @app_commands.describe(query="Song name or artist")
    async def samples(self, interaction: discord.Interaction, query: str):
        await interaction.response.defer(ephemeral=True)
        try:
            result = await whosampled.search(query)
        except Exception as exc:
            log.error("WhoSampled search failed: %s", exc)
            await interaction.followup.send(f"WhoSampled lookup failed: {exc}", ephemeral=True)
            return

        if result.blocked:
            embed = discord.Embed(
                title="🔍 WhoSampled Lookup",
                description=result.error or "WhoSampled is unavailable.",
                colour=discord.Colour.red(),
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        if result.error:
            embed = discord.Embed(
                title="🔍 WhoSampled Lookup",
                description=result.error,
                colour=discord.Colour.orange(),
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        description, fields = whosampled.to_embed_fields(result)
        embed = discord.Embed(
            title="🔍 WhoSampled Lookup",
            description=description or f"Query: **{query}**",
            colour=discord.Colour.blurple(),
        )
        for name, value in fields:
            embed.add_field(name=name, value=value, inline=False)
        if result.track_url:
            embed.set_footer(text="Source: WhoSampled")
            embed.url = result.track_url
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── Chime group ─────────────────────────────────────────

    @chime_group.command(name="set", description="Set the chime sound for join/leave")
    @app_commands.choices(target=[
        app_commands.Choice(name="Join", value="join"),
        app_commands.Choice(name="Leave", value="leave"),
        app_commands.Choice(name="Both", value="both"),
    ])
    @app_commands.describe(sound="Preset key (e.g. original-808-cowbell) or custom sound name")
    async def chime_set(self, interaction: discord.Interaction, target: str, sound: str):
        gid = interaction.guild.id
        # Validate the sound key
        if sound not in sounds.PRESETS and not sound.startswith("custom:"):
            path = sounds.sound_path(sound)
            if not os.path.exists(path):
                available = ", ".join(sounds.PRESETS.keys())
                await interaction.response.send_message(
                    f"Unknown sound **{sound}**. Available presets: {available}. "
                    f"Use `/chime import` to add a custom sound.",
                    ephemeral=True,
                )
                return

        if target == "both":
            cfg = sounds.set_chime_config(gid, join=sound, leave=sound)
        elif target == "join":
            cfg = sounds.set_chime_config(gid, join=sound)
        else:
            cfg = sounds.set_chime_config(gid, leave=sound)

        await interaction.response.send_message(
            f"HelloDJ chime set: **{target}** → **{sound}** (volume {cfg['volume']}%)"
        )

    @chime_group.command(name="import", description="Import a custom chime sound from a URL")
    @app_commands.describe(url="Audio file URL (mp3, wav, ogg, etc.)", name="Optional name for the sound")
    async def chime_import(self, interaction: discord.Interaction, url: str, name: str = ""):
        await interaction.response.defer(ephemeral=True)
        try:
            path = await sounds.import_sound(url, name or None)
        except Exception as exc:
            log.error("Chime import failed: %s", exc)
            await interaction.followup.send(f"Import failed: {exc}", ephemeral=True)
            return

        fname = os.path.basename(path)
        key = f"custom:{fname}"
        await interaction.followup.send(
            f"HelloDJ imported sound **{fname}**. Use `/chime set target:join sound:{key}` to apply it.",
            ephemeral=True,
        )

    @chime_group.command(name="list", description="List available chime sounds")
    async def chime_list(self, interaction: discord.Interaction):
        sounds_list = sounds.list_sounds()
        lines = []
        for s in sounds_list:
            status = "✅" if s["on_disk"] else "⬇️ (will download on first use)"
            lines.append(f"• **{s['name']}** (`{s['key']}`) — {s['source']} {status}")
        embed = discord.Embed(
            title="🔔 Available Chime Sounds",
            description="\n".join(lines) if lines else "No sounds available.",
            colour=discord.Colour.blurple(),
        )
        cfg = sounds.get_chime_config(interaction.guild.id)
        embed.set_footer(
            text=f"Current: join={cfg['join']}, leave={cfg['leave']}, volume={cfg['volume']}%"
        )
        await interaction.response.send_message(embed=embed)

    @chime_group.command(name="test", description="Play the current join chime")
    async def chime_test(self, interaction: discord.Interaction):
        if not interaction.user.voice:
            await interaction.response.send_message("You need to be in a voice channel first.", ephemeral=True)
            return
        player_obj = player.get_player(interaction.guild.id)
        if not player_obj or not player_obj.connected:
            await interaction.response.send_message("HelloDJ is not in a voice channel.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        # The wavelink Player implements the voice-client protocol (send_audio_packet),
        # same as the TTS path in voice/voice_commands.py.
        ok = await sounds.play_chime(interaction.guild.id, "join", player_obj)
        if ok:
            await interaction.followup.send("🔔 Chime played.", ephemeral=True)
        else:
            await interaction.followup.send("Could not play the chime. Check the logs.", ephemeral=True)

    @chime_group.command(name="volume", description="Set chime volume (0-100)")
    @app_commands.describe(volume="Volume level (0-100)")
    async def chime_volume(self, interaction: discord.Interaction, volume: int):
        if volume < 0 or volume > 100:
            await interaction.response.send_message("Volume must be between 0 and 100.", ephemeral=True)
            return
        cfg = sounds.set_chime_config(interaction.guild.id, volume=volume)
        await interaction.response.send_message(f"HelloDJ chime volume set to **{cfg['volume']}%**.")

    @chime_group.command(name="reset", description="Reset chime config to defaults")
    async def chime_reset(self, interaction: discord.Interaction):
        cfg = sounds.set_chime_config(
            interaction.guild.id,
            join=sounds.DEFAULT_PRESET,
            leave=sounds.DEFAULT_PRESET,
            volume=100,
        )
        await interaction.response.send_message(
            f"HelloDJ chime reset: join={cfg['join']}, leave={cfg['leave']}, volume={cfg['volume']}%"
        )

    # ── Continue ────────────────────────────────────────────

    @app_commands.command(name="continue", description="Resume a previously saved queue")
    async def continue_cmd(self, interaction: discord.Interaction):
        gid = interaction.guild.id
        saved = session.get(gid)
        if not saved or not (saved.get("current") or saved.get("queue")):
            await interaction.response.send_message("There's no saved queue to resume.", ephemeral=True)
            return
        if not interaction.user.voice:
            await interaction.response.send_message("You need to be in a voice channel first.", ephemeral=True)
            return

        await interaction.response.defer()
        voice_channel = interaction.user.voice.channel
        state = player.get_state(gid)
        state["voice_channel"] = voice_channel
        state["text_channel"] = interaction.channel

        entries = []
        if saved.get("current"):
            entries.append(saved["current"])
        entries.extend(saved.get("queue", []))
        count = await player.enqueue_and_start(interaction.guild, interaction.channel, entries, replace=True)
        await interaction.followup.send(f"HelloDJ resuming **{count}** track(s) from your saved queue.")

    # ── Voice state listener ────────────────────────────────

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, _before: discord.VoiceState, _after: discord.VoiceState):
        guild = member.guild
        player_obj = player.get_player(guild.id)
        if not player_obj or not player_obj.connected:
            return
        vc_channel = player_obj.channel
        if not vc_channel:
            return

        human_members = [m for m in vc_channel.members if not m.bot]
        state = player.get_state(guild.id)

        if human_members:
            alone_task = state.get("alone_task")
            if alone_task and not alone_task.done():
                alone_task.cancel()
            state["alone_task"] = None
            return

        if state.get("alone_task") and not state["alone_task"].done():
            return

        async def _leave_if_alone() -> None:
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                return
            p2 = player.get_player(guild.id)
            if p2 and p2.connected:
                if not any(not m.bot for m in p2.channel.members):
                    had_content = bool(state.get("current")) or bool(state["queue"])
                    if had_content:
                        await player.park(guild.id)
                    else:
                        await player.discard(guild.id)
                    player.clear_queue(state)
                    state["current"] = None
                    await p2.disconnect()
                    text_ch = state.get("text_channel")
                    if text_ch:
                        if had_content:
                            await text_ch.send("HelloDJ: Everyone left — I saved the queue. Use `/continue` to resume.")
                        else:
                            await text_ch.send("HelloDJ: Everyone left — disconnected from voice.")
            state["alone_task"] = None

        state["alone_task"] = asyncio.ensure_future(_leave_if_alone())


async def setup(bot: commands.Bot):
    await bot.add_cog(Music(bot))

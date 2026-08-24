"""HelloDJ — Radio cog: continuous internet radio streaming.

Plays live internet-radio streams straight into the voice channel. Radio
streams are continuous (no discrete track end), so they are played directly
via ``Playable.search(url)`` + ``player.play(track)`` — they are never
enqueued as discrete queue tracks.

Sources
-------
* **iHeartRadio** — public JSON API:
    * Search:  ``GET https://us.api.iheart.com/api/v3/search/combined?keywords={city}``
    * Resolve: ``GET https://us.api.iheart.com/api/v2/content/liveStations/{ids}``
    * Stream:  ``streams.secure_hls_stream`` / ``secure_shoutcast_stream``
      (``https://stream.revma.ihrhls.com/zc{id}/hls.m3u8`` or ``/zc{id}``)
* **The Lot Radio** — public HLS stream (verified).
* **Nightwave Plaza** — 24/7 vaporwave radio via radio.plaza.one/mp3.
* **Nightride FM** — Icecast streams: synthwave, chillsynth, darksynth channels.
* **313.FM** — Detroit electronic music via Icecast (icecast.313.fm:8000).
"""

import asyncio
import logging
import random

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
import wavelink
from wavelink import Playable, TrackSource

import player
from debug import get_debug_logger

log = logging.getLogger(__name__)
dbg = get_debug_logger("radio")

# Browser-ish User-Agent so the public JSON APIs don't reject the request.
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

# ── Verified iHeartRadio endpoints ────────────────────────────────────────
_IHEART_SEARCH = "https://us.api.iheart.com/api/v3/search/combined"
_IHEART_LIVE = "https://us.api.iheart.com/api/v2/content/liveStations"

# Stream code pattern used by streamtheworld for station-page embeds.
_STREAMTHEWORLD_REDIRECT = "https://playerservices.streamtheworld.com/api/livestream-redirect/{code}.aac"

# ── Curated presets ───────────────────────────────────────────────────────
# ``url`` is the live stream; ``note`` records how/why it was chosen.
PRESETS = {
    "thelot": {
        "name": "The Lot Radio",
        "url": "https://playback.livepeer.studio/hls/85c28sa2o8wppm58/index.m3u8",
        "note": (
            "Public HLS stream (verified live 2026-08-24: livepeercdn.studio "
            "307 → playback.livepeer.studio → nyc-prod-catalyst LP playback). "
            "Stream key updated from 68b9ebup9ajleurk to 85c28sa2o8wppm58."
        ),
    },
    "nightwave": {
        "name": "Nightwave Plaza",
        "url": "https://radio.plaza.one/mp3",
        "note": (
            "24/7 vaporwave radio. Direct MP3 stream via radio.plaza.one "
            "(plaza.one is the site, radio.plaza.one/mp3 is the Icecast endpoint)."
        ),
    },
    "nightride": {
        "name": "Nightride FM — Synthwave",
        "url": "https://stream.nightride.fm/nightride.ogg",
        "note": (
            "Nightride FM main synthwave/retrowave channel. Icecast at "
            "stream.nightride.fm, 320kbps OGG. See nightride.fm/stations."
        ),
    },
    "chillsynth": {
        "name": "Nightride FM — Chillsynth",
        "url": "https://stream.nightride.fm/chillsynth.ogg",
        "note": (
            "Nightride FM chillsynth/chillwave/instrumental channel. "
            "Icecast at stream.nightride.fm."
        ),
    },
    "darksynth": {
        "name": "Nightride FM — Darksynth",
        "url": "https://stream.nightride.fm/darksynth.ogg",
        "note": (
            "Nightride FM darksynth/cyberpunk/horror channel. "
            "Icecast at stream.nightride.fm."
        ),
    },
    "313fm": {
        "name": "313.FM",
        "url": "http://icecast.313.fm:8000/burst.mp3",
        "note": (
            "Detroit electronic music radio. Icecast server at "
            "icecast.313.fm:8000, mountpoint /burst.mp3 (icecast.ofdoom.com "
            "redirects here with 301). Verified live 2026-08-24."
        ),
    },
}


class Radio(commands.Cog):
    """Internet-radio streaming: /radio city, /radio direct, /radio preset."""

    radio_group = app_commands.Group(
        name="radio",
        description="Stream live internet radio — search by city, direct station, or preset",
    )

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Per-guild radio state: {guild_id: {"url": str, "title": str, "text_channel": Channel}}
        self._radio_sessions: dict[int, dict] = {}

    # ── ICY Metadata Fetching ──────────────────────────────────────────────

    async def _fetch_icy_metadata(self, stream_url: str) -> str | None:
        """Fetch the current ICY (Icecast/Shoutcast) metadata from a stream.

        Connects to the stream with Icy-MetaData: 1 header, reads just enough
        bytes to extract the first metadata block, then disconnects.
        Falls back to Icecast JSON status API for OGG streams (e.g. Nightride FM).
        Returns the StreamTitle string or None if unavailable.
        """
        # HLS streams (.m3u8) don't support ICY metadata
        if ".m3u8" in stream_url or "livepeer" in stream_url:
            return None

        # For Nightride FM OGG streams, use the Icecast status-json.xsl API
        if "stream.nightride.fm" in stream_url:
            return await self._fetch_nightride_metadata(stream_url)

        try:
            headers = {"Icy-MetaData": "1", "User-Agent": _USER_AGENT}
            timeout = aiohttp.ClientTimeout(total=5, sock_read=4)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(stream_url, headers=headers) as resp:
                    # Check if server supports ICY metadata
                    metaint_str = resp.headers.get("icy-metaint")
                    if not metaint_str:
                        return None
                    metaint = int(metaint_str)

                    # Read past the first audio chunk to reach metadata
                    await resp.content.readexactly(metaint)

                    # Read the metadata length byte (length * 16 = actual size)
                    length_byte = await resp.content.readexactly(1)
                    meta_length = length_byte[0] * 16
                    if meta_length == 0:
                        return None

                    # Read and decode the metadata block
                    meta_data = await resp.content.readexactly(meta_length)
                    meta_str = meta_data.decode("utf-8", errors="replace").rstrip("\x00")

                    # Parse StreamTitle='...' from the metadata
                    if "StreamTitle='" in meta_str:
                        start = meta_str.index("StreamTitle='") + len("StreamTitle='")
                        end = meta_str.index("';", start)
                        title = meta_str[start:end].strip()
                        return title if title else None
                    return None
        except (asyncio.TimeoutError, aiohttp.ClientError, ValueError, Exception) as exc:
            dbg("ICY metadata fetch failed for %s: %s", stream_url, exc)
            return None

    async def _fetch_nightride_metadata(self, stream_url: str) -> str | None:
        """Fetch now-playing info from Nightride FM's Icecast status JSON API.

        Maps the stream URL mountpoint to the status API's source list and
        extracts the display-title.
        """
        try:
            # Extract mountpoint from URL (e.g. /nightride.ogg → nightride.ogg)
            from urllib.parse import urlparse
            path = urlparse(stream_url).path.lstrip("/")

            status_url = "https://stream.nightride.fm/status-json.xsl"
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    status_url,
                    headers={"User-Agent": _USER_AGENT},
                    timeout=5,
                ) as resp:
                    data = await resp.json(content_type=None)

            sources = data.get("icestats", {}).get("source", [])
            if isinstance(sources, dict):
                sources = [sources]

            for source in sources:
                listen_url = source.get("listenurl", "")
                if path in listen_url:
                    display_title = source.get("display-title") or source.get("title")
                    if display_title:
                        return display_title
            return None
        except Exception as exc:
            dbg("Nightride metadata fetch failed: %s", exc)
            return None

    # ── Shared helpers ─────────────────────────────────────────────────────

    async def _ensure_player(self, interaction: discord.Interaction) -> wavelink.Player:
        """Ensure a wavelink player is connected for this guild."""
        voice_channel = interaction.user.voice.channel
        state = player.get_state(interaction.guild.id)
        state["voice_channel"] = voice_channel
        state["text_channel"] = interaction.channel
        state["persist_enabled"] = True

        player_obj = state.get("player")
        if not player_obj or not player_obj.connected:
            player_obj = await player.connect_player(voice_channel)
            state["player"] = player_obj
        return player_obj

    async def _get_json(self, url: str) -> dict:
        """GET a JSON endpoint with a browser User-Agent. Returns parsed JSON."""
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers={"User-Agent": _USER_AGENT}, timeout=15) as resp:
                resp.raise_for_status()
                return await resp.json(content_type=None)

    async def _search_city(self, city: str) -> dict | None:
        """Query iHeart for a city and return a random STATION result (or None)."""
        from urllib.parse import quote

        url = f"{_IHEART_SEARCH}?keywords={quote(city)}"
        data = await self._get_json(url)
        results = data.get("results") or []
        stations = [r for r in results if r.get("typeName") == "STATION"]
        if not stations:
            return None
        return random.choice(stations)

    async def _resolve_station(self, station_id) -> dict | None:
        """Resolve a station id to its stream URLs via liveStations. Returns hit or None."""
        url = f"{_IHEART_LIVE}/{station_id}"
        data = await self._get_json(url)
        hits = data.get("hits") or []
        return hits[0] if hits else None

    def _station_stream_url(self, hit: dict) -> str | None:
        """Pick the best stream URL from a liveStations hit (HTTPS preferred)."""
        streams = hit.get("streams") or {}
        return (
            streams.get("secure_hls_stream")
            or streams.get("hls_stream")
            or streams.get("secure_shoutcast_stream")
            or streams.get("shoutcast_stream")
        )

    # ── Playback (continuous stream — play directly, never enqueue) ───────

    async def _play_stream(
        self,
        interaction: discord.Interaction,
        stream_url: str,
        title: str,
    ) -> None:
        """Resolve and play a continuous stream. Does NOT enqueue a queue track."""
        await interaction.response.defer()
        await interaction.followup.send("🔄 Tuning in…", ephemeral=True)
        try:
            await self._ensure_player(interaction)
            state = player.get_state(interaction.guild.id)
            player_obj = state["player"]

            result = await Playable.search(stream_url, source=TrackSource.YouTube)
            track = result[0] if isinstance(result, list) and result else result
            if track is None:
                await interaction.followup.send("Could not resolve that stream URL.")
                return

            # Continuous radio: play directly (the stream never "ends"), and
            # clear any queue so the radio is the active playback.
            await player_obj.play(track)
            state["current"] = player._track_entry(track, "radio")

            # Track the radio session for now-playing lookups
            self._radio_sessions[interaction.guild.id] = {
                "url": stream_url,
                "title": title,
                "text_channel": interaction.channel,
            }

            # Try to fetch current ICY metadata (what song is playing)
            now_playing = await self._fetch_icy_metadata(stream_url)

            embed = discord.Embed(
                title="📻 HelloDJ — Radio",
                description=f"Now streaming **{title}**",
                colour=discord.Colour.blurple(),
            )
            embed.add_field(name="Station", value=title, inline=True)
            embed.add_field(name="Status", value="Live", inline=True)
            if now_playing:
                embed.add_field(name="Now Playing", value=now_playing, inline=False)
            await interaction.followup.send(embed=embed)

        except (wavelink.LavalinkLoadException, wavelink.NodeException) as exc:
            log.error("Radio stream failed (%s): %s", type(exc).__name__, exc)
            await interaction.followup.send(
                "Could not play that station — the music source failed or was unavailable."
            )
        except Exception as exc:
            log.error("Radio stream failed (%s): %s", type(exc).__name__, exc)
            await interaction.followup.send(f"Could not play that station: {exc}")

    # ── /radio city ────────────────────────────────────────────────────────

    @radio_group.command(name="city", description="Pick a random radio station in a city")
    @app_commands.describe(city="City to search, e.g. 'los angeles' or 'new york'")
    async def radio_city(self, interaction: discord.Interaction, city: str):
        if not interaction.user.voice:
            await interaction.response.send_message("You need to be in a voice channel first.")
            return
        try:
            station = await self._search_city(city)
            if station is None:
                await interaction.response.send_message(
                    f"No live radio stations found for **{city}**."
                )
                return
            sid = station.get("id")
            hit = await self._resolve_station(sid) if sid else None
            stream_url = self._station_stream_url(hit) if hit else None
            if not stream_url:
                await interaction.response.send_message(
                    f"Found **{station.get('name')}** but no playable stream was available."
                )
                return
            await self._play_stream(interaction, stream_url, station.get("name") or city)
        except Exception as exc:
            log.error("Radio city %r failed: %s", city, exc)
            await interaction.response.send_message(f"Could not look up that city: {exc}")

    # ── /radio direct ──────────────────────────────────────────────────────

    @radio_group.command(name="direct", description="Play a radio station by ID or stream code")
    @app_commands.describe(
        station="iHeart station ID (number) or a stream code string, e.g. 'KNEKFMAAC'"
    )
    async def radio_direct(self, interaction: discord.Interaction, station: str):
        if not interaction.user.voice:
            await interaction.response.send_message("You need to be in a voice channel first.")
            return
        station = station.strip()
        try:
            # Numeric ID → resolve via liveStations API.
            if station.isdigit():
                hit = await self._resolve_station(station)
                if hit is None:
                    await interaction.response.send_message(
                        f"No station found with id **{station}**."
                    )
                    return
                stream_url = self._station_stream_url(hit)
                title = hit.get("name") or station
                if not stream_url:
                    await interaction.response.send_message(
                        f"Found **{title}** but no playable stream was available."
                    )
                    return
                await self._play_stream(interaction, stream_url, title)
                return

            # Otherwise treat it as a streamtheworld stream code.
            stream_url = _STREAMTHEWORLD_REDIRECT.format(code=station)
            await self._play_stream(interaction, stream_url, station)

        except Exception as exc:
            log.error("Radio direct %r failed: %s", station, exc)
            await interaction.response.send_message(f"Could not play that station: {exc}")

    # ── /radio preset ──────────────────────────────────────────────────────

    @radio_group.command(name="preset", description="Play a curated internet-radio preset")
    @app_commands.describe(
        preset="Preset name: thelot, nightwave, nightride, chillsynth, darksynth, or 313fm",
    )
    async def radio_preset(self, interaction: discord.Interaction, preset: str):
        if not interaction.user.voice:
            await interaction.response.send_message("You need to be in a voice channel first.")
            return
        key = preset.strip().lower()
        entry = PRESETS.get(key)
        if entry is None:
            names = ", ".join(sorted(PRESETS))
            await interaction.response.send_message(
                f"Unknown preset **{preset}**. Available: {names}."
            )
            return
        await self._play_stream(interaction, entry["url"], entry["name"])

    # ── /radio nowplaying ──────────────────────────────────────────────────

    @radio_group.command(name="nowplaying", description="Show what song is currently playing on the radio")
    async def radio_nowplaying(self, interaction: discord.Interaction):
        session = self._radio_sessions.get(interaction.guild.id)
        if not session:
            await interaction.response.send_message(
                "No radio station is currently playing in this server.", ephemeral=True
            )
            return

        await interaction.response.defer()
        now_playing = await self._fetch_icy_metadata(session["url"])

        embed = discord.Embed(
            title="📻 Now Playing — Radio",
            colour=discord.Colour.blurple(),
        )
        embed.add_field(name="Station", value=session["title"], inline=True)
        if now_playing:
            embed.add_field(name="Track", value=now_playing, inline=False)
        else:
            embed.add_field(
                name="Track",
                value="*Metadata unavailable for this station*",
                inline=False,
            )
        await interaction.followup.send(embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Radio(bot))

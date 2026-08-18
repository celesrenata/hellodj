"""HelloDJ — Stream cog: Tidal music-video streaming to the text channel.

What this command does
----------------------
``/stream <query>`` searches Tidal for a track. When the track has an official
music video, HelloDJ downloads it and posts it as an **embedded video message**
in the current text channel (Discord auto-embeds video attachments), and also
queues the audio for voice playback. When the track has no video, HelloDJ falls
back to audio playback and shows a YouTube link.

DISCORD API LIMITATION — READ THIS
----------------------------------
Discord does **NOT** support a bot "screensharing" video into a voice channel.
The Discord API exposes no endpoint for a bot to broadcast video into voice:
streaming/GoLive is a user-guild feature, and bots can only set
``self_mute``/``self_deafen`` voice state. The realistic way to deliver a music
video is therefore to **embed it in a text channel** (Discord auto-embeds video
links/attachments posted as messages), which is exactly what this cog does.
There is no code path that pretends to screenshare into voice.

Video delivery strategy
-----------------------
1. Tidal's stream URL is often HLS (``.m3u8``) or a protected CDN manifest, so
   we prefer ``yt-dlp`` (a pure-Python package, NixOS-clean) to download a
   Discord-compatible ``.mp4``.
2. If ``yt-dlp`` is unavailable and the URL is a direct media file, we fall back
   to an ``aiohttp`` streamed download.
3. If the resulting file exceeds Discord's attachment limit (8 MB free /
   25 MB boosted server), we post the video as a **link** instead — Discord
   auto-embeds many video links in text channels.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import time

from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

import player
import tidal

log = logging.getLogger(__name__)

# ── limits & paths ─────────────────────────────────────────

# Discord attachment limit: 8 MiB on free servers, 25 MiB on boosted servers.
# Configurable via env; default to 8 MiB (the conservative floor).
ATTACHMENT_LIMIT = int(os.getenv("DISCORD_ATTACHMENT_LIMIT", str(8 * 1024 * 1024)))
VIDEO_DIR = "data/videos"

# Extensions Discord will render as an embedded video when sent as an attachment.
_EMBEDDABLE_VIDEO_EXT = {".mp4", ".webm", ".mov", ".m4v"}

# URL patterns that indicate a directly-downloadable media file (vs an HLS/protected manifest).
_DIRECT_MEDIA_RE = re.compile(r"\.(mp4|m4v|webm|mov)(\?|$)", re.IGNORECASE)


class StreamSelectView(discord.ui.View):
    """Dropdown of Tidal search results for the /stream command."""

    def __init__(self, results: list[dict], invoker_id: int, on_pick):
        super().__init__(timeout=60)
        self.results = results
        self.invoker_id = invoker_id
        self.on_pick = on_pick
        self.message: discord.Message | None = None

        options = []
        for i, info in enumerate(results):
            title = (info.get("title") or "Unknown")[:100]
            duration = info.get("duration") or 0
            total_secs = int(duration) // 1000
            mins, secs = divmod(total_secs, 60)
            artist = (info.get("artist") or "")[:50]
            desc = f"{artist} • {mins}:{secs:02d}" if artist else f"{mins}:{secs:02d}"
            options.append(discord.SelectOption(label=title, value=str(i), description=desc[:100]))

        select = discord.ui.Select(placeholder="Choose a Tidal track…", options=options)
        select.callback = self._on_select
        self.add_item(select)

        cancel = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary)
        cancel.callback = self._on_cancel
        self.add_item(cancel)

    async def _on_cancel(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message("Only the person who searched can cancel.", ephemeral=True)
            return
        await interaction.response.edit_message(content="Stream search cancelled.", view=None)
        self.stop()

    async def _on_select(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message("Only the person who searched can pick a track.", ephemeral=True)
            return
        idx = int(interaction.data["values"][0])
        info = self.results[idx]
        await self.on_pick(info, interaction)
        self.stop()

    async def on_timeout(self) -> None:
        if self.message:
            try:
                await self.message.edit(content="Stream search timed out.", view=None)
            except discord.HTTPException:
                pass


class Stream(commands.Cog):
    """Tidal music-video streaming to the text channel."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._client = tidal.get_client()

    # ── helpers ────────────────────────────────────────────

    def _sanitize_name(self, raw: str) -> str:
        """Sanitize a title into a safe filename stem."""
        name = re.sub(r"[^A-Za-z0-9._-]", "_", raw or "video")
        return name.strip("_") or "video"

    def _ytdlp_available(self) -> bool:
        return shutil.which("yt-dlp") is not None or _import_ytdlp() is not None

    # ── video download ─────────────────────────────────────

    async def _download_video(self, url: str, title: str) -> Path | None:
        """Download ``url`` into ``VIDEO_DIR`` and return the local Path, or None.

        Prefers yt-dlp (handles HLS manifests); falls back to a direct aiohttp
        stream only when the URL looks like a directly-downloadable media file.
        Runs the blocking download in an executor. Returns None on any failure
        (the caller falls back to posting a link).
        """
        os.makedirs(VIDEO_DIR, exist_ok=True)
        stem = self._sanitize_name(title)
        dest = Path(VIDEO_DIR) / f"{stem}-{int(time.time())}.mp4"

        ytdlp = _import_ytdlp()
        if ytdlp is not None:
            loop = asyncio.get_running_loop()
            try:
                ok = await loop.run_in_executor(None, lambda: _ytdlp_download_blocking(url, str(dest)))
                if ok:
                    log.info("stream: yt-dlp downloaded %s -> %s (%d bytes)",
                             url, dest, dest.stat().st_size)
                    return dest
                log.warning("stream: yt-dlp download failed for %s", url)
            except Exception as exc:
                log.warning("stream: yt-dlp download error for %s: %s", url, exc)

        if _DIRECT_MEDIA_RE.search(url):
            try:
                dest = await self._aiohttp_download(url, dest)
                if dest is not None:
                    log.info("stream: direct download %s -> %s (%d bytes)",
                             url, dest, dest.stat().st_size)
                    return dest
            except Exception as exc:
                log.warning("stream: direct download failed for %s: %s", url, exc)

        return None

    async def _aiohttp_download(self, url: str, dest: Path) -> Path | None:
        """Stream ``url`` to ``dest`` with aiohttp (chunked, avoids loading to RAM)."""
        client = tidal.get_client()
        session = client._session_or_new()
        try:
            async with session.get(
                url,
                headers=client._headers(),
                timeout=aiohttp_client_timeout(),
                allow_redirects=True,
            ) as resp:
                if resp.status != 200:
                    log.warning("stream: direct download status=%s", resp.status)
                    return None
                total = 0
                with open(dest, "wb") as fh:
                    async for chunk in resp.content.iter_chunked(256 * 1024):
                        fh.write(chunk)
                        total += len(chunk)
                        if total > ATTACHMENT_LIMIT:
                            log.warning("stream: download exceeds attachment limit (%d)", total)
                            return None
        except Exception as exc:
            log.warning("stream: direct download error for %s: %s", url, exc)
            return None
        if dest.stat().st_size == 0:
            return None
        return dest

    async def _send_video(self, channel: discord.TextChannel, path: Path, title: str, fallback_url: str) -> None:
        """Deliver the downloaded video to the text channel.

        If the file fits under Discord's attachment limit AND has an embeddable
        extension, send it as an attachment (Discord renders embedded video).
        Otherwise, post ``fallback_url`` as a link so Discord can auto-embed it.
        """
        size = path.stat().st_size
        ext = path.suffix.lower()

        if size <= ATTACHMENT_LIMIT and ext in _EMBEDDABLE_VIDEO_EXT:
            try:
                await channel.send(
                    content=f"🎬 **{title}** — official Tidal music video (embedded)",
                    file=discord.File(str(path), filename=f"{path.stem}{ext}"),
                )
                log.info("stream: sent embedded video %s (%d bytes)", path, size)
                return
            except discord.HTTPException as exc:
                log.warning("stream: could not send video attachment: %s", exc)

        # Attachment too large or non-embeddable → post the link instead.
        log.info("stream: video too large/non-embeddable (%d bytes) — posting link", size)
        await channel.send(
            content=f"🎬 **{title}** — official Tidal music video\n{fallback_url}",
            suppress_embeds=False,
        )

    # ── audio fallback ─────────────────────────────────────

    async def _queue_audio(self, interaction: discord.Interaction, title: str) -> bool:
        """Queue and play audio via the shared wavelink engine (YouTube source).

        Tidal audio is handled by Lavalink via the ``tdsearch:`` prefix with a
        YouTube fallback (see player._resolve_and_play). Here we resolve the
        title through the shared queue so playback uses the exact same path as
        ``/play``. Returns True when something was queued.
        """
        try:
            # Ensure the bot is connected to voice (mirrors music.py:_ensure_player).
            state = player.get_state(interaction.guild.id)
            state["voice_channel"] = interaction.user.voice.channel
            state["text_channel"] = interaction.channel
            state["persist_enabled"] = True

            player_obj = state.get("player")
            if not player_obj or not player_obj.connected:
                player_obj = await player.connect_player(interaction.user.voice.channel)
                state["player"] = player_obj

            await player.enqueue_and_start(
                interaction.guild,
                interaction.channel,
                [{"title": title, "webpage_url": None}],
                replace=False,
            )
            return True
        except Exception as exc:
            log.error("stream: audio fallback queue failed: %s", exc)
            return False

    async def _youtube_link(self, query: str) -> str | None:
        """Best-effort YouTube link for the fallback message (None on failure)."""
        try:
            import wavelink
            tracks = await wavelink.Playable.search(query, source=wavelink.TrackSource.YouTube)
            first = tracks[0] if isinstance(tracks, list) else tracks
            return getattr(first, "uri", None) or getattr(first, "url", None)
        except Exception as exc:
            log.warning("stream: could not fetch YouTube link: %s", exc)
            return None

    # ── core flow ──────────────────────────────────────────

    async def _stream_track(self, interaction: discord.Interaction, info: dict) -> None:
        """Stream one resolved Tidal track: video to channel + audio to voice."""
        title = info.get("title") or "Unknown"
        track_id = info.get("id")
        log.info("stream: streaming track id=%s title=%r", track_id, title)

        try:
            video_url = await self._client.get_video_url(track_id)
        except Exception as exc:
            log.warning("stream: video lookup failed for %r: %s", title, exc)
            video_url = None

        if video_url:
            try:
                path = await self._download_video(video_url, title)
                if path is not None:
                    await self._send_video(interaction.channel, path, title, video_url)
                    # Queue the audio too, so the voice channel gets the song.
                    await self._queue_audio(interaction, title)
                    await interaction.followup.send(
                        f"🎬 Streamed **{title}** to the channel and queued the audio in voice."
                    )
                    return
            except Exception as exc:
                log.error("stream: video delivery failed for %r: %s", title, exc)

            # Download failed → post the direct video link (auto-embeds) + audio.
            await interaction.channel.send(
                content=f"🎬 **{title}** — official Tidal music video\n{video_url}",
                suppress_embeds=False,
            )
            await self._queue_audio(interaction, title)
            await interaction.followup.send(
                f"🎬 Posted the **{title}** video link and queued the audio in voice."
            )
            return

        # No video available → audio fallback + YouTube link.
        yt = await self._youtube_link(title)
        await self._queue_audio(interaction, title)
        extra = f"\n▶️ YouTube: {yt}" if yt else ""
        await interaction.followup.send(
            f"🎵 **{title}** has no Tidal music video — playing the audio instead.{extra}"
        )

    # ── slash command ──────────────────────────────────────

    @app_commands.command(name="stream", description="Stream a Tidal music video to this channel (audio also plays in voice)")
    @app_commands.describe(query="Song name or artist (searches Tidal)")
    async def stream(self, interaction: discord.Interaction, query: str):
        if not self._client.configured:
            await interaction.response.send_message(
                "Tidal streaming is not configured. Add `TD_CLIENT_ID` and `TD_CLIENT_SECRET` "
                "to the environment (see bot/.env.example).",
                ephemeral=True,
            )
            return
        if not interaction.user.voice:
            await interaction.response.send_message(
                "You need to be in a voice channel first (audio also plays in voice)."
            )
            return
        await interaction.response.defer()

        try:
            results = await self._client.search(query, limit=5)
        except Exception as exc:
            log.error("stream: Tidal search failed: %s", exc)
            await interaction.followup.send(f"Tidal search failed: {exc}", ephemeral=True)
            return

        if not results:
            await interaction.followup.send(f"No Tidal results for **{query}**.")
            return

        async def on_pick(info: dict, picker: discord.Interaction):
            await self._stream_track(picker, info)

        if len(results) > 1:
            view = StreamSelectView(results, interaction.user.id, on_pick)
            msg = await interaction.followup.send("Select a Tidal track:", view=view)
            view.message = msg
            return

        await self._stream_track(interaction, results[0])


# ── module-level helpers (blocking, run in executor) ───────

def _import_ytdlp():
    """Lazily import yt_dlp; returns None when unavailable."""
    try:
        import yt_dlp  # noqa: WPS433 (optional dependency)
        return yt_dlp
    except ImportError:
        return None


def _ytdlp_download_blocking(url: str, dest: str) -> bool:
    """Blocking yt-dlp download (runs in executor). Returns True on success."""
    ytdlp = _import_ytdlp()
    if ytdlp is None:
        return False
    try:
        opts = {
            "outtmpl": dest,
            "format": "mp4",
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
        }
        with ytdlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
        return info is not None and os.path.exists(dest) and os.path.getsize(dest) > 0
    except Exception as exc:
        log.warning("stream: yt-dlp error for %s: %s", url, exc)
        return False


def aiohttp_client_timeout():
    import aiohttp
    return aiohttp.ClientTimeout(total=120)


async def setup(bot: commands.Bot):
    await bot.add_cog(Stream(bot))

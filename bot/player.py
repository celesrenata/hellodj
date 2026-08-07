"""Shared playback engine: queue orchestrator bridging wavelink events to persistence & UI.

Uses wavelink 3.5+ directly (no dismusic dependency).
"""

import asyncio
import logging
import random
import time

import discord
import wavelink
from wavelink import Playable, TrackSource

import session

log = logging.getLogger(__name__)

# Per-guild state
guild_state: dict[int, dict] = {}

# ── helpers ───────────────────────────────────────────────

def _to_entry(info: dict) -> dict:
    """Lightweight ``{webpage_url, title}`` form for persistence."""
    return {
        "webpage_url": info.get("webpage_url") or info.get("url"),
        "title": info.get("title", "Unknown"),
    }


def _snapshot(state: dict) -> dict:
    current = state.get("current")
    voice_channel = state.get("voice_channel")
    text_channel = state.get("text_channel")
    return {
        "voice_channel_id": voice_channel.id if voice_channel else None,
        "text_channel_id": text_channel.id if text_channel else None,
        "current": _to_entry(current) if current else None,
        "queue": [_to_entry(item) for item in state["queue"]],
    }


# ── state access ───────────────────────────────────────────

def get_state(guild_id: int) -> dict:
    if guild_id not in guild_state:
        guild_state[guild_id] = {
            "queue": [],
            "voice_channel": None,
            "text_channel": None,
            "current": None,
            "persist_enabled": True,
            "alone_task": None,
            "player": None,               # wavelink.Player instance
            "repeat_mode": "off",
            "source_provider": "youtube",
            "autoplay_enabled": False,
            "autoplay_genres": [],
            "filters": {},
            "now_playing_msg": None,
            "now_playing_task": None,
        }
    return guild_state[guild_id]


def get_player(guild_id: int) -> wavelink.Player | None:
    return get_state(guild_id).get("player")


# ── persistence ────────────────────────────────────────────

def persist(guild_id: int) -> None:
    state = get_state(guild_id)
    if not state.get("persist_enabled", True):
        return
    asyncio.ensure_future(
        session.save_guild(guild_id, auto_resume=True, **_snapshot(state))
    )


async def park(guild_id: int) -> None:
    state = get_state(guild_id)
    state["persist_enabled"] = False
    await session.save_guild(guild_id, auto_resume=False, **_snapshot(state))


async def discard(guild_id: int) -> None:
    get_state(guild_id)["persist_enabled"] = False
    await session.clear(guild_id)


# ── queue operations ───────────────────────────────────────

def clear_queue(state: dict) -> None:
    state["queue"].clear()


def shuffle_queue(state: dict) -> None:
    random.shuffle(state["queue"])


def remove_from_queue(state: dict, index: int) -> dict | None:
    if 0 <= index < len(state["queue"]):
        return state["queue"].pop(index)
    return None


def move_in_queue(state: dict, from_index: int, to_index: int) -> bool:
    n = len(state["queue"])
    if 0 <= from_index < n and 0 <= to_index < n:
        track = state["queue"].pop(from_index)
        state["queue"].insert(to_index, track)
        return True
    return False


def get_queue_page(state: dict, page: int = 0, page_size: int = 10) -> list[dict]:
    start = page * page_size
    end = start + page_size
    return state["queue"][start:end]


def set_repeat(state: dict, mode: str) -> None:
    assert mode in ("off", "single", "queue")
    state["repeat_mode"] = mode


async def add_track(state: dict, guild_id: int, entry: dict) -> None:
    state["queue"].append(entry)
    persist(guild_id)


async def enqueue_and_start(
    guild: discord.Guild,
    text_channel: discord.TextChannel,
    tracks: list[dict],
    *,
    replace: bool = False,
    shuffle: bool = False,
) -> int:
    state = get_state(guild.id)
    state["text_channel"] = text_channel
    state["persist_enabled"] = True

    if replace:
        clear_queue(state)

    tracks = list(tracks)
    if shuffle:
        random.shuffle(tracks)

    for track in tracks:
        state["queue"].append(track)

    # Trigger playback if a player exists
    player = get_player(guild.id)
    if player and player.connected:
        if not player.playing and not player.paused:
            await _play_next_from_queue(guild.id)

    persist(guild.id)
    return len(tracks)


# ── event-driven playback ─────────────────────────────────

async def _play_next_from_queue(guild_id: int) -> None:
    state = get_state(guild_id)
    player = state.get("player")
    if not player or not player.connected:
        return

    # Repeat mode
    if state["repeat_mode"] == "single" and state.get("current"):
        state["queue"].insert(0, state["current"])
    elif state["repeat_mode"] == "queue" and state.get("current"):
        state["queue"].append(state["current"])

    # Pop next
    if state["queue"]:
        next_entry = state["queue"].pop(0)
        state["current"] = next_entry
        persist(guild_id)
        await _resolve_and_play(player, guild_id, next_entry)
    else:
        await _on_queue_empty(guild_id)


async def _resolve_and_play(player: wavelink.Player, guild_id: int, entry: dict) -> None:
    state = get_state(guild_id)
    url = entry.get("webpage_url") or entry.get("url")
    title = entry.get("title", "Unknown")

    try:
        # Search using Playable.search with the configured source
        source_map = {
            "youtube": TrackSource.YouTube,
            "youtube_music": TrackSource.YouTubeMusic,
            "soundcloud": TrackSource.SoundCloud,
            "spotify": TrackSource.Spotify,
        }
        source = source_map.get(state.get("source_provider", "youtube"), TrackSource.YouTube)

        # For URLs, try to parse directly; for search, use Playable.search
        if url and ("http://" in url or "https://" in url):
            # Direct URL — try to get as a Playable
            tracks = await Playable.search(url, source=source)
        else:
            tracks = await Playable.search(title, source=source)

        if not tracks:
            # Fallback to YouTube
            tracks = await Playable.search(title or url, source=TrackSource.YouTube)

        if not tracks:
            log.warning("No track found for %s in guild %s", title, guild_id)
            await _play_next_from_queue(guild_id)
            return

        track = tracks[0] if isinstance(tracks, list) else tracks
        await player.play(track)

    except Exception as exc:
        log.error("Failed to resolve/play track %s: %s", title, exc)
        await _play_next_from_queue(guild_id)


async def _on_queue_empty(guild_id: int) -> None:
    state = get_state(guild_id)
    if not state.get("autoplay_enabled"):
        return

    genres = state.get("autoplay_genres", [])
    current = state.get("current")

    query_parts = []
    if genres:
        query_parts.append(" ".join(genres))
    elif current:
        query_parts.append(current.get("title", ""))
    query = query_parts[0] if query_parts else "popular music"

    try:
        tracks = await Playable.search(query, source=TrackSource.YouTubeMusic)
        if not tracks:
            tracks = await Playable.search(query, source=TrackSource.YouTube)

        if tracks:
            for t in tracks[:3]:
                entry = {
                    "webpage_url": str(t.url),
                    "title": t.name,
                    "author": t.author,
                    "duration": t.duration,
                }
                state["queue"].append(entry)
            persist(guild_id)
            await _play_next_from_queue(guild_id)
    except Exception as exc:
        log.warning("Autoplay search failed: %s", exc)


# ── wavelink event handlers ────────────────────────────────

async def on_track_start(guild_id: int, player: wavelink.Player, track: wavelink.Playable) -> None:
    state = get_state(guild_id)
    state["player"] = player

    info = {
        "webpage_url": str(track.url) if track.url else None,
        "title": track.name or "Unknown",
        "author": track.author or "",
        "duration": track.duration or 0,
    }
    state["current"] = info
    persist(guild_id)

    # Start progress bar updater
    state["now_playing_task"] = asyncio.ensure_future(
        _now_playing_updater(guild_id, player, track)
    )

    await _send_now_playing(guild_id, player, track)


async def on_track_end(guild_id: int, player: wavelink.Player, track: wavelink.Playable, reason: str) -> None:
    state = get_state(guild_id)

    np_task = state.get("now_playing_task")
    if np_task and not np_task.done():
        np_task.cancel()
    state["now_playing_task"] = None

    await _play_next_from_queue(guild_id)


async def on_track_exception(guild_id: int, player: wavelink.Player, track: wavelink.Playable, exc: Exception) -> None:
    log.warning("Track exception in guild %s for %s: %s", guild_id, track, exc)
    await on_track_end(guild_id, player, track, "exception")


# ── now-playing embed ──────────────────────────────────────

async def _send_now_playing(guild_id: int, player: wavelink.Player, track: wavelink.Playable) -> None:
    state = get_state(guild_id)
    channel = state.get("text_channel")
    if not channel:
        return

    embed = _build_now_playing_embed(track)
    view = NowPlayingView(guild_id)

    msg = state.get("now_playing_msg")
    if msg:
        try:
            await msg.edit(embed=embed, view=view)
            return
        except discord.NotFound:
            pass

    msg = await channel.send(embed=embed, view=view)
    state["now_playing_msg"] = msg


def _build_now_playing_embed(track: wavelink.Playable) -> discord.Embed:
    title = track.name or "Unknown"
    author = track.author or "Unknown Artist"
    duration = track.duration or 0
    mins, secs = divmod(duration // 1000, 60) if duration > 1000 else divmod(duration, 60)

    embed = discord.Embed(title="🎵 Now Playing", colour=discord.Colour.blurple())
    embed.add_field(name="Song", value=title, inline=True)
    embed.add_field(name="Artist", value=author, inline=True)
    embed.add_field(name="Duration", value=f"{mins}:{secs:02d}", inline=True)
    embed.set_footer(text="Use the buttons below to control playback")
    return embed


async def _now_playing_updater(guild_id: int, player: wavelink.Player, track: wavelink.Playable) -> None:
    state = get_state(guild_id)
    try:
        while player.playing or player.paused:
            await asyncio.sleep(5)
            if not player.playing and not player.paused:
                break

            msg = state.get("now_playing_msg")
            if not msg:
                continue

            position = player.position
            duration = track.duration
            if duration <= 0:
                continue

            bar = _progress_bar(position, duration)
            pos_m, pos_s = divmod(position // 1000, 60)
            dur_m, dur_s = divmod(duration // 1000, 60)

            embed = msg.embeds[0] if msg.embeds else discord.Embed(title="🎵 Now Playing", colour=discord.Colour.blurple())
            embed.description = f"`{bar}`  {pos_m}:{pos_s:02d} / {dur_m}:{dur_s:02d}"
            try:
                await msg.edit(embed=embed)
            except discord.HTTPException:
                pass
    except asyncio.CancelledError:
        pass


def _progress_bar(position: int, duration: int, width: int = 20) -> str:
    if duration <= 0:
        return "?" * width
    filled = int(position / duration * width)
    filled = max(0, min(filled, width))
    return "█" * filled + "░" * (width - filled)


# ── now-playing button view ────────────────────────────────

class NowPlayingView(discord.ui.View):
    """Persistent buttons attached to the now-playing embed."""

    def __init__(self, guild_id: int):
        super().__init__(timeout=300)
        self.guild_id = guild_id

    @discord.ui.button(label="⏮", style=discord.ButtonStyle.secondary, custom_id="np_prev")
    async def prev_track(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await self._skip_to_previous(interaction)

    @discord.ui.button(label="⏸", style=discord.ButtonStyle.primary, custom_id="np_toggle")
    async def pause_resume(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await self._toggle_pause(interaction)

    @discord.ui.button(label="⏭", style=discord.ButtonStyle.secondary, custom_id="np_next")
    async def next_track(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await self._skip_next(interaction)

    @discord.ui.button(label="🔄", style=discord.ButtonStyle.success, custom_id="np_repeat")
    async def repeat_toggle(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await self._cycle_repeat(interaction)

    @discord.ui.button(label="🔀", style=discord.ButtonStyle.success, custom_id="np_shuffle")
    async def shuffle_tracks(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await self._shuffle(interaction)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # Allow any guild member to use buttons
        return True

    async def _skip_to_previous(self, interaction: discord.Interaction) -> None:
        state = get_state(self.guild_id)
        if state.get("current"):
            state["queue"].insert(0, state["current"])
        if len(state["queue"]) >= 2:
            prev = state["queue"].pop(-1)
            state["queue"].insert(0, prev)
        player = state.get("player")
        if player:
            await player.stop()
        await interaction.response.defer()

    async def _toggle_pause(self, interaction: discord.Interaction) -> None:
        player = get_player(self.guild_id)
        if player:
            if player.paused:
                await player.resume()
            elif player.playing:
                await player.pause()
        await interaction.response.defer()

    async def _skip_next(self, interaction: discord.Interaction) -> None:
        player = get_player(self.guild_id)
        if player:
            await player.stop()
        await interaction.response.defer()

    async def _cycle_repeat(self, interaction: discord.Interaction) -> None:
        state = get_state(self.guild_id)
        modes = ["off", "single", "queue"]
        current = state["repeat_mode"]
        next_mode = modes[(modes.index(current) + 1) % len(modes)] if current in modes else "off"
        state["repeat_mode"] = next_mode
        await interaction.response.send_message(f"Repeat: **{next_mode}**", ephemeral=True)

    async def _shuffle(self, interaction: discord.Interaction) -> None:
        state = get_state(self.guild_id)
        shuffle_queue(state)
        persist(self.guild_id)
        await interaction.response.send_message("Queue shuffled.", ephemeral=True)

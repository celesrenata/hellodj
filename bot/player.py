"""HelloDJ — Shared playback engine: queue orchestrator bridging wavelink events to persistence & UI.

Uses wavelink 3.5+ directly (no dismusic dependency).
"""

import asyncio
import logging
import os
import random
import time

import discord
import wavelink
from wavelink import Playable, TrackSource

import session
from voice.hybrid_player import HybridPlayer

log = logging.getLogger(__name__)

# Per-guild state
guild_state: dict[int, dict] = {}

# Per-guild connect locks: serialize voice-channel connects so the
# wakeword/voice pipeline and /play cannot race and trigger
# `ClientException('Already connected to a voice channel.')`.
_connect_locks: dict[int, asyncio.Lock] = {}

# ── raw-event handshake instrumentation (diagnosis only) ────
# discord.py only forwards VOICE_STATE_UPDATE / VOICE_SERVER_UPDATE to the
# wavelink player when a registered voice client exists in
# ConnectionState._voice_clients[guild_id] at arrival time (state.py
# parse_voice_state_update / parse_voice_server_update). If the event lands in
# a registration-timing window, it is silently discarded and
# Player.on_voice_state_update / on_voice_server_update never populate
# session_id/token/endpoint. This hook logs, at INFO, whether the raw events
# arrive and whether _get_voice_client(guild.id) is non-None when they do, so
# the next live logs confirm recovery. Installed once per process.
_instrumented = False


def _install_voice_event_instrumentation(client: discord.Client) -> None:
    """Wrap the gateway voice-event parsers with INFO logging (one-time).

    Logs arrival of VOICE_STATE_UPDATE / VOICE_SERVER_UPDATE and whether a
    registered voice client exists for the guild at arrival time — the exact
    condition that gates forwarding to the wavelink player. Lightweight: two
    log lines per voice handshake, no per-event spam.
    """
    global _instrumented
    if _instrumented:
        return
    state = getattr(client, "_connection", None)
    if state is None or not hasattr(state, "parsers"):
        log.warning("voice event instrumentation skipped: no ConnectionState.parsers")
        return

    for name in ("VOICE_STATE_UPDATE", "VOICE_SERVER_UPDATE"):
        orig = state.parsers.get(name)
        if orig is None:
            continue

        def _wrapped(data, _orig=orig, _name=name):
            guild_id = data.get("guild_id")
            vc = None
            try:
                if guild_id is not None:
                    vc = state._get_voice_client(int(guild_id))
            except Exception:
                pass
            log.info(
                "raw-event %s arrived guild_id=%s _get_voice_client(guild_id)=%s "
                "(forwarded=%s) t=%.3fs",
                _name, guild_id, vc is not None, vc is not None,
                time.monotonic(),
            )
            return _orig(data)

        state.parsers[name] = _wrapped
        log.info("voice event instrumentation installed for %s", name)

    _instrumented = True

# ── helpers ───────────────────────────────────────────────

def _to_entry(info: dict) -> dict:
    """Lightweight ``{webpage_url, title}`` form for persistence."""
    return {
        "webpage_url": info.get("webpage_url") or info.get("url"),
        "title": info.get("title", "Unknown"),
    }


# ── wavelink 3.5.2 track → dict conversion ─────────────────
# Playable exposes `uri`/`title`/`author`/`length` — NOT `url`/`name`/`duration`.
# Playable.search returns wavelink.Search = list[Playable] | Playlist. This
# helper reads only the real 3.5.2 properties and logs the resolved type +
# attributes + provider at DEBUG so each provider setup (yt/ytm/sc/spotify/tidal)
# is diagnosable without guessing attribute names.

def _track_entry(track, provider: str | None = None) -> dict:
    """Convert a wavelink Playable (or Playlist) into a lightweight queue entry.

    Logs the object type and the resolved fields so a new provider can be
    validated from the bot log before it reaches the queue/embed layer.
    """
    uri = getattr(track, "uri", None)
    title = getattr(track, "title", None) or "Unknown"
    author = getattr(track, "author", None) or ""
    length = getattr(track, "length", None) or 0

    entry = {
        "webpage_url": str(uri) if uri else None,
        "title": title,
        "author": author,
        "duration": length,
    }

    log.debug(
        "track_entry type=%s provider=%s uri=%r title=%r author=%r length=%r",
        type(track).__name__, provider, uri, title, author, length,
    )
    return entry


def _search_entries(search, provider: str | None = None) -> list:
    """Convert a wavelink.Search result (list[Playable] | Playlist) to entries."""
    if isinstance(search, wavelink.Playlist):
        log.debug(
            "search_entries got Playlist name=%r n=%d provider=%s",
            search.name, len(search.tracks), provider,
        )
        return [_track_entry(t, provider) for t in search.tracks]
    # Plain list of Playable (or empty list)
    log.debug(
        "search_entries got list n=%d provider=%s first=%r",
        len(search), provider,
        [type(t).__name__ for t in search[:1]] if search else None,
    )
    return [_track_entry(t, provider) for t in search]


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


async def _handshake_complete(player) -> bool:
    """True when wavelink has finished the Discord voice handshake.

    Mirrors the checks wavelink uses: _connection_event set, and
    session_id/token/endpoint populated in _voice_state["voice"] (which only
    happens via on_voice_state_update / on_voice_server_update).
    """
    if player is None:
        return False
    ev = getattr(player, "_connection_event", None)
    if ev is not None and ev.is_set():
        return True
    vs = getattr(player, "_voice_state", {}) or {}
    voice = vs.get("voice", {}) or {}
    return bool(voice.get("session_id") and voice.get("token") and voice.get("endpoint"))


def _force_remove_stale(guild: discord.Guild | None, guild_id: int) -> None:
    """Force-remove a stale _voice_clients[guild_id] entry (the compounding symptom).

    discord.py registers the client in ConnectionState._voice_clients[guild_id]
    at abc.py:2149 BEFORE voice.connect() runs, and only removes it on
    `except asyncio.TimeoutError:` (abc.py:2153). wavelink raises
    ChannelTimeoutException — a plain Exception, not asyncio.TimeoutError — when
    the handshake times out, so discord.py's cleanup is skipped and a dead/stale
    player stays keyed in _voice_clients[guild_id]. The next /play then hits the
    "Already connected to a voice channel." guard at abc.py:2140 and never sends
    voice_state_update, so the handshake can never complete. Force-remove the
    stale registration so the next connect/re-issue can register a fresh player.
    """
    if not guild_id:
        return
    try:
        state = getattr(guild, "_state", None) if guild is not None else None
        if state is not None:
            state._remove_voice_client(guild_id)
            log.info(
                "connect_player: force-removed stale _voice_clients[%s] "
                "entry so a fresh connect can proceed",
                guild_id,
            )
    except Exception:
        log.exception(
            "connect_player: could not clear stale _voice_clients entry "
            "for guild_id=%s",
            guild_id,
        )


async def _reissue_voice_join(channel, cls, guild, guild_id: int) -> wavelink.Player | None:
    """Re-send opcode 4 on a fresh player so a dropped handshake self-recovers.

    The dropped-event race is a registration-timing bug in discord.py: the
    parse_voice_state_update / parse_voice_server_update handlers only forward
    the event to the player via `_get_voice_client(guild.id)` if a registered
    voice client exists at that instant. If the event arrives in a window where
    registration is incomplete, it is silently discarded and on_voice_*_update
    never populate session_id/token/endpoint. Simply re-issuing opcode 4 on the
    SAME player would be re-dropped if the guild object isn't cached; so here we
    construct a fresh player exactly like `channel.connect(cls=...)` does
    (abc.py:2143-2149: cls(client, channel) then _add_voice_client) so that
    registration is guaranteed BEFORE we send opcode 4 via guild.change_voice_state.

    Returns the player on success (handshake complete), None on timeout.
    """
    if guild is None:
        return None
    try:
        state = guild._state
        client = state._get_client()
        # Mirror abc.py connect(): construct the player, then register it.
        player = cls(client, channel)
        state._add_voice_client(guild_id, player)
        # Mirror wavelink.Player.connect(): register in the node then send opcode 4.
        if not getattr(player, "_guild", None):
            player._guild = guild
        player.node._players[guild.id] = player
        await guild.change_voice_state(channel=channel)
    except Exception:
        log.exception(
            "connect_player re-issue: could not construct/register player for "
            "guild_id=%s — dropping to timeout",
            guild_id,
        )
        return None

    # Wait a short window for the handshake to complete.
    start = time.monotonic()
    try:
        async with asyncio.timeout(3.0):
            while not _handshake_complete(player):
                await asyncio.sleep(0.25)
    except asyncio.TimeoutError:
        log.info(
            "connect_player re-issue handshake still incomplete after %.2fs "
            "(guild_id=%s) — dropping to timeout",
            time.monotonic() - start, guild_id,
        )
        return None
    log.info(
        "connect_player re-issue handshake COMPLETE after %.2fs (guild_id=%s)",
        time.monotonic() - start, guild_id,
    )
    return player


async def connect_player(channel: discord.abc.Connectable) -> wavelink.Player:
    """Connect a player to a voice channel.

    Prefers :class:`voice.hybrid_player.HybridPlayer` (a wavelink Player that
    also supports ``discord.ext.voice_recv``) so the voice-activation pipeline
    can receive incoming Opus frames via ``listen()``. Falls back to a plain
    ``wavelink.Player`` when voice_recv is unavailable.

    Implements a robust handshake that self-recovers from dropped events:
    instead of a single 30s connect that fails once, it retries with short
    per-attempt windows and, when the handshake has not completed, force-removes
    the stale registration and re-issues opcode 4 on a freshly registered player
    (see _reissue_voice_join). The overall budget stays close to the existing
    30s user-facing timeout; only the failure mode is made recoverable.
    """
    guild_id = getattr(getattr(channel, "guild", None), "id", None)
    if guild_id is None:
        # DM channels have no guild; use a shared lock under a sentinel key.
        guild_id = 0
    lock = _connect_locks.setdefault(guild_id, asyncio.Lock())
    async with lock:
        # Install the raw-event handshake instrumentation once so the next live
        # logs confirm whether VOICE_STATE_UPDATE / VOICE_SERVER_UPDATE arrive
        # and whether _get_voice_client(guild.id) is non-None at arrival time.
        guild0 = getattr(channel, "guild", None)
        if guild0 is not None:
            try:
                _install_voice_event_instrumentation(guild0._state._get_client())
            except Exception:
                log.exception("connect_player: could not install voice event instrumentation")

        timeout = float(os.getenv("VOICE_CONNECT_TIMEOUT", "30.0"))
        log.info(
            "connect_player: connecting to voice channel %s (guild_id=%s) "
            "timeout=%ss hybrid=%s",
            channel, guild_id, timeout, HybridPlayer is not None,
        )
        guild = getattr(channel, "guild", None)
        cls = HybridPlayer if HybridPlayer is not None else wavelink.Player
        # Short per-attempt window; the outer loop enforces the overall ~30s budget.
        # 10s (instead of 5s) gives the first join enough room to complete the
        # handshake, so the dropped-event recovery path no longer fires on every
        # routine first connect. Still capped by the overall budget.
        attempt_timeout = min(10.0, timeout / 3.0)
        deadline = time.monotonic() + timeout
        last_exc: Exception | None = None
        attempts = 0

        while time.monotonic() < deadline:
            attempts += 1
            # ── stale voice-client cleanup (preserved) ──────────────
            # A prior re-issue registers a fresh player in _voice_clients; clear
            # any stale entry BEFORE channel.connect() so the next attempt can
            # register a new player instead of hitting the "Already connected to
            # a voice channel." guard (abc.py:2140).
            _force_remove_stale(guild, guild_id)
            log.info(
                "connect_player attempt %d/%d (channel=%s guild_id=%s budget_left=%.1fs)",
                attempts, max(1, int(timeout / attempt_timeout)), channel, guild_id,
                deadline - time.monotonic(),
            )
            try:
                player = await channel.connect(cls=cls, timeout=attempt_timeout)
                if _handshake_complete(player):
                    log.info(
                        "connect_player handshake COMPLETE attempt=%d (guild_id=%s)",
                        attempts, guild_id,
                    )
                    return player
                # Handshake fields not populated despite connect() returning:
                # the dropped-event race. Re-issue on a fresh registration below.
                vs = getattr(player, "_voice_state", {}) or {}
                v = vs.get("voice", {}) or {}
                ev = getattr(player, "_connection_event", None)
                log.info(
                    "connect_player handshake INCOMPLETE after connect() attempt=%d "
                    "guild_id=%s — re-issuing opcode 4 "
                    "[conn_event=%s session=%r token=%r endpoint=%r]",
                    attempts, guild_id,
                    ev is not None and ev.is_set(),
                    v.get("session_id"), v.get("token"), v.get("endpoint"),
                )
            except Exception as exc:
                last_exc = exc
                # Distinguish dropped-event (empty handshake) vs late-event (fields
                # partially populated): dump whatever the failed player did hold.
                try:
                    vs = getattr(player, "_voice_state", {}) or {}
                    v = vs.get("voice", {}) or {}
                    ev = getattr(player, "_connection_event", None)
                except Exception:
                    vs, v, ev = {}, {}, None
                log.info(
                    "connect_player attempt=%d failed (guild_id=%s): %s "
                    "[conn_event=%s session=%r token=%r endpoint=%r]",
                    attempts, guild_id, exc,
                    ev is not None and ev.is_set(),
                    v.get("session_id"), v.get("token"), v.get("endpoint"),
                )

            # ── re-issue opcode 4 on a fresh, registered player ─────
            reissued = await _reissue_voice_join(channel, cls, guild, guild_id)
            if reissued is not None and _handshake_complete(reissued):
                log.info(
                    "connect_player RECOVERED via re-issue attempt=%d (guild_id=%s)",
                    attempts, guild_id,
                )
                return reissued

            # Give the gateway a short breather before the next attempt.
            await asyncio.sleep(0.5)

        # ── final failure: diagnostics + fail-fast ─────────────────
        # Handshake diagnostics: prove whether the gateway delivered the
        # VOICE_STATE_UPDATE / VOICE_SERVER_UPDATE events. The ChannelTimeoutException
        # fires when wavelink's _connection_event is never set, i.e. _update_player
        # PATCH never ran. Dump the player handshake fields so the root cause is
        # diagnosable, then re-raise.
        try:
            player = guild.voice_client if guild is not None else None
            vs = {}
            if player is not None:
                vs = getattr(player, "_voice_state", {}) or {}
                voice = vs.get("voice", {}) or {}
                log.error(
                    "connect_player TIMEOUT/FAILURE for channel=%s guild_id=%s "
                    "connection_event.set=%s session_id=%r token=%r endpoint=%r "
                    "voice_state=%r",
                    channel, guild_id,
                    getattr(player, "_connection_event", None) is not None
                    and player._connection_event.is_set(),
                    voice.get("session_id"), voice.get("token"),
                    voice.get("endpoint"), vs,
                )
            else:
                log.error(
                    "connect_player FAILURE for channel=%s guild_id=%s "
                    "guild.voice_client is None — cannot dump handshake state",
                    channel, guild_id,
                )
        except Exception:
            log.exception("connect_player: could not dump handshake diagnostics")

        # Preserve the fail-fast user-facing error message: re-raise so callers
        # still surface a specific error instead of the generic 30s timeout.
        if last_exc is not None:
            raise last_exc
        raise RuntimeError(
            f"Could not connect to voice channel {channel} after "
            f"{timeout:.0f}s (handshake never completed; dropped VOICE_* events)."
        )


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
            "spotify": "spsearch",
            "tidal": "tidal",
        }
        source = source_map.get(state.get("source_provider", "youtube"), TrackSource.YouTube)

        # For URLs, try to parse directly; for search, use Playable.search
        if state.get("source_provider") == "tidal":
            tidal_query = f"tdsearch:{title}" if not (url and ("http://" in url or "https://" in url)) else (url or title)
            tracks = await Playable.search(tidal_query, source=TrackSource.YouTube)
            if not tracks:
                tracks = await Playable.search(title or url, source=TrackSource.YouTube)
        elif url and ("http://" in url or "https://" in url):
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

        # Prefer explicit/original versions over remixes, covers, live versions
        if isinstance(tracks, list):
            tracks = _prefer_explicit_original(tracks, title)
            tracks = _prefer_highest_quality(tracks)

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
            for entry in _search_entries(tracks, provider="autoplay")[:3]:
                state["queue"].append(entry)
            persist(guild_id)
            await _play_next_from_queue(guild_id)
    except Exception as exc:
        log.warning("Autoplay search failed: %s", exc)


# ── wavelink event handlers ────────────────────────────────

async def on_track_start(guild_id: int, player: wavelink.Player, track: wavelink.Playable) -> None:
    state = get_state(guild_id)
    state["player"] = player

    info = _track_entry(track, provider="on_track_start")
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
    title = track.title or "Unknown"
    author = track.author or "Unknown Artist"
    duration = track.length or 0
    mins, secs = divmod(duration // 1000, 60) if duration > 1000 else divmod(duration, 60)

    embed = discord.Embed(title="🎵 HelloDJ — Now Playing", colour=discord.Colour.blurple())
    embed.add_field(name="Song", value=title, inline=True)
    embed.add_field(name="Artist", value=author, inline=True)
    embed.add_field(name="Duration", value=f"{mins}:{secs:02d}", inline=True)
    embed.set_footer(text="HelloDJ — Use the buttons below to control playback")
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
            duration = track.length
            if duration <= 0:
                continue

            bar = _progress_bar(position, duration)
            pos_m, pos_s = divmod(position // 1000, 60)
            dur_m, dur_s = divmod(duration // 1000, 60)

            embed = msg.embeds[0] if msg.embeds else discord.Embed(title="🎵 HelloDJ — Now Playing", colour=discord.Colour.blurple())
            embed.description = f"`{bar}`  {pos_m}:{pos_s:02d} / {dur_m}:{dur_s:02d}"
            try:
                await msg.edit(embed=embed)
            except discord.HTTPException:
                pass
    except asyncio.CancelledError:
        pass


def _progress_bar(position: int, duration: int, width: int = 9) -> str:
    if duration <= 0:
        return "?" * width
    filled = int(position / duration * width)
    filled = max(0, min(filled, width - 1))
    bar = ["▬"] * width
    bar[filled] = "🔘"
    return "".join(bar)


def _prefer_explicit_original(tracks: list, query: str) -> list:
    """Filter and sort tracks to prefer explicit/original versions.
    Removes or deprioritizes remixes, covers, live versions."""
    keywords_to_avoid = ['cover', 'remix', 'live', 'edit', 'acoustic', 'rehearsal', 'demo']
    preferred_keywords = ['explicit', 'original']

    def score(track):
        title_lower = track.title.lower() if hasattr(track, 'title') else ''
        artist_lower = track.author.lower() if hasattr(track, 'author') else ''
        score = 0
        # Boost if title has explicit/original
        for kw in preferred_keywords:
            if kw in title_lower:
                score += 10
        # Penalize if title has avoid keywords
        for kw in keywords_to_avoid:
            if kw in title_lower:
                score -= 10
        # Boost if artist name matches query closely
        if query.lower() in artist_lower:
            score += 5
        return score

    # Sort by score descending (best first)
    tracks.sort(key=score, reverse=True)
    return tracks


def _prefer_highest_quality(tracks: list) -> list:
    """Filter and sort tracks to prefer highest quality sources.
    Lavalink may return multiple tracks at different qualities."""
    # Track objects may have isrc or quality hints
    # Prefer tracks that are marked as highest quality
    def quality_score(track):
        # wavelink tracks may have 'quality' metadata
        if hasattr(track, 'quality'):
            quality_map = {'lossless': 100, 'high': 50, 'medium': 20, 'low': 0}
            return quality_map.get(track.quality, 10)
        # If no quality info, check bitrate or other hints
        if hasattr(track, 'bitrate') and track.bitrate:
            return track.bitrate // 100  # higher bitrate = higher score
        return 50  # default middle

    tracks.sort(key=quality_score, reverse=True)
    return tracks


# ── now-playing button view ────────────────────────────────

class NowPlayingView(discord.ui.View):
    """Persistent buttons attached to the now-playing embed."""

    def __init__(self, guild_id: int):
        super().__init__(timeout=300)
        self.guild_id = guild_id

    @discord.ui.button(label="▶️", style=discord.ButtonStyle.secondary, custom_id="np_play")
    async def play_resume(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await self._play_resume(interaction)

    @discord.ui.button(label="⏸", style=discord.ButtonStyle.primary, custom_id="np_toggle")
    async def pause_resume(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await self._toggle_pause(interaction)

    @discord.ui.button(label="⏭", style=discord.ButtonStyle.secondary, custom_id="np_next")
    async def next_track(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await self._skip_next(interaction)

    @discord.ui.button(label="🔀", style=discord.ButtonStyle.success, custom_id="np_shuffle")
    async def shuffle_tracks(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await self._shuffle(interaction)

    @discord.ui.button(label="⏹️", style=discord.ButtonStyle.danger, custom_id="np_stop")
    async def stop_playback(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await self._stop(interaction)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # Allow any guild member to use buttons
        return True

    async def _play_resume(self, interaction: discord.Interaction) -> None:
        player = get_player(self.guild_id)
        if player:
            if player.paused:
                await player.resume()
            elif not player.playing:
                await _play_next_from_queue(self.guild_id)
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

    async def _stop(self, interaction: discord.Interaction) -> None:
        player = get_player(self.guild_id)
        if player:
            await player.stop()
        await interaction.response.defer()

    async def _shuffle(self, interaction: discord.Interaction) -> None:
        state = get_state(self.guild_id)
        shuffle_queue(state)
        persist(self.guild_id)
        await interaction.response.send_message("HelloDJ queue shuffled.", ephemeral=True)

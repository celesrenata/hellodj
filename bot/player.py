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
import permissions
from voice.hybrid_player import HybridPlayer
import voice_debug
import blacklist as _blacklist
from debug import get_debug_logger, trace

log = logging.getLogger(__name__)
dbg = get_debug_logger("player")

# Per-guild state
guild_state: dict[int, dict] = {}

# Bot reference — set by bot.py at startup for cross-module access
_bot_ref: "discord.ext.commands.Bot | None" = None


def set_bot(bot) -> None:
    """Store a reference to the bot instance (called once from bot.py)."""
    global _bot_ref
    _bot_ref = bot

# ── track-start callback (visualizer integration) ───────────
# A fire-and-forget callback invoked when a new track starts playing.
# The VisualizerRegistry subscribes here during setup — player.py MUST NOT
# import from visualizer modules (audio independence, Req 8).
_on_track_start_callback = None


def set_on_track_start_callback(callback) -> None:
    """Register a callback for track start events.

    Args:
        callback: An async callable (guild_id: int, metadata: dict) -> None.
                  Exceptions are swallowed to preserve audio independence.
    """
    global _on_track_start_callback
    _on_track_start_callback = callback

# Per-guild connect locks: serialize voice-channel connects so the
# wakeword/voice pipeline and /play cannot race and trigger
# `ClientException('Already connected to a voice channel.')`.
_connect_locks: dict[int, asyncio.Lock] = {}

# ── mid-song recovery (retry) ───────────────────────────────
# When a track fails mid-song, on_track_exception re-resolves and replays the
# SAME track up to MAX_TRACK_RETRIES times (instead of advancing the queue) so
# the bot recovers from a transient failure instead of "falling over" in the
# middle of a song. RETRY_BACKOFF_SECONDS is the sleep inserted between retries.
# Both are overridable via env for tuning without a code change.
MAX_TRACK_RETRIES = int(os.getenv("HELLODJ_MAX_TRACK_RETRIES", "3"))
RETRY_BACKOFF_SECONDS = float(os.getenv("HELLODJ_RETRY_BACKOFF_SECONDS", "1.5"))

# ── duration sanity threshold ────────────────────────────────
# Lavalink reports Long.MAX_VALUE (9223372036854775807) for HLS/live streams
# that don't have a known duration. Treat anything > 24 hours as "unknown".
_DURATION_MAX_MS = 86_400_000  # 24 hours in milliseconds

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


# ── helpers ───────────────────────────────────────────────

def _to_entry(info: dict) -> dict:
    """Lightweight ``{webpage_url, title, type}`` form for persistence."""
    entry = {
        "webpage_url": info.get("webpage_url") or info.get("url"),
        "title": info.get("title", "Unknown"),
    }
    # Preserve entry type for video entries
    if info.get("type"):
        entry["type"] = info["type"]
    if info.get("query"):
        entry["query"] = info["query"]
    return entry


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
    source = getattr(track, "source", None) or provider or "unknown"
    # Album name from lavasrc plugin info (Spotify/Tidal/Apple Music)
    album = ""
    extras = getattr(track, "extras", None)
    if extras and hasattr(extras, "get"):
        album = extras.get("albumName", "") or ""
    if not album:
        raw = getattr(track, "raw_data", None)
        if raw and isinstance(raw, dict):
            album = raw.get("pluginInfo", {}).get("albumName", "") or ""

    entry = {
        "webpage_url": str(uri) if uri else None,
        "title": title,
        "author": author,
        "album": album,
        "duration": length if length <= _DURATION_MAX_MS else 0,
        "source": source,
    }

    log.debug(
        "track_entry type=%s provider=%s uri=%r title=%r author=%r album=%r length=%r source=%r",
        type(track).__name__, provider, uri, title, author, album, length, source,
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
        "history": [_to_entry(item) for item in state.get("history", [])[:10]],
        # Extended guild settings — must be persisted or they reset to
        # save_guild() defaults (source_provider="youtube", repeat_mode="off")
        # on every persist() call, wiping the user's /source choice on restart.
        "source_provider": state.get("source_provider", "youtube"),
        "repeat_mode": state.get("repeat_mode", "off"),
        "autoplay_enabled": state.get("autoplay_enabled", False),
        "autoplay_genres": list(state.get("autoplay_genres") or []),
        "filters": dict(state.get("filters") or {}),
        # /tune — enhanced-audio toggle persisted across restarts (like filters)
        "tune_enabled": state.get("tune_enabled", False),
        # /crossfade — seconds of fade overlap between tracks (0 = disabled)
        "crossfade_seconds": state.get("crossfade_seconds", 0.0),
    }


# ── state access ───────────────────────────────────────────

def get_state(guild_id: int) -> dict:
    if guild_id not in guild_state:
        guild_state[guild_id] = {
            "queue": [],
            "history": [],                # last N played tracks (most recent first)
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
            # /tune — enhanced-audio toggle, persisted across restarts
            "tune_enabled": False,
            "now_playing_msg": None,
            "now_playing_task": None,
            # /crossfade — seconds of fade between tracks (0 = disabled)
            "crossfade_seconds": 0.0,
            "crossfade_tasks": [],
            # Mid-song recovery: how many times the CURRENT track has been
            # retried after a mid-song exception. Reset on successful start.
            "track_retries": 0,
            # Video streaming state (Go Live screenshare)
            "video_streamer": None,        # VideoStreamer instance or None
            "video_queue": [],             # list[VideoSource] pending video playback
            # Active playlist tracking — syncs queue with playlist on add/remove
            "active_playlist": None,       # playlist name (str) or None
        }
    # Ensure history exists for states created before this field was added
    if "history" not in guild_state[guild_id]:
        guild_state[guild_id]["history"] = []
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
    player stays keyed in _voice_clients. The next /play then hits the
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


async def _gateway_leave_channel(guild: discord.Guild | None, guild_id: int) -> None:
    """Send op-4 with channel_id=None to tell Discord the bot has LEFT the voice channel.

    This is the critical step missing from the stale-state recovery: if Discord
    already believes the bot is in the target channel, re-sending op-4 to JOIN
    the same channel is a no-op — Discord won't issue a new VOICE_SERVER_UPDATE.
    By explicitly leaving first, the next join is treated as a fresh connection
    and Discord responds with both VOICE_STATE_UPDATE and VOICE_SERVER_UPDATE.

    Waits a short period after sending the disconnect for Discord to process it.
    """
    if guild is None:
        return
    try:
        await guild.change_voice_state(channel=None)
        log.info(
            "connect_player: sent gateway LEAVE (op-4 channel=None) for guild_id=%s",
            guild_id,
        )
        # Give Discord a moment to process the leave before we re-join.
        await asyncio.sleep(0.5)
    except Exception:
        log.exception(
            "connect_player: gateway LEAVE failed for guild_id=%s — proceeding anyway",
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
            while not await _handshake_complete(player):
                await asyncio.sleep(0.25)
    except asyncio.TimeoutError:
        log.info(
            "connect_player re-issue handshake still incomplete after %.2fs "
            "(guild_id=%s) — dropping to timeout",
            time.monotonic() - start, guild_id,
        )
        return None
    # Log the ACTUAL handshake fields at the moment of "COMPLETE". A real
    # gateway round-trip (opcode 4 -> VOICE_STATE_UPDATE -> VOICE_SERVER_UPDATE
    # -> PATCH to Lavalink) takes ~100-500ms minimum. A completion in ~10ms
    # means the fresh player inherited stale session_id/token/endpoint or a
    # pre-set _connection_event — i.e. the "handshake" is NOT driven by fresh
    # gateway events. This dump lets us prove/disprove that stale-state path.
    try:
        vs = getattr(player, "_voice_state", {}) or {}
        v = vs.get("voice", {}) or {}
        ev = getattr(player, "_connection_event", None)
        log.info(
            "connect_player re-issue COMPLETE detail (guild_id=%s) after=%.2fs "
            "conn_event=%s session_id=%r token=%r endpoint=%r voice_state=%r",
            guild_id, time.monotonic() - start,
            ev is not None and ev.is_set(),
            v.get("session_id"), v.get("token"), v.get("endpoint"), vs,
        )
    except Exception:
        log.exception("connect_player re-issue: could not dump COMPLETE detail guild_id=%s", guild_id)
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

        timeout = float(os.getenv("VOICE_CONNECT_TIMEOUT", "30.0"))
        log.info(
            "connect_player: connecting to voice channel %s (guild_id=%s) "
            "timeout=%ss hybrid=%s",
            channel, guild_id, timeout, HybridPlayer is not None,
        )
        guild = getattr(channel, "guild", None)
        cls = HybridPlayer if HybridPlayer is not None else wavelink.Player

        # ── switchable debug layer (HELLODJ_VOICE_DEBUG) ────────
        # Log the ACTUAL per-channel permissions on the exact target channel
        # (channel.permissions_for honors overwrites; guild.me.guild_permissions
        # does NOT) and the op-4 send. This discriminates per-channel Connect/
        # Speak denial from a registration race.
        voice_debug.log_per_channel_perms(guild, channel, label="connect_player")
        voice_debug.log_op4_send(guild, channel)

        # ── pre-connect stale state detection ─────────────────────
        # If Discord already thinks the bot is in the target channel (voice_client
        # exists or bot's voice state points at this channel) but there's no live
        # connection, leave first to ensure a fresh VOICE_SERVER_UPDATE on re-join.
        if guild is not None:
            existing_vc = guild.voice_client
            bot_voice_state = guild.me.voice if guild.me else None
            already_in_channel = (
                (existing_vc is not None)
                or (bot_voice_state is not None and bot_voice_state.channel is not None)
            )
            if already_in_channel:
                # Check if the existing connection is actually alive
                has_live_connection = False
                if existing_vc is not None:
                    has_live_connection = await _handshake_complete(existing_vc)
                if not has_live_connection:
                    log.info(
                        "connect_player: detected stale voice presence in guild_id=%s "
                        "(voice_client=%s bot_voice_state.channel=%s) — "
                        "sending gateway LEAVE to force fresh handshake",
                        guild_id,
                        existing_vc is not None,
                        getattr(bot_voice_state, "channel", None) if bot_voice_state else None,
                    )
                    _force_remove_stale(guild, guild_id)
                    await _gateway_leave_channel(guild, guild_id)

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
                # ── receive-sink registration race fix ──────────────────
                # Store the freshly connected player into state["player"]
                # IMMEDIATELY (before the handshake wait below), so the cog's
                # on_voice_state_update -> _start_receive finds a non-None
                # player even when the bot's own voice-state event fires while
                # connect() is still awaiting. Previously callers stored the
                # player only AFTER connect_player returned, leaving the
                # listener to hit the "No player yet" branch and never register
                # the voice_recv sink.
                if guild_id and player is not None:
                    try:
                        get_state(guild_id)["player"] = player
                        log.info(
                            "connect_player: stored player into state[%s] "
                            "immediately (player=%s) for receive-sink wiring",
                            guild_id, type(player).__name__,
                        )
                    except Exception:
                        log.exception("connect_player: could not store player in state guild_id=%s", guild_id)
                if await _handshake_complete(player):
                    log.info(
                        "connect_player handshake COMPLETE attempt=%d (guild_id=%s)",
                        attempts, guild_id,
                    )
                    # Diagnosis: prove whether the HybridPlayer established a
                    # REAL Discord voice connection (socket+SSRC+secret_key) or
                    # only forwarded to Lavalink. wavelink's Player.connect() only
                    # sends op-4 and PATCHes Lavalink; it never calls
                    # VoiceConnectionState.connect(), so _connection.is_connected()
                    # is normally False here — meaning TTS send_audio_packet and
                    # voice_recv listen() cannot work.
                    try:
                        conn = getattr(player, "_connection", None)
                        conn_ok = False
                        if conn is not None and not isinstance(conn, str):
                            try:
                                conn_ok = conn.is_connected()
                            except Exception:
                                conn_ok = False
                        log.info(
                            "connect_player COMPLETE connection diag (guild_id=%s) "
                            "player_type=%s connected_prop=%s _connection=%s "
                            "conn.is_connected=%s voice_state=%r",
                            guild_id, type(player).__name__,
                            getattr(player, "connected", False),
                            conn if conn is not None else "MISSING",
                            conn_ok,
                            getattr(player, "_voice_state", {}),
                        )
                    except Exception:
                        log.exception("connect_player COMPLETE connection diag failed")
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
            # If Discord never sent VOICE_SERVER_UPDATE (token/endpoint are None),
            # the bot is likely stuck in a "ghost connected" state from Discord's
            # perspective. Leave the channel first so the next join is treated as
            # fresh, then re-issue.
            try:
                vs_check = getattr(player, "_voice_state", {}) or {}
                v_check = vs_check.get("voice", {}) or {}
                no_server_update = not v_check.get("token") and not v_check.get("endpoint")
            except Exception:
                no_server_update = True

            if no_server_update:
                log.info(
                    "connect_player: no VOICE_SERVER_UPDATE received (guild_id=%s) "
                    "— sending gateway LEAVE before re-join to force fresh handshake",
                    guild_id,
                )
                _force_remove_stale(guild, guild_id)
                await _gateway_leave_channel(guild, guild_id)

            reissued = await _reissue_voice_join(channel, cls, guild, guild_id)
            if reissued is not None and await _handshake_complete(reissued):
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

        # Log which voice permissions the bot member is missing — the most
        # common non-gateway cause of a failed connect (Connect/Speak denied).
        if guild is not None:
            me = guild.me
            if me is not None:
                missing_voice = permissions.missing_voice_permissions(me)
                if missing_voice:
                    log.error(
                        "connect_player: guild_id=%s bot member %s is missing "
                        "voice permissions %s — Connect/Speak denial can cause "
                        "this failure",
                        guild_id, me, missing_voice,
                    )
                else:
                    log.info(
                        "connect_player: guild_id=%s bot member %s holds all "
                        "voice permissions (no Connect/Speak/ViewChannel denial)",
                        guild_id, me,
                    )

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
    state["active_playlist"] = None


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


async def jump_to(guild_id: int, *, history_index: int | None = None, queue_index: int | None = None) -> bool:
    """Jump to a track from history or queue and start playing it immediately.

    Parameters
    ----------
    guild_id:
        The guild to operate on.
    history_index:
        0-based index into state["history"] (most recent first). If provided,
        the track is removed from history and inserted at the front of the queue,
        then playback advances.
    queue_index:
        0-based index into state["queue"]. If provided, moves that track to
        the front of the queue, then playback advances.

    Returns True if playback was triggered, False on invalid index.
    """
    state = get_state(guild_id)
    from_history = False

    if history_index is not None:
        history = state.setdefault("history", [])
        if history_index < 0 or history_index >= len(history):
            return False
        track = history.pop(history_index)
        # When going "previous" (from history), push the current track to
        # queue[0] first, then insert the history track before it.
        # This way "next" returns to the track we just left.
        if state.get("current"):
            state["queue"].insert(0, state["current"])
        state["queue"].insert(0, track)
        from_history = True
    elif queue_index is not None:
        if queue_index < 0 or queue_index >= len(state["queue"]):
            return False
        # Move the selected track to position 0
        track = state["queue"].pop(queue_index)
        state["queue"].insert(0, track)
    else:
        return False

    # Stop current playback — this triggers _play_next_from_queue via on_track_end
    p = get_player(guild_id)
    if p and p.connected and p.playing:
        # on_track_end fires asynchronously via event dispatch, so we can't
        # rely on it running before we return. Instead, stop the player and
        # manually advance the queue ourselves. Set a flag to suppress the
        # duplicate advance from on_track_end.
        state["_jump_transition"] = True
        await p.stop()
        state.pop("_jump_transition", None)
        await _play_next_from_queue(guild_id, skip_history_push=from_history)
    else:
        # No player playing — manually advance
        await _play_next_from_queue(guild_id, skip_history_push=from_history)
    return True


def set_repeat(state: dict, mode: str) -> None:
    assert mode in ("off", "single", "queue")
    state["repeat_mode"] = mode


# ── crossfade (fade-out / fade-in transition) ──────────────
# Approach & limitations (documented):
#   Lavalink/wavelink plays ONE track at a time on a single voice stream, so a
#   true overlapping crossfade (both tracks audible simultaneously) is not
#   possible through the standard player API, and Lavalink exposes no crossfade
#   filter. This implementation instead performs a *fade-based* crossfade:
#     - the outgoing track fades to ~0 volume over the last `crossfade_seconds`
#       of its runtime (tracked by playback position), and
#     - the incoming track fades up from ~0 to full volume over the same window.
#   The audible result is a smooth, gapless-feeling transition rather than a
#   silence gap. It is not a true overlap mix (no dual-stream PCM mixing), which
#   is the documented limitation of the single-stream Lavalink model.
#   NOTE: crossfade drives Lavalink volume directly, so it overrides any manual
#   `/volume` while a fade is in progress; volume is reset to 1.0 after the fade.

def set_crossfade(state: dict, seconds: float) -> None:
    """Set the guild crossfade duration in seconds (0 disables)."""
    state["crossfade_seconds"] = max(0.0, float(seconds))


def get_crossfade_seconds(state: dict) -> float:
    """Return the guild crossfade duration in seconds (0 = disabled)."""
    try:
        return max(0.0, float(state.get("crossfade_seconds", 0.0) or 0.0))
    except (TypeError, ValueError):
        return 0.0


async def _safe_set_volume(player: wavelink.Player, volume: float) -> None:
    try:
        await player.set_volume(max(0.0, min(1.0, volume)))
    except Exception:
        log.warning("crossfade: could not set volume to %s", volume)


async def _crossfade_fade_in(guild_id: int, player: wavelink.Player, cf_sec: float) -> None:
    """Fade the incoming track up from near-silence to full volume."""
    try:
        await player.set_volume(0.05)
    except Exception:
        pass
    steps = max(1, int(cf_sec / 0.2))
    try:
        for i in range(1, steps + 1):
            vol = 0.05 + (0.95 * i / steps)
            await _safe_set_volume(player, vol)
            await asyncio.sleep(0.2)
        await _safe_set_volume(player, 1.0)
    except asyncio.CancelledError:
        await _safe_set_volume(player, 1.0)


async def _crossfade_fade_out(guild_id: int, player: wavelink.Player, duration_ms: int, cf_sec: float) -> None:
    """Wait until the track is within `cf_sec` of its end, then fade it to ~0."""
    target_pos = max(0, int(duration_ms) - int(cf_sec * 1000))
    try:
        while player.playing and (player.position or 0) < target_pos:
            await asyncio.sleep(0.5)
    except asyncio.CancelledError:
        return
    steps = max(1, int(cf_sec / 0.2))
    try:
        for i in range(steps, -1, -1):
            await _safe_set_volume(player, i / steps)
            await asyncio.sleep(0.2)
    except asyncio.CancelledError:
        return


def _cancel_crossfade_tasks(state: dict) -> None:
    tasks = state.get("crossfade_tasks") or []
    for t in tasks:
        if t and not t.done():
            t.cancel()
    state["crossfade_tasks"] = []


def reset_crossfade(guild_id: int) -> None:
    """Cancel any in-flight crossfade fades and restore full volume.

    Called by the cog's /stop, /clear, /leave paths so a mid-fade volume is
    never left near zero after playback is stopped.
    """
    state = get_state(guild_id)
    _cancel_crossfade_tasks(state)
    p = state.get("player")
    if p is not None:
        asyncio.ensure_future(_safe_set_volume(p, 1.0))


def _is_video_active(guild_id: int) -> bool:
    """Check if a video Activity session is currently active for this guild.

    Returns True if:
    - There's a registered and active video streamer session, OR
    - The current track in state is a music_video (setup in progress)

    This prevents audio from auto-starting during video setup/transition.
    """
    # Quick check: is the current state entry a video?
    state = guild_state.get(guild_id)
    if state:
        current = state.get("current")
        if current and current.get("type") == "music_video":
            return True

    # Full check: is there an active streamer?
    try:
        bot_ref = _bot_ref
        if bot_ref is None:
            return False
        video_cog = bot_ref.get_cog("Video")
        if video_cog is None:
            return False
        for (gid, _cid), streamer in video_cog._registry._sessions.items():
            if gid == guild_id and streamer.is_active:
                return True
    except Exception as exc:
        log.debug("_is_video_active check failed: %s", exc)
    return False


async def add_track(state: dict, guild_id: int, entry: dict) -> None:
    state["queue"].append(entry)
    persist(guild_id)
    # Auto-start playback if the player is idle (connected but not playing)
    # AND no video session is currently active
    if _is_video_active(guild_id):
        return
    p = get_player(guild_id)
    if p and p.connected and not p.playing and not p.paused:
        await _play_next_from_queue(guild_id)


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

    # Don't auto-start if a video session is active — just queue silently
    if _is_video_active(guild.id):
        persist(guild.id)
        return len(tracks)

    # Trigger playback — connect if needed, then start if idle
    p = get_player(guild.id)
    if not p or not p.connected:
        # No player or disconnected — connect to the voice channel if we have one
        vc = state.get("voice_channel")
        if vc:
            try:
                p = await connect_player(vc)
                state["player"] = p
            except Exception as exc:
                log.warning("enqueue_and_start: connect_player failed guild=%s: %s", guild.id, exc)
                p = None

    if p and p.connected and not p.playing and not p.paused:
        await _play_next_from_queue(guild.id)

    persist(guild.id)
    return len(tracks)


# ── event-driven playback ─────────────────────────────────

async def _play_next_from_queue(guild_id: int, *, skip_history_push: bool = False) -> None:
    state = get_state(guild_id)
    player = state.get("player")

    # Reentrance guard: prevent on_track_end from double-advancing
    import time as _time
    state["_advancing_queue_at"] = _time.monotonic()

    dbg.event("play_next", guild_id=guild_id, queue_len=len(state["queue"]),
              repeat_mode=state["repeat_mode"],
              current_title=state.get("current", {}).get("title") if state.get("current") else None)

    # Repeat mode
    if state["repeat_mode"] == "single" and state.get("current"):
        state["queue"].insert(0, state["current"])
    elif state["repeat_mode"] == "queue" and state.get("current"):
        state["queue"].append(state["current"])

    # Push outgoing track to history (most recent first, capped at 50)
    # skip_history_push=True when doing "previous" — we already placed the
    # outgoing track at the front of the queue so "next" returns to it.
    if state.get("current") and not skip_history_push:
        history = state.setdefault("history", [])
        history.insert(0, state["current"])
        del history[50:]  # keep a reasonable cap

    # Pop next
    if not state["queue"]:
        await _on_queue_empty(guild_id)
        return

    next_entry = state["queue"].pop(0)
    state["current"] = next_entry
    persist(guild_id)

    # Check if this is a video entry — needs Activity pipeline, not Lavalink
    if next_entry.get("type") == "music_video":
        await _start_video_from_queue(guild_id, next_entry)
        return

    # Audio entry — needs a connected player
    if not player or not player.connected:
        dbg.debug("play_next: no player or disconnected guild=%d — reconnecting", guild_id)
        vc = state.get("voice_channel")
        if vc:
            try:
                player = await connect_player(vc)
                state["player"] = player
            except Exception as exc:
                log.error("play_next: reconnect failed for guild=%d: %s", guild_id, exc)
                await _on_queue_empty(guild_id)
                return
        else:
            await _on_queue_empty(guild_id)
            return

    await _resolve_and_play(player, guild_id, next_entry)


async def _start_video_from_queue(guild_id: int, entry: dict) -> None:
    """Start a video Activity for a queued music_video entry.

    Stops audio playback but keeps the bot connected to voice.
    The bot should NOT leave and rejoin — it stays in the channel.
    """
    state = get_state(guild_id)

    # Set flag to prevent on_track_end from re-advancing the queue
    # when we stop the audio player intentionally
    state["_video_transition"] = True

    # Stop audio playback but keep connected (don't disconnect!)
    p = get_player(guild_id)
    if p and p.connected and (p.playing or p.paused):
        log.info("_start_video_from_queue: stopping audio (staying connected) guild=%d", guild_id)
        try:
            await p.stop()
        except Exception as exc:
            log.warning("Failed to stop audio before video: %s", exc)

    # Note: _video_transition flag is consumed by on_track_end (pop)
    # so we don't need to clear it here.

    # Get the bot and VideoCog
    if _bot_ref is None:
        log.error("_start_video_from_queue: bot reference not set")
        return
    video_cog = _bot_ref.get_cog("Video")
    if video_cog is None:
        log.error("_start_video_from_queue: VideoCog not loaded, skipping video entry")
        # Skip to next in queue
        await _play_next_from_queue(guild_id)
        return

    # The entry should have pre-resolved source info
    query = entry.get("query") or entry.get("title", "")
    text_channel = state.get("text_channel")
    voice_channel = state.get("voice_channel")

    # Fallback: if text_channel is None, try the voice channel's associated text channel
    if text_channel is None and voice_channel is not None and _bot_ref is not None:
        guild = _bot_ref.get_guild(guild_id)
        if guild:
            # Use the voice channel itself if it's a text-in-voice channel,
            # or find the guild's system/first text channel
            if hasattr(voice_channel, 'send'):
                text_channel = voice_channel
            elif guild.system_channel:
                text_channel = guild.system_channel
            else:
                for ch in guild.text_channels:
                    if ch.permissions_for(guild.me).send_messages:
                        text_channel = ch
                        break
            if text_channel:
                state["text_channel"] = text_channel

    if voice_channel is None:
        log.error("_start_video_from_queue: no voice_channel in state guild=%d", guild_id)
        await _play_next_from_queue(guild_id)
        return

    # Use the video cog's internal resolver + Activity launch
    log.info("_start_video_from_queue: launching music video for guild=%d query=%r", guild_id, query)
    try:
        from video.music_video_resolver import MusicVideoResolver
        from video.activity_streamer import ActivityStreamer
        from video.ws_hub import PlaybackState

        resolver = MusicVideoResolver()
        source = await resolver.resolve(query, source_provider=state.get("source_provider"))

        # Check for existing video session (active or idle but with connected clients)
        streamer = video_cog._registry.get(guild_id, voice_channel.id)
        if streamer is not None:
            if streamer.is_active:
                # Currently streaming — enqueue
                streamer.enqueue(source)
                if text_channel:
                    await text_channel.send(f"📥 Added to video queue: **{source.title}**")
                return
            else:
                # Idle streamer — reuse it (clients still connected via WS)
                log.info("_start_video_from_queue: reusing idle streamer for guild=%d", guild_id)
                await streamer.play(source)

                import time as _time
                # Set state as playing immediately — no countdown for reused sessions
                # since viewers are already connected
                video_cog._backend.ws_hub.set_state(
                    guild_id,
                    PlaybackState(playing=True, anchor_position=0.0, anchor_time=_time.time()),
                )
                # Mark playback as started (skip countdown protocol)
                streamer.waiting_for_viewer = False
                streamer.countdown_active = False
                streamer.playback_started = True
                streamer.start_time = _time.monotonic()

                # Broadcast session_change so clients reinit HLS and auto-play
                await video_cog._backend.ws_hub.broadcast_from_bot(guild_id, {
                    "type": "session_change",
                })

                if text_channel:
                    from cogs.video import _build_now_playing_embed
                    from views.unified_remote import UnifiedControlView
                    embed = _build_now_playing_embed(source, len(streamer.queue), elapsed_seconds=0.0)
                    view = UnifiedControlView()
                    # Edit existing Now Playing message if available (unified remote)
                    existing_msg = state.get("now_playing_msg")
                    if existing_msg:
                        try:
                            await existing_msg.edit(embed=embed, view=view)
                            msg = existing_msg
                        except (discord.NotFound, discord.HTTPException):
                            msg = await text_channel.send(embed=embed, view=view)
                    else:
                        msg = await text_channel.send(embed=embed, view=view)
                    state["now_playing_msg"] = msg
                    key = (guild_id, voice_channel.id)
                    video_cog._now_playing_messages[key] = msg
                    video_cog._start_seek_bar_update(key)
                return

        # Create new Activity session
        streamer = ActivityStreamer(
            guild_id=guild_id, channel_id=voice_channel.id,
            ws_hub=video_cog._backend.ws_hub,
            on_session_end=_on_video_session_end,
        )
        video_cog._registry.register(guild_id, voice_channel.id, streamer)
        video_cog._backend.ws_hub.register_streamer(guild_id, streamer)

        # Launch Activity
        assert video_cog._launcher is not None
        application_id = _bot_ref.user.id
        invite_data = await video_cog._launcher.launch(voice_channel.id, application_id)

        # Start playback
        await streamer.play(source)

        # Set initial state as NOT playing — playback position doesn't advance
        # until the first viewer connects and the countdown completes.
        # The ws_hub's _handle_ready resets position=0 and playing=True
        # after the countdown finishes.
        import time as _time
        video_cog._backend.ws_hub.set_state(
            guild_id,
            PlaybackState(playing=False, anchor_position=0.0, anchor_time=_time.time()),
        )

        if text_channel:
            invite_code = invite_data.get("code", "")
            activity_url = f"https://discord.gg/{invite_code}" if invite_code else None
            from cogs.video import _build_now_playing_embed
            from views.unified_remote import UnifiedControlView
            embed = _build_now_playing_embed(source, len(streamer.queue), activity_url=activity_url, elapsed_seconds=0.0)
            view = UnifiedControlView()
            # Edit existing Now Playing message if available (unified remote)
            existing_msg = state.get("now_playing_msg")
            if existing_msg:
                try:
                    await existing_msg.edit(embed=embed, view=view)
                    msg = existing_msg
                except (discord.NotFound, discord.HTTPException):
                    msg = await text_channel.send(embed=embed, view=view)
            else:
                msg = await text_channel.send(embed=embed, view=view)
            state["now_playing_msg"] = msg
            key = (guild_id, voice_channel.id)
            video_cog._now_playing_messages[key] = msg
            if activity_url:
                video_cog._activity_urls[key] = activity_url
            video_cog._start_seek_bar_update(key)

    except Exception as exc:
        log.error("_start_video_from_queue failed for guild=%d: %s", guild_id, exc, exc_info=True)
        if text_channel:
            await text_channel.send(f"❌ Failed to start music video: {exc}")
        # Clear current — the video failed, nothing is playing
        state["current"] = None
        persist(guild_id)
        # Try next in queue
        await _play_next_from_queue(guild_id)


async def _on_video_session_end(guild_id: int) -> None:
    """Callback fired when a video Activity session ends (queue empty).

    Advances the unified player queue so audio tracks resume after video.
    """
    log.info("_on_video_session_end: video finished, checking unified queue guild=%d", guild_id)
    state = get_state(guild_id)
    state["current"] = None  # Clear the video entry from current
    if state["queue"]:
        await _play_next_from_queue(guild_id)


async def _resolve_and_play(player: wavelink.Player, guild_id: int, entry: dict) -> None:
    state = get_state(guild_id)
    url = entry.get("webpage_url") or entry.get("url")
    title = entry.get("title", "Unknown")

    # Detect the actual source from the URL — overrides the guild source_provider
    # when the track URL clearly belongs to a specific service. This prevents
    # mismatches like a Spotify URL being resolved through the Tidal path.
    sp = state.get("source_provider", "youtube")
    if url:
        if "spotify.com" in url or "spotify:" in url:
            sp = "spotify"
        elif "tidal.com" in url or "tidal:" in url:
            sp = "tidal"
        elif "youtube.com" in url or "youtu.be" in url or "music.youtube.com" in url:
            sp = "youtube"
        elif "soundcloud.com" in url:
            sp = "soundcloud"

    log.info(
        "_resolve_and_play guild=%d title=%r source_provider=%r",
        guild_id, title, sp,
    )
    dbg.event("resolve_start", guild_id=guild_id, title=title, url=url,
              source_provider=sp, queue_len=len(state["queue"]))
    resolve_start = time.monotonic()

    try:
        # ── Direct stream resolution (bypass YouTube mirroring) ────────────
        # For Spotify/Tidal: try our stream services first. If they return a
        # direct URL, feed it to Lavalink as an HTTP source. If they fail,
        # fall through to the old LavaSrc/YouTube path.
        if sp in ("spotify", "tidal") and url:
            from stream_resolver import resolve_direct_stream
            try:
                direct_url = await resolve_direct_stream(sp, url)
                if direct_url:
                    tracks = await Playable.search(direct_url)
                    if tracks:
                        track = tracks[0] if isinstance(tracks, list) else tracks
                        # Inject real metadata — Lavalink's HTTP source doesn't know
                        # the track title/artist. Use object.__setattr__ to bypass
                        # wavelink's read-only property descriptors.
                        try:
                            object.__setattr__(track, "_title", title)
                        except Exception:
                            pass
                        try:
                            object.__setattr__(track, "_author", entry.get("author", "") or "")
                        except Exception:
                            pass
                        try:
                            object.__setattr__(track, "_uri", url)
                        except Exception:
                            pass
                        try:
                            object.__setattr__(track, "_source", sp)
                        except Exception:
                            pass
                        duration = entry.get("duration", 0)
                        if duration and duration > 0:
                            try:
                                object.__setattr__(track, "_length", duration)
                            except Exception:
                                pass
                        dbg.event("resolve_success", guild_id=guild_id, title=title,
                                  resolved_uri=url,
                                  resolved_title=title,
                                  resolved_author=track.author,
                                  resolved_length=track.length,
                                  source_provider=f"{sp}_direct",
                                  elapsed_ms=(time.monotonic() - resolve_start) * 1000)
                        # Mark the current entry with the real source for embeds
                        if state.get("current"):
                            state["current"]["source"] = sp
                        await player.play(track)
                        # Ensure not paused (may have been paused before video transition)
                        if player.paused:
                            await player.pause(False)
                        return
                    log.info("Direct stream URL returned but Lavalink couldn't load it — falling back")
            except Exception as direct_exc:
                log.warning(
                    "Direct stream resolution failed for %s (provider=%s), "
                    "falling back to LavasRC: %s", title, sp, direct_exc,
                )

        # Search using Playable.search with the configured source
        source_map = {
            "youtube": TrackSource.YouTube,
            "youtube_music": TrackSource.YouTubeMusic,
            "soundcloud": TrackSource.SoundCloud,
            "spotify": "spsearch",
            "tidal": "tidal",
        }
        source = source_map.get(sp, TrackSource.YouTube)

        # For URLs, try to parse directly; for search, use Playable.search
        if sp == "tidal":
            tidal_query = f"tdsearch:{title}" if not (url and ("http://" in url or "https://" in url)) else (url or title)
            tracks = await Playable.search(tidal_query, source=None)
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
            # Post-filter: prefer tracks whose title+author contain all search words
            query_words = [w.lower() for w in (title or "").split() if len(w) > 1]
            if query_words and len(tracks) > 1:
                filtered = [
                    t for t in tracks
                    if all(
                        w in (getattr(t, "title", "") + " " + getattr(t, "author", "")).lower()
                        for w in query_words
                    )
                ]
                if filtered:
                    tracks = filtered
            tracks = _prefer_explicit_original(tracks, title)
            tracks = _prefer_highest_quality(tracks)

        track = tracks[0] if isinstance(tracks, list) else tracks
        dbg.event("resolve_success", guild_id=guild_id, title=title,
                  resolved_uri=getattr(track, "uri", None),
                  resolved_title=getattr(track, "title", None),
                  resolved_author=getattr(track, "author", None),
                  resolved_length=getattr(track, "length", None),
                  source_provider=sp,
                  elapsed_ms=(time.monotonic() - resolve_start) * 1000)
        await player.play(track)
        # Ensure playback is not paused (player may have been paused before video transition)
        if player.paused:
            await player.pause(False)

    except Exception as exc:
        dbg.error("resolve_failed guild=%d title=%r provider=%r error=%s",
                  guild_id, title, sp, exc)
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


# ── mid-song recovery helpers ──────────────────────────────

def _is_connection_related(exc: Exception) -> bool:
    """True if the exception looks voice-connection related (dropped handshake, disconnect).

    Matches the wavelink exception classes used by the connect machinery plus a
    conservative message-text heuristic for provider/transport errors.
    """
    try:
        exc_mod = wavelink.exceptions
        if isinstance(
            exc,
            (exc_mod.ChannelTimeoutException, exc_mod.NodeException,
             exc_mod.InvalidChannelStateException, exc_mod.LavalinkException),
        ):
            return True
    except Exception:
        pass
    text = str(exc).lower()
    return any(
        k in text
        for k in ("connect", "disconnect", "timeout", "handshake", "voice",
                  "websocket", "not connected", "already connected", "closed")
    )


async def _attempt_reconnect(guild_id: int) -> None:
    """Force a fresh voice-connection when the player has dropped mid-song."""
    state = get_state(guild_id)
    channel = state.get("voice_channel")
    if channel is None:
        return
    try:
        player = await connect_player(channel)
        state["player"] = player
        log.info(
            "on_track_exception: reconnected player for guild %s after "
            "connection-related failure",
            guild_id,
        )
    except Exception as exc:
        log.warning(
            "on_track_exception: reconnect attempt failed for guild %s: %s",
            guild_id, exc,
        )


async def _announce_up_next(guild_id: int) -> None:
    """Peek the next queue entry and send a lightweight 'up next' heads-up.

    Announced only when there IS a next track AND repeat_mode is not 'single'
    (single-track repeat would replay the same song, so there is no meaningful
    'up next'). Returns early when the queue is empty — including the case where
    autoplay would later fill it, since there is no known track to announce yet.
    This is a lightweight heads-up, NOT a duplicate now-playing embed: the full
    embed is still sent on track start.
    """
    state = get_state(guild_id)
    channel = state.get("text_channel")
    if not channel:
        return
    if state.get("repeat_mode") == "single":
        return
    if not state["queue"]:
        return
    next_entry = state["queue"][0]
    title = next_entry.get("title", "Unknown")
    try:
        await channel.send(f"⏭️ Up next: **{title}**")
    except Exception as exc:
        log.warning(
            "Could not send 'up next' announcement in guild %s: %s", guild_id, exc,
        )


# ── /tune enhancement helper ───────────────────────────────
# Mirrors the tune chain defined in filters.py (`_apply_tune`) so player.py can
# re-apply it on every new track without a circular import (filters.py imports
# player, so player cannot import filters).
TUNE_GAINS = [0.5, 0.3, 0.2, 0.1, 0.1, 0, 0, -0.05, 0, 0.1, 0.15, 0.2, 0.25, 0.3, 0.35]


async def _apply_tune_to(player: wavelink.Player) -> None:
    """Apply the /tune enhancement chain to ``player`` (equalizer + timescale).

    Transparent "studio master" polish: gentle low boost + high-frequency lift
    for air/crispness, natural tempo (speed=1.0), and a very light distortion
    (scale=1.1) for warmth. No vibrato/tremolo (those add wobble).
    """
    bands = [{"band": i, "gain": g} for i, g in enumerate(TUNE_GAINS)]
    filters = player.filters
    filters.equalizer.set(bands=bands)
    filters.timescale.set(speed=1.0, pitch=1.0, rate=1.0)
    filters.distortion.set(scale=1.1)
    filters.rotation.reset()
    filters.low_pass.reset()
    filters.karaoke.reset()
    filters.channel_mix.reset()
    await player.set_filters(filters)


# ── wavelink event handlers ────────────────────────────────

async def on_track_start(guild_id: int, player: wavelink.Player, track: wavelink.Playable) -> None:
    state = get_state(guild_id)
    state["player"] = player

    # A track started successfully — clear any mid-song retry counter so the
    # next failure episode starts fresh at 0.
    state["track_retries"] = 0

    # Do NOT overwrite state["current"] here. It was already set correctly by
    # _play_next_from_queue with the original queue entry metadata (proper title,
    # author, Spotify/YouTube URL). Lavalink's track_start event often has
    # degraded metadata (e.g. "Unknown title" for HTTP proxy streams).
    persist(guild_id)

    # Cancel any in-flight progress-bar updater from a previous track so two
    # coroutines never both edit the same now-playing message in one window
    # (Discord 5-edits/5s per-message bucket -> 429).
    prev_task = state.get("now_playing_task")
    if prev_task and not prev_task.done():
        prev_task.cancel()

    # Start progress bar updater
    state["now_playing_task"] = asyncio.ensure_future(
        _now_playing_updater(guild_id, player, track)
    )

    # ── crossfade scheduling ─────────────────────────────────
    # Fade the just-started track in, and (when its length is known) schedule a
    # fade-out near its end. Each track gets its own fade pair; both are
    # cancelled on stop/clear/leave and on the next track end.
    _cancel_crossfade_tasks(state)
    cf = get_crossfade_seconds(state)
    if cf > 0:
        tasks = []
        tasks.append(asyncio.ensure_future(_crossfade_fade_in(guild_id, player, cf)))
        # Prefer entry duration over track.length (HLS streams report Long.MAX_VALUE)
        duration = getattr(track, "length", None) or 0
        if duration <= 0 or duration > _DURATION_MAX_MS:
            duration = (state.get("current") or {}).get("duration") or 0
        if 0 < duration <= _DURATION_MAX_MS:
            tasks.append(asyncio.ensure_future(_crossfade_fade_out(guild_id, player, duration, cf)))
        state["crossfade_tasks"] = tasks

    # ── /tune re-application (enhanced audio) ─────────────────
    # `/tune` is a permanent per-song "light switch": when tune_enabled is True
    # (persisted across restarts), re-apply the tune enhancement filter to every
    # NEW track that starts so the audio stays "less compressed and more crisp"
    # song after song. The chain is defined in filters.py (`_apply_tune`) and
    # mirrored here so player.py can re-apply it without a circular import.
    if state.get("tune_enabled"):
        try:
            await _apply_tune_to(player)
            log.info(
                "on_track_start: re-applied /tune enhancement for guild %s "
                "(tune_enabled=True)",
                guild_id,
            )
        except Exception as exc:
            log.warning(
                "on_track_start: could not re-apply /tune enhancement for guild %s: %s",
                guild_id, exc,
            )

    await _send_now_playing(guild_id, player, track)

    # ── visualizer track-change callback (Req 3.5, 5.5) ──────
    # Fire-and-forget: notify the visualizer system of the new track.
    # Exceptions are swallowed to maintain audio independence (Req 8).
    if _on_track_start_callback:
        entry = state.get("current") or {}
        metadata = {
            "title": entry.get("title", track.title or ""),
            "artist": entry.get("author", track.author or ""),
            "artwork_url": entry.get("artwork_url") or getattr(track, "artwork", None),
            "duration_ms": entry.get("duration") or getattr(track, "length", 0),
            "position_ms": 0,
        }
        try:
            await _on_track_start_callback(guild_id, metadata)
        except Exception:
            log.debug("Track start callback failed (visualizer)", exc_info=True)


async def on_track_end(guild_id: int, player: wavelink.Player, track: wavelink.Playable, reason: str) -> None:
    state = get_state(guild_id)

    # If we're transitioning to a video entry, suppress queue advancement
    # (the stop that triggers this event is intentional, not a track finishing)
    if state.pop("_video_transition", False):
        dbg.debug("on_track_end: suppressed during video transition guild=%d", guild_id)
        return

    # If unified_skip is handling the advancement, suppress duplicate advance
    if state.get("_skip_transition"):
        state.pop("_skip_transition", None)
        dbg.debug("on_track_end: suppressed during skip transition guild=%d", guild_id)
        return

    # If jump_to is handling the advancement, suppress duplicate advance
    if state.get("_jump_transition"):
        dbg.debug("on_track_end: suppressed during jump transition guild=%d", guild_id)
        return

    # Reentrance guard: if _play_next_from_queue ran recently (within 5s),
    # this on_track_end is from the track being replaced — don't double-advance
    import time as _time
    last_advance = state.get("_advancing_queue_at", 0)
    if (_time.monotonic() - last_advance) < 5.0:
        dbg.debug("on_track_end: suppressed — queue advanced %.1fs ago guild=%d",
                  _time.monotonic() - last_advance, guild_id)
        return

    np_task = state.get("now_playing_task")
    if np_task and not np_task.done():
        np_task.cancel()
    state["now_playing_task"] = None

    # Cancel any in-flight crossfade fades for the ended track.
    _cancel_crossfade_tasks(state)

    # Lightweight "up next" heads-up before advancing (only when a next track
    # exists and repeat isn't single-track). The full now-playing embed is still
    # the primary display, sent on track start.
    await _announce_up_next(guild_id)

    await _play_next_from_queue(guild_id)


async def on_track_exception(guild_id: int, player: wavelink.Player, track: wavelink.Playable, exc: Exception) -> None:
    state = get_state(guild_id)
    log.warning(
        "Track exception in guild %s for %r: %s (retries_used=%s)",
        guild_id, track, exc, state.get("track_retries", 0),
    )

    # ── mid-song recovery: retry the SAME track instead of advancing ────
    retries = state.get("track_retries", 0)
    if retries < MAX_TRACK_RETRIES:
        state["track_retries"] = retries + 1
        log.info(
            "Retrying track %r in guild %s (attempt %d/%d)",
            track, guild_id, state["track_retries"], MAX_TRACK_RETRIES,
        )

        # If the failure looks connection-related, force a fresh player connect
        # so the retry starts from a healthy voice socket.
        if _is_connection_related(exc):
            await _attempt_reconnect(guild_id)

        # Small backoff between retries so the provider/transport can recover.
        await asyncio.sleep(RETRY_BACKOFF_SECONDS)

        # Re-resolve and replay the SAME track (the `track` object is in scope),
        # using the same machinery as a normal play. This does NOT consume the
        # queue, so the current track is preserved.
        entry = _track_entry(track, provider="on_track_exception")
        state["current"] = entry
        await _resolve_and_play(state.get("player") or player, guild_id, entry)
        return

    # Retries exhausted — fall through to the existing end-of-track behavior
    # (advance the queue) and log that the track is being skipped.
    log.warning(
        "Skipping track %r in guild %s after %d failed retries",
        track, guild_id, MAX_TRACK_RETRIES,
    )
    state["track_retries"] = 0
    await on_track_end(guild_id, player, track, "exception")


# ── now-playing embed ──────────────────────────────────────

async def _send_now_playing(guild_id: int, player: wavelink.Player, track: wavelink.Playable) -> None:
    state = get_state(guild_id)
    channel = state.get("text_channel")
    if not channel:
        return

    # Prefer state["current"] (has correct metadata) over the live track object
    # (which may report "Unknown title" / Long.MAX_VALUE duration for HLS proxy
    # streams). Use the entry whenever it has ANY useful field — not just title.
    current_entry = state.get("current")
    entry_useful = (
        current_entry
        and (
            (current_entry.get("title") and current_entry["title"] not in ("Unknown", "Unknown title"))
            or current_entry.get("author")
            or (current_entry.get("duration") and 0 < current_entry["duration"] <= _DURATION_MAX_MS)
        )
    )
    if entry_useful:
        embed = build_now_playing_embed_from_entry(current_entry)
    else:
        embed = _build_now_playing_embed(track, current_entry)
    from views.unified_remote import UnifiedControlView
    view = UnifiedControlView()

    msg = state.get("now_playing_msg")
    if msg:
        try:
            await msg.edit(embed=embed, view=view)
            return
        except discord.NotFound:
            pass

    msg = await channel.send(embed=embed, view=view)
    state["now_playing_msg"] = msg


def _fmt_duration_ms(ms: int) -> str:
    """Format a duration in milliseconds to H:MM:SS or M:SS."""
    if ms <= 0 or ms > _DURATION_MAX_MS:
        return "LIVE" if ms > _DURATION_MAX_MS else "0:00"
    total_secs = ms // 1000
    hours, remainder = divmod(total_secs, 3600)
    mins, secs = divmod(remainder, 60)
    if hours > 0:
        return f"{hours}:{mins:02d}:{secs:02d}"
    return f"{mins}:{secs:02d}"


def _split_title(raw_title: str, author: str) -> tuple[str, str]:
    """Split a track title into (song, artist).

    YouTube returns titles as 'Artist - Song Name' while `author` is the
    channel/uploader (e.g. 'AAA FM'), NOT the artist. So parse the real artist
    out of the title. For sources where the title is just the song name (e.g.
    Spotify via LavaSrc), fall back to using `author` as the artist.
    """
    title = (raw_title or "Unknown").strip()
    artist_fallback = (author or "Unknown Artist").strip()
    # Strip YouTube auto-generated " - Topic" suffix from artist names
    if artist_fallback.endswith(" - Topic"):
        artist_fallback = artist_fallback[:-8].strip()

    # Split on the FIRST ' - ' separator only.
    if " - " in title:
        artist_part, song_part = title.split(" - ", 1)
        artist_part = artist_part.strip()
        song_part = song_part.strip()
        # Strip " - Topic" from parsed artist too
        if artist_part.endswith(" - Topic"):
            artist_part = artist_part[:-8].strip()
        if artist_part and song_part:
            return song_part, artist_part
    return title, artist_fallback


def build_now_playing_embed_from_entry(entry: dict) -> discord.Embed:
    """Build the same per-song now-playing embed from a queue entry dict.

    ``entry`` is ``state["current"]`` (produced by _track_entry) and carries
    ``webpage_url``/``title``/``author``/``duration`` — the same fields the
    wavelink-based _build_now_playing_embed renders from a Playable. Used by
    /remote when refreshing an existing now-playing message from stored state.
    """
    raw_title = entry.get("title") or "Unknown"
    author = entry.get("author") or "Unknown Artist"
    song, artist = _split_title(raw_title, author)
    duration = entry.get("duration") or 0
    if duration > _DURATION_MAX_MS:
        duration = 0
    source = entry.get("source") or "unknown"

    # Map http source to real provider based on URL pattern
    url_for_source = entry.get("webpage_url") or entry.get("url") or ""
    if source in ("unknown", "http"):
        if "spotify.com" in url_for_source or "spotify:" in url_for_source:
            source = "spotify"
        elif "tidal.com" in url_for_source:
            source = "tidal"

    embed = discord.Embed(title="🎵 HelloDJ — Now Playing", colour=discord.Colour.blurple())
    embed.add_field(name="Song", value=song, inline=True)
    embed.add_field(name="Artist", value=artist, inline=True)
    embed.add_field(name="Duration", value=_fmt_duration_ms(duration), inline=True)
    embed.add_field(name="Source", value=str(source).capitalize(), inline=True)

    # Progress bar at position 0 (will be updated by _now_playing_updater)
    if duration > 0:
        bar = _progress_bar(0, duration)
        dur_m, dur_s = divmod(duration // 1000, 60)
        embed.description = f"`{bar}`  0:00 / {dur_m}:{dur_s:02d}"

    url = entry.get("webpage_url") or entry.get("url") or entry.get("uri")
    if url:
        embed.add_field(name="Link", value=url, inline=False)
        embed.url = url
    embed.set_footer(text="HelloDJ — Use the buttons below to control playback")
    return embed


def _build_now_playing_embed(track: wavelink.Playable, entry: dict | None = None) -> discord.Embed:
    raw_title = track.title or "Unknown"
    author = track.author or "Unknown Artist"
    # Prefer entry metadata for author/duration when the track object reports
    # garbage (common for HLS/HTTP proxy streams).
    if entry:
        if entry.get("author") and author in ("Unknown Artist", ""):
            author = entry["author"]
        if entry.get("title") and raw_title in ("Unknown", "Unknown title"):
            raw_title = entry["title"]
    song, artist = _split_title(raw_title, author)
    duration = track.length or 0
    if duration > _DURATION_MAX_MS:
        # Track reports Long.MAX_VALUE (HLS stream) — use entry's duration
        duration = (entry or {}).get("duration") or 0
    source = getattr(track, "source", None) or "unknown"

    # Map internal source names to user-friendly labels
    uri = getattr(track, "uri", None) or ""
    if source == "http":
        # Direct stream sidecars use localhost URLs — map to real provider
        if "8802/stream" in uri:
            source = "spotify"
        elif "8801/stream" in uri:
            source = "tidal"

    # Album (may be None for some sources)
    album_name = None
    try:
        album_obj = getattr(track, "album", None)
        if album_obj:
            album_name = getattr(album_obj, "name", None)
    except Exception:
        album_name = None

    embed = discord.Embed(title="🎵 HelloDJ — Now Playing", colour=discord.Colour.blurple())
    embed.add_field(name="Song", value=song, inline=True)
    embed.add_field(name="Artist", value=artist, inline=True)
    embed.add_field(name="Duration", value=_fmt_duration_ms(duration), inline=True)
    embed.add_field(name="Source", value=source.capitalize(), inline=True)
    if album_name:
        embed.add_field(name="Album", value=album_name, inline=True)
    embed.set_footer(text="HelloDJ — Use the buttons below to control playback")
    return embed


async def _now_playing_updater(guild_id: int, player: wavelink.Player, track: wavelink.Playable) -> None:
    state = get_state(guild_id)
    me = asyncio.current_task()
    try:
        while player.playing or player.paused:
            await asyncio.sleep(15)
            if not player.playing and not player.paused:
                break

            # Stale-check: if a newer now-playing updater has been registered
            # (a new track started via on_track_start), this coroutine is
            # obsolete. Stop editing so we never overwrite the fresh embed
            # with a stale progress bar. task.cancel() is async, so a
            # cancelled updater can still land one in-flight msg.edit() after
            # the new track's _send_now_playing — this guard closes that race.
            if state.get("now_playing_task") is not me:
                break

            msg = state.get("now_playing_msg")
            if not msg:
                continue

            # Rebuild the embed from state["current"] (correct metadata) with
            # live position from the player.
            current_entry = state.get("current")
            current = player.current or track
            position = player.position
            # Prefer entry duration; only fall back to track.length if entry has none
            entry_dur = (current_entry or {}).get("duration") or 0
            track_dur = current.length or 0
            if entry_dur > 0 and entry_dur <= _DURATION_MAX_MS:
                duration = entry_dur
            elif track_dur > 0 and track_dur <= _DURATION_MAX_MS:
                duration = track_dur
            else:
                continue

            bar = _progress_bar(position, duration)
            pos_m, pos_s = divmod(position // 1000, 60)
            dur_m, dur_s = divmod(duration // 1000, 60)

            if current_entry and current_entry.get("title") and current_entry["title"] not in ("Unknown", "Unknown title"):
                embed = build_now_playing_embed_from_entry(current_entry)
            else:
                embed = _build_now_playing_embed(current, current_entry)
            embed.description = f"`{bar}`  {pos_m}:{pos_s:02d} / {dur_m}:{dur_s:02d}"
            try:
                from views.unified_remote import UnifiedControlView
                await msg.edit(embed=embed, view=UnifiedControlView())
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
            return track.bitrate // 100  # higher bitrate = higher quality
        return 50  # default middle

    tracks.sort(key=quality_score, reverse=True)
    return tracks


# ── now-playing button view ────────────────────────────────

class NowPlayingView(discord.ui.View):
    """Unified remote control panel attached to the now-playing embed.

    The now-playing message posted when a song starts IS the remote control:
    Row 0: ⏮ Previous • ⏯ Play/Pause • ⏭ Next • ➕ Add to Playlist • 🚫 Block
    Row 1: Filter/EQ dropdown (bassboost, nightcore, 8d, vaporwave, tune, EQ, reset)
    """

    def __init__(self, guild_id: int):
        super().__init__(timeout=300)
        self.guild_id = guild_id

        # Add filter dropdown on row 1
        filter_select = discord.ui.Select(
            placeholder="🎛️ Filters & EQ…",
            options=[
                discord.SelectOption(label="Bass Boost", value="bassboost", emoji="🔊", description="Boost low-end frequencies"),
                discord.SelectOption(label="Nightcore", value="nightcore", emoji="⚡", description="Speed up + pitch shift"),
                discord.SelectOption(label="8D Audio", value="8d", emoji="🌀", description="Spatial panning effect"),
                discord.SelectOption(label="Vaporwave", value="vaporwave", emoji="🌊", description="Slowed, mellow vibe"),
                discord.SelectOption(label="Tune (Enhanced)", value="tune", emoji="✨", description="Studio master polish"),
                discord.SelectOption(label="Equalizer", value="equalizer", emoji="🎛️", description="10-band custom EQ"),
                discord.SelectOption(label="Reset Filters", value="reset", emoji="🔄", description="Remove all effects"),
            ],
            row=1,
            custom_id="np_filter_select",
        )
        filter_select.callback = self._on_filter_select
        self.add_item(filter_select)

    @discord.ui.button(label="⏮", style=discord.ButtonStyle.secondary, custom_id="np_prev", row=0)
    async def prev_track(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await self._seek_start(interaction)

    @discord.ui.button(label="⏯", style=discord.ButtonStyle.primary, custom_id="np_toggle", row=0)
    async def pause_resume(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._toggle_pause(interaction, button)

    @discord.ui.button(label="⏭", style=discord.ButtonStyle.secondary, custom_id="np_next", row=0)
    async def next_track(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await self._skip_next(interaction)

    @discord.ui.button(label="➕", style=discord.ButtonStyle.success, custom_id="np_playlist", row=0)
    async def add_to_playlist(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await self._show_playlist_picker(interaction)

    @discord.ui.button(label="🚫", style=discord.ButtonStyle.secondary, custom_id="np_block", row=0)
    async def block_track(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await self._block(interaction)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # Allow any guild member to use the buttons
        return True

    async def _seek_start(self, interaction: discord.Interaction) -> None:
        """⏮ Previous — seek back to the start of the current song."""
        player = get_player(self.guild_id)
        if player:
            try:
                await player.seek(0)
            except Exception:
                pass
        await interaction.response.defer()

    async def _toggle_pause(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        """⏯ Play/Pause toggle — flip playback state and refresh the button icon."""
        player = get_player(self.guild_id)
        if not player:
            await interaction.response.defer()
            return
        if player.paused:
            await player.pause(False)
            button.label = "⏯"
        elif player.playing:
            await player.pause(True)
            button.label = "▶️"
        await interaction.response.edit_message(view=self)

    async def _skip_next(self, interaction: discord.Interaction) -> None:
        """⏭ Next — skip to the next track."""
        player = get_player(self.guild_id)
        if player:
            await player.stop()
        await interaction.response.defer()

    async def _block(self, interaction: discord.Interaction) -> None:
        """🚫 Block — permanently blacklist the current track, then skip it. Admin only."""
        # Admin gate: only guild administrators or OAuth-bound admins can block
        import oauth_store
        is_admin = False
        if interaction.guild:
            is_admin = interaction.user.guild_permissions.administrator
        if not is_admin:
            is_admin = oauth_store.is_bound_admin(interaction.user.id)
        if not is_admin:
            await interaction.response.send_message(
                "🚫 Only administrators can block tracks.", ephemeral=True
            )
            return

        state = get_state(self.guild_id)
        current = state.get("current")
        title = (current or {}).get("title", "Unknown")
        if current:
            _blacklist.add_blacklist_entry(self.guild_id, current)
        player = get_player(self.guild_id)
        if player:
            await player.stop()
        await interaction.response.send_message(
            f"🚫 Blocked **{title}**.", ephemeral=True
        )

    async def _show_playlist_picker(self, interaction: discord.Interaction) -> None:
        """➕ Add to Playlist — show a dropdown to pick which playlist."""
        import storage

        state = get_state(self.guild_id)
        current = state.get("current")
        if not current:
            await interaction.response.send_message(
                "Nothing is playing right now.", ephemeral=True
            )
            return

        playlist_names = storage.names(self.guild_id)

        if not playlist_names:
            # No playlists exist — offer to create one
            await interaction.response.send_message(
                "No playlists exist yet. Use `/playlist create <name>` to create one first.",
                ephemeral=True,
            )
            return

        # Build a select menu with the guild's playlists
        view = _PlaylistSelectView(self.guild_id, current)
        await interaction.response.send_message(
            "Choose a playlist to add the current song to:",
            view=view,
            ephemeral=True,
        )

    async def _on_filter_select(self, interaction: discord.Interaction) -> None:
        """Handle filter dropdown selection from the now-playing panel."""
        value = interaction.data["values"][0]
        player_obj = get_player(self.guild_id)
        if not player_obj:
            await interaction.response.send_message(
                "HelloDJ is not connected to voice.", ephemeral=True
            )
            return

        state = get_state(self.guild_id)

        if value == "reset":
            filters = player_obj.filters
            filters.equalizer.reset()
            filters.timescale.reset()
            filters.rotation.reset()
            filters.low_pass.reset()
            filters.distortion.reset()
            filters.vibrato.reset()
            filters.tremolo.reset()
            filters.karaoke.reset()
            filters.channel_mix.reset()
            await player_obj.set_filters(filters)
            state["filters"] = {}
            state["tune_enabled"] = False
            persist(self.guild_id)
            await interaction.response.send_message("🔄 All filters reset.", ephemeral=True)

        elif value == "bassboost":
            gains = [0.0, 0.1, 0.15, 0.1, 0.05, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
            bands = [{"band": i, "gain": g} for i, g in enumerate(gains)]
            filters = player_obj.filters
            filters.equalizer.set(bands=bands)
            await player_obj.set_filters(filters)
            state["filters"]["bassboost"] = {"level": "moderate", "gains": gains}
            persist(self.guild_id)
            await interaction.response.send_message("🔊 Bass Boost applied.", ephemeral=True)

        elif value == "nightcore":
            filters = player_obj.filters
            filters.timescale.set(speed=1.25, pitch=1.25, rate=1.0)
            await player_obj.set_filters(filters)
            state["filters"]["nightcore"] = {"speed": 1.25, "pitch": 1.25}
            persist(self.guild_id)
            await interaction.response.send_message("⚡ Nightcore applied.", ephemeral=True)

        elif value == "8d":
            filters = player_obj.filters
            filters.rotation.set(rotation_hz=0.5)
            await player_obj.set_filters(filters)
            state["filters"]["8d"] = {"rotation": 0.5}
            persist(self.guild_id)
            await interaction.response.send_message("🌀 8D Audio applied.", ephemeral=True)

        elif value == "vaporwave":
            gains = [0.15, 0.15, 0.15, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
            bands = [{"band": i, "gain": g} for i, g in enumerate(gains)]
            filters = player_obj.filters
            filters.timescale.set(speed=0.85, pitch=0.9, rate=0.85)
            filters.equalizer.set(bands=bands)
            await player_obj.set_filters(filters)
            state["filters"]["vaporwave"] = {"speed": 0.85, "pitch": 0.9}
            persist(self.guild_id)
            await interaction.response.send_message("🌊 Vaporwave applied.", ephemeral=True)

        elif value == "tune":
            # Toggle tune on/off
            tune_on = not state.get("tune_enabled", False)
            state["tune_enabled"] = tune_on
            if tune_on:
                await _apply_tune_to(player_obj)
                persist(self.guild_id)
                await interaction.response.send_message("✨ Tune (enhanced audio) enabled.", ephemeral=True)
            else:
                filters = player_obj.filters
                filters.equalizer.reset()
                filters.timescale.reset()
                filters.distortion.reset()
                await player_obj.set_filters(filters)
                persist(self.guild_id)
                await interaction.response.send_message("✨ Tune disabled.", ephemeral=True)

        elif value == "equalizer":
            from cogs.equalizer_view import EqualizerView
            eq_view = EqualizerView(self.guild_id)
            from cogs.equalizer_view import _build_eq_embed
            embed = _build_eq_embed(eq_view.gains, eq_view.selected_band)
            await interaction.response.send_message(embed=embed, view=eq_view, ephemeral=True)

        else:
            await interaction.response.send_message("Unknown filter.", ephemeral=True)


class _PlaylistSelectView(discord.ui.View):
    """Ephemeral dropdown for picking a playlist to add the current track to."""

    def __init__(self, guild_id: int, track_entry: dict):
        super().__init__(timeout=30)
        self.guild_id = guild_id
        self.track_entry = track_entry

        import storage
        playlist_names = storage.names(guild_id)

        options = [
            discord.SelectOption(label=name[:100], value=name[:100])
            for name in playlist_names[:25]  # Discord max 25 options
        ]

        select = discord.ui.Select(
            placeholder="Select a playlist…",
            options=options,
            custom_id="np_playlist_select",
        )
        select.callback = self._on_select
        self.add_item(select)

    async def _on_select(self, interaction: discord.Interaction) -> None:
        import storage

        playlist_name = interaction.data["values"][0]
        track = {
            "url": self.track_entry.get("webpage_url") or self.track_entry.get("url") or "",
            "title": self.track_entry.get("title", "Unknown"),
            "duration": self.track_entry.get("duration", 0),
        }

        try:
            resolved_name = await storage.add_track(self.guild_id, playlist_name, track)
            await interaction.response.edit_message(
                content=f"✅ Added **{track['title']}** to **{resolved_name}**.",
                view=None,
            )
        except Exception as exc:
            await interaction.response.edit_message(
                content=f"❌ Could not add to playlist: {exc}",
                view=None,
            )

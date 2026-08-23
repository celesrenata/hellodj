"""HelloDJ — WebSocket synchronization hub for Activity playback.

Manages WebSocket connections per guild for synchronized playback control.
All connected clients for a guild share playback state. When any client
sends a control message (play, pause, seek), it is broadcast to all other
clients in the same guild. Late joiners receive the current state on connect.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import time
from collections.abc import Awaitable
from typing import TYPE_CHECKING, Callable

from aiohttp import web

from video.stroke_registry import StrokeData, StrokeRegistry

if TYPE_CHECKING:
    from video.activity_streamer import ActivityStreamer

    from search.models import SearchResult

log = logging.getLogger(__name__)

_HEARTBEAT_INTERVAL = 30.0  # seconds between server pings

_VALID_STROKE_TYPES = {"freehand", "line", "rect", "ellipse", "circle", "triangle", "star", "arrow", "text", "sticker"}


@dataclasses.dataclass
class PlaybackState:
    """Server-authoritative playback state for a guild.

    Uses an anchor-based model for jitter-free global sync:
    - anchor_position: the video position (seconds) at anchor_time
    - anchor_time: monotonic time (time.monotonic()) when anchor_position was set
    - playing: whether the video is advancing from the anchor

    Clients compute current position as:
        if playing: anchor_position + (local_mono_now + server_offset - anchor_time)
        else: anchor_position

    The anchor only changes on seek, play, or pause — NOT on periodic ticks.
    This eliminates network-latency-induced jitter for global viewers.

    The _epoch_offset field captures the difference between wall-clock and
    monotonic time at construction, allowing conversion back to wall-clock
    via the anchor_time_wall property for backward compatibility.
    """

    playing: bool = True
    anchor_position: float = 0.0  # video position in seconds at anchor_time
    anchor_time: float = dataclasses.field(default_factory=time.monotonic)  # monotonic
    _epoch_offset: float = dataclasses.field(
        default_factory=lambda: time.time() - time.monotonic()
    )
    subtitle_lang: str | None = None  # "for everyone" subtitle
    audio_lang: str | None = None  # "for everyone" audio track

    @property
    def anchor_time_wall(self) -> float:
        """Wall-clock equivalent of anchor_time (for backward compat)."""
        return self.anchor_time + self._epoch_offset

    @property
    def position(self) -> float:
        """Compute current position from anchor (for backward compat)."""
        if self.playing:
            return self.anchor_position + (time.monotonic() - self.anchor_time)
        return self.anchor_position

    def seek_to(self, position: float) -> None:
        """Update anchor for a seek operation."""
        self.anchor_position = position
        self.anchor_time = time.monotonic()

    def set_playing(self, playing: bool) -> None:
        """Toggle play/pause, freezing or resuming the anchor."""
        if playing and not self.playing:
            # Resuming: anchor stays at current position, time resets to now
            self.anchor_time = time.monotonic()
        elif not playing and self.playing:
            # Pausing: freeze position at current computed value
            self.anchor_position = self.anchor_position + (time.monotonic() - self.anchor_time)
            self.anchor_time = time.monotonic()
        self.playing = playing


class WebSocketHub:
    """Per-guild WebSocket connection manager for playback synchronization.

    All connected clients for a guild share playback state. When any client
    sends a control message (play, pause, seek), it is broadcast to all other
    clients in the same guild. Late joiners receive the current state on connect.

    Args:
        validate_guild_token: Callable that maps a token string to a guild_id
            (int) if valid, or None if the token is invalid/unrecognized.
    """

    def __init__(self, validate_guild_token: Callable[[str], int | None]) -> None:
        self._validate_guild_token = validate_guild_token
        self._connections: dict[int, set[web.WebSocketResponse]] = {}
        self._playback_state: dict[int, PlaybackState] = {}
        self._stroke_registries: dict[int, StrokeRegistry] = {}  # guild_id → registry
        self._viewer_count_callback: Callable[[int, int, int], Awaitable[None]] | None = None
        self._streamers: dict[int, ActivityStreamer] = {}  # guild_id → streamer
        self._lyrics_state_getter: Callable[[int], object | None] | None = None
        # Search: shared engine + active in-flight search tasks per guild
        self._search_engine: object | None = None  # lazy-initialized UnifiedSearchEngine
        self._active_searches: dict[int, dict[str, asyncio.Task]] = {}  # guild_id → {request_id → task}

    def set_viewer_count_callback(
        self, callback: Callable[[int, int, int], Awaitable[None]]
    ) -> None:
        """Register a callback for viewer count transitions.

        The callback receives (guild_id, old_count, new_count) and is invoked
        when the viewer count transitions from 0→1 or reaches 0.

        Args:
            callback: Async callable(guild_id, old_count, new_count).
        """
        self._viewer_count_callback = callback

    def set_lyrics_state_getter(
        self, getter: Callable[[int], object | None]
    ) -> None:
        """Register a getter for per-guild lyrics state.

        Used for late-joiner sync: when a new client connects, the hub
        checks whether lyrics are enabled and sends the current lyrics
        payload if available.

        The getter accepts a guild_id and returns an object with `enabled`
        (bool) and `current_lyrics` (with `to_ws_message()` method) attributes,
        or None if no lyrics service exists for the guild.

        Uses a callback pattern to avoid circular imports between ws_hub
        and LyricsService.

        Args:
            getter: Callable(guild_id) -> LyricsState | None.
        """
        self._lyrics_state_getter = getter

    def viewer_count(self, guild_id: int) -> int:
        """Return the number of connected viewers for a guild."""
        return len(self._connections.get(guild_id, set()))

    def register_streamer(self, guild_id: int, streamer: ActivityStreamer) -> None:
        """Register an ActivityStreamer for countdown protocol integration.

        The WebSocketHub uses the streamer to check elapsed time, trigger
        countdowns, and handle ready messages.

        Args:
            guild_id: Guild ID this streamer belongs to.
            streamer: The ActivityStreamer instance.
        """
        self._streamers[guild_id] = streamer
        log.debug("Registered streamer for guild %d", guild_id)

    def unregister_streamer(self, guild_id: int) -> None:
        """Unregister the ActivityStreamer for a guild.

        Called when a video session ends or the streamer is destroyed.

        Args:
            guild_id: Guild ID to unregister.
        """
        self._streamers.pop(guild_id, None)
        log.debug("Unregistered streamer for guild %d", guild_id)

    async def _on_viewer_count_change(
        self, guild_id: int, old_count: int, new_count: int
    ) -> None:
        """Notify the registered callback of a viewer count transition.

        Logs the transition and swallows any callback exceptions to prevent
        viewer tracking issues from disrupting WebSocket operation.
        """
        log.debug(
            "Viewer count change for guild %d: %d → %d",
            guild_id,
            old_count,
            new_count,
        )
        if self._viewer_count_callback is not None:
            try:
                await self._viewer_count_callback(guild_id, old_count, new_count)
            except Exception:
                log.exception(
                    "Viewer count callback error for guild %d (%d → %d)",
                    guild_id,
                    old_count,
                    new_count,
                )

    async def handle_ws(self, request: web.Request) -> web.WebSocketResponse:
        """Handle incoming WebSocket connection request.

        Authenticates via the `token` query parameter using the same token
        scheme as HTTP routes (Discord instance_id). The guild_id from the
        URL path must match the guild_id embedded in the token.

        Route: GET /activity/ws/{guild_id}?token=<instance_id>
        """
        # Parse guild_id from URL
        raw_guild = request.match_info.get("guild_id", "")
        try:
            guild_id = int(raw_guild)
        except (ValueError, TypeError):
            return web.Response(status=400, text="Invalid guild ID")

        # Authenticate via token query param
        token = request.query.get("token", "").strip()
        if not token:
            return web.Response(status=401, text="Missing token")

        token_guild = self._validate_guild_token(token)
        if token_guild is None:
            return web.Response(status=401, text="Invalid token")

        if token_guild != guild_id:
            return web.Response(status=403, text="Token not authorized for this guild")

        # Upgrade to WebSocket
        ws = web.WebSocketResponse(heartbeat=_HEARTBEAT_INTERVAL)
        await ws.prepare(request)

        # Register connection
        if guild_id not in self._connections:
            self._connections[guild_id] = set()
        old_count = len(self._connections[guild_id])
        self._connections[guild_id].add(ws)
        new_count = len(self._connections[guild_id])

        log.debug("WebSocket connected for guild %d (total: %d)", guild_id, new_count)

        # Notify viewer count change (0→1 transition)
        if old_count == 0 and new_count == 1:
            await self._on_viewer_count_change(guild_id, 0, 1)

        # Send current state to late joiner (always includes strokes)
        state = self._playback_state.get(guild_id)
        strokes = self.get_stroke_registry(guild_id).get_all()
        streamer = self._streamers.get(guild_id)

        # Cancel any pending disconnect timeout if a viewer reconnects during COUNTDOWN
        if streamer is not None and streamer.countdown_active:
            if streamer._csm._disconnect_timer is not None:
                streamer._csm._disconnect_timer.cancel()
                streamer._csm._disconnect_timer = None
                log.debug(
                    "Disconnect timer cancelled for guild %d — viewer reconnected",
                    guild_id,
                )

        # Countdown protocol integration:
        # If a streamer is active, send phase-appropriate message to new client.
        countdown_sent = False
        if streamer is not None and streamer.state.value in ("buffering", "streaming"):
            if streamer.should_countdown():
                # First viewer in WAITING phase — start the countdown
                streamer.start_countdown()
                countdown_msg = {
                    "type": "countdown",
                    "seconds": streamer.countdown_seconds,
                    "video_title": streamer.source.title if streamer.source else "",
                    "phase": "countdown",
                }
                # Broadcast to ALL clients (including this new one)
                await self.broadcast(guild_id, countdown_msg)
                countdown_sent = True
            elif streamer.countdown_active and not streamer.playback_started:
                # Countdown already in progress — send remaining time to new joiner
                remaining = streamer.get_countdown_remaining()
                if remaining > 0:
                    countdown_msg = {
                        "type": "countdown",
                        "seconds": remaining,
                        "video_title": streamer.source.title if streamer.source else "",
                        "phase": "countdown",
                    }
                    try:
                        await ws.send_json(countdown_msg)
                    except (ConnectionResetError, RuntimeError):
                        self._connections[guild_id].discard(ws)
                        return ws
                    countdown_sent = True
            elif streamer.waiting_for_viewer:
                # Still in WAITING phase (segment zero not ready yet, or no
                # countdown triggered yet) — inform client
                waiting_msg = {
                    "type": "waiting",
                    "status": "waiting_for_segment_zero",
                }
                try:
                    await ws.send_json(waiting_msg)
                except (ConnectionResetError, RuntimeError):
                    self._connections[guild_id].discard(ws)
                    return ws
                countdown_sent = True

        if not countdown_sent and (state is not None or strokes):
            # Anchor-based late-joiner sync: send the anchor point so the client
            # can compute its own position using its local clock. No grace period
            # needed — the client only seeks if drift > 3s.
            # Include media_type so the frontend knows whether to enter VIDEO_PLAYING
            # or stay in visualizer mode (audio only).
            media_type = "video" if streamer is not None and streamer.is_active else "audio"
            state_msg = {
                "type": "state",
                "media_type": media_type,
                "playing": state.playing if state else False,
                "anchor_position": state.anchor_position if state else 0.0,
                "anchor_time_mono": state.anchor_time if state else time.monotonic(),
                "anchor_time": state.anchor_time_wall if state else time.time(),
                "position": state.position if state else 0.0,  # backward compat
                "timestamp": time.time(),
                "subtitle_lang": state.subtitle_lang if state else None,
                "audio_lang": state.audio_lang if state else None,
                "strokes": strokes,
            }
            try:
                await ws.send_json(state_msg)
            except (ConnectionResetError, RuntimeError):
                self._connections[guild_id].discard(ws)
                return ws

        # If no video streamer is active but a visualizer engine is configured,
        # send a visualizer activation message to the late-joiner so it enters
        # the correct mode (e.g. DVD screensaver).
        if streamer is None and not countdown_sent:
            try:
                import guild_settings
                engine = guild_settings.get_visualizer_engine(guild_id)
                if engine and engine != "off":
                    # Get guild icon URL (prefer guild icon for DVD screensaver)
                    icon_url = getattr(self, 'bot_avatar_url', '') or ""
                    if hasattr(self, '_bot_ref') and self._bot_ref is not None:
                        guild = self._bot_ref.get_guild(guild_id)
                        if guild and guild.icon:
                            icon_url = guild.icon.url
                    viz_msg = {
                        "type": "visualizer",
                        "engine": engine,
                        "state": "active",
                        "config": {
                            "avatar_url": icon_url,
                        },
                    }
                    await ws.send_json(viz_msg)
            except Exception:
                pass  # Non-fatal — visualizer info is best-effort

        # Late-joiner lyrics sync: send current lyrics if overlay is enabled
        if self._lyrics_state_getter is not None:
            try:
                lyrics_state = self._lyrics_state_getter(guild_id)
                if (
                    lyrics_state is not None
                    and lyrics_state.enabled
                    and lyrics_state.current_lyrics is not None
                ):
                    await ws.send_json(lyrics_state.current_lyrics.to_ws_message())
            except (ConnectionResetError, RuntimeError):
                self._connections[guild_id].discard(ws)
                return ws
            except Exception:
                # Lyrics sync failure must never disrupt client connection
                log.debug(
                    "Failed to send lyrics state to late-joiner for guild %d",
                    guild_id,
                    exc_info=True,
                )

        # Message loop
        try:
            async for msg in ws:
                if msg.type == web.WSMsgType.TEXT:
                    await self._handle_message(guild_id, ws, msg.data)
                elif msg.type in (web.WSMsgType.ERROR, web.WSMsgType.CLOSE):
                    break
        finally:
            conns = self._connections.get(guild_id, set())
            was_present = ws in conns
            conns.discard(ws)
            remaining = len(conns)
            log.debug(
                "WebSocket disconnected for guild %d (remaining: %d)",
                guild_id,
                remaining,
            )
            # Start 5s disconnect timeout if all viewers left during COUNTDOWN
            if was_present and remaining == 0:
                streamer = self._streamers.get(guild_id)
                if streamer is not None and streamer.countdown_active:
                    # Don't cancel immediately — allow 5s for reconnection
                    if streamer._csm._disconnect_timer is not None:
                        streamer._csm._disconnect_timer.cancel()
                    streamer._csm._disconnect_timer = asyncio.create_task(
                        self._countdown_disconnect_timeout(guild_id)
                    )

            # Notify viewer count change (1→0 transition)
            if was_present and remaining == 0:
                await self._on_viewer_count_change(guild_id, 1, 0)

        return ws

    async def _handle_message(
        self, guild_id: int, sender: web.WebSocketResponse, raw: str
    ) -> None:
        """Process an incoming WebSocket message from a client."""
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return

        msg_type = data.get("type")

        # Countdown protocol: handle `ready` message
        if msg_type == "ready":
            await self._handle_ready(guild_id, sender)
            return

        # Whiteboard message dispatch
        if msg_type == "stroke_add":
            await self._handle_stroke_add(guild_id, sender, data)
            return
        if msg_type == "stroke_remove":
            await self._handle_stroke_remove(guild_id, sender, data)
            return
        if msg_type == "whiteboard_reset":
            await self._handle_whiteboard_reset(guild_id, sender, data)
            return

        # Skip / Previous — delegate to unified controls (non-blocking)
        if msg_type == "skip":
            log.info("WS skip requested for guild %d", guild_id)
            asyncio.ensure_future(self._handle_unified_skip(guild_id))
            return
        if msg_type == "previous":
            log.info("WS previous requested for guild %d", guild_id)
            asyncio.ensure_future(self._handle_unified_previous(guild_id))
            return

        # Clock sync handshake — respond immediately regardless of playback state
        if msg_type == "clock_sync":
            await self._handle_clock_sync(guild_id, sender, data)
            return

        # Search protocol: search_request, search_cancel, search_play, search_enqueue
        if msg_type == "search_request":
            asyncio.ensure_future(self._handle_search_request(guild_id, sender, data))
            return
        if msg_type == "search_cancel":
            asyncio.ensure_future(self._handle_search_cancel(guild_id, data))
            return
        if msg_type == "search_play":
            asyncio.ensure_future(self._handle_search_play(guild_id, sender, data))
            return
        if msg_type == "search_enqueue":
            asyncio.ensure_future(self._handle_search_enqueue(guild_id, sender, data))
            return

        if msg_type not in ("play", "pause", "seek", "subtitle_change", "audio_change"):
            return

        # Update server-authoritative state
        state = self._playback_state.setdefault(guild_id, PlaybackState())

        if msg_type == "play":
            state.set_playing(True)
            # Also control Lavalink audio player if no video is active
            asyncio.ensure_future(self._handle_audio_play_pause(guild_id, playing=True))
        elif msg_type == "pause":
            pos = data.get("position")
            if pos is not None:
                state.seek_to(pos)
            state.set_playing(False)
            # Also control Lavalink audio player if no video is active
            asyncio.ensure_future(self._handle_audio_play_pause(guild_id, playing=False))
        elif msg_type == "seek":
            pos = data.get("position", state.anchor_position)
            state.seek_to(pos)
            # Forward seek to the ActivityStreamer so its elapsed timer stays in sync
            streamer = self._streamers.get(guild_id)
            if streamer is not None:
                streamer.on_seek(pos)
            else:
                # No video streamer — seek the Lavalink audio player
                asyncio.ensure_future(self._handle_audio_seek(guild_id, pos))
        elif msg_type == "subtitle_change":
            if data.get("for_everyone"):
                state.subtitle_lang = data.get("lang")
        elif msg_type == "audio_change":
            if data.get("for_everyone"):
                state.audio_lang = data.get("lang")

        # Broadcast to all other clients — include anchor_time for sync
        broadcast_msg = {
            **data,
            "anchor_time": state.anchor_time_wall,
            "anchor_time_mono": state.anchor_time,
            "anchor_position": state.anchor_position,
            "timestamp": time.time(),
        }
        await self.broadcast(guild_id, broadcast_msg, exclude=sender)

    async def _handle_ready(
        self, guild_id: int, sender: web.WebSocketResponse
    ) -> None:
        """Handle a client's `ready` message after countdown completes.

        Only the first `ready` triggers the `start` broadcast. Subsequent
        `ready` messages are ignored (edge case: multiple clients finish
        countdown at slightly different times).
        """
        streamer = self._streamers.get(guild_id)
        if streamer is None:
            # No active streamer — ignore stale ready
            return

        if not streamer.countdown_active:
            # No active countdown — ignore spurious ready
            return

        triggered = streamer.on_ready_received()
        if not triggered:
            # Playback already started or countdown not active — ignore
            return

        # Broadcast `start` to all connected clients EXCEPT the sender
        # (the sender already started HLS from its own countdown onComplete)
        mono_now = time.monotonic()
        start_msg = {
            "type": "start",
            "position": 0.0,
            "anchor_position": 0.0,
            "anchor_time_mono": mono_now,
            "anchor_time": mono_now + (time.time() - time.monotonic()),
            "timestamp": time.time(),
        }
        await self.broadcast(guild_id, start_msg, exclude=sender)

        # Update the PlaybackState to reflect position 0 playing
        state = self._playback_state.setdefault(guild_id, PlaybackState())
        state.playing = True
        state.anchor_position = 0.0
        state.anchor_time = time.monotonic()

        log.info(
            "Countdown complete for guild %d — broadcast start to %d clients",
            guild_id,
            len(self._connections.get(guild_id, set())),
        )

    async def _handle_clock_sync(
        self, guild_id: int, sender: web.WebSocketResponse, data: dict
    ) -> None:
        """Respond to clock_sync with server monotonic time.

        The reply echoes the client's original timestamp and provides the
        server's current monotonic clock value. The client uses the round-trip
        to compute its offset relative to the server clock. This handler
        responds regardless of the current playback or countdown state.
        """
        client_t1 = data.get("client_t1")
        if client_t1 is None:
            log.debug("clock_sync missing client_t1 from guild %d — ignoring", guild_id)
            return
        reply = {
            "type": "clock_sync_reply",
            "client_t1": client_t1,
            "server_mono": time.monotonic(),
        }
        await sender.send_json(reply)

    async def _handle_stroke_add(
        self, guild_id: int, sender: web.WebSocketResponse, data: dict
    ) -> None:
        """Validate, store, and broadcast a new stroke."""
        stroke_id = data.get("id")
        stroke_type = data.get("stroke_type")
        points = data.get("points")
        color = data.get("color")
        width = data.get("width")
        author = data.get("author")

        if not all([stroke_id, stroke_type, points, color, width is not None, author]):
            await self._send_error(sender, "stroke_add: missing required fields")
            return

        if stroke_type not in _VALID_STROKE_TYPES:
            await self._send_error(sender, f"stroke_add: invalid type '{stroke_type}'")
            return

        if not isinstance(points, list) or len(points) == 0:
            await self._send_error(sender, "stroke_add: empty points array")
            return

        if stroke_type == "sticker":
            sticker_category = data.get("sticker_category")
            sticker_filename = data.get("sticker_filename")
            if not sticker_category or not sticker_filename:
                await self._send_error(sender, "stroke_add: sticker requires sticker_category and sticker_filename")
                return

        registry = self.get_stroke_registry(guild_id)
        stroke_data = StrokeData(
            id=stroke_id,
            type=stroke_type,
            author=author,
            color=color,
            width=width,
            points=points,
            text=data.get("text"),
            text_bg=data.get("text_bg", False),
            sticker_category=data.get("sticker_category"),
            sticker_filename=data.get("sticker_filename"),
            animated=data.get("animated", False),
        )

        if not registry.add(stroke_data):
            await self._send_error(sender, "Whiteboard is full (500 stroke limit)")
            return

        broadcast_msg = {**data, "timestamp": time.time()}
        await self.broadcast(guild_id, broadcast_msg, exclude=sender)

    async def _handle_stroke_remove(
        self, guild_id: int, sender: web.WebSocketResponse, data: dict
    ) -> None:
        """Remove a stroke from registry and broadcast removal."""
        stroke_id = data.get("id")
        if not stroke_id:
            await self._send_error(sender, "stroke_remove: missing stroke ID")
            return

        registry = self.get_stroke_registry(guild_id)
        if not registry.remove(stroke_id):
            # Stroke not found — possibly already removed. Silently ignore.
            return

        broadcast_msg = {"type": "stroke_remove", "id": stroke_id, "timestamp": time.time()}
        await self.broadcast(guild_id, broadcast_msg, exclude=sender)

    async def _handle_whiteboard_reset(
        self, guild_id: int, sender: web.WebSocketResponse, data: dict
    ) -> None:
        """Clear all strokes and broadcast reset to all viewers."""
        registry = self.get_stroke_registry(guild_id)
        registry.clear()

        broadcast_msg = {"type": "whiteboard_reset", "timestamp": time.time()}
        await self.broadcast(guild_id, broadcast_msg, exclude=sender)

    async def broadcast(
        self,
        guild_id: int,
        message: dict,
        *,
        exclude: web.WebSocketResponse | None = None,
    ) -> None:
        """Send a JSON message to all connected clients for a guild.

        Args:
            guild_id: The guild to broadcast to.
            message: The JSON-serializable message dict.
            exclude: Optional WebSocket to exclude (typically the sender).
        """
        connections = self._connections.get(guild_id, set())
        stale: list[web.WebSocketResponse] = []

        for ws in connections:
            if ws is exclude:
                continue
            try:
                await ws.send_json(message)
            except (ConnectionResetError, RuntimeError):
                stale.append(ws)

        # Clean up stale connections and check for viewer count transitions
        if stale:
            count_before = len(connections)
            for ws in stale:
                connections.discard(ws)
            count_after = len(connections)
            if count_before > 0 and count_after == 0:
                await self._on_viewer_count_change(guild_id, count_before, 0)

    async def broadcast_from_bot(self, guild_id: int, message: dict) -> None:
        """Send a JSON message to ALL connected clients for a guild.

        Used by bot-side controls (Now Playing embed buttons) where there
        is no sender to exclude.

        Args:
            guild_id: The guild to broadcast to.
            message: The JSON-serializable message dict.
        """
        await self.broadcast(guild_id, message)

    async def _handle_unified_skip(self, guild_id: int) -> None:
        """Background task: run unified skip and broadcast result."""
        try:
            from playback.unified_controls import unified_skip
            result = await unified_skip(guild_id)
            log.info("WS skip result for guild %d: %s", guild_id, result)
        except Exception as exc:
            log.error("WS skip failed for guild %d: %s", guild_id, exc)
        await self.broadcast(guild_id, {"type": "session_change"}, exclude=None)

    async def _handle_unified_previous(self, guild_id: int) -> None:
        """Background task: run unified previous and broadcast result."""
        try:
            from playback.unified_controls import unified_previous
            result = await unified_previous(guild_id)
            log.info("WS previous result for guild %d: %s", guild_id, result)
        except Exception as exc:
            log.error("WS previous failed for guild %d: %s", guild_id, exc)
        await self.broadcast(guild_id, {"type": "session_change"}, exclude=None)

    async def _handle_audio_play_pause(self, guild_id: int, *, playing: bool) -> None:
        """Control the Lavalink audio player play/pause from Activity controls.

        Only acts when no video streamer is active for this guild — otherwise
        the play/pause is for the video element (handled by client-side HLS).
        """
        streamer = self._streamers.get(guild_id)
        if streamer is not None and streamer.is_active:
            return  # Video is active — play/pause is for the video element

        try:
            import player
            p = player.get_player(guild_id)
            if p and p.connected:
                if playing and p.paused:
                    await p.pause(False)
                    log.info("WS play: resumed audio for guild %d", guild_id)
                elif not playing and p.playing:
                    await p.pause(True)
                    log.info("WS pause: paused audio for guild %d", guild_id)
        except Exception as exc:
            log.warning("WS audio play/pause failed for guild %d: %s", guild_id, exc)

    async def _handle_audio_seek(self, guild_id: int, position: float) -> None:
        """Seek the Lavalink audio player from Activity scrubber.

        Only acts when no video streamer is active — video seek is handled
        by the HLS element directly.
        """
        try:
            import player
            p = player.get_player(guild_id)
            if p and p.connected and (p.playing or p.paused):
                # position is in seconds from the frontend; Lavalink expects milliseconds
                position_ms = int(position * 1000)
                await p.seek(position_ms)
                log.info("WS seek: seeked audio to %.1fs for guild %d", position, guild_id)
        except Exception as exc:
            log.warning("WS audio seek failed for guild %d: %s", guild_id, exc)

    # ------------------------------------------------------------------
    # Search protocol handlers
    # ------------------------------------------------------------------

    def _get_search_engine(self):
        """Lazily initialize and return the shared UnifiedSearchEngine."""
        if self._search_engine is None:
            from search import UnifiedSearchEngine
            self._search_engine = UnifiedSearchEngine()
        return self._search_engine

    async def _handle_search_request(
        self, guild_id: int, sender: web.WebSocketResponse, data: dict
    ) -> None:
        """Handle a search_request message: stream results as providers respond.

        Cancels any previous in-flight search for this guild, then runs a new
        streaming search in a background task.

        Requirements: 17.1, 17.2, 17.3, 17.4, 17.5, 17.6, 17.7, 17.8
        """
        request_id = data.get("request_id", "")
        query = data.get("query", "")
        filters = data.get("filters", {})

        # Cancel all previous searches for this guild
        guild_searches = self._active_searches.setdefault(guild_id, {})
        for rid, task in list(guild_searches.items()):
            if not task.done():
                task.cancel()
        guild_searches.clear()

        # Define callback that sends partial results to this client
        async def on_provider_result(provider: str, results: list) -> None:
            msg = {
                "type": "search_partial_result",
                "request_id": request_id,
                "provider": provider,
                "results": [self._serialize_search_result(r) for r in results],
            }
            try:
                await sender.send_json(msg)
            except (ConnectionResetError, RuntimeError):
                pass

        # Run search in background task
        async def _do_search() -> None:
            try:
                engine = self._get_search_engine()
                all_results = await engine.search_streaming(
                    query,
                    guild_id=guild_id,
                    provider_filter=filters.get("provider") if filters.get("provider") != "all" else None,
                    content_type=filters.get("content_type", "tracks"),
                    sort_order=filters.get("sort_order", "relevance"),
                    on_provider_result=on_provider_result,
                )
                await sender.send_json({
                    "type": "search_complete",
                    "request_id": request_id,
                    "total_results": len(all_results),
                })
            except asyncio.CancelledError:
                pass  # Search was superseded by a new request
            except Exception as e:
                log.warning("Search failed for guild %d request %s: %s", guild_id, request_id, e)
                try:
                    await sender.send_json({
                        "type": "search_error",
                        "request_id": request_id,
                        "message": str(e),
                    })
                except (ConnectionResetError, RuntimeError):
                    pass
            finally:
                guild_searches.pop(request_id, None)

        task = asyncio.create_task(_do_search())
        guild_searches[request_id] = task

    async def _handle_search_cancel(self, guild_id: int, data: dict) -> None:
        """Handle a search_cancel message: cancel in-flight search tasks.

        Requirements: 17.6
        """
        request_id = data.get("request_id", "")
        guild_searches = self._active_searches.get(guild_id, {})
        task = guild_searches.pop(request_id, None)
        if task and not task.done():
            task.cancel()

    async def _handle_search_play(
        self, guild_id: int, sender: web.WebSocketResponse, data: dict
    ) -> None:
        """Handle a search_play message: resolve track and start playback.

        Decodes provider + track_id, resolves via wavelink, delegates to the
        player module's queue system for immediate playback.

        Requirements: 16.1
        """
        request_id = data.get("request_id", "")
        provider = data.get("provider", "")
        track_id = data.get("track_id", "")

        try:
            import wavelink
            import player as player_module
            from search.formatter import ChoiceFormatter

            # Encode to get the lavalink prefix
            encoded = ChoiceFormatter.encode_value(provider, track_id)
            lavalink_prefix, decoded_id = ChoiceFormatter.decode_value(encoded)

            if lavalink_prefix is None:
                raise ValueError(f"Unknown provider: {provider}")

            # Resolve the track via wavelink
            # YouTube needs full URL (ytsearch: is for text queries, not ID lookups)
            if lavalink_prefix == "ytsearch":
                search_query = f"https://www.youtube.com/watch?v={decoded_id}"
            else:
                search_query = f"{lavalink_prefix}:{decoded_id}"
            tracks = await wavelink.Playable.search(search_query)
            if not tracks:
                raise ValueError(f"No playable track found for {provider}:{track_id}")

            track = tracks[0]
            title = getattr(track, "title", "Unknown")

            # Use the player module to start/enqueue the track immediately
            state = player_module.get_state(guild_id)
            entry = player_module._track_entry(track, provider)

            # Clear queue and set as current for immediate playback
            p = player_module.get_player(guild_id)
            if p and p.connected:
                # Insert at front and trigger play under the queue lock
                state["queue"].insert(0, entry)
                player_module.persist(guild_id)
                lock = player_module._get_queue_lock(guild_id)
                async with lock:
                    if p.playing or p.paused:
                        await p.stop()
                    await player_module._play_next_from_queue(guild_id)

            await sender.send_json({
                "type": "search_play_ack",
                "request_id": request_id,
                "success": True,
                "track_title": title,
            })
        except Exception as e:
            log.warning("search_play failed for guild %d: %s", guild_id, e)
            try:
                await sender.send_json({
                    "type": "search_play_ack",
                    "request_id": request_id,
                    "success": False,
                    "message": str(e),
                })
            except (ConnectionResetError, RuntimeError):
                pass

    async def _handle_search_enqueue(
        self, guild_id: int, sender: web.WebSocketResponse, data: dict
    ) -> None:
        """Handle a search_enqueue message: resolve track and add to queue.

        Does not interrupt current playback — appends to the end of the queue.

        Requirements: 16.3
        """
        request_id = data.get("request_id", "")
        provider = data.get("provider", "")
        track_id = data.get("track_id", "")

        try:
            import wavelink
            import player as player_module
            from search.formatter import ChoiceFormatter

            # Encode to get the lavalink prefix
            encoded = ChoiceFormatter.encode_value(provider, track_id)
            lavalink_prefix, decoded_id = ChoiceFormatter.decode_value(encoded)

            if lavalink_prefix is None:
                raise ValueError(f"Unknown provider: {provider}")

            # Resolve the track via wavelink
            # YouTube needs full URL (ytsearch: is for text queries, not ID lookups)
            if lavalink_prefix == "ytsearch":
                search_query = f"https://www.youtube.com/watch?v={decoded_id}"
            else:
                search_query = f"{lavalink_prefix}:{decoded_id}"
            tracks = await wavelink.Playable.search(search_query)
            if not tracks:
                raise ValueError(f"No playable track found for {provider}:{track_id}")

            track = tracks[0]
            title = getattr(track, "title", "Unknown")

            # Add to queue without interrupting current playback
            state = player_module.get_state(guild_id)
            entry = player_module._track_entry(track, provider)
            await player_module.add_track(state, guild_id, entry)
            position = len(state["queue"])

            await sender.send_json({
                "type": "search_enqueue_ack",
                "request_id": request_id,
                "success": True,
                "position": position,
                "track_title": title,
            })
        except Exception as e:
            log.warning("search_enqueue failed for guild %d: %s", guild_id, e)
            try:
                await sender.send_json({
                    "type": "search_enqueue_ack",
                    "request_id": request_id,
                    "success": False,
                    "position": 0,
                    "message": str(e),
                })
            except (ConnectionResetError, RuntimeError):
                pass

    @staticmethod
    def _serialize_search_result(result: "SearchResult") -> dict:
        """Convert a SearchResult to a JSON-serializable dict for WebSocket."""
        return {
            "title": result.title,
            "artist": result.artist,
            "album": result.album,
            "release_year": result.release_year,
            "duration_ms": result.duration_ms,
            "artwork_url": result.artwork_url,
            "isrc": result.isrc,
            "provider": result.provider,
            "track_id": result.track_id,
            "variant_type": result.variant_type,
            "has_music_video": result.has_music_video,
        }

    def get_state(self, guild_id: int) -> PlaybackState | None:
        """Get the current playback state for a guild."""
        return self._playback_state.get(guild_id)

    def set_state(self, guild_id: int, state: PlaybackState) -> None:
        """Set the playback state for a guild."""
        self._playback_state[guild_id] = state

    async def _countdown_disconnect_timeout(self, guild_id: int) -> None:
        """5s timeout: if no viewer reconnects, reset countdown to WAITING."""
        try:
            await asyncio.sleep(5.0)
        except asyncio.CancelledError:
            return
        streamer = self._streamers.get(guild_id)
        if streamer is not None and streamer.countdown_active:
            streamer._csm.reset()
            streamer._csm._disconnect_timer = None
            log.info(
                "Countdown reset for guild %d — no viewer reconnected within 5s",
                guild_id,
            )

    async def disconnect_all(self, guild_id: int) -> None:
        """Close all WebSocket connections for a guild (on session end)."""
        connections = self._connections.pop(guild_id, set())
        previous_count = len(connections)
        for ws in connections:
            try:
                await ws.close()
            except (ConnectionResetError, RuntimeError):
                pass
        self._playback_state.pop(guild_id, None)
        # Notify viewer count change if viewers were connected
        if previous_count > 0:
            await self._on_viewer_count_change(guild_id, previous_count, 0)
        log.debug("Disconnected all WebSocket clients for guild %d", guild_id)

    # ------------------------------------------------------------------
    # Stroke registry management
    # ------------------------------------------------------------------

    def get_stroke_registry(self, guild_id: int) -> StrokeRegistry:
        """Get or create the stroke registry for a guild."""
        if guild_id not in self._stroke_registries:
            self._stroke_registries[guild_id] = StrokeRegistry()
        return self._stroke_registries[guild_id]

    def clear_stroke_registry(self, guild_id: int) -> None:
        """Clear all strokes for a guild (session end)."""
        registry = self._stroke_registries.pop(guild_id, None)
        if registry:
            registry.clear()

    def init_stroke_registry(self, guild_id: int) -> None:
        """Initialize an empty stroke registry for a new session."""
        self._stroke_registries[guild_id] = StrokeRegistry()

    # ------------------------------------------------------------------
    # Error helper
    # ------------------------------------------------------------------

    async def _send_error(self, ws: web.WebSocketResponse, message: str) -> None:
        """Send an error notification to a single client."""
        try:
            await ws.send_json({"type": "error", "message": message})
        except (ConnectionResetError, RuntimeError):
            pass

    async def notify_visualizer_error(
        self, guild_id: int, engine: str, message: str = ""
    ) -> None:
        """Broadcast a visualizer error notification to all viewers.

        Called by VisualizerManager when entering ERROR state (Req 11 AC 2).

        Args:
            guild_id: The guild experiencing the error.
            engine: The engine type that failed.
            message: Optional human-readable error description.
        """
        error_msg = {
            "type": "visualizer_error",
            "engine": engine,
            "message": message or f"Visualizer engine '{engine}' encountered an error",
            "fallback": "dvd",
        }
        await self.broadcast(guild_id, error_msg)

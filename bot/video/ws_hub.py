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

log = logging.getLogger(__name__)

_HEARTBEAT_INTERVAL = 30.0  # seconds between server pings

_VALID_STROKE_TYPES = {"freehand", "line", "rect", "ellipse", "arrow", "text", "sticker"}


@dataclasses.dataclass
class PlaybackState:
    """Server-authoritative playback state for a guild."""

    playing: bool = True
    position: float = 0.0  # seconds
    last_update: float = dataclasses.field(default_factory=time.monotonic)
    subtitle_lang: str | None = None  # "for everyone" subtitle
    audio_lang: str | None = None  # "for everyone" audio track


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

        # Countdown protocol integration:
        # If a streamer is active and within the first 5s, trigger or join countdown
        # instead of sending the regular state message.
        countdown_sent = False
        if streamer is not None and streamer.state.value in ("buffering", "streaming"):
            if streamer.should_countdown():
                # First viewer within 5s — start the countdown
                streamer.start_countdown()
                countdown_msg = {
                    "type": "countdown",
                    "seconds": streamer.countdown_seconds,
                    "video_title": streamer.source.title if streamer.source else "",
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
                    }
                    try:
                        await ws.send_json(countdown_msg)
                    except (ConnectionResetError, RuntimeError):
                        self._connections[guild_id].discard(ws)
                        return ws
                    countdown_sent = True

        if not countdown_sent and (state is not None or strokes):
            # Standard late-joiner sync: send computed position.
            # Skip position sync during the first 15s after playback_started
            # to avoid jitter while the client is still buffering/stabilizing.
            streamer_obj = self._streamers.get(guild_id)
            grace_period_active = False
            if streamer_obj and streamer_obj.playback_started and streamer_obj.start_time > 0:
                time_since_start = time.monotonic() - streamer_obj.start_time
                if time_since_start < 15.0:
                    grace_period_active = True

            if state and state.playing:
                elapsed = time.monotonic() - state.last_update
                current_position = state.position + elapsed
            else:
                current_position = state.position if state else 0.0

            state_msg = {
                "type": "state",
                "playing": state.playing if state else False,
                # During grace period, send position 0 to avoid seeking
                "position": 0.0 if grace_period_active else current_position,
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
            # Cancel countdown if all viewers disconnected
            if was_present and remaining == 0:
                streamer = self._streamers.get(guild_id)
                if streamer is not None and streamer.countdown_active:
                    streamer.cancel_countdown()

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

        # Skip / Previous — trigger queue navigation via the streamer
        if msg_type == "skip":
            log.info("WS skip requested for guild %d", guild_id)
            streamer = self._streamers.get(guild_id)
            if streamer and hasattr(streamer, "skip"):
                try:
                    await streamer.skip()
                except Exception as exc:
                    log.debug("WS skip failed for guild %d: %s", guild_id, exc)
                    # If streamer is idle, try advancing the unified player queue
                    await self._try_unified_queue_advance(guild_id)
                else:
                    # Skip succeeded. If streamer ended up idle (queue was empty),
                    # advance the unified player queue.
                    if streamer.state.value == "idle":
                        await self._try_unified_queue_advance(guild_id)
                # Notify all clients to check for new session
                await self.broadcast(guild_id, {"type": "session_change"}, exclude=None)
            return
        if msg_type == "previous":
            log.info("WS previous requested for guild %d", guild_id)
            streamer = self._streamers.get(guild_id)
            if streamer and hasattr(streamer, "previous"):
                try:
                    await streamer.previous()
                except Exception as exc:
                    log.debug("WS previous failed for guild %d: %s", guild_id, exc)
                # Notify all clients to check for new session
                await self.broadcast(guild_id, {"type": "session_change"}, exclude=None)
            return

        if msg_type not in ("play", "pause", "seek", "subtitle_change", "audio_change"):
            return

        # Update server-authoritative state
        state = self._playback_state.setdefault(guild_id, PlaybackState())

        if msg_type == "play":
            state.playing = True
            state.position = data.get("position", state.position)
            state.last_update = time.monotonic()
        elif msg_type == "pause":
            state.playing = False
            state.position = data.get("position", state.position)
            state.last_update = time.monotonic()
        elif msg_type == "seek":
            state.position = data.get("position", state.position)
            state.last_update = time.monotonic()
        elif msg_type == "subtitle_change":
            if data.get("for_everyone"):
                state.subtitle_lang = data.get("lang")
        elif msg_type == "audio_change":
            if data.get("for_everyone"):
                state.audio_lang = data.get("lang")

        # Broadcast to all other clients in this guild
        broadcast_msg = {**data, "timestamp": time.time()}
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
        start_msg = {
            "type": "start",
            "position": 0.0,
            "timestamp": time.time(),
        }
        await self.broadcast(guild_id, start_msg, exclude=sender)

        # Update the PlaybackState to reflect position 0 playing
        state = self._playback_state.setdefault(guild_id, PlaybackState())
        state.playing = True
        state.position = 0.0
        state.last_update = time.monotonic()

        log.info(
            "Countdown complete for guild %d — broadcast start to %d clients",
            guild_id,
            len(self._connections.get(guild_id, set())),
        )

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

    async def _try_unified_queue_advance(self, guild_id: int) -> None:
        """Try to advance the unified player queue after video skip with empty queue.

        Imports the player module and calls _play_next_from_queue if the
        unified queue has items. This bridges the video Activity skip button
        to the bot's unified playback system.
        """
        try:
            from player import get_state, _play_next_from_queue

            state = get_state(guild_id)
            if state and state.get("queue"):
                log.info(
                    "WS skip: video queue empty, advancing unified queue for guild %d (%d items)",
                    guild_id,
                    len(state["queue"]),
                )
                state["current"] = None
                await _play_next_from_queue(guild_id)
            else:
                log.debug("WS skip: both video and unified queues empty for guild %d", guild_id)
        except Exception as exc:
            log.warning("WS skip: failed to advance unified queue for guild %d: %s", guild_id, exc)

    def get_state(self, guild_id: int) -> PlaybackState | None:
        """Get the current playback state for a guild."""
        return self._playback_state.get(guild_id)

    def set_state(self, guild_id: int, state: PlaybackState) -> None:
        """Set the playback state for a guild."""
        self._playback_state[guild_id] = state

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

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
from typing import Callable

from aiohttp import web

from video.stroke_registry import StrokeData, StrokeRegistry

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
        self._connections[guild_id].add(ws)

        log.debug("WebSocket connected for guild %d (total: %d)", guild_id, len(self._connections[guild_id]))

        # Send current state to late joiner (always includes strokes)
        state = self._playback_state.get(guild_id)
        strokes = self.get_stroke_registry(guild_id).get_all()
        if state is not None or strokes:
            # Compute current position from elapsed time if playing
            if state and state.playing:
                elapsed = time.monotonic() - state.last_update
                current_position = state.position + elapsed
            else:
                current_position = state.position if state else 0.0

            state_msg = {
                "type": "state",
                "playing": state.playing if state else False,
                "position": current_position,
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

        # Message loop
        try:
            async for msg in ws:
                if msg.type == web.WSMsgType.TEXT:
                    await self._handle_message(guild_id, ws, msg.data)
                elif msg.type in (web.WSMsgType.ERROR, web.WSMsgType.CLOSE):
                    break
        finally:
            self._connections.get(guild_id, set()).discard(ws)
            log.debug(
                "WebSocket disconnected for guild %d (remaining: %d)",
                guild_id,
                len(self._connections.get(guild_id, set())),
            )

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

        # Clean up stale connections
        for ws in stale:
            connections.discard(ws)

    async def broadcast_from_bot(self, guild_id: int, message: dict) -> None:
        """Send a JSON message to ALL connected clients for a guild.

        Used by bot-side controls (Now Playing embed buttons) where there
        is no sender to exclude.

        Args:
            guild_id: The guild to broadcast to.
            message: The JSON-serializable message dict.
        """
        await self.broadcast(guild_id, message)

    def get_state(self, guild_id: int) -> PlaybackState | None:
        """Get the current playback state for a guild."""
        return self._playback_state.get(guild_id)

    def set_state(self, guild_id: int, state: PlaybackState) -> None:
        """Set the playback state for a guild."""
        self._playback_state[guild_id] = state

    async def disconnect_all(self, guild_id: int) -> None:
        """Close all WebSocket connections for a guild (on session end)."""
        connections = self._connections.pop(guild_id, set())
        for ws in connections:
            try:
                await ws.close()
            except (ConnectionResetError, RuntimeError):
                pass
        self._playback_state.pop(guild_id, None)
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

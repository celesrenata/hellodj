"""WebSocket synchronization hub for the Activity (Requirements 6.2, 18.2).

Manages per-guild WebSocket connections and the server-authoritative state that
keeps all connected Activity clients in sync: video play/pause/seek, whiteboard
strokes, visualizer control, and synced lyrics. When any client sends a control
message, it is broadcast to the other clients in the same guild; late joiners
receive the current state on connect.

The hub runs behind ALB/CloudFront at ``/activity/ws/{guild_id}`` (R18.2).
aiohttp is imported lazily inside the connection handler so the module — and the
pure message-handling logic exercised by tests — imports without aiohttp
installed.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from .lyrics import LyricsStore
from .models import PlaybackState
from .visualizer import VisualizerRegistry
from .whiteboard import StrokeRegistry, validate_stroke_payload

log = logging.getLogger(__name__)

__all__ = ["WebSocketHub", "GuildTokenValidator"]

#: Maps a token string to a guild id (or ``None`` if invalid).
GuildTokenValidator = Callable[[str], "int | None"]

# Control messages that mutate playback state and are re-broadcast.
_PLAYBACK_MESSAGES = frozenset(
    {"play", "pause", "seek", "subtitle_change", "audio_change"}
)


class WebSocketHub:
    """Per-guild WebSocket connection manager for Activity state sync.

    The hub owns:
        * the set of live connections per guild,
        * the authoritative :class:`PlaybackState` per guild,
        * a :class:`StrokeRegistry` per guild (whiteboard),
        * references to the shared visualizer and lyrics stores (control plane).

    It exposes :meth:`handle_ws` for the aiohttp route and a set of pure
    ``apply_*`` helpers (used by both the socket loop and tests) that update
    state and return the message that should be broadcast.
    """

    def __init__(
        self,
        validate_guild_token: GuildTokenValidator,
        visualizer: VisualizerRegistry | None = None,
        lyrics: LyricsStore | None = None,
        max_strokes: int = 500,
        heartbeat_interval_s: float = 30.0,
    ) -> None:
        """Initialise the hub with a token validator and shared stores."""
        self._validate_guild_token = validate_guild_token
        self._visualizer = visualizer or VisualizerRegistry()
        self._lyrics = lyrics or LyricsStore()
        self._max_strokes = max_strokes
        self._heartbeat = heartbeat_interval_s
        self._connections: dict[int, set[Any]] = {}
        self._playback: dict[int, PlaybackState] = {}
        self._strokes: dict[int, StrokeRegistry] = {}
        self._active_video: set[int] = set()
        self._viewer_cb: (
            Callable[[int, int, int], Awaitable[None]] | None
        ) = None

    # ------------------------------------------------------------------ #
    # Accessors / registration
    # ------------------------------------------------------------------ #
    def set_viewer_count_callback(
        self, callback: Callable[[int, int, int], Awaitable[None]]
    ) -> None:
        """Register an async callback for 0↔1 viewer-count transitions."""
        self._viewer_cb = callback

    def viewer_count(self, guild_id: int) -> int:
        """Return the number of connected viewers for a guild."""
        return len(self._connections.get(guild_id, set()))

    def stroke_registry(self, guild_id: int) -> StrokeRegistry:
        """Return (creating if needed) the guild's stroke registry."""
        reg = self._strokes.get(guild_id)
        if reg is None:
            reg = StrokeRegistry(self._max_strokes)
            self._strokes[guild_id] = reg
        return reg

    def playback_state(self, guild_id: int) -> PlaybackState:
        """Return (creating if needed) the guild's playback state."""
        state = self._playback.get(guild_id)
        if state is None:
            state = PlaybackState(playing=False)
            self._playback[guild_id] = state
        return state

    def mark_video_active(self, guild_id: int, active: bool) -> None:
        """Record whether a video stream is currently active for the guild."""
        if active:
            self._active_video.add(guild_id)
        else:
            self._active_video.discard(guild_id)

    def media_type(self, guild_id: int) -> str:
        """Return ``"video"`` if a video stream is active, else ``"audio"``."""
        return "video" if guild_id in self._active_video else "audio"

    # ------------------------------------------------------------------ #
    # Pure state transitions (shared by socket loop and tests)
    # ------------------------------------------------------------------ #
    def apply_playback(self, guild_id: int, data: dict) -> dict | None:
        """Apply a playback control message and return the broadcast payload.

        Returns ``None`` for message types that are not playback controls.
        """
        msg_type = data.get("type")
        if msg_type not in _PLAYBACK_MESSAGES:
            return None
        state = self.playback_state(guild_id)
        if msg_type == "play":
            state.set_playing(True)
        elif msg_type == "pause":
            pos = data.get("position")
            if pos is not None:
                state.seek_to(pos)
            state.set_playing(False)
        elif msg_type == "seek":
            state.seek_to(data.get("position", state.anchor_position))
        elif msg_type == "subtitle_change" and data.get("for_everyone"):
            state.subtitle_lang = data.get("lang")
        elif msg_type == "audio_change" and data.get("for_everyone"):
            state.audio_lang = data.get("lang")
        return {
            **data,
            "anchor_time": state.anchor_time_wall,
            "anchor_time_mono": state.anchor_time,
            "anchor_position": state.anchor_position,
            "timestamp": time.time(),
        }

    def apply_stroke_add(self, guild_id: int, data: dict) -> tuple[dict | None, str | None]:
        """Validate + store a stroke; return ``(broadcast, error)``."""
        stroke, error = validate_stroke_payload(data)
        if error is not None or stroke is None:
            return None, error
        if not self.stroke_registry(guild_id).add(stroke):
            return None, "stroke_add: registry at capacity"
        return {**data, "timestamp": time.time()}, None

    def apply_stroke_remove(self, guild_id: int, data: dict) -> dict | None:
        """Remove a stroke; return the broadcast payload if it existed."""
        stroke_id = data.get("id")
        if not stroke_id:
            return None
        if not self.stroke_registry(guild_id).remove(str(stroke_id)):
            return None
        return {**data, "timestamp": time.time()}

    def apply_whiteboard_reset(self, guild_id: int, data: dict) -> dict:
        """Clear all strokes for a guild and return the broadcast payload."""
        self.stroke_registry(guild_id).clear()
        return {**data, "timestamp": time.time()}

    def build_late_join_messages(self, guild_id: int) -> list[dict]:
        """Assemble the messages a newly-connected client should receive.

        Order: current playback state (with strokes), then visualizer state (if
        no video is active and an engine is selected), then lyrics (if enabled).
        """
        messages: list[dict] = []
        state = self._playback.get(guild_id)
        strokes = self.stroke_registry(guild_id).get_all()
        if state is not None or strokes:
            base = (
                state.to_message(self.media_type(guild_id))
                if state is not None
                else PlaybackState(playing=False).to_message(
                    self.media_type(guild_id)
                )
            )
            base["strokes"] = strokes
            messages.append(base)
        if guild_id not in self._active_video:
            viz = self._visualizer.state_message(guild_id)
            if viz is not None:
                messages.append(viz)
        lyrics = self._lyrics.state_message(guild_id)
        if lyrics is not None:
            messages.append(lyrics)
        return messages

    # ------------------------------------------------------------------ #
    # Broadcast
    # ------------------------------------------------------------------ #
    async def broadcast(
        self, guild_id: int, message: dict, exclude: Any | None = None
    ) -> None:
        """Send ``message`` (as JSON) to all guild clients except ``exclude``."""
        conns = list(self._connections.get(guild_id, set()))
        dead: list[Any] = []
        for ws in conns:
            if ws is exclude:
                continue
            try:
                await ws.send_json(message)
            except (ConnectionResetError, RuntimeError):
                dead.append(ws)
        for ws in dead:
            self._connections.get(guild_id, set()).discard(ws)

    # ------------------------------------------------------------------ #
    # aiohttp connection handling (lazy aiohttp import)
    # ------------------------------------------------------------------ #
    async def handle_ws(self, request: Any) -> Any:
        """Handle a WebSocket upgrade at ``/activity/ws/{guild_id}``.

        Authenticates via the ``token`` query parameter (mapped to a guild id by
        the injected validator); the token's guild must match the URL guild.
        aiohttp is imported here so the module loads without it installed.
        """
        from aiohttp import web

        raw_guild = request.match_info.get("guild_id", "")
        try:
            guild_id = int(raw_guild)
        except (ValueError, TypeError):
            return web.Response(status=400, text="Invalid guild ID")

        token = request.query.get("token", "").strip()
        if not token:
            return web.Response(status=401, text="Missing token")
        token_guild = self._validate_guild_token(token)
        if token_guild is None:
            return web.Response(status=401, text="Invalid token")
        if token_guild != guild_id:
            return web.Response(status=403, text="Token not authorized")

        ws = web.WebSocketResponse(heartbeat=self._heartbeat)
        await ws.prepare(request)
        await self._register(guild_id, ws)
        try:
            await self._recv_loop(guild_id, ws, web)
        finally:
            await self._unregister(guild_id, ws)
        return ws

    async def _register(self, guild_id: int, ws: Any) -> None:
        """Add a connection and send late-joiner state."""
        conns = self._connections.setdefault(guild_id, set())
        old = len(conns)
        conns.add(ws)
        if old == 0:
            await self._notify_viewer_change(guild_id, 0, 1)
        for message in self.build_late_join_messages(guild_id):
            try:
                await ws.send_json(message)
            except (ConnectionResetError, RuntimeError):
                conns.discard(ws)
                return

    async def _unregister(self, guild_id: int, ws: Any) -> None:
        """Remove a connection and fire the 1→0 viewer transition."""
        conns = self._connections.get(guild_id, set())
        was_present = ws in conns
        conns.discard(ws)
        if was_present and len(conns) == 0:
            await self._notify_viewer_change(guild_id, 1, 0)

    async def _recv_loop(self, guild_id: int, ws: Any, web: Any) -> None:
        """Read and dispatch messages until the socket closes."""
        async for msg in ws:
            if msg.type == web.WSMsgType.TEXT:
                await self._dispatch(guild_id, ws, msg.data)
            elif msg.type in (web.WSMsgType.ERROR, web.WSMsgType.CLOSE):
                break

    async def _dispatch(self, guild_id: int, sender: Any, raw: str) -> None:
        """Decode one text frame and route it to the right handler."""
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return
        msg_type = data.get("type")

        if msg_type == "clock_sync":
            await self._reply_clock_sync(sender, data)
            return
        if msg_type == "stroke_add":
            broadcast, error = self.apply_stroke_add(guild_id, data)
            if error is not None:
                await self._send_error(sender, error)
            elif broadcast is not None:
                await self.broadcast(guild_id, broadcast, exclude=sender)
            return
        if msg_type == "stroke_remove":
            broadcast = self.apply_stroke_remove(guild_id, data)
            if broadcast is not None:
                await self.broadcast(guild_id, broadcast, exclude=sender)
            return
        if msg_type == "whiteboard_reset":
            await self.broadcast(
                guild_id,
                self.apply_whiteboard_reset(guild_id, data),
                exclude=sender,
            )
            return

        broadcast = self.apply_playback(guild_id, data)
        if broadcast is not None:
            await self.broadcast(guild_id, broadcast, exclude=sender)

    async def _reply_clock_sync(self, sender: Any, data: dict) -> None:
        """Answer a ``clock_sync`` with the server monotonic time."""
        client_t1 = data.get("client_t1")
        if client_t1 is None:
            return
        await sender.send_json(
            {
                "type": "clock_sync_reply",
                "client_t1": client_t1,
                "server_mono": time.monotonic(),
            }
        )

    async def _send_error(self, sender: Any, message: str) -> None:
        """Send a best-effort ``error`` frame to a single client."""
        try:
            await sender.send_json({"type": "error", "message": message})
        except (ConnectionResetError, RuntimeError):
            pass

    async def _notify_viewer_change(
        self, guild_id: int, old_count: int, new_count: int
    ) -> None:
        """Invoke the viewer-count callback, swallowing callback errors."""
        if self._viewer_cb is None:
            return
        try:
            await self._viewer_cb(guild_id, old_count, new_count)
        except Exception:
            log.exception(
                "viewer-count callback failed for guild %d (%d->%d)",
                guild_id,
                old_count,
                new_count,
            )

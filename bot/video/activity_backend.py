"""HelloDJ — Activity backend: HTTP server for Activity frontend and HLS delivery.

Serves the Activity frontend (static HTML/JS/CSS), the status API, and HLS
segments for active video sessions. Runs as an aiohttp web application on
port 8090 within the bot process.

Authentication uses the Discord Embedded App SDK instance_id as a session
token, validated against a registered token → guild_id mapping.
"""

from __future__ import annotations

import dataclasses
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from aiohttp import web

from video.sticker_catalog import (
    StickerCatalog,
    handle_sticker_catalog,
    handle_sticker_image,
)
from video.ws_hub import WebSocketHub

if TYPE_CHECKING:
    from video.session_registry import SessionRegistry

log = logging.getLogger(__name__)

# Directory containing the static Activity frontend files
_FRONTEND_DIR = Path(__file__).parent / "activity_frontend"

# Directory containing whiteboard JS modules (ES modules served at /activity/modules/)
_MODULES_DIR = Path(__file__).parent / "activity"

# Allowed static filenames to prevent path traversal
_ALLOWED_STATIC_FILES = {
    "app.js", "style.css", "discord-sdk.js", "hls.min.js", "countdown.mp4",
    "whiteboard-bundle.js",
    # Individual whiteboard modules (kept for direct access/debugging)
    "canvas_resize.js", "color_picker.js", "controls_passthrough.js",
    "coords.js", "eraser_tool.js", "hittest.js", "line_tool.js",
    "pen_tool.js", "renderer.js", "reset.js", "shape_tool.js",
    "sticker_picker.js", "sticker_tool.js", "text_bg_toggle.js",
    "text_tool.js", "tools.js", "undo_restore.js", "undo.js",
    "whiteboard.js", "ws_whiteboard.js",
}

# Allowed whiteboard module filenames
_ALLOWED_MODULE_FILES = {
    "canvas_resize.js", "color_picker.js", "controls_passthrough.js",
    "coords.js", "eraser_tool.js", "hittest.js", "line_tool.js",
    "pen_tool.js", "renderer.js", "reset.js", "shape_tool.js",
    "sticker_picker.js", "sticker_tool.js", "text_bg_toggle.js",
    "text_tool.js", "tools.js", "undo_restore.js", "undo.js",
    "whiteboard.js", "ws_whiteboard.js",
}


class ActivityBackend:
    """HTTP server for Activity frontend and HLS delivery.

    Serves:
    - GET /activity/             → index.html (Activity frontend)
    - GET /activity/static/{fn}  → app.js, style.css
    - GET /activity/status/{gid} → JSON session status (authenticated)
    - GET /activity/stream/{gid}/playlist.m3u8  → HLS playlist (authenticated)
    - GET /activity/stream/{gid}/subtitles/{lang}.vtt → WebVTT subtitle file (authenticated)
    - GET /activity/stream/{gid}/{seg}.ts       → HLS segments (authenticated)

    Authentication is enforced on /activity/status/ and /activity/stream/ routes.
    Tokens are registered by the cog after Discord Activity launch using
    `register_token(instance_id, guild_id)`.
    """

    def __init__(self, registry: SessionRegistry) -> None:
        self._registry = registry
        self._tokens: dict[str, int] = {}  # instance_id → guild_id

        self._ws_hub = WebSocketHub(self._validate_ws_token)

        self.app = web.Application(middlewares=[self._cors_middleware])
        self._setup_routes()
        self._init_sticker_catalog()

        self.runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None

    # ------------------------------------------------------------------
    # Public attributes
    # ------------------------------------------------------------------

    @property
    def ws_hub(self) -> WebSocketHub:
        """The WebSocket hub for playback synchronization.

        Exposed so the cog can call ``ws_hub.broadcast_from_bot()`` for
        bot-initiated control actions (e.g., Now Playing embed buttons).
        """
        return self._ws_hub

    # ------------------------------------------------------------------
    # CORS middleware
    # ------------------------------------------------------------------

    @web.middleware
    async def _cors_middleware(self, request: web.Request, handler) -> web.Response:
        """Add CORS headers to all responses for Discord Activity iframe."""
        response = await handler(request)
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type"
        response.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
        return response

    # ------------------------------------------------------------------
    # Sticker catalog initialization
    # ------------------------------------------------------------------

    def _init_sticker_catalog(self) -> None:
        """Initialize the sticker catalog and store it on the app for handlers."""
        sticker_catalog = StickerCatalog(Path("stickers"))
        sticker_catalog.load()
        self.app["sticker_catalog"] = sticker_catalog

    # ------------------------------------------------------------------
    # Token management
    # ------------------------------------------------------------------

    def register_token(self, instance_id: str, guild_id: int) -> None:
        """Register an Activity session token for authentication.

        Called by the cog after launching a Discord Activity so that the
        frontend can authenticate using the instance_id from the
        Embedded App SDK.

        Args:
            instance_id: The instance_id from Discord Embedded App SDK.
            guild_id: The guild this token is scoped to.
        """
        self._tokens[instance_id] = guild_id
        log.debug("Registered Activity token for guild %d", guild_id)

    def revoke_token(self, instance_id: str) -> None:
        """Revoke a previously registered Activity session token."""
        removed = self._tokens.pop(instance_id, None)
        if removed is not None:
            log.debug("Revoked Activity token for guild %d", removed)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self, port: int = 8090) -> None:
        """Start the HTTP server on the specified port.

        Args:
            port: TCP port to listen on. Defaults to 8090.
        """
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        self._site = web.TCPSite(self.runner, "0.0.0.0", port)
        await self._site.start()
        log.info("Activity backend started on port %d", port)

    async def stop(self) -> None:
        """Stop the HTTP server and clean up resources."""
        if self._site is not None:
            await self._site.stop()
            self._site = None
        if self.runner is not None:
            await self.runner.cleanup()
            self.runner = None
        log.info("Activity backend stopped")

    # ------------------------------------------------------------------
    # Route setup
    # ------------------------------------------------------------------

    def _setup_routes(self) -> None:
        """Configure aiohttp routes."""
        self.app.router.add_get("/activity/", self.handle_index)
        self.app.router.add_get("/activity/static/{filename}", self.handle_static)
        self.app.router.add_get("/activity/status/{guild_id}", self.handle_status)
        self.app.router.add_get(
            "/activity/stream/{guild_id}/playlist.m3u8", self.handle_playlist
        )
        self.app.router.add_get(
            "/activity/stream/{guild_id}/subtitles/{lang}.vtt", self.handle_subtitle
        )
        # Variant playlists (video.m3u8, audio_ja.m3u8, etc.) — must be BEFORE {segment}.ts
        self.app.router.add_get(
            "/activity/stream/{guild_id}/{variant}.m3u8", self.handle_variant_playlist
        )
        self.app.router.add_get(
            "/activity/stream/{guild_id}/{segment}.ts", self.handle_segment
        )
        self.app.router.add_get("/activity/ws/{guild_id}", self._ws_hub.handle_ws)

        # Sticker catalog routes
        self.app.router.add_get("/activity/stickers/catalog", handle_sticker_catalog)
        self.app.router.add_get(
            "/activity/stickers/{category}/{filename}", handle_sticker_image
        )

        # Whiteboard module routes (ES modules served from activity/ directory)
        self.app.router.add_get("/activity/modules/{filename}", self.handle_module)

    # ------------------------------------------------------------------
    # Authentication helpers
    # ------------------------------------------------------------------

    def _extract_token(self, request: web.Request) -> str | None:
        """Extract the session token from the request.

        Checks the Authorization header (Bearer scheme) first, then falls
        back to the `token` query parameter.

        Returns:
            The token string, or None if not present.
        """
        # Check Authorization: Bearer <token>
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:].strip()
            if token:
                return token

        # Fallback: ?token=<token> query parameter
        token = request.query.get("token", "").strip()
        return token if token else None

    def _validate_ws_token(self, token: str) -> int | None:
        """Validate a WebSocket token and return the guild_id, or None if invalid.

        Uses the same token scheme as HTTP routes: the token is a Discord
        Embedded App SDK instance_id containing an embedded guild_id.

        Args:
            token: The instance_id token from the WebSocket query param.

        Returns:
            The guild_id if the token is valid, None otherwise.
        """
        return self._parse_guild_from_instance_id(token)

    def _validate_token(
        self, request: web.Request, guild_id: int
    ) -> web.Response | None:
        """Validate the request token for the given guild.

        Discord's Embedded App SDK provides an instance_id with the format:
            i-{launch_id}-gc-{guild_id}-{channel_id}

        We parse the guild_id from the instance_id and verify it matches
        the requested resource. This ensures:
        - Only requests from inside a Discord Activity iframe can access streams
        - A token for guild A cannot access guild B's stream
        - An active session must exist for the guild

        Returns:
            None if authentication succeeds, or a web.Response with the
            appropriate error status if it fails.
        """
        token = self._extract_token(request)

        if not token:
            return self._json_error(401, "Missing authentication token")

        # Parse guild_id from Discord instance_id format: i-{id}-gc-{guild_id}-{channel_id}
        token_guild = self._parse_guild_from_instance_id(token)
        if token_guild is None:
            return self._json_error(401, "Invalid authentication token")

        if token_guild != guild_id:
            return self._json_error(403, "Token not authorized for this guild")

        # Verify an active session exists
        if self._registry.get(guild_id) is None:
            return self._json_error(404, "No active session for this guild")

        return None

    @staticmethod
    def _parse_guild_from_instance_id(instance_id: str) -> int | None:
        """Extract guild_id from a Discord Activity instance_id.

        Expected format: i-{launch_id}-gc-{guild_id}-{channel_id}
        Returns the guild_id as int, or None if parsing fails.
        """
        try:
            # Split on '-gc-' to isolate the guild and channel portion
            parts = instance_id.split("-gc-")
            if len(parts) != 2:
                return None
            # The second part is "{guild_id}-{channel_id}"
            guild_channel = parts[1]
            guild_str = guild_channel.split("-")[0]
            return int(guild_str)
        except (ValueError, IndexError):
            return None

    # ------------------------------------------------------------------
    # Route handlers
    # ------------------------------------------------------------------

    async def handle_index(self, request: web.Request) -> web.Response:
        """GET /activity/ → serve index.html from activity_frontend/."""
        index_path = _FRONTEND_DIR / "index.html"
        if not index_path.is_file():
            return self._json_error(404, "Activity frontend not found")
        return web.FileResponse(index_path, headers={"Content-Type": "text/html"})

    async def handle_static(self, request: web.Request) -> web.Response:
        """GET /activity/static/{filename} → serve allowed static files."""
        filename = request.match_info["filename"]

        # Prevent path traversal and restrict to known filenames
        if filename not in _ALLOWED_STATIC_FILES:
            return self._json_error(404, "Static file not found")

        file_path = _FRONTEND_DIR / filename
        if not file_path.is_file():
            return self._json_error(404, "Static file not found")

        content_type = "application/javascript" if filename.endswith(".js") else \
                      "text/css" if filename.endswith(".css") else \
                      "video/mp4" if filename.endswith(".mp4") else \
                      "application/octet-stream"
        return web.FileResponse(file_path, headers={"Content-Type": content_type})

    async def handle_module(self, request: web.Request) -> web.Response:
        """GET /activity/modules/{filename} → serve whiteboard ES module files."""
        filename = request.match_info["filename"]

        # Restrict to allowed module filenames
        if filename not in _ALLOWED_MODULE_FILES:
            return self._json_error(404, "Module not found")

        file_path = _MODULES_DIR / filename
        if not file_path.is_file():
            return self._json_error(404, "Module not found")

        return web.FileResponse(
            file_path,
            headers={"Content-Type": "application/javascript"},
        )

    async def handle_status(self, request: web.Request) -> web.Response:
        """GET /activity/status/{guild_id} → JSON session status."""
        guild_id = self._parse_guild_id(request)
        if guild_id is None:
            return self._json_error(404, "Invalid guild ID")

        # Authenticate
        auth_error = self._validate_token(request, guild_id)
        if auth_error is not None:
            return auth_error

        # Look up session
        streamer = self._registry.get(guild_id)
        if streamer is None:
            return self._json_error(404, "No active session for this guild")

        # Build SessionStatus response
        from video import SessionStatus

        subtitles = []
        if streamer.pipeline is not None and hasattr(streamer.pipeline, 'subtitle_tracks'):
            subtitles = streamer.pipeline.subtitle_tracks

        audio_tracks: list[dict] = []
        if streamer.pipeline is not None and hasattr(streamer.pipeline, 'audio_tracks'):
            audio_tracks = streamer.pipeline.audio_tracks

        # Get playing state from WebSocket hub
        playing = True
        ws_state = self._ws_hub.get_state(guild_id)
        if ws_state is not None:
            playing = ws_state.playing

        status = SessionStatus(
            state=streamer.state.value,
            video_title=streamer.source.title if streamer.source else None,
            video_duration=(
                streamer.source.duration_seconds if streamer.source else 0.0
            ),
            elapsed_seconds=streamer.get_elapsed_seconds(),
            playlist_url=(
                f"/activity/stream/{guild_id}/playlist.m3u8"
                if streamer.state.value == "streaming"
                else None
            ),
            queue_length=len(streamer.queue),
            session_id=streamer.session_id,
            subtitles=subtitles,
            audio_tracks=audio_tracks,
            playing=playing,
            uploader=(
                streamer.source.metadata.get("uploader")
                if streamer.source and streamer.source.source_type == "upload"
                else None
            ),
        )

        return web.json_response(dataclasses.asdict(status))

    async def handle_playlist(self, request: web.Request) -> web.Response:
        """GET /activity/stream/{guild_id}/playlist.m3u8 → serve HLS playlist."""
        guild_id = self._parse_guild_id(request)
        if guild_id is None:
            return self._json_error(404, "Invalid guild ID")

        # Authenticate
        auth_error = self._validate_token(request, guild_id)
        if auth_error is not None:
            return auth_error

        # Look up session
        streamer = self._registry.get(guild_id)
        if streamer is None:
            return self._json_error(404, "No active session for this guild")

        if streamer.pipeline is None:
            return self._json_error(404, "No active pipeline for this session")

        playlist_path = streamer.pipeline.playlist_path
        if not playlist_path.is_file():
            return self._json_error(404, "Playlist not yet available")

        return web.FileResponse(
            playlist_path,
            headers={"Content-Type": "application/vnd.apple.mpegurl"},
        )

    async def handle_variant_playlist(self, request: web.Request) -> web.Response:
        """GET /activity/stream/{guild_id}/{variant}.m3u8 → serve variant playlist.

        When multi-audio is active, ffmpeg produces separate variant playlists
        (e.g. video.m3u8, audio_ja.m3u8) referenced by the master playlist.m3u8.
        This route serves those per-variant playlist files.
        """
        guild_id = self._parse_guild_id(request)
        if guild_id is None:
            return self._json_error(404, "Invalid guild ID")

        # Authenticate
        auth_error = self._validate_token(request, guild_id)
        if auth_error is not None:
            return auth_error

        # Look up session
        streamer = self._registry.get(guild_id)
        if streamer is None:
            return self._json_error(404, "No active session for this guild")

        if streamer.pipeline is None:
            return self._json_error(404, "No active pipeline for this session")

        variant = request.match_info["variant"]

        # Sanitize variant name: only allow alphanumeric + underscore/dash
        if not variant or not all(c.isalnum() or c in "-_" for c in variant):
            return self._json_error(404, "Invalid variant name")

        variant_path = streamer.pipeline.output_dir / f"{variant}.m3u8"
        if not variant_path.is_file():
            return self._json_error(404, "Variant playlist not found")

        return web.FileResponse(
            variant_path,
            headers={"Content-Type": "application/vnd.apple.mpegurl"},
        )

    async def handle_subtitle(self, request: web.Request) -> web.Response:
        """GET /activity/stream/{guild_id}/subtitles/{lang}.vtt → serve WebVTT file."""
        guild_id = self._parse_guild_id(request)
        if guild_id is None:
            return self._json_error(404, "Invalid guild ID")

        # Authenticate
        auth_error = self._validate_token(request, guild_id)
        if auth_error is not None:
            return auth_error

        # Look up session
        streamer = self._registry.get(guild_id)
        if streamer is None:
            return self._json_error(404, "No active session for this guild")

        if streamer.pipeline is None:
            return self._json_error(404, "No active pipeline for this session")

        lang = request.match_info["lang"]

        # Sanitize lang: only allow alphanumeric + underscore/dash (no path traversal)
        if not lang or not all(c.isalnum() or c in "-_" for c in lang):
            return self._json_error(404, "Invalid subtitle language")

        # Validate lang against known subtitle tracks for this session
        known_langs = {t["lang"] for t in streamer.pipeline.subtitle_tracks}
        if lang not in known_langs:
            return self._json_error(404, "Subtitle language not available")

        # Serve the WebVTT file
        vtt_path = streamer.pipeline.output_dir / "subtitles" / f"{lang}.vtt"
        if not vtt_path.is_file():
            return self._json_error(404, "Subtitle file not found")

        return web.FileResponse(vtt_path, headers={"Content-Type": "text/vtt"})

    async def handle_segment(self, request: web.Request) -> web.Response:
        """GET /activity/stream/{guild_id}/{segment}.ts → serve HLS segment.

        Segments don't require auth — they're only discoverable through the
        authenticated playlist. This avoids issues with hls.js not forwarding
        auth tokens on segment requests.
        """
        guild_id = self._parse_guild_id(request)
        if guild_id is None:
            return self._json_error(404, "Invalid guild ID")

        # Look up session
        streamer = self._registry.get(guild_id)
        if streamer is None:
            return self._json_error(404, "No active session for this guild")

        if streamer.pipeline is None:
            return self._json_error(404, "No active pipeline for this session")

        segment_name = request.match_info["segment"] + ".ts"

        # Sanitize segment name: only allow alphanumeric + underscore/dash
        if not all(c.isalnum() or c in "-_" for c in request.match_info["segment"]):
            return self._json_error(404, "Invalid segment name")

        segment_path = streamer.pipeline.output_dir / segment_name
        if not segment_path.is_file():
            return self._json_error(404, "Segment not found")

        return web.FileResponse(
            segment_path,
            headers={"Content-Type": "video/MP2T"},
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_guild_id(request: web.Request) -> int | None:
        """Parse guild_id from the request path, returning None on failure."""
        raw = request.match_info.get("guild_id", "")
        try:
            return int(raw)
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _json_error(status: int, message: str) -> web.Response:
        """Create a JSON error response."""
        return web.json_response(
            {"error": message},
            status=status,
        )

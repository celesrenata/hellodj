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

if TYPE_CHECKING:
    from video.session_registry import SessionRegistry

log = logging.getLogger(__name__)

# Directory containing the static Activity frontend files
_FRONTEND_DIR = Path(__file__).parent / "activity_frontend"

# Allowed static filenames to prevent path traversal
_ALLOWED_STATIC_FILES = {"app.js", "style.css"}


class ActivityBackend:
    """HTTP server for Activity frontend and HLS delivery.

    Serves:
    - GET /activity/             → index.html (Activity frontend)
    - GET /activity/static/{fn}  → app.js, style.css
    - GET /activity/status/{gid} → JSON session status (authenticated)
    - GET /activity/stream/{gid}/playlist.m3u8  → HLS playlist (authenticated)
    - GET /activity/stream/{gid}/{seg}.ts       → HLS segments (authenticated)

    Authentication is enforced on /activity/status/ and /activity/stream/ routes.
    Tokens are registered by the cog after Discord Activity launch using
    `register_token(instance_id, guild_id)`.
    """

    def __init__(self, registry: SessionRegistry) -> None:
        self._registry = registry
        self._tokens: dict[str, int] = {}  # instance_id → guild_id

        self.app = web.Application()
        self._setup_routes()

        self.runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None

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
            "/activity/stream/{guild_id}/{segment}.ts", self.handle_segment
        )

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

    def _validate_token(
        self, request: web.Request, guild_id: int
    ) -> web.Response | None:
        """Validate the request token for the given guild.

        Returns:
            None if authentication succeeds, or a web.Response with the
            appropriate error status if it fails.
        """
        token = self._extract_token(request)

        if not token:
            return self._json_error(401, "Missing authentication token")

        token_guild = self._tokens.get(token)
        if token_guild is None:
            return self._json_error(401, "Invalid authentication token")

        if token_guild != guild_id:
            return self._json_error(403, "Token not authorized for this guild")

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

        content_type = "application/javascript" if filename.endswith(".js") else "text/css"
        return web.FileResponse(file_path, headers={"Content-Type": content_type})

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

    async def handle_segment(self, request: web.Request) -> web.Response:
        """GET /activity/stream/{guild_id}/{segment}.ts → serve HLS segment."""
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

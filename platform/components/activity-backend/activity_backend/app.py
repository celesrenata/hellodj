"""aiohttp application factory and HTTP endpoints for the activity-backend.

Builds the aiohttp ``Application`` that serves the Activity under the
``/activity/`` prefix (R18.2): a health check, video/visualizer/lyrics control
endpoints, and the WebSocket hub route. Video and visualizer control endpoints
emit transcode requests to the ``hls-transcode`` component and return the
CloudFront HLS URL derived from :class:`~activity_backend.hls.HlsCatalog`
(R18.4).

aiohttp is imported lazily inside :func:`create_app` and the handler closures so
this module imports cleanly (for tests / ``py_compile``) without aiohttp
installed. The request-handling logic that does not need aiohttp is factored
into pure helpers (:class:`ActivityHandlers`) so it can be unit-tested directly.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from .config import ActivityConfig
from .hls import HlsCatalog
from .lyrics import LyricsStore, parse_lrc
from .transcode_client import (
    TranscodeClient,
    TranscodeError,
    TranscodeKind,
    TranscodeRequest,
)
from .visualizer import VisualizerRegistry
from .ws_hub import WebSocketHub

log = logging.getLogger(__name__)

__all__ = ["ActivityHandlers", "create_app"]


class ActivityHandlers:
    """Pure request-handling logic behind the aiohttp endpoints.

    Each method takes already-decoded input and returns a ``(status, body)``
    pair, so the logic is testable without constructing aiohttp requests. The
    thin aiohttp handlers in :func:`create_app` decode the request, call these,
    and encode the JSON response.
    """

    def __init__(
        self,
        config: ActivityConfig,
        hub: WebSocketHub,
        catalog: HlsCatalog,
        transcode: TranscodeClient,
        visualizer: VisualizerRegistry,
        lyrics: LyricsStore,
    ) -> None:
        """Wire the handlers to the shared stores and clients."""
        self._config = config
        self._hub = hub
        self._catalog = catalog
        self._transcode = transcode
        self._visualizer = visualizer
        self._lyrics = lyrics

    @staticmethod
    def health() -> tuple[int, dict[str, Any]]:
        """Return the liveness/readiness body."""
        return 200, {"status": "ok", "component": "activity-backend"}

    async def start_video(
        self, guild_id: int, body: dict[str, Any]
    ) -> tuple[int, dict[str, Any]]:
        """Start a video stream: emit a transcode request, return HLS URL.

        The activity-backend does not transcode; it asks ``hls-transcode`` to
        produce HLS to S3 and returns the CloudFront playlist URL (R18.4).
        """
        source_uri = str(body.get("sourceUri", "")).strip()
        if not source_uri:
            return 400, {"error": "sourceUri is required"}
        stream_id = str(body.get("streamId") or uuid.uuid4().hex)
        location = self._catalog.locate(guild_id, "video", stream_id)
        request = TranscodeRequest(
            guild_id=guild_id,
            kind=TranscodeKind.VIDEO,
            stream_id=stream_id,
            s3_bucket=location.bucket,
            s3_key_prefix=location.key_prefix,
            source_uri=source_uri,
            options=body.get("options"),
        )
        try:
            result = await self._transcode.request_transcode(request)
        except TranscodeError as exc:
            log.warning("video transcode request failed: %s", exc)
            return 502, {"error": "transcode request failed"}
        self._hub.mark_video_active(guild_id, True)
        return 202, {
            "accepted": result.accepted,
            "streamId": stream_id,
            "playlistUrl": location.playlist_url,
            "playlistKey": location.playlist_key,
        }

    async def stop_video(self, guild_id: int, body: dict[str, Any]) -> tuple[int, dict]:
        """Stop a guild's video stream (best-effort transcode stop)."""
        stream_id = str(body.get("streamId", "")).strip()
        if stream_id:
            try:
                await self._transcode.stop_transcode(guild_id, stream_id)
            except TranscodeError as exc:
                log.debug("video transcode stop failed: %s", exc)
        self._hub.mark_video_active(guild_id, False)
        return 200, {"stopped": True}

    async def set_visualizer(
        self, guild_id: int, body: dict[str, Any]
    ) -> tuple[int, dict[str, Any]]:
        """Select a visualizer engine and, unless off, start its transcode."""
        engine = str(body.get("engine", "off")).strip() or "off"
        state = await self._visualizer.set_engine(
            guild_id, engine, body.get("config")
        )
        if engine == "off":
            return 200, {"engine": engine, "active": False}

        stream_id = str(body.get("streamId") or uuid.uuid4().hex)
        location = self._catalog.locate(guild_id, "visualizer", stream_id)
        request = TranscodeRequest(
            guild_id=guild_id,
            kind=TranscodeKind.VISUALIZER,
            stream_id=stream_id,
            s3_bucket=location.bucket,
            s3_key_prefix=location.key_prefix,
            engine=engine,
            options=body.get("config"),
        )
        try:
            await self._transcode.request_transcode(request)
        except TranscodeError as exc:
            log.warning("visualizer transcode request failed: %s", exc)
            return 502, {"error": "transcode request failed"}
        self._visualizer.set_hls_ready(guild_id, location.playlist_url)
        return 202, {
            "engine": engine,
            "active": state.active,
            "streamId": stream_id,
            "playlistUrl": location.playlist_url,
        }

    def set_lyrics(
        self, guild_id: int, body: dict[str, Any]
    ) -> tuple[int, dict[str, Any]]:
        """Set/toggle synced lyrics for a guild (accepts LRC or parsed lines)."""
        if "enabled" in body and "lrc" not in body and "lines" not in body:
            state = self._lyrics.set_enabled(guild_id, bool(body["enabled"]))
            return 200, {"enabled": state.enabled}

        track_key = str(body.get("trackKey", "")).strip()
        if not track_key:
            return 400, {"error": "trackKey is required"}
        if "lrc" in body:
            lines = parse_lrc(str(body["lrc"]))
        else:
            lines = [
                (float(item[0]), str(item[1]))
                for item in body.get("lines", [])
                if isinstance(item, list | tuple) and len(item) == 2
            ]
        state = self._lyrics.set_lyrics(guild_id, track_key, lines)
        return 200, {"enabled": state.enabled, "lineCount": len(state.lines)}


def _parse_guild(request: Any) -> int | None:
    """Extract an integer guild id from the request match info."""
    try:
        return int(request.match_info.get("guild_id", ""))
    except (ValueError, TypeError):
        return None


def create_app(
    config: ActivityConfig,
    hub: WebSocketHub,
    handlers: ActivityHandlers,
) -> Any:
    """Build the aiohttp ``Application`` and register Activity routes (R18.2).

    aiohttp is imported here so importing this module does not require it.
    """
    from aiohttp import web

    app = web.Application()
    prefix = config.route_prefix

    async def health(_request: web.Request) -> web.Response:
        status, body = handlers.health()
        return web.json_response(body, status=status)

    async def start_video(request: web.Request) -> web.Response:
        guild_id = _parse_guild(request)
        if guild_id is None:
            return web.json_response({"error": "invalid guild"}, status=400)
        status, body = await handlers.start_video(guild_id, await request.json())
        return web.json_response(body, status=status)

    async def stop_video(request: web.Request) -> web.Response:
        guild_id = _parse_guild(request)
        if guild_id is None:
            return web.json_response({"error": "invalid guild"}, status=400)
        status, body = await handlers.stop_video(guild_id, await request.json())
        return web.json_response(body, status=status)

    async def set_visualizer(request: web.Request) -> web.Response:
        guild_id = _parse_guild(request)
        if guild_id is None:
            return web.json_response({"error": "invalid guild"}, status=400)
        status, body = await handlers.set_visualizer(
            guild_id, await request.json()
        )
        return web.json_response(body, status=status)

    async def set_lyrics(request: web.Request) -> web.Response:
        guild_id = _parse_guild(request)
        if guild_id is None:
            return web.json_response({"error": "invalid guild"}, status=400)
        status, body = handlers.set_lyrics(guild_id, await request.json())
        return web.json_response(body, status=status)

    app.router.add_get(f"{prefix}/health", health)
    app.router.add_post(f"{prefix}/guilds/{{guild_id}}/video", start_video)
    app.router.add_post(f"{prefix}/guilds/{{guild_id}}/video/stop", stop_video)
    app.router.add_post(
        f"{prefix}/guilds/{{guild_id}}/visualizer", set_visualizer
    )
    app.router.add_post(f"{prefix}/guilds/{{guild_id}}/lyrics", set_lyrics)
    app.router.add_get(f"{prefix}/ws/{{guild_id}}", hub.handle_ws)
    return app

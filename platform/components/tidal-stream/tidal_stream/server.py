"""aiohttp server for the ``tidal-stream`` sidecar.

Exposes the direct Tidal streaming endpoints and the HelloDJ-owned OAuth
callback endpoint ``/auth/callback`` that the web-ui forwards the Tidal
authorization code to (R9.2). Streaming and search resolution run through the
:class:`~tidal_stream.streaming.TidalStreamer`, which is authenticated with the
first-party single-app-id token from the
:class:`~tidal_stream.token_manager.TidalTokenManager` (R9.1, R9.4).

Endpoints:
    * ``GET  /healthz``                    - liveness probe.
    * ``GET  /search?q=&limit=``           - Tidal track search (R6.1).
    * ``GET  /tracks/{track_id}/stream``   - direct stream URL resolution (R6.1).
    * ``GET  /auth/callback?code=``        - first-party OAuth code exchange (R9.2).

Requirements: 6.1, 9.1, 9.2, 9.4, 9.5, 15.1
"""

from __future__ import annotations

import logging

from aiohttp import web

from .config import TidalStreamSettings
from .oauth_client import FirstPartyTidalOAuthClient, TidalOAuthHTTPError
from .secrets import TidalRefreshTokenStore
from .streaming import TidalStreamer, TidalStreamError
from .token_manager import TidalTokenManager

__all__ = ["build_app", "create_components"]

log = logging.getLogger(__name__)

_STREAMER_KEY = web.AppKey("tidal_streamer", TidalStreamer)
_TOKENS_KEY = web.AppKey("tidal_token_manager", TidalTokenManager)


def create_components(
    settings: TidalStreamSettings,
    *,
    clock,
) -> tuple[TidalTokenManager, TidalStreamer]:
    """Build the token manager and streamer from settings.

    The first-party client is constructed from the single-app-id config; its
    constructor rejects any legacy key-split config (R9.3). The refresh token is
    read from / written to AWS Secrets Manager (R9.2).
    """
    store = TidalRefreshTokenStore(
        settings.refresh_secret_id,
        region_name=settings.region_name,
    )
    client = FirstPartyTidalOAuthClient(
        settings.client_config(),
        token_url=settings.token_url,
    )
    token_manager = TidalTokenManager(
        store,
        client,
        clock=clock,
        expiry_skew_seconds=settings.expiry_skew_seconds,
    )
    streamer = TidalStreamer(
        token_manager,
        api_base=settings.api_base,
        country_code=settings.country_code,
    )
    return token_manager, streamer


async def _handle_health(request: web.Request) -> web.Response:
    """Liveness probe."""
    return web.json_response({"status": "ok"})


async def _handle_search(request: web.Request) -> web.Response:
    """Search Tidal tracks (R6.1)."""
    query = request.query.get("q", "").strip()
    if not query:
        return web.json_response({"error": "missing query parameter 'q'"}, status=400)
    try:
        limit = int(request.query.get("limit", "10"))
    except ValueError:
        return web.json_response({"error": "limit must be an integer"}, status=400)

    streamer = request.app[_STREAMER_KEY]
    try:
        results = await streamer.search(query, limit=limit)
    except TidalStreamError as error:
        log.warning("tidal-stream: search failed: %s", error)
        return web.json_response({"error": str(error)}, status=502)
    return web.json_response({"results": results})


async def _handle_stream(request: web.Request) -> web.Response:
    """Resolve the direct stream URL for a Tidal track (R6.1)."""
    track_id = request.match_info.get("track_id", "").strip()
    if not track_id:
        return web.json_response({"error": "missing track_id"}, status=400)

    streamer = request.app[_STREAMER_KEY]
    try:
        url = await streamer.get_stream_url(track_id)
    except TidalStreamError as error:
        log.warning("tidal-stream: stream resolution failed: %s", error)
        return web.json_response({"error": str(error)}, status=502)
    return web.json_response({"track_id": track_id, "stream_url": url})


async def _handle_auth_callback(request: web.Request) -> web.Response:
    """HelloDJ-owned OAuth callback: exchange the code for tokens (R9.2).

    The web-ui forwards the Tidal authorization ``code`` here. The single-app-id
    first-party client exchanges it and the refresh token is persisted to
    Secrets Manager. No Cognito involvement (R9.5).
    """
    error_param = request.query.get("error")
    if error_param:
        description = request.query.get("error_description", "")
        return web.json_response(
            {"error": error_param, "error_description": description},
            status=400,
        )

    code = request.query.get("code", "").strip()
    if not code:
        return web.json_response({"error": "missing authorization code"}, status=400)

    token_manager = request.app[_TOKENS_KEY]
    try:
        token = await _exchange_in_thread(token_manager, code)
    except (TidalOAuthHTTPError, ValueError) as error:
        log.warning("tidal-stream: auth callback exchange failed: %s", error)
        return web.json_response({"error": str(error)}, status=502)
    return web.json_response(
        {"status": "authorized", "expires_at": token.expires_at}
    )


async def _exchange_in_thread(token_manager: TidalTokenManager, code: str):
    """Run the synchronous code exchange off the event loop."""
    import asyncio

    return await asyncio.to_thread(token_manager.complete_authorization, code)


def build_app(
    token_manager: TidalTokenManager,
    streamer: TidalStreamer,
) -> web.Application:
    """Build the aiohttp application with all routes wired.

    Args:
        token_manager: The first-party Tidal token manager.
        streamer: The direct Tidal streamer.

    Returns:
        A configured :class:`aiohttp.web.Application`.
    """
    app = web.Application()
    app[_TOKENS_KEY] = token_manager
    app[_STREAMER_KEY] = streamer

    app.add_routes(
        [
            web.get("/healthz", _handle_health),
            web.get("/search", _handle_search),
            web.get("/tracks/{track_id}/stream", _handle_stream),
            web.get("/auth/callback", _handle_auth_callback),
        ]
    )

    async def _close_streamer(_: web.Application) -> None:
        await streamer.close()

    app.on_cleanup.append(_close_streamer)
    return app

"""aiohttp server for the ``spotify-stream`` sidecar (multi-tenant).

Exposes the direct Spotify streaming endpoints — now **per-user** — plus health
and the sidecar side of the one-time librespot capture contract. Each stream/
preload request carries the ``guild_id`` in its path (Lavalink builds the URL);
the server resolves the guild's owning Cognito ``sub`` server-side and serves the
track from that user's live librespot session, selected from the per-``sub``
:data:`~spotify_stream.session_pool.SpotifySessionPool` (multi-tenant-source-
streaming task 2.3). Concurrent requests from different guilds use different
users' Spotify accounts with no shared-account fallback (R3.1, R3.2, R3.6, R6.1,
R10.5).

The sidecar is READ-ONLY on tokens: per-user credentials are resolved from the
unified store (``hellodj-core`` + KMS Decrypt-only) via the shared
:class:`~hellodj_platform_logic.user_credential_resolver.UserCredentialResolver`,
whose expiry re-read (R2.2) picks up the value the durable watchdog refreshed
out-of-band — the sidecar never refreshes or writes. The single global session
is gone.

Endpoints:
    * ``GET  /health``                             - liveness + per-``sub`` pool state.
    * ``GET  /auth/status``                        - multi-session auth summary (R7.3).
    * ``GET  /preload/{guild_id}/{track_id}``      - warm the per-user cache (R3.2).
    * ``GET  /stream/{guild_id}/{track_id}``       - per-user audio stream (R3.2).
    * ``POST /auth/librespot/start``               - begin one-time capture (task 2.2).
    * ``POST /auth/librespot/complete``            - finish capture, return blob.

Requirements: 3.1, 3.2, 3.6, 7.3, 7.5, 10.5
"""

from __future__ import annotations

import hashlib
import logging

from aiohttp import web
from hellodj_platform_logic.session_registry import SessionRegistry

from .librespot_capture import LibrespotCaptureError, LibrespotCaptureService
from .session_pool import (
    SpotifyCredentialUnavailableError,
    SpotifyStreamRouter,
    normalize_track_id,
)

__all__ = ["build_app"]

log = logging.getLogger(__name__)

_ROUTER_KEY = web.AppKey("spotify_stream_router", SpotifyStreamRouter)
_REGISTRY_KEY = web.AppKey("spotify_session_registry", SessionRegistry)
_CAPTURE_KEY = web.AppKey("librespot_capture", LibrespotCaptureService)

#: Content-type per librespot codec (the pool transcodes OGG→MP3).
_CONTENT_TYPE_FALLBACK = "audio/mpeg"


def _content_type(codec) -> str:
    """Map a librespot ``SuperAudioFormat`` to a content type (best-effort)."""
    try:
        from librespot.audio import SuperAudioFormat

        return {
            SuperAudioFormat.VORBIS: "audio/ogg",
            SuperAudioFormat.MP3: "audio/mpeg",
            SuperAudioFormat.AAC: "audio/aac",
            SuperAudioFormat.FLAC: "audio/flac",
        }.get(codec, _CONTENT_TYPE_FALLBACK)
    except Exception:  # noqa: BLE001 - librespot absent in tests: default MP3
        return _CONTENT_TYPE_FALLBACK


def _sub_digest(sub: str) -> str:
    """Return a short non-reversible digest of a ``sub`` for health output (R7.3)."""
    return hashlib.sha256(sub.encode("utf-8")).hexdigest()[:12]


def _unavailable_status(reason: str) -> int:
    """Map a credential-unavailable reason to an observable HTTP status (R7.1)."""
    if reason in ("no_owner", "no_credential", "no_librespot_credential"):
        return 404
    return 502


async def _handle_health(request: web.Request) -> web.Response:
    """Liveness probe reporting per-``sub`` session-pool state (R7.3, R7.5).

    Reports the number of live sessions and each tracked ``sub``'s state
    (including specific failure reasons), never a single global status and never
    any token material. When no router is wired the sidecar reports
    ``not_ready`` for the multi-tenant path (no fake-green — R7.5).
    """
    router = request.app.get(_ROUTER_KEY)
    if router is None:
        return web.json_response(
            {"status": "not_ready", "service": "spotify-stream",
             "reason": "credential_store_unavailable"}
        )
    registry = request.app[_REGISTRY_KEY]
    return web.json_response(
        {
            "status": "ok",
            "service": "spotify-stream",
            "live_sessions": registry.live_count(),
            "tracked_sessions": len(registry),
        }
    )


async def _handle_auth_status(request: web.Request) -> web.Response:
    """Report the multi-session auth state (R7.3): per-``sub`` states, no global.

    Never a single global session status and never any token material. Each
    tracked user is reported by a short non-reversible digest of its ``sub``
    with its specific phase/reason (including any ``failed`` state).
    """
    router = request.app.get(_ROUTER_KEY)
    if router is None:
        return web.json_response(
            {"status": "not_ready", "reason": "credential_store_unavailable",
             "sessions": {}}
        )
    registry = request.app[_REGISTRY_KEY]
    sessions = {
        _sub_digest(sub): {"phase": state.phase.value, "reason": state.reason}
        for sub, state in registry.states().items()
    }
    return web.json_response(
        {
            "status": "ok",
            "live_sessions": registry.live_count(),
            "tracked_sessions": len(registry),
            "sessions": sessions,
        }
    )


def _resolve_router(request: web.Request):
    """Return the wired router or an observable not-ready ``web.Response``."""
    router = request.app.get(_ROUTER_KEY)
    if router is None:
        return web.json_response(
            {"error": "credential store unavailable", "reason": "not_ready"},
            status=503,
        )
    return router


async def _handle_preload(request: web.Request) -> web.Response:
    """Warm the per-user cache for a guild owner's track (R3.2)."""
    guild_id = request.match_info.get("guild_id", "").strip()
    track_id = request.match_info.get("track_id", "").strip()
    if not guild_id or not track_id:
        return web.json_response({"error": "missing guild_id or track_id"}, status=400)

    router = _resolve_router(request)
    if isinstance(router, web.Response):
        return router
    import asyncio

    try:
        audio, _codec = await asyncio.to_thread(
            router.load_track_for_guild, guild_id, track_id
        )
    except SpotifyCredentialUnavailableError as exc:
        return _unavailable_response(guild_id, exc)
    return web.json_response(
        {"status": "ok", "track_id": normalize_track_id(track_id), "size": len(audio)}
    )


async def _handle_stream(request: web.Request) -> web.StreamResponse:
    """Stream a Spotify track for a guild owner's account (R3.2)."""
    guild_id = request.match_info.get("guild_id", "").strip()
    track_id = request.match_info.get("track_id", "").strip()
    if not guild_id or not track_id:
        return web.json_response({"error": "missing guild_id or track_id"}, status=400)

    router = _resolve_router(request)
    if isinstance(router, web.Response):
        return router
    import asyncio

    try:
        audio, codec = await asyncio.to_thread(
            router.load_track_for_guild, guild_id, track_id
        )
    except SpotifyCredentialUnavailableError as exc:
        return _unavailable_response(guild_id, exc)

    return web.Response(
        body=audio,
        content_type=_content_type(codec),
        headers={
            "Content-Length": str(len(audio)),
            "Accept-Ranges": "bytes",
            "Cache-Control": "no-cache",
        },
    )


def _unavailable_response(
    guild_id: str, exc: SpotifyCredentialUnavailableError
) -> web.Response:
    """Build the observable, non-secret error response (R7.1)."""
    log.info(
        "spotify-stream: guild %s Spotify credential unavailable (%s)",
        guild_id, exc.reason,
    )
    return web.json_response(
        {"error": "no Spotify credential for this guild", "reason": exc.reason},
        status=_unavailable_status(exc.reason),
    )


async def _handle_librespot_start(request: web.Request) -> web.Response:
    """Begin a one-time librespot capture; return the authorize URL (task 2.2)."""
    capture = request.app.get(_CAPTURE_KEY)
    if capture is None:
        return web.json_response({"error": "capture unavailable"}, status=503)
    body = await _read_json(request)
    sub = str(body.get("sub", "") or "").strip()
    redirect_uri = str(body.get("redirect_uri", "") or "").strip()
    if not sub or not redirect_uri:
        return web.json_response({"error": "missing sub or redirect_uri"}, status=400)
    import asyncio

    try:
        url = await asyncio.to_thread(capture.start, sub, redirect_uri)
    except LibrespotCaptureError as exc:
        log.warning("spotify-stream: librespot start failed (%s)", exc)
        return web.json_response({"error": "capture start failed"}, status=502)
    return web.json_response({"authorize_url": url})


async def _handle_librespot_complete(request: web.Request) -> web.Response:
    """Finish a librespot capture; return the reusable blob (task 2.2).

    The reusable ``{username, credentials, type}`` blob is returned to the web-ui
    (which stores it envelope-encrypted). It is never logged here.
    """
    capture = request.app.get(_CAPTURE_KEY)
    if capture is None:
        return web.json_response({"error": "capture unavailable"}, status=503)
    body = await _read_json(request)
    sub = str(body.get("sub", "") or "").strip()
    code = str(body.get("code", "") or "").strip()
    if not sub or not code:
        return web.json_response({"error": "missing sub or code"}, status=400)
    import asyncio

    try:
        creds = await asyncio.to_thread(capture.complete, sub, code)
    except LibrespotCaptureError as exc:
        log.warning("spotify-stream: librespot complete failed (%s)", exc)
        return web.json_response({"error": "capture failed"}, status=502)
    return web.json_response({"credentials": creds})


async def _read_json(request: web.Request) -> dict:
    """Parse a JSON request body, returning ``{}`` on any error."""
    try:
        parsed = await request.json()
    except Exception:  # noqa: BLE001 - malformed body → empty dict
        return {}
    return parsed if isinstance(parsed, dict) else {}


def build_app(
    router: SpotifyStreamRouter | None,
    *,
    capture: LibrespotCaptureService | None = None,
) -> web.Application:
    """Build the aiohttp application with the per-user routes wired.

    Args:
        router: The per-user :class:`SpotifyStreamRouter`, or ``None`` when the
            unified credential store is unavailable (the streaming routes then
            report observably not-ready rather than serving a single account —
            R7.5, R10.5).
        capture: The OPTIONAL librespot capture service; when provided, the
            ``/auth/librespot/{start,complete}`` routes are registered (task 2.2).

    Returns:
        A configured :class:`aiohttp.web.Application`.
    """
    app = web.Application()
    if router is not None:
        app[_ROUTER_KEY] = router
        app[_REGISTRY_KEY] = router.registry

    routes = [
        web.get("/health", _handle_health),
        web.get("/auth/status", _handle_auth_status),
        web.get("/preload/{guild_id}/{track_id}", _handle_preload),
        web.get("/stream/{guild_id}/{track_id}", _handle_stream),
    ]
    if capture is not None:
        app[_CAPTURE_KEY] = capture
        routes.append(web.post("/auth/librespot/start", _handle_librespot_start))
        routes.append(web.post("/auth/librespot/complete", _handle_librespot_complete))
    app.add_routes(routes)

    if router is not None:
        registry = router.registry

        async def _close_registry(_: web.Application) -> None:
            """Close every live per-user librespot session on shutdown (R8.4)."""
            registry.close_all()

        app.on_cleanup.append(_close_registry)
    return app

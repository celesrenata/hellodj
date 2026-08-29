"""aiohttp server for the ``tidal-stream`` sidecar (multi-tenant).

Exposes the direct Tidal streaming endpoints — now **per-user** — plus health.
Each streaming/search request carries the ``guild_id`` in its path (mirroring
the Spotify sidecar); the server resolves the guild's owning Cognito ``sub``
server-side and serves the request from that user's live
:class:`~tidal_stream.user_sessions.TidalUserClient`, selected from the per-``sub``
:data:`~tidal_stream.user_sessions.TidalSessionRegistry`
(multi-tenant-source-streaming task 3.1). Concurrent requests from different
guilds use different users' tokens with no cross-user fallback (R5.1, R5.2,
R5.4, R6.1, R10.5).

The sidecar is READ-ONLY on tokens: per-user tokens are resolved from the
unified credential store (``hellodj-core`` + KMS Decrypt-only) via the shared
:class:`~hellodj_platform_logic.user_credential_resolver.UserCredentialResolver`,
whose expiry re-read (R2.2) picks up the value the durable watchdog refreshed
out-of-band — the sidecar never refreshes or writes (R5.3). The single
startup-bound account is gone.

Endpoints:
    * ``GET  /healthz``                             - liveness probe + pool state.
    * ``GET  /search/{guild_id}?q=&limit=``         - per-user Tidal search (R5.1).
    * ``GET  /stream/{guild_id}/{track_id}``        - per-user stream URL (R5.1).
    * ``GET  /auth/callback?code=``                 - OPTIONAL legacy first-party
      code-exchange forward (present only when a token manager is configured;
      NOT part of the per-user streaming path).

Requirements: 5.1, 5.2, 5.3, 5.4, 6.1, 7.3, 10.5
"""

from __future__ import annotations

import logging

from aiohttp import web
from hellodj_platform_logic.session_registry import SessionRegistry
from hellodj_platform_logic.types import SessionRegistryConfig

from .config import TidalStreamSettings
from .oauth_client import FirstPartyTidalOAuthClient, TidalOAuthHTTPError
from .resolver_bootstrap import build_user_credential_resolver
from .secrets import TidalRefreshTokenStore
from .streaming import TidalStreamer, TidalStreamError
from .token_manager import TidalTokenManager
from .user_sessions import (
    ReadOnlyTidalTokenSource,
    TidalCredentialUnavailableError,
    TidalSessionRegistry,
    TidalStreamRouter,
)

__all__ = ["build_app", "create_components", "create_router"]

log = logging.getLogger(__name__)

_ROUTER_KEY = web.AppKey("tidal_stream_router", TidalStreamRouter)
_REGISTRY_KEY = web.AppKey("tidal_session_registry", SessionRegistry)
_TOKENS_KEY = web.AppKey("tidal_token_manager", TidalTokenManager)


def create_router(
    settings: TidalStreamSettings,
) -> TidalStreamRouter | None:
    """Build the per-user streaming router, or ``None`` if the store is absent.

    Wires the unified-store resolver + guild→owner lookup (R1.1) and the bounded
    per-``sub`` session registry (R8), then a :class:`TidalStreamRouter` whose
    session factory builds a read-only, per-user :class:`TidalStreamer`. Returns
    ``None`` when the unified credential store cannot be reached, so the sidecar
    starts observably not-ready rather than serving a single ambient account
    (R7.5, R10.5).
    """
    wired = build_user_credential_resolver(settings)
    if wired is None:
        return None
    resolver, owner_lookup = wired

    registry: TidalSessionRegistry = SessionRegistry(
        SessionRegistryConfig(
            max_sessions=settings.max_sessions,
            idle_timeout_seconds=settings.session_idle_timeout_seconds,
        ),
    )

    def _streamer_factory(token_source: ReadOnlyTidalTokenSource) -> TidalStreamer:
        return TidalStreamer(
            token_source,
            api_base=settings.api_base,
            country_code=settings.country_code,
        )

    return TidalStreamRouter(
        owner_lookup,
        resolver,
        registry,
        streamer_factory=_streamer_factory,
    )


def create_components(
    settings: TidalStreamSettings,
    *,
    clock,
) -> TidalTokenManager | None:
    """Build the OPTIONAL legacy first-party token manager for ``/auth/callback``.

    Returns a :class:`TidalTokenManager` only when a ``refresh_secret_id`` is
    configured — the legacy first-party code-exchange forward the web-ui uses to
    complete Tidal authorization. Returns ``None`` when no secret is configured
    (pure multi-tenant): the ``/auth/callback`` route is then not registered.
    This token manager is NEVER read by the per-user streaming path (R5.1/R5.3).
    """
    if not settings.refresh_secret_id:
        return None
    store = TidalRefreshTokenStore(
        settings.refresh_secret_id,
        region_name=settings.region_name,
    )
    client = FirstPartyTidalOAuthClient(
        settings.client_config(),
        token_url=settings.token_url,
    )
    return TidalTokenManager(
        store,
        client,
        clock=clock,
        expiry_skew_seconds=settings.expiry_skew_seconds,
    )


async def _handle_health(request: web.Request) -> web.Response:
    """Liveness probe reporting per-``sub`` session-pool state (R7.3).

    Reports the number of live sessions and each tracked ``sub``'s state
    (including specific failure reasons), never a single global status and never
    any token material. When no router is wired the sidecar reports
    ``not_ready`` for the multi-tenant path (no fake-green — R7.5).
    """
    router = request.app.get(_ROUTER_KEY)
    if router is None:
        return web.json_response(
            {"status": "not_ready", "reason": "credential_store_unavailable"}
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


def _sub_digest(sub: str) -> str:
    """Return a short non-reversible digest of a ``sub`` for health output.

    The owning ``sub`` is server-side only and must not be echoed verbatim; a
    truncated SHA-256 lets an operator distinguish/count per-user sessions
    without exposing the identity or any token material (R6.4, R7.3).
    """
    import hashlib

    return hashlib.sha256(sub.encode("utf-8")).hexdigest()[:12]


def _unavailable_status(reason: str) -> int:
    """Map a credential-unavailable reason to an observable HTTP status (R7.1)."""
    # A missing owner/credential is a 404 (nothing to serve for this guild); a
    # failed/undecryptable credential is a 502 (upstream credential problem).
    if reason in ("no_owner", "no_credential"):
        return 404
    return 502


async def _handle_search(request: web.Request) -> web.Response:
    """Search Tidal using the requesting guild's owning user's token (R5.1)."""
    guild_id = request.match_info.get("guild_id", "").strip()
    if not guild_id:
        return web.json_response({"error": "missing guild_id"}, status=400)
    query = request.query.get("q", "").strip()
    if not query:
        return web.json_response({"error": "missing query parameter 'q'"}, status=400)
    try:
        limit = int(request.query.get("limit", "10"))
    except ValueError:
        return web.json_response({"error": "limit must be an integer"}, status=400)

    client = _resolve_client(request, guild_id)
    if isinstance(client, web.Response):
        return client
    try:
        results = await client.search(query, limit=limit)
    except TidalStreamError as error:
        log.warning("tidal-stream: search failed: %s", error)
        return web.json_response({"error": str(error)}, status=502)
    return web.json_response({"results": results})


async def _handle_stream(request: web.Request) -> web.Response:
    """Resolve a direct stream URL using the guild owner's token (R5.1)."""
    guild_id = request.match_info.get("guild_id", "").strip()
    track_id = request.match_info.get("track_id", "").strip()
    if not guild_id:
        return web.json_response({"error": "missing guild_id"}, status=400)
    if not track_id:
        return web.json_response({"error": "missing track_id"}, status=400)

    client = _resolve_client(request, guild_id)
    if isinstance(client, web.Response):
        return client
    try:
        url = await client.get_stream_url(track_id)
    except TidalStreamError as error:
        log.warning("tidal-stream: stream resolution failed: %s", error)
        return web.json_response({"error": str(error)}, status=502)
    return web.json_response({"track_id": track_id, "stream_url": url})


def _resolve_client(request: web.Request, guild_id: str):
    """Return the guild owner's client, or an observable error ``web.Response``.

    Centralizes the router lookup so a missing router (store unavailable) and a
    :class:`TidalCredentialUnavailableError` both map to a non-secret, attributable
    HTTP error with NO cross-user fallback (R5.4, R7.1, R10.5).
    """
    router = request.app.get(_ROUTER_KEY)
    if router is None:
        return web.json_response(
            {"error": "credential store unavailable", "reason": "not_ready"},
            status=503,
        )
    try:
        return router.client_for_guild(guild_id)
    except TidalCredentialUnavailableError as error:
        log.info(
            "tidal-stream: guild %s Tidal credential unavailable (%s)",
            guild_id, error.reason,
        )
        return web.json_response(
            {"error": "no Tidal credential for this guild", "reason": error.reason},
            status=_unavailable_status(error.reason),
        )


async def _handle_auth_callback(request: web.Request) -> web.Response:
    """OPTIONAL legacy first-party OAuth code-exchange forward (R9.2).

    Registered only when a token manager is configured. NOT part of the per-user
    streaming path; it completes the single-app-id authorization the web-ui
    forwards.
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
    router: TidalStreamRouter | None,
    *,
    token_manager: TidalTokenManager | None = None,
) -> web.Application:
    """Build the aiohttp application with the per-user routes wired.

    Args:
        router: The per-user :class:`TidalStreamRouter`, or ``None`` when the
            unified credential store is unavailable (the streaming routes then
            report observably not-ready rather than serving a single account).
        token_manager: The OPTIONAL legacy first-party token manager; when
            provided, the legacy ``/auth/callback`` code-exchange route is
            registered.

    Returns:
        A configured :class:`aiohttp.web.Application`.
    """
    app = web.Application()
    if router is not None:
        app[_ROUTER_KEY] = router
        app[_REGISTRY_KEY] = router.registry

    routes = [
        web.get("/healthz", _handle_health),
        web.get("/search/{guild_id}", _handle_search),
        web.get("/stream/{guild_id}/{track_id}", _handle_stream),
    ]
    if token_manager is not None:
        app[_TOKENS_KEY] = token_manager
        routes.append(web.get("/auth/callback", _handle_auth_callback))
    app.add_routes(routes)

    if router is not None:
        registry = router.registry

        async def _close_registry(_: web.Application) -> None:
            """Close every live per-user session on shutdown (R8.4).

            :meth:`SessionRegistry.close_all` invokes each client's sync
            ``close`` hook, which schedules the aiohttp session ``aclose`` on the
            still-running cleanup loop; a short yield lets those teardown tasks
            run before the loop stops so sessions are released cleanly.
            """
            import asyncio

            registry.close_all()
            await asyncio.sleep(0)

        app.on_cleanup.append(_close_registry)
    return app

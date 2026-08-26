"""Composition root and entry point for the activity-backend component.

Wires the config, HLS catalog, transcode client, visualizer/lyrics stores, the
WebSocket hub, and the aiohttp application together, then serves it. The heavy
runtime dependencies (aiohttp) are imported lazily inside :func:`build_transport`
and :func:`run`, so this module — and the wiring exercised by tests — imports
without aiohttp installed.
"""

from __future__ import annotations

import logging
from typing import Any

from .app import ActivityHandlers, create_app
from .config import ActivityConfig
from .hls import HlsCatalog
from .lyrics import LyricsStore
from .transcode_client import TranscodeClient
from .visualizer import VisualizerRegistry
from .ws_hub import WebSocketHub

log = logging.getLogger(__name__)

__all__ = ["build_transport", "build_components", "run"]


def build_transport() -> Any:
    """Create the aiohttp-backed transcode transport (lazy aiohttp import).

    Returns an object satisfying
    :class:`~activity_backend.transcode_client.Transport`.
    """
    import aiohttp

    class _AioHttpTransport:
        """aiohttp adapter implementing the transcode ``Transport`` protocol."""

        async def post_json(
            self, url: str, payload: dict[str, Any]
        ) -> dict[str, Any]:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload) as resp:
                    resp.raise_for_status()
                    return await resp.json()

    return _AioHttpTransport()


def _default_token_validator(token: str) -> int | None:
    """Fallback token→guild validator used when none is injected.

    The Discord Activity passes an instance/guild token; the production
    validator maps it to a guild id via the orchestrator. Absent that, this
    accepts a bare numeric guild id so the server is runnable in local/dev.
    """
    token = token.strip()
    if token.isdigit():
        return int(token)
    return None


def build_components(
    config: ActivityConfig,
    transport: Any,
    validate_guild_token: Any = None,
) -> tuple[WebSocketHub, ActivityHandlers, Any]:
    """Construct the hub, handlers, and aiohttp app from ``config``.

    Args:
        config: Runtime settings.
        transport: JSON transport for the transcode client (injected so tests
            can supply a fake without aiohttp).
        validate_guild_token: Optional token→guild validator; defaults to
            :func:`_default_token_validator`.

    Returns:
        A ``(hub, handlers, app)`` tuple. ``app`` is the aiohttp application
        (built lazily via :func:`~activity_backend.app.create_app`).
    """
    catalog = HlsCatalog(config)
    transcode = TranscodeClient(config.transcode_base_url, transport)
    visualizer = VisualizerRegistry()
    lyrics = LyricsStore()
    hub = WebSocketHub(
        validate_guild_token or _default_token_validator,
        visualizer=visualizer,
        lyrics=lyrics,
        max_strokes=config.max_strokes_per_guild,
        heartbeat_interval_s=config.heartbeat_interval_s,
    )
    handlers = ActivityHandlers(
        config, hub, catalog, transcode, visualizer, lyrics
    )
    app = create_app(config, hub, handlers)
    return hub, handlers, app


def run(config: ActivityConfig | None = None) -> None:
    """Build and serve the Activity backend until the process is stopped."""
    from aiohttp import web

    cfg = config or ActivityConfig.from_env()
    transport = build_transport()
    _hub, _handlers, app = build_components(cfg, transport)
    log.info(
        "activity-backend serving on %s:%d under %s",
        cfg.host,
        cfg.port,
        cfg.route_prefix,
    )
    web.run_app(app, host=cfg.host, port=cfg.port)


def main() -> None:
    """Console entry point: configure logging and run the server."""
    logging.basicConfig(level=logging.INFO)
    run()


if __name__ == "__main__":
    main()

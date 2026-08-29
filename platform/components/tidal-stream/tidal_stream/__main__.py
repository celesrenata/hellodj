"""Entrypoint for the ``tidal-stream`` sidecar (multi-tenant).

Builds runtime settings from the environment, wires the per-user streaming
router (unified-store resolver + guild→owner lookup + bounded per-``sub`` session
registry), the OPTIONAL legacy first-party token manager for ``/auth/callback``,
and serves the aiohttp app. Run as an independently deployable container
(R15.1)::

    python -m tidal_stream

The single startup-bound account is gone: each request resolves its own user's
Tidal token from the unified credential store, read-only (R5.1/R5.3).

Requirements: 5.1, 5.3, 6.1, 9.1, 9.2, 15.1
"""

from __future__ import annotations

import logging
import time

from aiohttp import web

from .config import TidalStreamSettings
from .server import build_app, create_components, create_router


def main() -> None:
    """Configure logging, wire the per-user router, and run the HTTP server."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = TidalStreamSettings.from_env()
    router = create_router(settings)
    token_manager = create_components(settings, clock=time.time)
    app = build_app(router, token_manager=token_manager)
    logging.getLogger(__name__).info(
        "tidal-stream listening on %s:%s (app_id=%s, multi_tenant=%s, "
        "legacy_callback=%s)",
        settings.host,
        settings.port,
        settings.app_id,
        router is not None,
        token_manager is not None,
    )
    web.run_app(app, host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()

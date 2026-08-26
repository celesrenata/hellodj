"""Entrypoint for the ``tidal-stream`` sidecar.

Builds runtime settings from the environment, constructs the first-party token
manager and streamer, and serves the aiohttp app (streaming + the HelloDJ-owned
``/auth/callback`` endpoint). Run as an independently deployable container
(R15.1)::

    python -m tidal_stream

Requirements: 6.1, 9.1, 9.2, 15.1
"""

from __future__ import annotations

import logging
import time

from aiohttp import web

from .config import TidalStreamSettings
from .server import build_app, create_components


def main() -> None:
    """Configure logging, build components, and run the HTTP server."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = TidalStreamSettings.from_env()
    token_manager, streamer = create_components(settings, clock=time.time)
    app = build_app(token_manager, streamer)
    logging.getLogger(__name__).info(
        "tidal-stream listening on %s:%s (app_id=%s)",
        settings.host,
        settings.port,
        settings.app_id,
    )
    web.run_app(app, host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()

"""Entrypoint for the ``playback-orchestrator`` component.

The orchestrator is the single writer for session/queue state on the
``hellodj-session`` hot table. Its routing/classification/filtering logic is a
pure, importable library (see :mod:`playback_orchestrator`); this module makes
the component *runnable* as an independently deployable container (R15.1)::

    python -m playback_orchestrator

It runs a long-lived process that exposes a minimal health endpoint on
``PORT`` (default 8080, matching the image's ``ExposedPorts``) so the
deployment stays up and Kubernetes readiness/liveness probes have a target.
Only the Python standard library is used here so the runtime image needs no
extra dependencies beyond what the flake already bundles.

Requirements: 6.1, 6.4, 15.1
"""

from __future__ import annotations

import logging
import os
import signal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import FrameType

_LOG = logging.getLogger("playback_orchestrator")


class _HealthHandler(BaseHTTPRequestHandler):
    """Serve ``GET /healthz`` (and ``/``) with a 200; everything else 404."""

    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        if self.path in ("/", "/healthz", "/health"):
            body = b'{"status":"ok","component":"playback-orchestrator"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, fmt: str, *args: object) -> None:
        """Route the stdlib access log through the component logger."""
        _LOG.debug("health-server: " + fmt, *args)


def _install_signal_handlers(server: ThreadingHTTPServer) -> None:
    """Shut the server down cleanly on SIGTERM/SIGINT."""

    def _handle(signum: int, _frame: FrameType | None) -> None:
        _LOG.info("received signal %s, shutting down", signum)
        server.shutdown()

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)


def main() -> None:
    """Configure logging and run the health server until terminated."""
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    host = os.environ.get("HOST", "0.0.0.0")  # noqa: S104 (container bind)
    port = int(os.environ.get("PORT", "8080"))
    server = ThreadingHTTPServer((host, port), _HealthHandler)
    _install_signal_handlers(server)
    _LOG.info(
        "playback-orchestrator ready: health server on %s:%s (stage=%s)",
        host,
        port,
        os.environ.get("HELLODJ_STAGE", "unknown"),
    )
    try:
        server.serve_forever()
    finally:
        server.server_close()
        _LOG.info("playback-orchestrator stopped")


if __name__ == "__main__":
    main()

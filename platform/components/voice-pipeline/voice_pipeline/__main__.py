"""Module entry point for the ``voice-pipeline`` component.

The container image runs ``python -m voice_pipeline``. ``python -m <package>``
executes the package's ``__main__`` submodule; without this module the
interpreter fails at startup with ``No module named voice_pipeline.__main__;
'voice_pipeline' is a package and cannot be directly executed`` and the pod
crash-loops.

:func:`voice_pipeline.main.main` is a one-shot configuration/model probe that
returns and exits — fine as a CLI check, but a container whose process exits is
treated by Kubernetes as a crash and restarted in a ``CrashLoopBackOff``. In the
deployed wiring the live opus stream is driven by ``discord-bot-core``; this
component's process must therefore stay resident. So we run the probe once for
its startup log/validation, then block on a minimal stdlib health server
(mirroring ``playback-orchestrator``) so readiness/liveness probes have a target
and the Deployment stays up. No extra dependencies are required.
"""

from __future__ import annotations

import logging
import os
import signal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import FrameType

from .main import main as probe_main

_LOG = logging.getLogger("voice_pipeline")


class _HealthHandler(BaseHTTPRequestHandler):
    """Serve ``GET /healthz`` (and ``/``) with a 200; everything else 404."""

    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        if self.path in ("/", "/healthz", "/health"):
            body = b'{"status":"ok","component":"voice-pipeline"}'
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
    """Run the startup probe, then serve health until terminated."""
    # Run the one-shot config/wakeword-model probe for its validation + log.
    # A non-zero probe result is logged but does not stop the health server —
    # the pod stays schedulable and observable rather than crash-looping.
    try:
        probe_rc = probe_main()
        if probe_rc != 0:
            _LOG.warning("voice-pipeline startup probe returned %s", probe_rc)
    except Exception:  # noqa: BLE001 - never let the probe crash the container
        _LOG.exception("voice-pipeline startup probe raised; continuing")

    host = os.environ.get("HOST", "0.0.0.0")  # noqa: S104 (container bind)
    port = int(os.environ.get("PORT", "8080"))
    server = ThreadingHTTPServer((host, port), _HealthHandler)
    _install_signal_handlers(server)
    _LOG.info("voice-pipeline ready: health server on %s:%s", host, port)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        _LOG.info("voice-pipeline stopped")


if __name__ == "__main__":
    main()

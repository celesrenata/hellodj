"""Entrypoint for the ``playback-orchestrator`` component.

The orchestrator is the single writer for session/queue state on the
``hellodj-session`` hot table. Its routing/classification/filtering logic is a
pure, importable library (see :mod:`playback_orchestrator`); this module makes
the component *runnable* as an independently deployable container (R15.1)::

    python -m playback_orchestrator

It runs a long-lived process that exposes a minimal health endpoint on
``PORT`` (default 8080, matching the image's ``ExposedPorts``) so the
deployment stays up and Kubernetes readiness/liveness probes have a target.
The stdlib health server needs no extra dependencies beyond what the flake
already bundles. Alongside it, ``main`` starts the durable token-refresh
**watchdog** on a daemon thread (see :mod:`playback_orchestrator.token_watchdog`
/ :mod:`playback_orchestrator.watchdog_bootstrap`); the watchdog self-degrades
to a no-op when no datastore / KMS / provider clients are configured, so the
health server always comes up (R5.1, R5.7). It also starts the AWS multi-bot
**instance runtime** on a second daemon thread (see
:mod:`playback_orchestrator.instance_bootstrap`); like the watchdog it
self-degrades to a no-op (health server unaffected) when no pool/claims are
configured or discord.py is absent, and its instances are disconnected cleanly
on SIGTERM/SIGINT (aws-multi-bot-runtime R2.1-R2.4).

Requirements: 5.1, 5.7, 6.1, 6.4, 15.1 (aws-multi-bot-runtime 2.1, 2.2, 2.3, 2.4)
"""

from __future__ import annotations

import json
import logging
import os
import signal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import FrameType
from typing import Any

from .playback_api import PLAYBACK_ROUTE, PlaybackService, handle_playback

_LOG = logging.getLogger("playback_orchestrator")

#: Max request body the playback endpoint will read (guards against a runaway
#: Content-Length). A play request is a few hundred bytes; 64 KiB is generous.
_MAX_BODY_BYTES = 64 * 1024


def _make_handler(
    playback: PlaybackService | None,
) -> type[BaseHTTPRequestHandler]:
    """Build the HTTP handler class bound to the (optional) playback service.

    The service is captured in a closure so the stdlib handler (which the server
    instantiates per request) can reach it without global state. A ``None``
    service still serves health + a clean "not configured" playback body.
    """

    class _Handler(BaseHTTPRequestHandler):
        """Serve ``GET /healthz`` + ``POST /v1/playback``; else 404."""

        def do_GET(self) -> None:  # noqa: N802 (http.server API)
            if self.path in ("/", "/healthz", "/health"):
                self._write_json(200, b'{"status":"ok","component":"playback-orchestrator"}')
            else:
                self.send_response(404)
                self.end_headers()

        def do_POST(self) -> None:  # noqa: N802 (http.server API)
            if self.path != PLAYBACK_ROUTE:
                self.send_response(404)
                self.end_headers()
                return
            body = handle_playback(playback, self._read_json_body())
            self._write_json(200, json.dumps(body).encode("utf-8"))

        # -- helpers -------------------------------------------------------

        def _read_json_body(self) -> dict[str, Any]:
            """Read + parse the JSON request body (empty dict on any problem)."""
            try:
                length = int(self.headers.get("Content-Length", "0") or "0")
            except ValueError:
                return {}
            if length <= 0 or length > _MAX_BODY_BYTES:
                return {}
            try:
                raw = self.rfile.read(length)
                parsed = json.loads(raw or b"{}")
            except (ValueError, OSError):
                return {}
            return parsed if isinstance(parsed, dict) else {}

        def _write_json(self, status: int, body: bytes) -> None:
            """Write a JSON response with the given status + body."""
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt: str, *args: object) -> None:
            """Route the stdlib access log through the component logger."""
            _LOG.debug("health-server: " + fmt, *args)

    return _Handler


def _install_signal_handlers(
    server: ThreadingHTTPServer, on_shutdown: object | None = None
) -> None:
    """Shut the server down cleanly on SIGTERM/SIGINT.

    ``on_shutdown`` is an optional zero-arg callable run before the health
    server stops — used to disconnect the multi-bot instance runtime's
    Bot_Instances cleanly within the shutdown window (Requirement 2.4). It is
    best-effort: a failure to shut the runtime down never blocks the server
    shutdown.
    """

    def _handle(signum: int, _frame: FrameType | None) -> None:
        _LOG.info("received signal %s, shutting down", signum)
        if on_shutdown is not None:
            try:
                on_shutdown()  # type: ignore[operator]
            except Exception as exc:  # noqa: BLE001 - never block shutdown
                _LOG.warning("instance runtime shutdown error: %s", exc)
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

    # Build the playback API service (router + single-writer session store).
    # Degrades to None when no session table / boto3 is configured; the handler
    # then serves a clean "not configured" body instead of a 404, so the bot's
    # /play reply is truthful. This is the endpoint the bot's PlaybackClient
    # POSTs to (POST /v1/playback) — without it every /play hit the 404 and read
    # as "Playback service is unavailable right now."
    from .playback_bootstrap import build_playback_service

    playback = build_playback_service()
    if playback is None:
        _LOG.warning(
            "playback API: no session table configured — /v1/playback will "
            "report 'not configured' until HELLODJ_SESSION_TABLE + creds are wired"
        )
    else:
        _LOG.info("playback API: serving POST /v1/playback")
    server = ThreadingHTTPServer((host, port), _make_handler(playback))

    # Start the durable token-refresh watchdog on a daemon thread next to the
    # health server (R5.1). It self-degrades to a no-op (logs "degraded:
    # watchdog disabled") when no datastore / KMS / provider clients are
    # configured, so the health server always comes up regardless (R5.7).
    from .watchdog_bootstrap import start_watchdog_thread

    start_watchdog_thread()

    # Start the AWS multi-bot instance runtime on its own daemon thread/loop
    # (aws-multi-bot-runtime R2.1). It connects one voice-only secondary gateway
    # per claimed pool application, isolated from the health server and watchdog
    # (R2.2). It self-degrades to a no-op (logs "degraded: instance runtime
    # disabled") when no pool/claims/datastore are configured or discord.py is
    # absent, so the health server still comes up (R2.3). The returned handle's
    # stop() disconnects the instances cleanly on SIGTERM/SIGINT (R2.4).
    from .instance_bootstrap import start_instance_runtime_thread

    instance_runtime = start_instance_runtime_thread()

    _install_signal_handlers(
        server,
        on_shutdown=(
            instance_runtime.stop if instance_runtime is not None else None
        ),
    )

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

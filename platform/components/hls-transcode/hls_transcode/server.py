"""aiohttp HTTP service exposing the transcode start/stop API.

The activity-backend calls this service over intra-node loopback to start/stop
HLS transcode jobs (Decision D2 — co-located, so the producer -> transcoder hop
is free). The endpoints delegate to a :class:`~hls_transcode.jobs.JobManager`
whose planning is pure; this module only maps HTTP to that manager and reports
health. aiohttp is imported lazily inside :func:`build_app`/:func:`main` so the
package imports for tests / py_compile without aiohttp installed (R15.1).

Endpoints:
    * ``GET  /healthz``               - liveness probe.
    * ``POST /v1/transcode``          - start/refresh an HLS transcode job.
    * ``POST /v1/transcode/stop``     - stop a transcode job.
    * ``GET  /v1/pressure``           - current CPU/GPU pressure snapshot.

Requirements: 3.1, 3.9, 3.11, 6.2, 15.1, 16.4, 18.4
"""

from __future__ import annotations

import logging
from typing import Any

from .config import TranscodeConfig
from .hls_writer import HlsWriter
from .jobs import JobError, JobManager
from .scheduler import TranscodeScheduler

__all__ = ["build_handlers", "build_app", "create_job_manager", "main"]

log = logging.getLogger(__name__)


def create_job_manager(config: TranscodeConfig) -> JobManager:
    """Compose a :class:`JobManager` from the runtime configuration.

    Wires the hybrid-GPU scheduler (thresholds + GPU availability) and the HLS
    writer (S3 bucket/prefix + CloudFront domain) into a job manager. No AWS or
    ffmpeg objects are created here.
    """
    scheduler = TranscodeScheduler(
        config.gpu_thresholds,
        gpu_available=config.gpu_available,
    )
    writer = HlsWriter(
        scratch_root=JobManager.scratch_root(),
        s3_bucket=config.hls_s3_bucket,
        s3_prefix=config.hls_s3_prefix,
        cloudfront_domain=config.cloudfront_domain,
    )
    return JobManager(config, scheduler, writer)


class TranscodeHandlers:
    """Pure request handlers returning ``(status, body)`` tuples.

    Kept aiohttp-free so the start/stop/pressure logic is unit-testable without
    a server; :func:`build_app` adapts these into aiohttp coroutines.
    """

    def __init__(self, manager: JobManager) -> None:
        """Initialise with the job manager the handlers delegate to."""
        self._manager = manager

    def start_transcode(
        self, payload: dict[str, Any]
    ) -> tuple[int, dict[str, Any]]:
        """Handle a start/refresh request; returns ``(status, body)``.

        Accepts the activity-backend's transcode payload (``guildId``,
        ``kind``, ``streamId``, optional ``sourceUri``) and returns the resolved
        playlist location and selected encoder path.
        """
        try:
            guild_id = int(payload["guildId"])
            stream_id = str(payload["streamId"])
        except (KeyError, ValueError, TypeError):
            return 400, {"error": "guildId and streamId are required"}
        kind = str(payload.get("kind", "video"))
        source_uri = payload.get("sourceUri")
        try:
            plan = self._manager.plan_transcode(
                guild_id, kind, stream_id, source_uri=source_uri
            )
        except JobError as error:
            return 400, {"error": str(error)}
        return 202, {
            "accepted": True,
            "encoder": plan.encoder.value,
            "playlistKey": plan.artifacts.playlist_key,
            "playlistUrl": plan.artifacts.playlist_url,
        }

    def stop_transcode(
        self, payload: dict[str, Any]
    ) -> tuple[int, dict[str, Any]]:
        """Handle a stop request; returns ``(status, body)``."""
        try:
            guild_id = int(payload["guildId"])
            stream_id = str(payload["streamId"])
        except (KeyError, ValueError, TypeError):
            return 400, {"error": "guildId and streamId are required"}
        stopped = self._manager.stop_transcode(guild_id, stream_id)
        return 200, {"stopped": stopped}

    def pressure(self) -> tuple[int, dict[str, Any]]:
        """Return the current CPU/GPU pressure snapshot (R16.4)."""
        snapshot = self._manager.scheduler.pressure_snapshot()
        return 200, {
            "cpuPressure": snapshot.cpu_pressure,
            "gpuActive": snapshot.gpu_active,
            "state": snapshot.state.value,
            "activeJobs": snapshot.active_jobs,
        }


def build_handlers(config: TranscodeConfig) -> TranscodeHandlers:
    """Build the pure :class:`TranscodeHandlers` from configuration."""
    return TranscodeHandlers(create_job_manager(config))


def build_app(handlers: TranscodeHandlers) -> Any:
    """Build the aiohttp application wiring HTTP routes to the handlers.

    aiohttp is imported lazily here so importing this module (for tests /
    py_compile) does not require aiohttp to be installed.

    Args:
        handlers: The pure request handlers to adapt into aiohttp coroutines.

    Returns:
        A configured ``aiohttp.web.Application``.
    """
    from aiohttp import web

    async def _health(_: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    async def _start(request: web.Request) -> web.Response:
        payload = await _read_json(request)
        status, body = handlers.start_transcode(payload)
        return web.json_response(body, status=status)

    async def _stop(request: web.Request) -> web.Response:
        payload = await _read_json(request)
        status, body = handlers.stop_transcode(payload)
        return web.json_response(body, status=status)

    async def _pressure(_: web.Request) -> web.Response:
        status, body = handlers.pressure()
        return web.json_response(body, status=status)

    async def _read_json(request: web.Request) -> dict[str, Any]:
        try:
            data = await request.json()
        except Exception:  # malformed/absent body -> empty payload
            return {}
        return data if isinstance(data, dict) else {}

    app = web.Application()
    app.add_routes(
        [
            web.get("/healthz", _health),
            web.post("/v1/transcode", _start),
            web.post("/v1/transcode/stop", _stop),
            web.get("/v1/pressure", _pressure),
        ]
    )
    return app


def main() -> None:
    """Console entry point: build config + app and run the aiohttp server."""
    from aiohttp import web

    logging.basicConfig(level=logging.INFO)
    config = TranscodeConfig.from_env()
    handlers = build_handlers(config)
    app = build_app(handlers)
    log.info(
        "hls-transcode listening on %s:%s (gpu_available=%s)",
        config.host,
        config.port,
        config.gpu_available,
    )
    web.run_app(app, host=config.host, port=config.port)


if __name__ == "__main__":
    main()

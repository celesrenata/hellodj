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

import asyncio
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

    When a :class:`~hls_transcode.runtime.FfmpegProcessManager` is injected the
    handlers ALSO drive the real ffmpeg lifecycle: a start request spawns the
    encoder for the resolved plan (mirroring segments to S3 via the uploader),
    and a stop request drains it (R17). Without a process manager the handlers
    stay pure planning-only (the shape the smoke tests exercise).
    """

    def __init__(
        self,
        manager: JobManager,
        *,
        process_manager: object | None = None,
        uploader: object | None = None,
    ) -> None:
        """Initialise with the job manager and optional ffmpeg process manager.

        Args:
            manager: The job manager the handlers delegate planning to.
            process_manager: Optional :class:`FfmpegProcessManager`; when
                supplied, start/stop drive the real ffmpeg process lifecycle.
            uploader: Optional :class:`SegmentUploader` passed to the process
                manager so produced segments mirror to the S3 CloudFront origin.
        """
        self._manager = manager
        self._process_manager = process_manager
        self._uploader = uploader

    def start_transcode(
        self, payload: dict[str, Any]
    ) -> tuple[int, dict[str, Any]]:
        """Handle a start/refresh request; returns ``(status, body)``.

        Accepts the activity-backend's transcode payload (``guildId``,
        ``kind``, ``streamId``, optional ``sourceUri``) and returns the resolved
        playlist location and selected encoder path. When a process manager is
        wired, the ffmpeg process is spawned as a background task (the HTTP
        response does not block on encoder startup).
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
        if self._process_manager is not None:
            asyncio.ensure_future(
                self._process_manager.start(plan, uploader=self._uploader)
            )
        return 202, {
            "accepted": True,
            "encoder": plan.encoder.value,
            "playlistKey": plan.artifacts.playlist_key,
            "playlistUrl": plan.artifacts.playlist_url,
        }

    def stop_transcode(
        self, payload: dict[str, Any]
    ) -> tuple[int, dict[str, Any]]:
        """Handle a stop request; returns ``(status, body)``.

        Stops tracking the job and, when a process manager is wired, drains the
        ffmpeg process as a background task (SIGTERM -> wait -> SIGKILL, R17).
        """
        try:
            guild_id = int(payload["guildId"])
            stream_id = str(payload["streamId"])
        except (KeyError, ValueError, TypeError):
            return 400, {"error": "guildId and streamId are required"}
        stopped = self._manager.stop_transcode(guild_id, stream_id)
        if self._process_manager is not None:
            asyncio.ensure_future(
                self._process_manager.stop(guild_id, stream_id)
            )
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


def build_handlers(
    config: TranscodeConfig, manager: JobManager | None = None
) -> TranscodeHandlers:
    """Build the pure :class:`TranscodeHandlers` from configuration.

    Args:
        config: The component runtime configuration.
        manager: Optional shared :class:`JobManager`; a fresh one is composed
            when omitted. Pass the same instance the runtime control loop uses
            so active-job counts and the encoder decision stay consistent.
    """
    return TranscodeHandlers(manager or create_job_manager(config))


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
    """Console entry point: run the aiohttp server + the hybrid control loop.

    Composes ONE shared :class:`JobManager` used by both the HTTP handlers and
    the runtime control loop, wires the ffmpeg process manager + S3 segment
    uploader so start/stop drive the real encoder lifecycle, and runs the
    control loop (demand -> scheduler -> CloudWatch pressure) as a background
    task next to the aiohttp server. On shutdown the loop is stopped and every
    ffmpeg process is drained within the bounded window (R17).
    """
    from aiohttp import web

    from .runtime import (
        FfmpegProcessManager,
        SegmentUploader,
        build_runtime,
    )
    from .s3_sink import S3Sink, create_s3_client

    logging.basicConfig(level=logging.INFO)
    config = TranscodeConfig.from_env()

    # One shared manager for the handlers AND the control loop.
    manager = create_job_manager(config)
    runtime = build_runtime(config, manager)
    process_manager = FfmpegProcessManager()

    # Segment uploader (only when an HLS bucket is configured; otherwise the
    # encoder still runs but nothing mirrors to S3 — degraded mode).
    uploader: SegmentUploader | None = None
    if config.hls_s3_bucket:
        uploader = SegmentUploader(
            S3Sink(config.hls_s3_bucket, create_s3_client(config.aws_region))
        )

    handlers = TranscodeHandlers(
        manager, process_manager=process_manager, uploader=uploader
    )
    app = build_app(handlers)

    async def _on_startup(_: Any) -> None:
        app["control_loop"] = asyncio.ensure_future(runtime.run())

    async def _on_cleanup(_: Any) -> None:
        runtime.request_stop()
        task = app.get("control_loop")
        if task is not None:
            task.cancel()
        await process_manager.stop_all()

    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)

    log.info(
        "hls-transcode listening on %s:%s (gpu_available=%s, hls_bucket=%s)",
        config.host,
        config.port,
        config.gpu_available,
        config.hls_s3_bucket or "<none>",
    )
    web.run_app(app, host=config.host, port=config.port)


if __name__ == "__main__":
    main()

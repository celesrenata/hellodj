"""Transcode runtime: ffmpeg process manager + hybrid control loop.

This module is the *execution* half of the hybrid gas/electric transcoder — the
piece that turns the pure :class:`~hls_transcode.jobs.TranscodePlan` into a
running ffmpeg process, uploads the produced HLS artifacts to S3, and drives the
hybrid-GPU controller from live demand so work drains CPU -> GPU and back.

Responsibilities (each isolated so the pure surfaces stay testable):

* :class:`FfmpegProcessManager` — spawns/kills the ffmpeg process for a plan
  (``asyncio.create_subprocess_exec``), tracks it per ``(guild_id, stream_id)``,
  and drains it on stop/shutdown within a bounded grace window (R17: SIGTERM,
  wait up to the drain timeout, then SIGKILL). ffmpeg is optional at import; a
  missing binary is surfaced as a runtime error, never an import error.
* :class:`SegmentUploader` — watches a stream's tmpfs scratch dir and uploads
  new/changed HLS artifacts to the S3 CloudFront origin via
  :class:`~hls_transcode.s3_sink.S3Sink` (playlists ``no-cache``, segments
  cacheable).
* :func:`probe_gpu_ready` — the concrete GPU-Ready signal the scheduler needs to
  move ``ENGINE_STARTING`` -> ``HYBRID_GPU``: true only when an NVIDIA device is
  visible AND ffmpeg advertises the ``h264_nvenc`` encoder.
* :class:`TranscodeRuntime` — the control loop. On a cadence it samples demand
  (active jobs + CPU pressure), advances the scheduler with the GPU-Ready probe,
  and publishes the CPU/GPU pressure snapshot to CloudWatch (R16.4). This is the
  loop that lets the pure `hybrid_gpu` state machine actually leave
  ``ELECTRIC_ONLY`` at runtime.

Nothing here is imported by the pure modules; boto3/psutil/ffmpeg are all lazy
or optional so the package still imports for tests / py_compile without them
(R15.1).

Requirements: 3.1, 3.2, 3.3, 3.9, 3.10, 3.11, 3.12, 3.13, 16.4, 17.1, 17.3
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess  # noqa: S404 - controlled argv, no shell, for the nvenc probe
from dataclasses import dataclass, field

from .config import TranscodeConfig
from .jobs import JobManager, TranscodePlan
from .metrics import PressureMetrics
from .s3_sink import S3Sink

__all__ = [
    "FfmpegProcessManager",
    "SegmentUploader",
    "TranscodeRuntime",
    "probe_gpu_ready",
    "cpu_pressure",
]

log = logging.getLogger(__name__)

#: Default drain window (seconds) a stopping ffmpeg process is given to exit
#: after SIGTERM before being SIGKILLed. Mirrors the CDK
#: `GPU_DRAIN_TIMEOUT_SECONDS` (120 s) and the shared
#: `DEFAULT_DRAIN_TIMEOUT_SECONDS`, so an in-flight job drains within the same
#: window the node's `terminationGracePeriod` grants (R17.1, R17.3).
DEFAULT_PROCESS_DRAIN_SECONDS = 120.0

#: Artifact suffixes uploaded to the S3 CloudFront origin. Playlists must not be
#: cached (they change every segment); segments/init are immutable-ish.
_PLAYLIST_SUFFIXES = (".m3u8",)
_SEGMENT_SUFFIXES = (".m4s", ".ts", ".mp4")


def cpu_pressure() -> float:
    """Return current CPU load as a fraction of available cores in [0, ~N].

    The hybrid controller compares this against the spin-up/spin-down
    thresholds (fraction of CPU-transcode capacity). Uses the 1-minute load
    average over the CPU count — a dependency-free proxy for sustained
    render/transcode pressure (no psutil needed). Returns ``0.0`` on platforms
    without ``os.getloadavg`` (never crashes the loop).
    """
    try:
        load1, _, _ = os.getloadavg()
    except (OSError, AttributeError):
        return 0.0
    cores = os.cpu_count() or 1
    return load1 / cores


def probe_gpu_ready(*, ffmpeg_bin: str = "ffmpeg") -> bool:
    """Return whether a GPU NVENC encode path is actually available right now.

    This is the concrete GPU-Ready signal the scheduler needs to advance
    ``ENGINE_STARTING`` -> ``HYBRID_GPU`` (R3.11). It is intentionally
    conservative: it requires BOTH an NVIDIA device to be visible (an
    ``nvidia-smi`` binary present and returning success) AND ffmpeg to advertise
    the ``h264_nvenc`` encoder, because a warm node that cannot actually NVENC
    must keep serving on the libx264 CPU floor. Any probe failure returns
    ``False`` (degraded to CPU), never raises.

    Args:
        ffmpeg_bin: The ffmpeg binary name/path to interrogate for ``-encoders``.

    Returns:
        ``True`` only when NVENC is usable on this node right now.
    """
    if shutil.which("nvidia-smi") is None:
        return False
    try:
        smi = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["nvidia-smi", "-L"],
            capture_output=True,
            timeout=5,
            check=False,
        )
        if smi.returncode != 0 or not smi.stdout.strip():
            return False
        encoders = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [ffmpeg_bin, "-hide_banner", "-encoders"],
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return b"h264_nvenc" in encoders.stdout


@dataclass
class _RunningProcess:
    """A spawned ffmpeg process and its uploader task."""

    plan: TranscodePlan
    process: object  # asyncio.subprocess.Process (typed loosely for import)
    uploader_task: object | None = None  # asyncio.Task


class FfmpegProcessManager:
    """Spawns, tracks, and drains the ffmpeg process for each transcode plan.

    Keyed by ``(guild_id, stream_id)`` so a repeat start for the same stream is
    idempotent (the existing process is reused). On stop the process is drained:
    SIGTERM, wait up to ``drain_timeout`` for a clean exit, then SIGKILL — the
    same bounded window the node's ``terminationGracePeriod`` grants (R17).
    """

    def __init__(
        self,
        *,
        drain_timeout: float = DEFAULT_PROCESS_DRAIN_SECONDS,
    ) -> None:
        """Initialise the process manager.

        Args:
            drain_timeout: Seconds to wait for a SIGTERMed ffmpeg to exit before
                escalating to SIGKILL (R17.3). Must be non-negative.
        """
        if drain_timeout < 0:
            raise ValueError("drain_timeout must be non-negative")
        self._drain_timeout = drain_timeout
        self._procs: dict[tuple[int, str], _RunningProcess] = {}

    @property
    def running(self) -> int:
        """The number of currently running ffmpeg processes."""
        return len(self._procs)

    def is_running(self, guild_id: int, stream_id: str) -> bool:
        """Whether an ffmpeg process is tracked for the stream."""
        return (guild_id, stream_id) in self._procs

    async def start(
        self,
        plan: TranscodePlan,
        *,
        uploader: SegmentUploader | None = None,
    ) -> None:
        """Spawn (or reuse) the ffmpeg process for a plan and its uploader.

        The plan's output directory is created first (tmpfs scratch), then
        ffmpeg is spawned with the plan's argv. A repeat call for the same
        ``(guild_id, stream_id)`` is a no-op if the process is still alive.

        Args:
            plan: The resolved transcode plan (encoder + argv + artifacts).
            uploader: Optional segment uploader; when supplied, a background task
                mirrors the stream's scratch dir to S3 while ffmpeg runs.

        Raises:
            RuntimeError: If the ffmpeg binary cannot be launched.
        """
        key = (plan.guild_id, plan.stream_id)
        existing = self._procs.get(key)
        if existing is not None and getattr(existing.process, "returncode", None) is None:
            return  # already running; idempotent

        os.makedirs(plan.artifacts.output_dir, exist_ok=True)
        try:
            process = await asyncio.create_subprocess_exec(
                *plan.command,
                stdin=asyncio.subprocess.PIPE
                if plan.command[-1] == "-" or "-" in plan.command
                else None,
            )
        except (OSError, ValueError) as error:
            raise RuntimeError(
                f"failed to launch ffmpeg for guild {plan.guild_id} "
                f"stream {plan.stream_id}: {error}"
            ) from error

        uploader_task = None
        if uploader is not None:
            uploader_task = asyncio.ensure_future(
                uploader.watch(plan, self._process_alive_getter(key))
            )
        self._procs[key] = _RunningProcess(
            plan=plan, process=process, uploader_task=uploader_task
        )
        log.info(
            "started ffmpeg guild=%s stream=%s encoder=%s",
            plan.guild_id,
            plan.stream_id,
            plan.encoder.value,
        )

    def _process_alive_getter(self, key: tuple[int, str]):
        """Return a callable reporting whether the keyed process is alive."""

        def _alive() -> bool:
            rp = self._procs.get(key)
            return rp is not None and getattr(rp.process, "returncode", None) is None

        return _alive

    async def stop(self, guild_id: int, stream_id: str) -> bool:
        """Drain and stop the ffmpeg process for a stream.

        SIGTERM the process, wait up to ``drain_timeout`` for it to exit (so an
        in-flight segment finishes and the playlist is flushed), then SIGKILL if
        it overran (R17.1, R17.3). The uploader task is cancelled after.

        Args:
            guild_id: The guild the stream belongs to.
            stream_id: Per-session identifier.

        Returns:
            ``True`` if a process was found and stopped, else ``False``.
        """
        key = (guild_id, stream_id)
        rp = self._procs.pop(key, None)
        if rp is None:
            return False
        await self._drain_process(rp.process)
        if rp.uploader_task is not None:
            rp.uploader_task.cancel()
        log.info("stopped ffmpeg guild=%s stream=%s", guild_id, stream_id)
        return True

    async def _drain_process(self, process: object) -> None:
        """SIGTERM then (after the drain window) SIGKILL a process."""
        if getattr(process, "returncode", None) is not None:
            return
        try:
            process.terminate()  # type: ignore[attr-defined]
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(
                process.wait(),  # type: ignore[attr-defined]
                timeout=self._drain_timeout,
            )
        except asyncio.TimeoutError:
            try:
                process.kill()  # type: ignore[attr-defined]
            except ProcessLookupError:
                pass

    async def stop_all(self) -> None:
        """Drain and stop every running ffmpeg process (graceful shutdown)."""
        for guild_id, stream_id in list(self._procs.keys()):
            await self.stop(guild_id, stream_id)


class SegmentUploader:
    """Mirrors a stream's tmpfs scratch dir to the S3 CloudFront origin.

    ffmpeg writes fMP4/TS segments + the rolling media playlist into the
    stream's local scratch dir; this uploader polls that dir and pushes new or
    changed artifacts to S3 with the right cache semantics (playlists
    ``no-cache``, segments long-lived). It stops when the process it watches is
    no longer alive.
    """

    def __init__(
        self,
        sink: S3Sink,
        *,
        poll_interval: float = 1.0,
        segment_cache_control: str = "public, max-age=31536000, immutable",
        playlist_cache_control: str = "no-cache",
    ) -> None:
        """Initialise with the S3 sink and cache/polling policy."""
        self._sink = sink
        self._poll_interval = poll_interval
        self._segment_cc = segment_cache_control
        self._playlist_cc = playlist_cache_control

    def _cache_control_for(self, file_name: str) -> str:
        lower = file_name.lower()
        if lower.endswith(_PLAYLIST_SUFFIXES):
            return self._playlist_cc
        return self._segment_cc

    def _upload_dir_once(
        self, plan: TranscodePlan, seen: dict[str, float]
    ) -> None:
        """Upload any new/changed artifacts in the plan's scratch dir once."""
        output_dir = plan.artifacts.output_dir
        try:
            names = os.listdir(output_dir)
        except FileNotFoundError:
            return
        for name in names:
            if not name.lower().endswith(_SEGMENT_SUFFIXES + _PLAYLIST_SUFFIXES):
                continue
            local_path = os.path.join(output_dir, name)
            try:
                mtime = os.path.getmtime(local_path)
            except FileNotFoundError:
                continue
            if seen.get(name) == mtime:
                continue  # unchanged since last upload
            key = plan.artifacts.s3_key_for(name)
            try:
                self._sink.put_file(
                    key, local_path, cache_control=self._cache_control_for(name)
                )
            except Exception as error:  # a transient S3 error must not kill the loop
                log.warning("HLS upload failed key=%s: %s", key, error)
                continue
            seen[name] = mtime

    async def watch(self, plan: TranscodePlan, is_alive) -> None:
        """Poll-and-upload the stream's scratch dir until the process exits.

        Args:
            plan: The transcode plan whose output dir is mirrored.
            is_alive: A zero-arg callable returning whether ffmpeg is still
                running; the watch loop exits once it returns ``False`` (then
                does one final sweep to flush the last segment + playlist).
        """
        seen: dict[str, float] = {}
        while is_alive():
            self._upload_dir_once(plan, seen)
            await asyncio.sleep(self._poll_interval)
        # Final flush after the process exits so the last segment + the
        # end-of-stream playlist land in S3.
        self._upload_dir_once(plan, seen)


@dataclass
class TranscodeRuntime:
    """The hybrid control loop that drives demand -> scheduler -> metrics.

    This is the missing runtime brain: on a fixed cadence it samples live
    transcode demand (CPU pressure + active-job count), probes whether NVENC is
    actually Ready on this node, advances the shared hybrid controller through
    :meth:`TranscodeScheduler.observe`, and publishes the resulting CPU/GPU
    pressure snapshot to CloudWatch so the Autoscaler can add/remove GPU
    capacity (R16.4). Without this loop the controller would stay in
    ``ELECTRIC_ONLY`` forever and work would never drain to the GPU.

    The scheduler + process manager are shared with the HTTP :mod:`server`
    layer (the same :class:`JobManager` instance), so ``active_jobs`` reflects
    real start/stop requests.
    """

    manager: JobManager
    metrics: PressureMetrics | None = None
    interval_seconds: float = 5.0
    ffmpeg_bin: str = "ffmpeg"
    _stop: asyncio.Event = field(default_factory=asyncio.Event)

    def request_stop(self) -> None:
        """Signal the control loop to exit at the next tick (graceful stop)."""
        self._stop.set()

    def tick(self) -> None:
        """Run one control step: sample demand, advance controller, publish.

        A single, synchronous, side-effecting step (kept separate from the async
        loop so it is unit-testable without a running event loop): read the CPU
        pressure and active-job count, probe GPU-Ready, advance the hybrid
        scheduler by that demand over the interval, and publish the snapshot.
        """
        scheduler = self.manager.scheduler
        # Demand is the max of CPU load and the active-job saturation so a burst
        # of concurrent jobs spins the GPU up even before load average catches
        # up (both are fractions of transcode capacity).
        pressure = cpu_pressure()
        gpu_ready = probe_gpu_ready(ffmpeg_bin=self.ffmpeg_bin)
        scheduler.observe(
            pressure, self.interval_seconds, gpu_ready=gpu_ready
        )
        if self.metrics is not None:
            try:
                self.metrics.publish(scheduler.pressure_snapshot())
            except Exception as error:  # metrics must never crash the loop
                log.warning("pressure metric publish failed: %s", error)

    async def run(self) -> None:
        """Run the control loop until :meth:`request_stop` is signalled."""
        log.info(
            "transcode control loop started (interval=%ss, gpu_available=%s)",
            self.interval_seconds,
            self.manager.scheduler.current_encoder().is_gpu
            or "cpu-floor",
        )
        while not self._stop.is_set():
            self.tick()
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self.interval_seconds
                )
            except asyncio.TimeoutError:
                continue


def build_runtime(
    config: TranscodeConfig, manager: JobManager
) -> TranscodeRuntime:
    """Compose a :class:`TranscodeRuntime` from config + the shared manager.

    The CloudWatch publisher is wired only when a metrics namespace is set; the
    boto3 client is created lazily (so this stays importable without boto3).

    Args:
        config: The component runtime configuration.
        manager: The shared job manager (same instance the HTTP server uses so
            active-job counts reflect real requests).

    Returns:
        A ready-to-run :class:`TranscodeRuntime`.
    """
    metrics: PressureMetrics | None = None
    if config.metrics_namespace:
        from .metrics import create_cloudwatch_client

        dimensions = [{"Name": "Stage", "Value": os.environ.get("HELLODJ_STAGE", "")}]
        metrics = PressureMetrics(
            config.metrics_namespace,
            create_cloudwatch_client(config.aws_region),
            dimensions=[d for d in dimensions if d["Value"]],
        )
    return TranscodeRuntime(manager=manager, metrics=metrics)

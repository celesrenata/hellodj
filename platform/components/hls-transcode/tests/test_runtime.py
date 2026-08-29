"""Tests for the transcode runtime (process manager, uploader, control loop).

These exercise the runtime WITHOUT spawning a real ffmpeg or touching AWS: the
ffmpeg process is faked, the S3 sink is a fake recorder, and the GPU-Ready probe
is monkeypatched. The central assertions are:

* the control loop actually advances the hybrid controller from live demand, so
  work drains ELECTRIC_ONLY -> ENGINE_STARTING -> HYBRID_GPU (and back) at
  runtime (the gap the pure state machine could not close on its own);
* the process manager drains a stopped ffmpeg within the bounded window (R17);
* the segment uploader mirrors produced artifacts to S3 with the right cache
  semantics and survives a transient upload error.
"""

from __future__ import annotations

import asyncio
import os

from hellodj_platform_logic.types import HybridGpuState, HybridGpuThresholds

from hls_transcode.config import TranscodeConfig
from hls_transcode.jobs import JobManager
from hls_transcode.hls_writer import HlsWriter
from hls_transcode.metrics import PressureMetrics
from hls_transcode.runtime import (
    FfmpegProcessManager,
    SegmentUploader,
    TranscodeRuntime,
    cpu_pressure,
    probe_gpu_ready,
)
from hls_transcode.s3_sink import S3Sink
from hls_transcode.scheduler import TranscodeScheduler


def _thresholds() -> HybridGpuThresholds:
    return HybridGpuThresholds(
        spin_up_threshold=0.8,
        spin_down_threshold=0.3,
        spin_up_window_seconds=10.0,
        spin_down_window_seconds=20.0,
    )


def _manager(gpu_available: bool = True) -> JobManager:
    cfg = TranscodeConfig.from_env({"HELLODJ_HLS_S3_BUCKET": "b"})
    return JobManager(
        cfg,
        TranscodeScheduler(_thresholds(), gpu_available=gpu_available),
        HlsWriter(scratch_root="/s", s3_bucket="b", s3_prefix="hls"),
    )


# --------------------------------------------------------------------------- #
# Pure probes
# --------------------------------------------------------------------------- #


def test_cpu_pressure_non_negative() -> None:
    assert cpu_pressure() >= 0.0


def test_probe_gpu_ready_false_without_nvidia(monkeypatch) -> None:
    # No nvidia-smi on PATH -> not ready (degrades to libx264).
    monkeypatch.setattr("hls_transcode.runtime.shutil.which", lambda _: None)
    assert probe_gpu_ready() is False


# --------------------------------------------------------------------------- #
# Control loop drives the hybrid controller (the closed gap)
# --------------------------------------------------------------------------- #


def test_control_loop_drains_cpu_to_gpu_and_back(monkeypatch) -> None:
    manager = _manager(gpu_available=True)
    runtime = TranscodeRuntime(manager=manager, interval_seconds=10.0)

    # Force sustained high demand + a Ready GPU: ELECTRIC_ONLY -> ENGINE_STARTING
    # -> HYBRID_GPU, i.e. work drains to the GPU.
    monkeypatch.setattr("hls_transcode.runtime.cpu_pressure", lambda: 0.95)
    monkeypatch.setattr(
        "hls_transcode.runtime.probe_gpu_ready", lambda *, ffmpeg_bin="ffmpeg": True
    )
    assert manager.scheduler.status.state is HybridGpuState.ELECTRIC_ONLY
    runtime.tick()
    assert manager.scheduler.status.state is HybridGpuState.ENGINE_STARTING
    runtime.tick()
    assert manager.scheduler.status.state is HybridGpuState.HYBRID_GPU
    assert manager.scheduler.current_encoder().is_gpu is True

    # Now sustained low demand: HYBRID_GPU -> COASTING -> ELECTRIC_ONLY, i.e.
    # work drains back to the CPU and the GPU scales to zero. The spin-down
    # window (20 s) needs two 10 s ticks to accumulate; the GPU stays warm and
    # keeps serving until the sustained window elapses (hysteresis).
    monkeypatch.setattr("hls_transcode.runtime.cpu_pressure", lambda: 0.05)
    monkeypatch.setattr(
        "hls_transcode.runtime.probe_gpu_ready", lambda *, ffmpeg_bin="ffmpeg": False
    )
    runtime.tick()  # below_seconds=10 (< 20 window) -> still HYBRID_GPU
    assert manager.scheduler.status.state is HybridGpuState.HYBRID_GPU
    runtime.tick()  # below_seconds=20 (>= window) -> COASTING
    assert manager.scheduler.status.state is HybridGpuState.COASTING
    # COASTING accrues its own window from zero; two more low ticks + GPU no
    # longer Ready -> scale-to-zero back to ELECTRIC_ONLY.
    runtime.tick()
    runtime.tick()
    assert manager.scheduler.status.state is HybridGpuState.ELECTRIC_ONLY
    assert manager.scheduler.current_encoder().is_gpu is False


def test_control_loop_publishes_metrics(monkeypatch) -> None:
    manager = _manager()

    class _FakeCw:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        def put_metric_data(self, **kwargs: object) -> None:
            self.calls.append(kwargs)

    fake = _FakeCw()
    runtime = TranscodeRuntime(
        manager=manager,
        metrics=PressureMetrics("HelloDJ/Transcode", fake),
        interval_seconds=5.0,
    )
    monkeypatch.setattr("hls_transcode.runtime.cpu_pressure", lambda: 0.5)
    monkeypatch.setattr(
        "hls_transcode.runtime.probe_gpu_ready", lambda *, ffmpeg_bin="ffmpeg": False
    )
    runtime.tick()
    assert fake.calls, "control loop must publish a pressure metric each tick"
    assert fake.calls[0]["Namespace"] == "HelloDJ/Transcode"


# --------------------------------------------------------------------------- #
# ffmpeg process manager: spawn + drain (R17)
# --------------------------------------------------------------------------- #


class _FakeProcess:
    """A fake asyncio subprocess that records terminate/kill and exits on wait."""

    def __init__(self, *, overruns: bool = False) -> None:
        self.returncode: int | None = None
        self.terminated = False
        self.killed = False
        self._overruns = overruns

    def terminate(self) -> None:
        self.terminated = True
        if not self._overruns:
            self.returncode = 0

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    async def wait(self) -> int:
        if self._overruns and self.returncode is None:
            # Never exits on its own -> wait_for times out -> kill().
            await asyncio.sleep(3600)
        return self.returncode or 0


def _plan(manager: JobManager):
    return manager.plan_transcode(7, "video", "sid", source_uri="loopback://m")


def test_process_manager_start_and_stop(monkeypatch) -> None:
    manager = _manager()
    plan = _plan(manager)
    fake = _FakeProcess()

    async def _fake_exec(*_args, **_kwargs):
        return fake

    monkeypatch.setattr(
        "hls_transcode.runtime.asyncio.create_subprocess_exec", _fake_exec
    )
    monkeypatch.setattr(
        "hls_transcode.runtime.os.makedirs", lambda *a, **k: None
    )

    async def _scenario() -> None:
        pm = FfmpegProcessManager(drain_timeout=1.0)
        await pm.start(plan)
        assert pm.running == 1
        assert pm.is_running(7, "sid")
        stopped = await pm.stop(7, "sid")
        assert stopped is True
        assert fake.terminated is True
        assert pm.running == 0

    asyncio.run(_scenario())


def test_process_manager_kills_overrunning_process(monkeypatch) -> None:
    manager = _manager()
    plan = _plan(manager)
    fake = _FakeProcess(overruns=True)

    async def _fake_exec(*_args, **_kwargs):
        return fake

    monkeypatch.setattr(
        "hls_transcode.runtime.asyncio.create_subprocess_exec", _fake_exec
    )
    monkeypatch.setattr(
        "hls_transcode.runtime.os.makedirs", lambda *a, **k: None
    )

    async def _scenario() -> None:
        pm = FfmpegProcessManager(drain_timeout=0.05)
        await pm.start(plan)
        await pm.stop(7, "sid")
        assert fake.terminated is True
        assert fake.killed is True  # overran the drain window -> SIGKILL (R17.3)

    asyncio.run(_scenario())


# --------------------------------------------------------------------------- #
# Segment uploader
# --------------------------------------------------------------------------- #


class _FakeS3:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def put_object(self, **kwargs: object) -> None:
        self.calls.append(kwargs)


def test_uploader_mirrors_artifacts_with_cache_control(tmp_path) -> None:
    manager = _manager()
    writer = HlsWriter(
        scratch_root=str(tmp_path), s3_bucket="b", s3_prefix="hls"
    )
    plan = JobManager(
        manager._config, manager.scheduler, writer  # noqa: SLF001 - test wiring
    ).plan_transcode(7, "video", "sid", source_uri="u")

    os.makedirs(plan.artifacts.output_dir, exist_ok=True)
    with open(f"{plan.artifacts.output_dir}/index.m3u8", "w") as fh:
        fh.write("#EXTM3U")
    with open(f"{plan.artifacts.output_dir}/seg_00001.m4s", "wb") as fh:
        fh.write(b"\x00\x01")

    fake = _FakeS3()
    uploader = SegmentUploader(S3Sink("b", fake))
    uploader._upload_dir_once(plan, {})  # noqa: SLF001 - test the single sweep

    by_key = {c["Key"]: c for c in fake.calls}
    playlist = next(k for k in by_key if k.endswith("index.m3u8"))
    segment = next(k for k in by_key if k.endswith("seg_00001.m4s"))
    assert by_key[playlist]["CacheControl"] == "no-cache"
    assert "immutable" in by_key[segment]["CacheControl"]


def test_uploader_survives_transient_error(tmp_path) -> None:
    manager = _manager()
    writer = HlsWriter(scratch_root=str(tmp_path), s3_bucket="b", s3_prefix="hls")
    plan = JobManager(
        manager._config, manager.scheduler, writer  # noqa: SLF001
    ).plan_transcode(7, "video", "sid", source_uri="u")

    os.makedirs(plan.artifacts.output_dir, exist_ok=True)
    with open(f"{plan.artifacts.output_dir}/index.m3u8", "w") as fh:
        fh.write("#EXTM3U")

    class _BoomS3:
        def put_object(self, **_kwargs: object) -> None:
            raise RuntimeError("transient")

    uploader = SegmentUploader(S3Sink("b", _BoomS3()))
    # Must not raise despite the S3 error.
    uploader._upload_dir_once(plan, {})  # noqa: SLF001

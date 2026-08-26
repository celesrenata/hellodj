"""Smoke tests for hls-transcode.

These exercise the pure, dependency-free surfaces (config, encoder selection +
HLS command building, hybrid-GPU-driven scheduler, HLS layout/key derivation,
S3 sink + CloudWatch metrics with fake injected clients, visualizer frame
contract, and the request handlers) without requiring aiohttp or boto3.

The central assertion is that the encoder selection *follows the hybrid GPU
controller state*: libx264 (CPU floor) whenever the GPU is not preferred, NVENC
only while the controller reports the GPU Ready (HYBRID_GPU) and the deployment
has a GPU node group.
"""

from __future__ import annotations

from hellodj_platform_logic.hybrid_gpu import initial_status, run
from hellodj_platform_logic.types import HybridGpuState, HybridGpuThresholds

from hls_transcode.config import TranscodeConfig
from hls_transcode.encoder import (
    EncoderPath,
    EncoderSelector,
    EncodeSpec,
    HlsSegmentType,
    build_hls_command,
)
from hls_transcode.hls_writer import HlsWriter
from hls_transcode.jobs import JobError, JobManager
from hls_transcode.metrics import PressureMetrics, build_metric_data
from hls_transcode.s3_sink import S3Sink, content_type_for
from hls_transcode.scheduler import TranscodeScheduler
from hls_transcode.server import build_handlers, create_job_manager
from hls_transcode.visualizer import (
    FrameSpec,
    SolidColorFrameSource,
    VisualizerRenderer,
)

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #


def test_config_defaults() -> None:
    cfg = TranscodeConfig.from_env({})
    assert cfg.port == 8080
    assert cfg.hls_s3_prefix == "hls"
    assert cfg.gpu_available is False
    assert cfg.gpu_thresholds.spin_up_threshold > cfg.gpu_thresholds.spin_down_threshold


def test_config_reads_env() -> None:
    cfg = TranscodeConfig.from_env(
        {
            "HELLODJ_HLS_S3_BUCKET": "hellodj-hls",
            "HELLODJ_CLOUDFRONT_DOMAIN": "cdn.hellodj.bot/",
            "HELLODJ_GPU_AVAILABLE": "true",
            "HELLODJ_GPU_SPIN_UP": "0.9",
            "HELLODJ_GPU_SPIN_DOWN": "0.2",
        }
    )
    assert cfg.hls_s3_bucket == "hellodj-hls"
    assert cfg.cloudfront_domain == "cdn.hellodj.bot"
    assert cfg.gpu_available is True
    assert cfg.gpu_thresholds.spin_up_threshold == 0.9
    assert cfg.gpu_thresholds.spin_down_threshold == 0.2


# --------------------------------------------------------------------------- #
# Encoder selection follows the controller state (core requirement)
# --------------------------------------------------------------------------- #


def _thresholds() -> HybridGpuThresholds:
    return HybridGpuThresholds(
        spin_up_threshold=0.8,
        spin_down_threshold=0.3,
        spin_up_window_seconds=10.0,
        spin_down_window_seconds=20.0,
    )


def test_selector_cpu_when_gpu_not_preferred() -> None:
    selector = EncoderSelector(gpu_available=True)
    assert selector.select(initial_status()) is EncoderPath.LIBX264


def test_selector_nvenc_only_when_gpu_ready_and_available() -> None:
    thresholds = _thresholds()
    # Drive the shared controller to HYBRID_GPU: sustained high demand + ready.
    status = run(
        [
            _sample(0.95, 10.0),
            _sample(0.95, 10.0),
        ],
        thresholds,
        gpu_ready_after=True,
    )
    assert status.state is HybridGpuState.HYBRID_GPU
    assert status.gpu_preferred is True

    # With a GPU node group, HYBRID_GPU -> NVENC.
    assert EncoderSelector(gpu_available=True).select(status) is EncoderPath.NVENC
    # Without a GPU node group, always libx264 even when preferred (R3.9).
    assert EncoderSelector(gpu_available=False).select(status) is EncoderPath.LIBX264


def test_selector_cpu_during_spin_up_window() -> None:
    thresholds = _thresholds()
    # ENGINE_STARTING (GPU requested but not Ready): CPU keeps serving.
    status = run([_sample(0.95, 10.0)], thresholds, gpu_ready_after=False)
    assert status.state is HybridGpuState.ENGINE_STARTING
    assert status.gpu_preferred is False
    assert EncoderSelector(gpu_available=True).select(status) is EncoderPath.LIBX264


def _sample(demand: float, duration: float):
    from hellodj_platform_logic.hybrid_gpu import DemandSample

    return DemandSample(demand=demand, duration_seconds=duration)


# --------------------------------------------------------------------------- #
# HLS command building
# --------------------------------------------------------------------------- #


def test_build_command_libx264_fmp4() -> None:
    spec = EncodeSpec(
        input_uri="loopback://media",
        output_dir="/tmp/out",
        segment_type=HlsSegmentType.FMP4,
    )
    cmd = build_hls_command(EncoderPath.LIBX264, spec)
    assert cmd[0] == "ffmpeg"
    assert "libx264" in cmd
    assert "-re" in cmd  # realtime pacing for live streaming
    assert "fmp4" in cmd
    assert cmd[-1].endswith("/index.m3u8")


def test_build_command_nvenc_mpegts() -> None:
    spec = EncodeSpec(
        input_uri="loopback://media",
        output_dir="/tmp/out",
        segment_type=HlsSegmentType.MPEGTS,
    )
    cmd = build_hls_command(EncoderPath.NVENC, spec)
    assert "h264_nvenc" in cmd
    assert "mpegts" in cmd


# --------------------------------------------------------------------------- #
# Scheduler drives selection + pressure snapshot
# --------------------------------------------------------------------------- #


def test_scheduler_selects_and_tracks_jobs() -> None:
    sched = TranscodeScheduler(_thresholds(), gpu_available=True)
    assert sched.current_encoder() is EncoderPath.LIBX264
    assert sched.gpu_requested() is False

    sched.observe(0.95, 10.0, gpu_ready=False)
    assert sched.status.state is HybridGpuState.ENGINE_STARTING
    assert sched.gpu_requested() is True
    assert sched.current_encoder() is EncoderPath.LIBX264  # CPU floor covers boot

    sched.observe(0.95, 10.0, gpu_ready=True)
    assert sched.status.state is HybridGpuState.HYBRID_GPU
    assert sched.current_encoder() is EncoderPath.NVENC

    sched.job_started()
    sched.job_started()
    snap = sched.pressure_snapshot()
    assert snap.active_jobs == 2
    assert snap.gpu_active is True
    sched.job_finished()
    assert sched.pressure_snapshot().active_jobs == 1


# --------------------------------------------------------------------------- #
# HLS writer layout + S3 key derivation
# --------------------------------------------------------------------------- #


def test_hls_writer_layout() -> None:
    writer = HlsWriter(
        scratch_root="/tmp/hellodj_hls",
        s3_bucket="hellodj-hls",
        s3_prefix="hls",
        cloudfront_domain="cdn.hellodj.bot",
    )
    art = writer.plan(123, "video", "abc")
    assert art.output_dir == "/tmp/hellodj_hls/guild=123/video/abc"
    assert art.s3_key_prefix == "hls/guild=123/video/abc"
    assert art.playlist_key == "hls/guild=123/video/abc/index.m3u8"
    assert art.playlist_url == (
        "https://cdn.hellodj.bot/hls/guild=123/video/abc/index.m3u8"
    )
    assert art.s3_key_for("seg_00001.m4s") == "hls/guild=123/video/abc/seg_00001.m4s"


def test_hls_writer_no_cdn_empty_url() -> None:
    writer = HlsWriter(scratch_root="/s", s3_bucket="b", s3_prefix="hls")
    assert writer.plan(1, "visualizer", "x").playlist_url == ""


# --------------------------------------------------------------------------- #
# S3 sink (fake injected client) + content types
# --------------------------------------------------------------------------- #


class _FakeS3:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def put_object(self, **kwargs: object) -> None:
        self.calls.append(kwargs)


def test_content_type_for() -> None:
    assert content_type_for("index.m3u8") == "application/vnd.apple.mpegurl"
    assert content_type_for("seg_0.m4s") == "video/iso.segment"
    assert content_type_for("seg_0.ts") == "video/mp2t"
    assert content_type_for("init.mp4") == "video/mp4"


def test_s3_sink_put_bytes_sets_content_type() -> None:
    fake = _FakeS3()
    sink = S3Sink("hellodj-hls", fake)
    sink.put_bytes("hls/x/index.m3u8", b"#EXTM3U", cache_control="no-cache")
    assert fake.calls[0]["Bucket"] == "hellodj-hls"
    assert fake.calls[0]["ContentType"] == "application/vnd.apple.mpegurl"
    assert fake.calls[0]["CacheControl"] == "no-cache"


# --------------------------------------------------------------------------- #
# CloudWatch metrics (fake injected client)
# --------------------------------------------------------------------------- #


class _FakeCloudWatch:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def put_metric_data(self, **kwargs: object) -> None:
        self.calls.append(kwargs)


def test_metrics_publish_pressure() -> None:
    sched = TranscodeScheduler(_thresholds(), gpu_available=True)
    sched.observe(0.5, 5.0)
    fake = _FakeCloudWatch()
    metrics = PressureMetrics(
        "HelloDJ/Transcode", fake, dimensions=[{"Name": "Stage", "Value": "beta"}]
    )
    metrics.publish(sched.pressure_snapshot())
    assert fake.calls[0]["Namespace"] == "HelloDJ/Transcode"
    names = {m["MetricName"] for m in fake.calls[0]["MetricData"]}
    assert names == {"CpuTranscodePressure", "GpuActive", "ActiveTranscodeJobs"}


def test_build_metric_data_shape() -> None:
    sched = TranscodeScheduler(_thresholds(), gpu_available=True)
    data = build_metric_data(sched.pressure_snapshot())
    assert len(data) == 3
    assert all("Value" in m for m in data)


# --------------------------------------------------------------------------- #
# Visualizer frame contract
# --------------------------------------------------------------------------- #


def test_visualizer_frame_size_and_input_args() -> None:
    source = SolidColorFrameSource(FrameSpec(width=4, height=2, pixel_format="rgba"))
    renderer = VisualizerRenderer(source)
    frame = renderer.render_frame({"level": 1.0})
    assert len(frame) == 4 * 2 * 4
    assert frame[:4] == bytes((255, 255, 255, 255))
    args = renderer.ffmpeg_input_args()
    assert "rawvideo" in args
    assert "4x2" in args


# --------------------------------------------------------------------------- #
# Job manager planning + handlers
# --------------------------------------------------------------------------- #


def test_job_manager_plans_video_and_visualizer() -> None:
    cfg = TranscodeConfig.from_env(
        {"HELLODJ_HLS_S3_BUCKET": "b", "HELLODJ_CLOUDFRONT_DOMAIN": "cdn.x"}
    )
    manager = create_job_manager(cfg)
    plan = manager.plan_transcode(
        7, "video", "sid", source_uri="loopback://media"
    )
    assert plan.encoder is EncoderPath.LIBX264
    assert plan.artifacts.playlist_key.endswith("index.m3u8")
    assert plan.command[0] == "ffmpeg"
    assert manager.active_plan(7, "sid") is plan

    # Visualizer with no source uses the frame pipe input "-".
    viz = manager.plan_transcode(7, "visualizer", "v1", source_uri=None)
    assert "-" in viz.command
    assert manager.scheduler.active_jobs == 2

    assert manager.stop_transcode(7, "sid") is True
    assert manager.scheduler.active_jobs == 1


def test_job_manager_video_requires_source() -> None:
    cfg = TranscodeConfig.from_env({"HELLODJ_HLS_S3_BUCKET": "b"})
    manager = JobManager(
        cfg,
        TranscodeScheduler(cfg.gpu_thresholds, gpu_available=False),
        HlsWriter(scratch_root="/s", s3_bucket="b", s3_prefix="hls"),
    )
    try:
        manager.plan_transcode(1, "video", "s", source_uri=None)
    except JobError:
        pass
    else:
        raise AssertionError("expected JobError for video without source")


def test_handlers_start_and_stop() -> None:
    cfg = TranscodeConfig.from_env(
        {"HELLODJ_HLS_S3_BUCKET": "b", "HELLODJ_CLOUDFRONT_DOMAIN": "cdn.x"}
    )
    handlers = build_handlers(cfg)
    status, body = handlers.start_transcode(
        {"guildId": "55", "kind": "video", "streamId": "sid", "sourceUri": "u"}
    )
    assert status == 202
    assert body["encoder"] == "libx264"
    assert body["playlistUrl"].startswith("https://cdn.x/")

    status, body = handlers.stop_transcode({"guildId": "55", "streamId": "sid"})
    assert status == 200
    assert body["stopped"] is True


def test_handlers_start_missing_fields() -> None:
    handlers = build_handlers(TranscodeConfig.from_env({}))
    status, body = handlers.start_transcode({"kind": "video"})
    assert status == 400
    assert "error" in body


def test_handlers_pressure() -> None:
    handlers = build_handlers(TranscodeConfig.from_env({}))
    status, body = handlers.pressure()
    assert status == 200
    assert body["state"] == "electric_only"
    assert body["gpuActive"] is False

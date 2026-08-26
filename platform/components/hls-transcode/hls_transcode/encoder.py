"""FFmpeg encoder path selection and HLS command construction.

This module owns the *encode path* half of the hybrid gas/electric model
(Decision D3). It maps the shared hybrid-GPU controller state onto a concrete
ffmpeg encoder:

* :attr:`EncoderPath.LIBX264` — software H.264 on the Graviton CPU. This is the
  always-available floor that serves every interactive request immediately, and
  covers the entire GPU spin-up window so the Interactive_Latency_Budget holds
  (Requirements 3.1, 3.9, 3.12, 3.13).
* :attr:`EncoderPath.NVENC` — hardware ``h264_nvenc`` on a warm, time-sliced
  G5g node. Preferred only while the hybrid controller reports the GPU
  ``Ready`` (state ``HYBRID_GPU`` / ``gpu_preferred`` — Requirement 3.11).

The selection is a pure function of the controller status, so it is exercised
directly by unit tests with no ffmpeg, no GPU, and no AWS. Command construction
produces the argv both for fMP4 (CMAF) and MPEG-TS HLS segmenting; nothing here
spawns a process — that is the scheduler/runtime's job — keeping this module
importable and testable in isolation.

Requirements: 3.1, 3.9, 3.11, 6.2
"""

from __future__ import annotations

import enum
from dataclasses import dataclass

from hellodj_platform_logic.hybrid_gpu import ControllerStatus

__all__ = [
    "EncoderPath",
    "HlsSegmentType",
    "EncodeSpec",
    "EncoderSelector",
    "build_hls_command",
]


class EncoderPath(enum.Enum):
    """The concrete ffmpeg encoder a job runs on.

    ``LIBX264`` is the CPU/Graviton software path (always available floor);
    ``NVENC`` is the GPU ``h264_nvenc`` path, used only while a warm GPU is
    ``Ready`` (Decision D3).
    """

    LIBX264 = "libx264"
    NVENC = "h264_nvenc"

    @property
    def is_gpu(self) -> bool:
        """Whether this path uses the GPU (NVENC)."""
        return self is EncoderPath.NVENC


class HlsSegmentType(enum.Enum):
    """HLS segment container format.

    ``FMP4`` produces CMAF (fragmented MP4) segments with an init segment;
    ``MPEGTS`` produces classic ``.ts`` segments. Both are valid HLS outputs
    consumed by hls.js in the Activity frontend (R6.2).
    """

    FMP4 = "fmp4"
    MPEGTS = "mpegts"


@dataclass(frozen=True)
class EncodeSpec:
    """Parameters for one HLS transcode invocation.

    Attributes:
        input_uri: The media source ffmpeg reads. For co-located streams this is
            a loopback/intra-node endpoint or local FIFO (Decision D2); it may be
            ``"-"`` for a piped visualizer frame source.
        output_dir: Local scratch directory (tmpfs) where segments/playlist are
            written before being uploaded to S3.
        segment_type: fMP4 (CMAF) or MPEG-TS segmenting.
        segment_duration_s: Target segment duration in seconds.
        width: Output pixel width.
        height: Output pixel height.
        framerate: Output frame rate.
        video_bitrate: Target video bitrate (e.g. ``"6000k"``).
        gop_seconds: Keyframe interval in seconds (aligns to segment boundaries).
        realtime: Whether to pace input at native rate (``-re``) for live
            streaming.
        playlist_name: Name of the HLS media playlist within ``output_dir``.
    """

    input_uri: str
    output_dir: str
    segment_type: HlsSegmentType = HlsSegmentType.FMP4
    segment_duration_s: float = 2.0
    width: int = 1280
    height: int = 720
    framerate: int = 30
    video_bitrate: str = "6000k"
    gop_seconds: float = 1.0
    realtime: bool = True
    playlist_name: str = "index.m3u8"


class EncoderSelector:
    """Selects the ffmpeg encoder path from the hybrid-GPU controller status.

    The selector is the bridge between the pure gas/electric state machine
    (:mod:`hellodj_platform_logic.hybrid_gpu`) and the concrete encoder. It is a
    thin, pure mapping: prefer NVENC only when the controller says the GPU is
    ``Ready`` (``gpu_preferred``) *and* the deployment actually has a GPU node
    group; otherwise fall back to libx264 (the CPU floor).
    """

    def __init__(self, *, gpu_available: bool) -> None:
        """Initialise with whether a GPU node group exists at all.

        Args:
            gpu_available: ``False`` for a software-transcode-only deployment
                (R3.9), in which case NVENC is never selected regardless of the
                controller status.
        """
        self._gpu_available = gpu_available

    def select(self, status: ControllerStatus) -> EncoderPath:
        """Return the encoder path for the given controller status.

        NVENC is chosen only when both the GPU is available in the deployment
        and the controller prefers the GPU (state ``HYBRID_GPU``); every other
        status maps to the libx264 CPU floor (R3.1, R3.9, R3.11).

        Args:
            status: The current hybrid-GPU controller status.

        Returns:
            :attr:`EncoderPath.NVENC` when the warm GPU is preferred, else
            :attr:`EncoderPath.LIBX264`.
        """
        if self._gpu_available and status.gpu_preferred:
            return EncoderPath.NVENC
        return EncoderPath.LIBX264


def _video_codec_args(path: EncoderPath, spec: EncodeSpec) -> list[str]:
    """Build the codec-specific ffmpeg args for the chosen path.

    libx264 uses ``-preset veryfast`` + CRF-style rate control tuned for live
    latency; NVENC uses ``h264_nvenc`` with low-latency high-quality settings.
    """
    if path is EncoderPath.NVENC:
        return [
            "-c:v",
            "h264_nvenc",
            "-preset",
            "p4",
            "-tune",
            "ll",
            "-rc",
            "vbr",
            "-b:v",
            spec.video_bitrate,
            "-maxrate",
            spec.video_bitrate,
        ]
    return [
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-tune",
        "zerolatency",
        "-b:v",
        spec.video_bitrate,
        "-maxrate",
        spec.video_bitrate,
    ]


def _hls_muxer_args(spec: EncodeSpec) -> list[str]:
    """Build the HLS muxer args for the requested segment type."""
    gop_frames = max(1, int(round(spec.gop_seconds * spec.framerate)))
    common = [
        "-g",
        str(gop_frames),
        "-keyint_min",
        str(gop_frames),
        "-sc_threshold",
        "0",
        "-f",
        "hls",
        "-hls_time",
        f"{spec.segment_duration_s:g}",
        "-hls_flags",
        "delete_segments+independent_segments",
        "-hls_list_size",
        "6",
    ]
    if spec.segment_type is HlsSegmentType.FMP4:
        common += [
            "-hls_segment_type",
            "fmp4",
            "-hls_fmp4_init_filename",
            "init.mp4",
            "-hls_segment_filename",
            f"{spec.output_dir.rstrip('/')}/seg_%05d.m4s",
        ]
    else:
        common += [
            "-hls_segment_type",
            "mpegts",
            "-hls_segment_filename",
            f"{spec.output_dir.rstrip('/')}/seg_%05d.ts",
        ]
    common.append(f"{spec.output_dir.rstrip('/')}/{spec.playlist_name}")
    return common


def build_hls_command(path: EncoderPath, spec: EncodeSpec) -> list[str]:
    """Build the full ffmpeg argv for an HLS transcode.

    The command reads ``spec.input_uri``, encodes with the selected path, and
    writes an HLS playlist plus segments (fMP4 or MPEG-TS) into
    ``spec.output_dir`` for upload to S3. Nothing is executed here; the returned
    argv is handed to the runtime process manager.

    Args:
        path: The encoder path selected by :class:`EncoderSelector`.
        spec: The encode parameters.

    Returns:
        The ffmpeg argv as a list of strings (``ffmpeg`` first).
    """
    argv: list[str] = ["ffmpeg", "-hide_banner", "-loglevel", "warning"]
    if spec.realtime:
        argv.append("-re")
    argv += ["-i", spec.input_uri]
    argv += _video_codec_args(path, spec)
    argv += [
        "-pix_fmt",
        "yuv420p",
        "-r",
        str(spec.framerate),
        "-s",
        f"{spec.width}x{spec.height}",
    ]
    argv += _hls_muxer_args(spec)
    return argv

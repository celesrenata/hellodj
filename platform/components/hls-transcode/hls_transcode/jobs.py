"""Transcode job management: request handling over the pure surfaces.

This module is the composition point between the pure/injectable pieces — the
hybrid-GPU-driven :class:`~hls_transcode.scheduler.TranscodeScheduler`, the
:class:`~hls_transcode.encoder.EncoderSelector`/command builder, the
:class:`~hls_transcode.hls_writer.HlsWriter` layout model, and the pressure
:class:`~hls_transcode.metrics.PressureMetrics` publisher — and the HTTP layer
in :mod:`hls_transcode.server`.

It turns a start/stop request into a *plan*: which encoder path to run, the
ffmpeg argv, and the S3/CloudFront output location, and it tracks active jobs so
pressure can be reported to CloudWatch (R16.4). Spawning the ffmpeg process and
uploading segments is the runtime's concern; this manager stays free of ffmpeg
and AWS calls so it is fully unit-testable.

Requirements: 3.1, 3.9, 3.11, 6.2, 16.4
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import TranscodeConfig
from .encoder import EncoderPath, EncodeSpec, HlsSegmentType, build_hls_command
from .hls_writer import HlsArtifacts, HlsWriter
from .scheduler import TranscodeScheduler

__all__ = ["TranscodePlan", "JobManager", "JobError"]

# tmpfs scratch root on the transcode node (RAM-backed; see design AMI notes).
_SCRATCH_ROOT = "/tmp/hellodj_hls"  # noqa: S108 - RAM-backed tmpfs by design


class JobError(ValueError):
    """Raised when a transcode request is malformed or references no source."""


@dataclass(frozen=True)
class TranscodePlan:
    """The resolved plan for a transcode job (no process spawned yet).

    Attributes:
        guild_id: The guild the stream belongs to.
        kind: ``"video"`` or ``"visualizer"``.
        stream_id: Per-session identifier.
        encoder: The selected encoder path (libx264 / NVENC).
        artifacts: The resolved local + S3 + CloudFront HLS locations.
        command: The ffmpeg argv to run.
    """

    guild_id: int
    kind: str
    stream_id: str
    encoder: EncoderPath
    artifacts: HlsArtifacts
    command: list[str]


class JobManager:
    """Plans and tracks HLS transcode jobs for the HTTP layer.

    Args:
        config: The component runtime configuration.
        scheduler: The hybrid-GPU-driven scheduler (selects the encoder path).
        writer: The HLS layout/key derivation model.
    """

    def __init__(
        self,
        config: TranscodeConfig,
        scheduler: TranscodeScheduler,
        writer: HlsWriter,
    ) -> None:
        """Initialise with config, scheduler, and HLS writer."""
        self._config = config
        self._scheduler = scheduler
        self._writer = writer
        # Active plans keyed by (guild_id, stream_id) for stop/idempotency.
        self._active: dict[tuple[int, str], TranscodePlan] = {}

    @property
    def scheduler(self) -> TranscodeScheduler:
        """The underlying transcode scheduler."""
        return self._scheduler

    def _build_spec(self, artifacts: HlsArtifacts, source_uri: str) -> EncodeSpec:
        """Build the :class:`EncodeSpec` for a stream's artifacts + source."""
        return EncodeSpec(
            input_uri=source_uri,
            output_dir=artifacts.output_dir,
            segment_type=HlsSegmentType.FMP4,
            segment_duration_s=self._config.segment_duration_s,
            playlist_name=artifacts.playlist_name,
        )

    def plan_transcode(
        self,
        guild_id: int,
        kind: str,
        stream_id: str,
        *,
        source_uri: str | None,
    ) -> TranscodePlan:
        """Resolve the encoder path, output location, and ffmpeg command.

        The encoder path comes from the hybrid scheduler (NVENC only while the
        warm GPU is ``Ready``; libx264 floor otherwise — R3.1, R3.9, R3.11). A
        visualizer stream reads frames from the visualizer pipe (input ``"-"``)
        while a video stream reads its co-located loopback/intra-node source
        (Decision D2).

        Args:
            guild_id: The guild the stream belongs to.
            kind: ``"video"`` or ``"visualizer"``.
            stream_id: Per-session identifier.
            source_uri: Media source for a video stream; may be ``None`` for a
                visualizer stream driven by the frame pipe.

        Returns:
            A :class:`TranscodePlan` ready for the runtime to execute.

        Raises:
            JobError: If a non-visualizer stream has no source URI.
        """
        normalized_kind = (kind or "").strip().lower() or "video"
        if normalized_kind != "visualizer" and not source_uri:
            raise JobError(
                f"video stream for guild {guild_id} requires a source_uri"
            )
        artifacts = self._writer.plan(guild_id, normalized_kind, stream_id)
        # Visualizer frames arrive on the encoder's stdin pipe (input "-").
        effective_source = source_uri if source_uri else "-"
        encoder = self._scheduler.current_encoder()
        command = build_hls_command(
            encoder, self._build_spec(artifacts, effective_source)
        )
        plan = TranscodePlan(
            guild_id=guild_id,
            kind=normalized_kind,
            stream_id=stream_id,
            encoder=encoder,
            artifacts=artifacts,
            command=command,
        )
        key = (guild_id, stream_id)
        if key not in self._active:
            self._scheduler.job_started()
        self._active[key] = plan
        return plan

    def stop_transcode(self, guild_id: int, stream_id: str) -> bool:
        """Stop tracking a job; returns ``True`` if it was active.

        Args:
            guild_id: The guild the stream belongs to.
            stream_id: Per-session identifier.

        Returns:
            ``True`` when an active job was found and removed, else ``False``.
        """
        key = (guild_id, stream_id)
        if key in self._active:
            del self._active[key]
            self._scheduler.job_finished()
            return True
        return False

    def active_plan(self, guild_id: int, stream_id: str) -> TranscodePlan | None:
        """Return the active plan for a stream, or ``None`` if not active."""
        return self._active.get((guild_id, stream_id))

    @staticmethod
    def scratch_root() -> str:
        """Return the tmpfs scratch root used for HLS output."""
        return _SCRATCH_ROOT

"""HelloDJ ``hls-transcode`` component.

This component performs HLS transcode and co-located visualizer rendering for
the Discord Activity, preserving the existing video-streaming and audio
visualizer features through the AWS re-platform (Requirement 6.2). It runs on
the taint/label-isolated transcode node group, co-located with the media
producers (lavalink / activity-backend) so the producer -> transcoder hop is
loopback/intra-node and free (Decision D2).

The encode path follows the hybrid "gas/electric" model (Decision D3): the CPU
path (libx264 on Graviton) is the always-available floor that serves every
interactive request immediately (Requirements 3.1, 3.9), and the GPU path
(``h264_nvenc`` on a warm time-sliced G5g node) is preferred only while the
shared :mod:`hellodj_platform_logic.hybrid_gpu` controller reports the GPU
``Ready`` (Requirement 3.11). The component publishes CPU/GPU pressure metrics
to CloudWatch so the Autoscaler can add/remove transcode capacity
(Requirement 16.4).

It is an independently deployable, independently versioned component
(Requirement 15.1): its own Nix-built image, its own semantic version, and its
own CI/CD path.

Public surface:
    * :class:`~hls_transcode.config.TranscodeConfig` — runtime settings.
    * :class:`~hls_transcode.encoder.EncoderSelector` — libx264/NVENC selection.
    * :class:`~hls_transcode.scheduler.TranscodeScheduler` — hybrid-GPU-driven
      job scheduler.
    * :class:`~hls_transcode.hls_writer.HlsWriter` — HLS artifact model.
    * :class:`~hls_transcode.s3_sink.S3Sink` — S3 (CloudFront origin) sink.
    * :class:`~hls_transcode.metrics.PressureMetrics` — CloudWatch publisher.
    * :class:`~hls_transcode.visualizer.VisualizerRenderer` — visualizer hook.
"""

from __future__ import annotations

__all__ = ["__version__"]

#: Independent semantic version for the hls-transcode component (R15.1).
__version__ = "0.1.0"

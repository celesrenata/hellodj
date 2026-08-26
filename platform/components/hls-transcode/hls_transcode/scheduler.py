"""Transcode scheduler driven by the hybrid gas/electric GPU controller.

The scheduler is the runtime brain of the hybrid model (Decision D3). It holds
the current :class:`~hellodj_platform_logic.hybrid_gpu.ControllerStatus`, accepts
transcode-demand samples, advances the shared controller state machine, and
exposes the resulting encoder decision (libx264 vs NVENC) to the runtime. It
also snapshots CPU/GPU pressure so the metrics publisher can ship it to
CloudWatch for the Autoscaler (R16.4).

All state transitions delegate to :func:`hellodj_platform_logic.hybrid_gpu.advance`
so the runtime and the property tests share a single source of truth. The
scheduler makes no AWS or ffmpeg calls; it is pure orchestration over the shared
controller plus the pure :class:`~hls_transcode.encoder.EncoderSelector`.

Requirements: 3.1, 3.2, 3.3, 3.9, 3.11, 16.4
"""

from __future__ import annotations

from dataclasses import dataclass

from hellodj_platform_logic.hybrid_gpu import (
    ControllerStatus,
    DemandSample,
    advance,
    initial_status,
)
from hellodj_platform_logic.types import HybridGpuState, HybridGpuThresholds

from .encoder import EncoderPath, EncoderSelector

__all__ = ["PressureSnapshot", "TranscodeScheduler"]


@dataclass(frozen=True)
class PressureSnapshot:
    """A point-in-time view of transcode pressure for metrics/decisions.

    Attributes:
        cpu_pressure: Current CPU-transcode demand (fraction of CPU capacity).
        gpu_active: Whether jobs are currently preferring the GPU
            (``gpu_preferred`` in the controller status).
        state: The hybrid controller state the pressure was observed in.
        active_jobs: Number of transcode jobs currently registered.
    """

    cpu_pressure: float
    gpu_active: bool
    state: HybridGpuState
    active_jobs: int


class TranscodeScheduler:
    """Drives libx264 vs NVENC selection from live transcode demand.

    The scheduler owns the mutable controller status (the rest of the hybrid
    logic is pure). Feed it demand via :meth:`observe`, and read the encoder
    decision via :meth:`current_encoder`. On GPU ``Ready`` the scheduler prefers
    NVENC for new/rebalanced jobs; otherwise it stays on the CPU floor, which
    always serves (R3.1, R3.9, R3.11).
    """

    def __init__(
        self,
        thresholds: HybridGpuThresholds,
        *,
        gpu_available: bool,
        status: ControllerStatus | None = None,
    ) -> None:
        """Initialise the scheduler.

        Args:
            thresholds: Hybrid controller thresholds/windows.
            gpu_available: Whether a GPU node group exists in the deployment;
                when ``False`` the scheduler never selects NVENC (R3.9).
            status: Optional starting controller status (defaults to
                :func:`hellodj_platform_logic.hybrid_gpu.initial_status`).
        """
        self._thresholds = thresholds
        self._status = status if status is not None else initial_status()
        self._selector = EncoderSelector(gpu_available=gpu_available)
        self._active_jobs = 0
        self._last_pressure = 0.0

    @property
    def status(self) -> ControllerStatus:
        """The current hybrid-GPU controller status."""
        return self._status

    @property
    def active_jobs(self) -> int:
        """The number of currently registered transcode jobs."""
        return self._active_jobs

    def job_started(self) -> None:
        """Record that a transcode job started (increments active-job count)."""
        self._active_jobs += 1

    def job_finished(self) -> None:
        """Record that a transcode job finished (never drops below zero)."""
        self._active_jobs = max(0, self._active_jobs - 1)

    def observe(
        self,
        demand: float,
        duration_seconds: float,
        *,
        gpu_ready: bool = False,
    ) -> ControllerStatus:
        """Advance the controller with one demand observation.

        Args:
            demand: Measured transcode pressure over the sample window.
            duration_seconds: How long that demand was sustained (>= 0).
            gpu_ready: Whether the GPU node reports ``Ready`` with NVENC
                capacity right now (drives ``ENGINE_STARTING`` -> ``HYBRID_GPU``
                and gates scale-to-zero).

        Returns:
            The updated :class:`ControllerStatus`.
        """
        self._last_pressure = demand
        self._status = advance(
            self._status,
            DemandSample(demand=demand, duration_seconds=duration_seconds),
            self._thresholds,
            gpu_ready=gpu_ready,
        )
        return self._status

    def current_encoder(self) -> EncoderPath:
        """Return the encoder path for a job scheduled right now.

        Prefers NVENC only while the GPU is ``Ready`` (``HYBRID_GPU``); the CPU
        floor otherwise (R3.11).
        """
        return self._selector.select(self._status)

    def gpu_requested(self) -> bool:
        """Whether a GPU node has been requested (spin-up in progress or up).

        ``True`` in every state except ``ELECTRIC_ONLY``. Used by the runtime to
        decide whether to signal Karpenter/the node group to provision a GPU.
        """
        return self._status.state is not HybridGpuState.ELECTRIC_ONLY

    def pressure_snapshot(self) -> PressureSnapshot:
        """Return a :class:`PressureSnapshot` for metrics publication (R16.4)."""
        return PressureSnapshot(
            cpu_pressure=self._last_pressure,
            gpu_active=self._status.gpu_preferred,
            state=self._status.state,
            active_jobs=self._active_jobs,
        )

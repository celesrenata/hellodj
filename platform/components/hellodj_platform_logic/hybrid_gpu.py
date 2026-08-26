"""Hybrid "gas/electric" GPU spin-up / scale-to-zero transcode controller.

This module implements the pure state machine behind the default GPU-present
transcode configuration (Decision D3, the hybrid "gas/electric" model). It is
free of any AWS, Kubernetes, or Karpenter dependency so that both the runtime
transcode scheduler (task 16.1) and the property tests can import a single
source of truth, and it makes no live calls so the correctness properties can
exercise it directly.

The metaphor: the CPU path (libx264 on Graviton) is the *electric motor* and
always drives; the GPU node (time-sliced ``g5g`` Spot) is the *gas engine* that
spins up only under sustained load and shuts off when the platform is coasting.

States (:class:`~hellodj_platform_logic.types.HybridGpuState`):

* ``ELECTRIC_ONLY`` — CPU transcode alone; no GPU node exists.
* ``ENGINE_STARTING`` — demand stayed above ``spin_up_threshold`` for the
  sustained spin-up window, so a GPU node has been requested. The GPU is not
  yet ``Ready``; the CPU path keeps serving every request (covers the boot
  window, so the Interactive_Latency_Budget holds — R3.12, R3.13).
* ``HYBRID_GPU`` — the GPU node is ``Ready`` and NVENC advertises capacity, so
  new/rebalanced jobs prefer the GPU; the CPU path idles but stays available.
* ``COASTING`` — demand stayed below ``spin_down_threshold`` for the sustained
  spin-down window; jobs are draining back to the CPU and the GPU node is on
  its way to scaling to zero. Demand rising again returns to ``HYBRID_GPU``
  before scale-to-zero completes.

Invariants enforced (Property 15):

* **CPU always serves.** :attr:`ControllerStatus.cpu_serving` is ``True`` in
  every state, so no interactive request is ever left unserved — including the
  entire GPU spin-up window.
* **GPU requested only after sustained spin-up.** The move out of
  ``ELECTRIC_ONLY`` happens only once demand has been strictly above
  ``spin_up_threshold`` continuously for ``spin_up_window_seconds``.
* **GPU preferred only while Ready.** :attr:`ControllerStatus.gpu_preferred`
  is ``True`` only in ``HYBRID_GPU`` (the GPU node is ``Ready``).
* **Scale-to-zero only after sustained spin-down.** The GPU node scales to zero
  (return to ``ELECTRIC_ONLY``) only once demand has been strictly below
  ``spin_down_threshold`` continuously for ``spin_down_window_seconds``.
* **Hysteresis.** ``spin_up_threshold`` is strictly greater than
  ``spin_down_threshold`` (validated), so the node does not flap.

The controller is advanced by demand *samples*, each carrying a demand level
and the duration that level was observed. Advancing is a pure function of the
current state plus the sample (and a GPU-node-Ready signal), so a full run is a
left fold of :func:`advance` over a sample sequence.

Design references:
    * Decision D3 and "The hybrid gas/electric transcode model" state diagram
    * Correctness Property 15 (hybrid GPU spin-up / scale-to-zero state machine)

Requirements: 3.1, 3.2, 3.3, 3.9, 3.10, 3.11, 3.12, 3.13
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace

from hellodj_platform_logic.types import HybridGpuState, HybridGpuThresholds

__all__ = [
    "DemandSample",
    "ControllerStatus",
    "HybridGpuError",
    "initial_status",
    "advance",
    "run",
]


class HybridGpuError(ValueError):
    """Raised when the controller is configured or driven with bad inputs.

    This signals a programming error (for example thresholds that violate the
    ``spin_up_threshold > spin_down_threshold`` hysteresis requirement, or a
    negative sample duration), not a normal transcode-demand condition.
    """


@dataclass(frozen=True)
class DemandSample:
    """One observation of transcode demand held for a span of time.

    ``demand`` is the measured transcode/GPU-job pressure over the sample; it is
    compared against the controller thresholds. ``duration_seconds`` is how long
    that demand level was sustained and must be non-negative. A run is a
    sequence of these samples advanced in order.
    """

    demand: float
    duration_seconds: float


@dataclass(frozen=True)
class ControllerStatus:
    """Immutable snapshot of the hybrid controller after processing samples.

    Attributes:
        state: The current :class:`HybridGpuState`.
        cpu_serving: Whether the CPU (electric) path is serving. Always ``True``
            in every state (Property 15 / R3.12): the CPU covers every request,
            including the GPU spin-up window.
        gpu_preferred: Whether new/rebalanced jobs prefer the GPU. ``True`` only
            in :attr:`HybridGpuState.HYBRID_GPU` (the GPU node is ``Ready``).
        above_seconds: How long, up to now, demand has been continuously
            strictly above ``spin_up_threshold`` (the spin-up accumulator).
            Reset to ``0`` whenever a sample is not above the threshold.
        below_seconds: How long, up to now, demand has been continuously
            strictly below ``spin_down_threshold`` (the spin-down accumulator).
            Reset to ``0`` whenever a sample is not below the threshold.
    """

    state: HybridGpuState
    cpu_serving: bool
    gpu_preferred: bool
    above_seconds: float
    below_seconds: float


def _validate_thresholds(thresholds: HybridGpuThresholds) -> None:
    """Validate the hysteresis and window configuration.

    Args:
        thresholds: The spin-up/spin-down thresholds and sustained windows.

    Raises:
        HybridGpuError: If ``spin_down_threshold`` is not strictly less than
            ``spin_up_threshold`` (no hysteresis gap) or either sustained window
            is negative.
    """
    if not thresholds.spin_down_threshold < thresholds.spin_up_threshold:
        raise HybridGpuError(
            "spin_up_threshold must be strictly greater than "
            "spin_down_threshold (hysteresis); got "
            f"spin_up_threshold={thresholds.spin_up_threshold!r}, "
            f"spin_down_threshold={thresholds.spin_down_threshold!r}"
        )
    if thresholds.spin_up_window_seconds < 0:
        raise HybridGpuError(
            "spin_up_window_seconds must be non-negative; got "
            f"{thresholds.spin_up_window_seconds!r}"
        )
    if thresholds.spin_down_window_seconds < 0:
        raise HybridGpuError(
            "spin_down_window_seconds must be non-negative; got "
            f"{thresholds.spin_down_window_seconds!r}"
        )


def initial_status() -> ControllerStatus:
    """Return the starting status: ``ELECTRIC_ONLY`` with the CPU serving.

    A session starts on audio + CPU transcode alone with no GPU node, so the
    CPU path is serving, the GPU is not preferred, and both sustained-duration
    accumulators are zero.

    Returns:
        The initial :class:`ControllerStatus` (Property 15 start state).
    """
    return ControllerStatus(
        state=HybridGpuState.ELECTRIC_ONLY,
        cpu_serving=True,
        gpu_preferred=False,
        above_seconds=0.0,
        below_seconds=0.0,
    )


def _accumulate(
    status: ControllerStatus,
    sample: DemandSample,
    thresholds: HybridGpuThresholds,
) -> tuple[float, float]:
    """Update the spin-up/spin-down sustained-duration accumulators.

    A demand strictly above ``spin_up_threshold`` extends the spin-up window and
    resets the spin-down window; a demand strictly below ``spin_down_threshold``
    extends the spin-down window and resets the spin-up window; a demand in the
    hysteresis band (between the two thresholds, inclusive) resets both, since
    it neither argues for spinning up nor for scaling to zero.

    Args:
        status: The status before this sample.
        sample: The demand observation being applied.
        thresholds: The controller thresholds.

    Returns:
        The ``(above_seconds, below_seconds)`` accumulators after the sample.
    """
    if sample.demand > thresholds.spin_up_threshold:
        return status.above_seconds + sample.duration_seconds, 0.0
    if sample.demand < thresholds.spin_down_threshold:
        return 0.0, status.below_seconds + sample.duration_seconds
    # In the hysteresis band: neither spinning up nor scaling to zero.
    return 0.0, 0.0


def _next_state(
    state: HybridGpuState,
    above_seconds: float,
    below_seconds: float,
    thresholds: HybridGpuThresholds,
    gpu_ready: bool,
) -> HybridGpuState:
    """Compute the next state from the accumulators and the GPU-ready signal.

    Transitions mirror the design state diagram:

    * ``ELECTRIC_ONLY`` -> ``ENGINE_STARTING`` once the spin-up window is met.
    * ``ENGINE_STARTING`` -> ``HYBRID_GPU`` once the GPU node is ``Ready``.
      (Demand falling here does not scale to zero: no node is billing yet, and
      the request is still latent; the CPU keeps serving meanwhile.)
    * ``HYBRID_GPU`` -> ``COASTING`` once the spin-down window is met.
    * ``COASTING`` -> ``ELECTRIC_ONLY`` once the spin-down window is met and the
      GPU node has finished draining/scaling to zero (``gpu_ready`` is False).
    * ``COASTING`` -> ``HYBRID_GPU`` if demand climbs back over the spin-up
      window before scale-to-zero completes.

    Args:
        state: The current state.
        above_seconds: Sustained seconds strictly above ``spin_up_threshold``.
        below_seconds: Sustained seconds strictly below ``spin_down_threshold``.
        thresholds: The controller thresholds and sustained windows.
        gpu_ready: Whether the GPU node reports ``Ready`` with NVENC capacity.

    Returns:
        The next :class:`HybridGpuState`.
    """
    spin_up_met = above_seconds >= thresholds.spin_up_window_seconds
    spin_down_met = below_seconds >= thresholds.spin_down_window_seconds

    if state is HybridGpuState.ELECTRIC_ONLY:
        if spin_up_met:
            return HybridGpuState.ENGINE_STARTING
        return HybridGpuState.ELECTRIC_ONLY

    if state is HybridGpuState.ENGINE_STARTING:
        if gpu_ready:
            return HybridGpuState.HYBRID_GPU
        return HybridGpuState.ENGINE_STARTING

    if state is HybridGpuState.HYBRID_GPU:
        if spin_down_met:
            return HybridGpuState.COASTING
        return HybridGpuState.HYBRID_GPU

    # state is HybridGpuState.COASTING
    if spin_up_met:
        # Demand rose again before scale-to-zero completed.
        return HybridGpuState.HYBRID_GPU
    if spin_down_met and not gpu_ready:
        # Jobs drained back to CPU and the GPU node has scaled to zero.
        return HybridGpuState.ELECTRIC_ONLY
    return HybridGpuState.COASTING


def advance(
    status: ControllerStatus,
    sample: DemandSample,
    thresholds: HybridGpuThresholds,
    gpu_ready: bool = False,
) -> ControllerStatus:
    """Advance the controller by one demand sample.

    This is the pure transition function of the state machine. It first folds
    the sample into the sustained-duration accumulators, then derives the next
    state, and finally recomputes the CPU-serving / GPU-preferred flags for that
    state. The accumulators are cleared on any state change so each newly
    entered state measures its own sustained window from scratch.

    The CPU path serves in every returned status (Property 15 / R3.12), so an
    interactive request is never left unserved — even mid spin-up. The GPU is
    preferred only in :attr:`HybridGpuState.HYBRID_GPU`, i.e. only while the GPU
    node is ``Ready``.

    Args:
        status: The status before this sample.
        sample: The demand observation to apply. Its ``duration_seconds`` must
            be non-negative.
        thresholds: The controller thresholds and sustained windows;
            ``spin_up_threshold`` must be strictly greater than
            ``spin_down_threshold``.
        gpu_ready: Whether the GPU node currently reports ``Ready`` with NVENC
            capacity. Drives ``ENGINE_STARTING`` -> ``HYBRID_GPU`` and gates the
            ``COASTING`` -> ``ELECTRIC_ONLY`` scale-to-zero.

    Returns:
        The next :class:`ControllerStatus`.

    Raises:
        HybridGpuError: If ``thresholds`` violate hysteresis/window rules or the
            sample duration is negative.

    Requirements: 3.1, 3.2, 3.3, 3.9, 3.10, 3.11, 3.12, 3.13
    """
    _validate_thresholds(thresholds)
    if sample.duration_seconds < 0:
        raise HybridGpuError(
            "DemandSample.duration_seconds must be non-negative; got "
            f"{sample.duration_seconds!r}"
        )

    above_seconds, below_seconds = _accumulate(status, sample, thresholds)
    next_state = _next_state(
        status.state, above_seconds, below_seconds, thresholds, gpu_ready
    )

    if next_state is not status.state:
        # A transition resets both sustained-duration windows so the newly
        # entered state accrues its own window from zero.
        above_seconds = 0.0
        below_seconds = 0.0

    return replace(
        status,
        state=next_state,
        cpu_serving=True,
        gpu_preferred=next_state is HybridGpuState.HYBRID_GPU,
        above_seconds=above_seconds,
        below_seconds=below_seconds,
    )


def run(
    samples: Iterable[DemandSample],
    thresholds: HybridGpuThresholds,
    gpu_ready_after: bool = True,
    status: ControllerStatus | None = None,
) -> ControllerStatus:
    """Fold :func:`advance` over a sequence of demand samples.

    A convenience driver for evaluating a whole demand run. The GPU-node-Ready
    signal is modeled simply: the node is considered ``Ready`` for a sample
    once the controller has already requested it (i.e. the previous state was
    :attr:`HybridGpuState.ENGINE_STARTING` or later) when ``gpu_ready_after`` is
    ``True``. For fine-grained control of the readiness signal per step, call
    :func:`advance` directly.

    Args:
        samples: The ordered demand observations to apply.
        thresholds: The controller thresholds and sustained windows.
        gpu_ready_after: When ``True`` (default), the GPU node reports ``Ready``
            on any sample taken while the controller is no longer in
            ``ELECTRIC_ONLY`` (i.e. it has been requested). When ``False`` the
            node never reports ``Ready`` (models a boot that never completes,
            during which the CPU keeps serving).
        status: The starting status; defaults to :func:`initial_status`.

    Returns:
        The final :class:`ControllerStatus` after all samples.

    Raises:
        HybridGpuError: If ``thresholds`` or any sample are invalid.
    """
    current = status if status is not None else initial_status()
    for sample in samples:
        gpu_ready = gpu_ready_after and current.state in (
            HybridGpuState.ENGINE_STARTING,
            HybridGpuState.HYBRID_GPU,
            HybridGpuState.COASTING,
        )
        current = advance(current, sample, thresholds, gpu_ready=gpu_ready)
    return current

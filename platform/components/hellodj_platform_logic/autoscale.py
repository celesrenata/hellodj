"""Autoscaling decision logic over CPU, RAM, and GPU pressure.

This module holds the pure autoscaling decision function that the cluster
autoscaler (and the CDK layer that configures it) consult to decide whether to
add capacity, remove capacity, or hold steady. It is imported by both the CDK
infrastructure layer and the runtime components so infrastructure-as-code and
runtime share a single source of truth, and it performs no live AWS calls so
the correctness properties can exercise it directly.

Implemented here:

* :func:`decide` — Property 7 / R16. Given a triple of utilization readings
  (CPU, RAM, GPU pressure) and per-signal scale-out and scale-in thresholds,
  decide :attr:`~hellodj_platform_logic.types.AutoscaleDecision.SCALE_OUT` when
  *any* signal exceeds its scale-out threshold, decide
  :attr:`~hellodj_platform_logic.types.AutoscaleDecision.SCALE_IN` only when
  *all* signals are below their scale-in thresholds, and otherwise
  :attr:`~hellodj_platform_logic.types.AutoscaleDecision.HOLD`.

The decision is *monotonic*: raising any single signal can only move the
decision "up" toward scale-out (SCALE_IN -> HOLD -> SCALE_OUT) and can never
downgrade a scale-out. Scale-out is evaluated first and with strict priority,
so once any signal is over its scale-out threshold, increasing any signal keeps
the decision at scale-out.

Design references:
    * Correctness Property 7: Autoscaling decision over CPU, RAM, and GPU
      pressure (scale-out/scale-in/hold rules plus monotonicity)
    * Requirement 16: Autoscaling with CPU, RAM, and GPU pressure awareness
    * Testing Strategy thresholds (defaults: out at 70%, in at 40%)

Requirements: 3.2, 3.3, 16.1, 16.2, 16.3, 16.4, 16.5
"""

from __future__ import annotations

from hellodj_platform_logic.types import (
    AutoscaleDecision,
    ScaleThresholds,
    UtilizationReading,
)

__all__ = ["decide"]


def _any_over_scale_out(
    reading: UtilizationReading,
    scale_out: UtilizationReading,
) -> bool:
    """Return True when any signal strictly exceeds its scale-out threshold.

    Scale-out fires as soon as a single dimension (CPU, RAM, or GPU) crosses its
    configured scale-out threshold (R16.2, R16.3, R16.4). A strict ``>`` is used
    so a reading sitting exactly on the threshold does not trigger scale-out;
    this keeps the boundary behavior consistent with the scale-in check below.
    """
    return (
        reading.cpu > scale_out.cpu
        or reading.ram > scale_out.ram
        or reading.gpu > scale_out.gpu
    )


def _all_under_scale_in(
    reading: UtilizationReading,
    scale_in: UtilizationReading,
) -> bool:
    """Return True when every signal is strictly below its scale-in threshold.

    Scale-in requires *all* dimensions to be quiet simultaneously (R16.5): CPU,
    RAM, and GPU must each be below their scale-in threshold before capacity is
    removed, so a single busy signal keeps capacity in place.
    """
    return (
        reading.cpu < scale_in.cpu
        and reading.ram < scale_in.ram
        and reading.gpu < scale_in.gpu
    )


def decide(
    cpu: float,
    ram: float,
    gpu: float,
    scale_out_thresholds: UtilizationReading | None = None,
    scale_in_thresholds: UtilizationReading | None = None,
) -> AutoscaleDecision:
    """Decide whether to scale out, scale in, or hold on measured pressure.

    Implements Property 7 / R16. The rules, evaluated in priority order:

    1. **Scale out** if *any* of ``cpu``, ``ram``, ``gpu`` strictly exceeds its
       scale-out threshold (R16.2-R16.4).
    2. Otherwise **scale in** only if *all* three are strictly below their
       scale-in thresholds (R16.5).
    3. Otherwise **hold**.

    Scale-out has strict priority over scale-in, which guarantees monotonicity:
    raising any single signal can only move the decision toward scale-out
    (SCALE_IN -> HOLD -> SCALE_OUT) and can never downgrade an existing
    scale-out. The default thresholds (scale out at 70%, scale in at 40%) leave
    a hold band between 40% and 70% that provides hysteresis and prevents
    flapping.

    Thresholds are supplied via :class:`UtilizationReading` values (typically
    the ``scale_out``/``scale_in`` fields of a
    :class:`~hellodj_platform_logic.types.ScaleThresholds`). When omitted, the
    per-signal defaults from :class:`ScaleThresholds` are used.

    Args:
        cpu: CPU utilization as a fraction (``0.0``-``1.0``).
        ram: Memory utilization as a fraction (``0.0``-``1.0``).
        gpu: GPU pressure as a fraction (``0.0``-``1.0``).
        scale_out_thresholds: Per-signal scale-out thresholds. Defaults to the
            :class:`ScaleThresholds` scale-out defaults (70% each).
        scale_in_thresholds: Per-signal scale-in thresholds. Defaults to the
            :class:`ScaleThresholds` scale-in defaults (40% each).

    Returns:
        The :class:`AutoscaleDecision` for the given readings and thresholds.
    """
    defaults = ScaleThresholds()
    scale_out = scale_out_thresholds or defaults.scale_out
    scale_in = scale_in_thresholds or defaults.scale_in

    reading = UtilizationReading(cpu=cpu, ram=ram, gpu=gpu)

    if _any_over_scale_out(reading, scale_out):
        return AutoscaleDecision.SCALE_OUT
    if _all_under_scale_in(reading, scale_in):
        return AutoscaleDecision.SCALE_IN
    return AutoscaleDecision.HOLD

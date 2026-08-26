"""GPU scale-to-zero idle decision for the single shared transcode GPU host.

This module implements the pure decision function behind GPU scale-to-zero on
the single, time-sliced Karpenter-provisioned GPU node pool that every stage's
transcode pods schedule onto (Requirement 8). It performs no AWS or Kubernetes
calls so both the CDK ``eks-stack`` construct (which configures the NodePool's
idle window / ``consolidationPolicy``) and the property tests can import a
single source of truth.

The function answers one question: *given the configured idle window, how long
the GPU has been idle, and how many transcode jobs are active, should the GPU
scale to zero?* It encodes the design invariants:

* **Scale-to-zero iff idle beyond the window with no active work (R8.5).** The
  GPU scales to zero if and only if there are zero active jobs *and* the
  elapsed continuous idle time is at least the configured idle window.
* **Never scale to zero under load (R8.6).** Whenever a GPU-requiring workload
  is present (``active_jobs > 0``) the decision never scales to zero, so a
  workload arriving at zero triggers/keeps scale-up to serve it.

The valid idle-window range [60, 900] seconds (default 300) is enforced by
:class:`~hellodj_platform_logic.types.GpuIdleConfig` at construction time, so
this function receives an already-validated configuration.

Design references:
    * "GPU scale-to-zero (R8.5)" and "Scale-up (R8.6)" in Components and
      Interfaces §8, modeled by the pure ``gpu_idle_decision`` function.
    * Correctness Property 8: GPU scales to zero exactly when idle beyond the
      window with no active work.

Requirements: 8.5, 8.6
"""

from __future__ import annotations

from hellodj_platform_logic.types import GpuIdleConfig

__all__ = ["gpu_idle_decision"]


def gpu_idle_decision(
    cfg: GpuIdleConfig,
    idle_elapsed_s: float,
    active_jobs: int,
) -> bool:
    """Decide whether the shared transcode GPU should scale to zero.

    Implements Property 8 / R8.5, R8.6. The GPU scales to zero *if and only if*
    both of the following hold:

    * there are **zero** active transcode jobs (``active_jobs == 0``); and
    * the continuous idle time is **at least** the configured idle window
      (``idle_elapsed_s >= cfg.idle_window_seconds``).

    Consequently the decision never scales to zero while a GPU-requiring
    workload is present (``active_jobs > 0``), regardless of elapsed idle time
    (R8.6); a workload present at zero instead drives scale-up. Non-positive
    ``active_jobs`` (i.e. exactly zero) is the only job count that permits
    scale-to-zero; any ``active_jobs`` at or above one is active work.

    The configured idle window is already constrained to the valid range
    [60, 900] seconds by :class:`~hellodj_platform_logic.types.GpuIdleConfig`,
    so callers cannot pass an out-of-range window through a constructed config.

    Args:
        cfg: The validated idle-window configuration (default 300 s, within the
            valid range [60, 900] s enforced at construction).
        idle_elapsed_s: The continuous idle time, in seconds, with no active
            transcode workload. Compared against ``cfg.idle_window_seconds``.
        active_jobs: The number of active transcode jobs requiring the GPU. Any
            value greater than zero counts as active work and forbids
            scale-to-zero.

    Returns:
        ``True`` when the GPU should scale to zero (zero active jobs and idle at
        least the configured window); ``False`` otherwise.

    Requirements: 8.5, 8.6
    """
    return active_jobs <= 0 and idle_elapsed_s >= cfg.idle_window_seconds

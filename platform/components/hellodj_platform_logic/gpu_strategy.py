"""GPU acquisition strategy selection and placement decision logic.

This module holds the pure decision functions that pick *how* the platform
acquires GPU capacity for transcode/visualizer workloads. It is imported by
both the CDK infrastructure layer and the runtime transcode scheduler so that
infrastructure-as-code and runtime share a single source of truth, and it makes
no live AWS calls so the correctness properties can exercise it directly.

Currently implemented:

* :func:`select_strategy` — Decision D3 / Property 3. Given candidate GPU
  acquisition strategies each annotated with an incremental cost and a
  warm-start latency, return the lowest-cost strategy whose warm-start latency
  is within the Interactive_Latency_Budget, and never return an over-budget
  strategy when a feasible one exists.
* :func:`place_gpu` — Decision D2 / Property 4. Given the inter-host streaming
  cost and the egress cost, return co-located placement whenever the inter-host
  streaming cost is greater than the egress cost, and separate-host placement
  otherwise.

Design references:
    * Decision D2: GPU Placement — Co-located (inter-host streaming vs egress)
    * Decision D3: GPU Acquisition Strategy (per-job vs warm-shared vs software)
    * Correctness Property 3: GPU acquisition strategy selection
    * Correctness Property 4: GPU placement decision

Requirements: 3.4, 3.5, 3.6, 3.9, 3.10, 3.12, 3.13
"""

from __future__ import annotations

from collections.abc import Iterable

from hellodj_platform_logic.types import (
    INTERACTIVE_LATENCY_BUDGET_SECONDS,
    GpuPlacement,
    GpuStrategy,
    GpuStrategyCandidate,
)

__all__ = ["place_gpu", "select_strategy"]


def _sort_key(candidate: GpuStrategyCandidate) -> tuple[float, float, int]:
    """Return a total-ordering key for choosing among feasible candidates.

    Candidates are ranked by:

    1. ``incremental_cost`` ascending — the primary objective (R3.10: lowest
       cost that satisfies the budget).
    2. ``warm_start_latency_seconds`` ascending — a deterministic tie-break that
       prefers the snappier strategy when costs are equal.
    3. The strategy's declaration order in :class:`GpuStrategy` — a final
       deterministic tie-break so selection is stable regardless of input
       ordering.
    """
    return (
        candidate.incremental_cost,
        candidate.warm_start_latency_seconds,
        list(GpuStrategy).index(candidate.strategy),
    )


def select_strategy(
    candidates: Iterable[GpuStrategyCandidate],
    latency_budget: float = INTERACTIVE_LATENCY_BUDGET_SECONDS,
) -> GpuStrategyCandidate | None:
    """Select the lowest-cost GPU strategy that meets the latency budget.

    Implements Decision D3 / Property 3. A candidate is *feasible* when its
    ``warm_start_latency_seconds`` is less than or equal to ``latency_budget``
    (the Interactive_Latency_Budget, ≤ 5 seconds per R3.13). Among all feasible
    candidates the lowest ``incremental_cost`` one is returned, with ties broken
    deterministically by lower latency and then by strategy declaration order.

    Per R3.12 an over-budget (cold-start) strategy is never returned while any
    feasible strategy exists. When *no* candidate is feasible the function
    returns ``None`` rather than an over-budget strategy, so callers must decide
    how to handle an entirely infeasible candidate set (for interactive work the
    software-CPU path is expected to always be feasible, keeping this in the
    feasible set).

    Args:
        candidates: The GPU acquisition strategies to choose among, each
            annotated with an incremental cost and warm-start latency.
        latency_budget: The maximum acceptable warm-start latency in seconds.
            Defaults to :data:`INTERACTIVE_LATENCY_BUDGET_SECONDS`.

    Returns:
        The lowest-cost feasible :class:`GpuStrategyCandidate`, or ``None`` if
        every candidate exceeds ``latency_budget`` or the input is empty.
    """
    feasible = [
        candidate
        for candidate in candidates
        if candidate.warm_start_latency_seconds <= latency_budget
    ]
    if not feasible:
        return None
    return min(feasible, key=_sort_key)


def place_gpu(inter_host_cost: float, egress_cost: float) -> GpuPlacement:
    """Decide GPU workload placement from data-transfer cost analysis.

    Implements Decision D2 / Property 4. The transcode/visualizer path streams
    live media from the producers (Lavalink/activity-backend) and emits HLS
    segments. Placing the GPU on a *separate* host forces every second of that
    media across an inter-host network leg, whereas co-locating the GPU with the
    producers keeps that hop loopback/intra-node and leaves only the produced
    HLS to leave via managed egress.

    Per R3.6, when the inter-host streaming cost is strictly greater than the
    egress cost the workload is placed :attr:`GpuPlacement.CO_LOCATED` within
    the cluster; otherwise it is placed on a :attr:`GpuPlacement.SEPARATE_HOST`.

    Args:
        inter_host_cost: The cost of streaming media between a separate app node
            and GPU node (the inter-host/inter-AZ transfer leg).
        egress_cost: The cost of egressing the produced media instead.

    Returns:
        :attr:`GpuPlacement.CO_LOCATED` when ``inter_host_cost`` is greater than
        ``egress_cost``, otherwise :attr:`GpuPlacement.SEPARATE_HOST`.
    """
    if inter_host_cost > egress_cost:
        return GpuPlacement.CO_LOCATED
    return GpuPlacement.SEPARATE_HOST

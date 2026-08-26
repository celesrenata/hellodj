"""Property test for the GPU placement decision (task 4.4).

Property 4 (design "GPU placement decision"): for any pair of costs
(inter-host streaming cost, egress cost), :func:`place_gpu`:

* returns :attr:`GpuPlacement.CO_LOCATED` whenever the inter-host streaming
  cost is strictly greater than the egress cost, and
* returns :attr:`GpuPlacement.SEPARATE_HOST` otherwise (including the equality
  boundary, where inter-host cost is not *greater* than egress cost).

The placement function is pure, so the property is exercised directly over
arbitrary finite, non-negative ``(inter_host_cost, egress_cost)`` pairs with
Hypothesis (>=100 iterations), including the equality boundary.

Feature: aws-saas-replatform, Property 4

Validates: Requirements 3.6
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from hellodj_platform_logic.gpu_strategy import place_gpu
from hellodj_platform_logic.types import GpuPlacement

# Finite, non-negative floats keep the cost comparison total and meaningful,
# modeling real (non-negative) AWS transfer/egress dollar costs.
_costs = st.floats(
    min_value=0.0, max_value=1.0e6, allow_nan=False, allow_infinity=False
)


@settings(max_examples=300)
@given(inter_host_cost=_costs, egress_cost=_costs)
def test_co_located_iff_inter_host_greater_than_egress(
    inter_host_cost: float, egress_cost: float
) -> None:
    """Co-located chosen exactly when inter-host > egress (Property 4).

    Feature: aws-saas-replatform, Property 4

    Validates: Requirements 3.6
    """
    result = place_gpu(inter_host_cost, egress_cost)

    if inter_host_cost > egress_cost:
        # R3.6: inter-host streaming costs more than egress -> co-locate.
        assert result is GpuPlacement.CO_LOCATED
    else:
        # Not strictly greater (including equal) -> separate host.
        assert result is GpuPlacement.SEPARATE_HOST


@settings(max_examples=200)
@given(cost=_costs)
def test_equality_boundary_selects_separate_host(cost: float) -> None:
    """At the equality boundary (inter-host == egress), pick separate host.

    Equal costs mean inter-host streaming does not cost *more* than egress, so
    the strict ">" rule of R3.6 does not fire and placement stays separate-host.

    Feature: aws-saas-replatform, Property 4

    Validates: Requirements 3.6
    """
    assert place_gpu(cost, cost) is GpuPlacement.SEPARATE_HOST


@settings(max_examples=200)
@given(inter_host_cost=_costs, egress_cost=_costs)
def test_result_is_always_a_valid_placement(
    inter_host_cost: float, egress_cost: float
) -> None:
    """Every decision is one of the two defined placements (totality).

    Feature: aws-saas-replatform, Property 4

    Validates: Requirements 3.6
    """
    assert place_gpu(inter_host_cost, egress_cost) in (
        GpuPlacement.CO_LOCATED,
        GpuPlacement.SEPARATE_HOST,
    )

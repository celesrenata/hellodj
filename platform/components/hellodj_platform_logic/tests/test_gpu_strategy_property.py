"""Property test for GPU acquisition strategy selection (task 4.2).

Property 3 (design "GPU acquisition strategy selection"): for any set of
candidate GPU acquisition strategies each annotated with an incremental cost
and a warm-start latency, :func:`select_strategy`:

* returns the lowest-cost candidate whose ``warm_start_latency_seconds`` is
  within the latency budget (feasible), and
* never returns an over-budget candidate when a feasible one exists, and
* returns ``None`` exactly when the candidate set is empty or every candidate
  is over budget.

The selection function is pure, so the property is exercised directly over
arbitrary ``(cost, latency)`` candidate lists and arbitrary budgets with
Hypothesis (>=100 iterations).

Feature: aws-saas-replatform, Property 3

Validates: Requirements 3.9, 3.10, 3.12, 3.13
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from hellodj_platform_logic.gpu_strategy import select_strategy
from hellodj_platform_logic.types import (
    INTERACTIVE_LATENCY_BUDGET_SECONDS,
    GpuStrategy,
    GpuStrategyCandidate,
)

# Finite, non-NaN floats keep cost/latency comparisons total and meaningful.
_costs = st.floats(
    min_value=0.0, max_value=1.0e6, allow_nan=False, allow_infinity=False
)
_latencies = st.floats(
    min_value=0.0, max_value=600.0, allow_nan=False, allow_infinity=False
)
_strategies = st.sampled_from(list(GpuStrategy))


@st.composite
def _candidates(draw: st.DrawFn) -> list[GpuStrategyCandidate]:
    """Generate arbitrary candidate lists spanning the input space.

    Strategies may repeat across candidates (the real caller passes one per
    :class:`GpuStrategy`, but the selection logic must remain correct for any
    list), and costs/latencies range across feasible and over-budget values so
    empty-, all-feasible-, all-infeasible- and mixed-sets are all reachable.
    """
    return draw(
        st.lists(
            st.builds(
                GpuStrategyCandidate,
                strategy=_strategies,
                incremental_cost=_costs,
                warm_start_latency_seconds=_latencies,
            ),
            max_size=8,
        )
    )


# Budgets around the 5s Interactive_Latency_Budget so feasibility flips.
_budgets = st.floats(
    min_value=0.0, max_value=600.0, allow_nan=False, allow_infinity=False
)


@settings(max_examples=300)
@given(candidates=_candidates(), budget=_budgets)
def test_selection_is_lowest_feasible_cost_within_budget(
    candidates: list[GpuStrategyCandidate], budget: float
) -> None:
    """Chosen candidate is feasible and lowest-cost; else None (Property 3).

    Feature: aws-saas-replatform, Property 3

    Validates: Requirements 3.9, 3.10, 3.12, 3.13
    """
    result = select_strategy(candidates, latency_budget=budget)

    feasible = [
        candidate
        for candidate in candidates
        if candidate.warm_start_latency_seconds <= budget
    ]

    if not feasible:
        # No feasible candidate -> never return an over-budget strategy (R3.12).
        assert result is None
        return

    assert result is not None
    # The returned candidate must itself be feasible (budget never violated when
    # a feasible one exists -- R3.12, R3.13).
    assert result.warm_start_latency_seconds <= budget
    # It must be one of the supplied candidates.
    assert result in candidates
    # It must be the lowest incremental cost among all feasible candidates
    # (R3.10: lowest-cost strategy that satisfies the budget).
    min_feasible_cost = min(c.incremental_cost for c in feasible)
    assert result.incremental_cost == min_feasible_cost


@settings(max_examples=300)
@given(candidates=_candidates(), budget=_budgets)
def test_never_returns_over_budget_when_feasible_exists(
    candidates: list[GpuStrategyCandidate], budget: float
) -> None:
    """Budget is never violated whenever any feasible candidate exists (R3.12).

    Feature: aws-saas-replatform, Property 3

    Validates: Requirements 3.12, 3.13
    """
    result = select_strategy(candidates, latency_budget=budget)
    has_feasible = any(
        c.warm_start_latency_seconds <= budget for c in candidates
    )
    if has_feasible:
        assert result is not None
        assert result.warm_start_latency_seconds <= budget
    else:
        assert result is None


@settings(max_examples=100)
@given(candidates=_candidates())
def test_default_budget_matches_interactive_latency_budget(
    candidates: list[GpuStrategyCandidate],
) -> None:
    """The default budget is the 5s Interactive_Latency_Budget (R3.13).

    Feature: aws-saas-replatform, Property 3

    Validates: Requirements 3.13
    """
    assert (
        select_strategy(candidates)
        == select_strategy(
            candidates, latency_budget=INTERACTIVE_LATENCY_BUDGET_SECONDS
        )
    )


def test_empty_candidate_set_returns_none() -> None:
    """An empty candidate set yields None (edge case).

    Feature: aws-saas-replatform, Property 3

    Validates: Requirements 3.10, 3.12
    """
    assert select_strategy([]) is None


def test_all_infeasible_returns_none() -> None:
    """When every candidate is over budget, None is returned (R3.12).

    Feature: aws-saas-replatform, Property 3

    Validates: Requirements 3.12, 3.13
    """
    over_budget = [
        GpuStrategyCandidate(GpuStrategy.PER_JOB, 1.0, 180.0),
        GpuStrategyCandidate(GpuStrategy.WARM_SHARED, 0.5, 60.0),
    ]
    assert select_strategy(over_budget, latency_budget=5.0) is None

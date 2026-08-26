"""Property test for the three-tier cost model (task 7.8).

Property 13 (design "Cost-tier monotonicity and itemization"): *for any* set of
non-negative itemized line items (compute, GPU, Data_Layer, Edge_Cache_Service,
Log_Store, Observability_Stack) across the three tiers, where the
Recommended-with-Headroom tier equals the Recommended tier plus a non-negative
reserve, the Cost_Model SHALL itemize all six categories in every tier and SHALL
satisfy ``total(Minimum) <= total(Recommended) <= total(Recommended-with-Headroom)``.

The property is exercised in two facets:

1. **Canonical model facet** -- assert the shipped
   :data:`hellodj_platform_logic.cost_model.COST_MODEL` (and its public
   accessors) itemizes all six categories in every tier with non-negative
   amounts, exposes a non-negative per-category headroom reserve
   (Recommended-with-Headroom >= Recommended per category), and has
   monotonically non-decreasing tier totals (R20.1, R20.2, R20.5, R20.6).

2. **Generative facet** -- generate arbitrary non-negative Minimum line items,
   a non-negative delta lifting each category to Recommended, and a
   non-negative per-category headroom reserve, then build three tiers with a
   small local constructor that mirrors the cost model's tier-building
   (Recommended-with-Headroom = Recommended + reserve). Assert the invariants
   hold for *any* such valid non-negative itemization: all six categories are
   itemized per tier and the tier totals are monotonic. This demonstrates the
   property holds for the whole valid input space, not just the shipped numbers.

Feature: aws-saas-replatform, Property 13

Validates: Requirements 20.1, 20.2, 20.5, 20.6
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from hellodj_platform_logic.cost_model import (
    COST_MODEL,
    itemization_for,
    reserve_for,
    total_for,
)
from hellodj_platform_logic.types import CostCategory, CostTier

_ALL_CATEGORIES = frozenset(CostCategory)

# Ordered tiers for monotonicity checks (Minimum -> Recommended -> Headroom).
_ORDERED_TIERS = (
    CostTier.MINIMUM,
    CostTier.RECOMMENDED,
    CostTier.RECOMMENDED_WITH_HEADROOM,
)

# Bounded, finite, non-negative dollar amounts keep the generated space sensible
# while still covering zero (free-tier categories) and large spikes.
_amount = st.floats(
    min_value=0.0,
    max_value=100_000.0,
    allow_nan=False,
    allow_infinity=False,
)


@st.composite
def _tier_totals_from_itemization(
    draw: st.DrawFn,
) -> tuple[
    dict[CostCategory, float],
    dict[CostCategory, float],
    dict[CostCategory, float],
    float,
    float,
    float,
]:
    """Draw a valid non-negative itemization across the three tiers.

    Returns the per-category line items for each of the three tiers plus their
    totals, built by a local constructor that mirrors the cost model:

    * ``minimum[c]``  -- an arbitrary non-negative amount per category.
    * ``recommended[c] = minimum[c] + step[c]`` for a non-negative ``step``,
      so Recommended is never below Minimum per category (R20.5).
    * ``headroom[c] = recommended[c] + reserve[c]`` for a non-negative
      ``reserve`` (R20.6).

    Every tier therefore itemizes exactly the six categories with non-negative
    amounts, and the per-category chain is non-decreasing, which is the valid
    input space Property 13 quantifies over.
    """
    minimum: dict[CostCategory, float] = {}
    recommended: dict[CostCategory, float] = {}
    headroom: dict[CostCategory, float] = {}
    for category in CostCategory:
        base = draw(_amount)
        step = draw(_amount)  # Minimum -> Recommended per-category increase.
        reserve = draw(_amount)  # Recommended -> Headroom reserve (R20.6).
        minimum[category] = base
        recommended[category] = base + step
        headroom[category] = base + step + reserve
    return (
        minimum,
        recommended,
        headroom,
        sum(minimum.values()),
        sum(recommended.values()),
        sum(headroom.values()),
    )


@settings(max_examples=200)
@given(itemization=_tier_totals_from_itemization())
def test_generated_itemization_is_itemized_and_monotonic(
    itemization: tuple[
        dict[CostCategory, float],
        dict[CostCategory, float],
        dict[CostCategory, float],
        float,
        float,
        float,
    ],
) -> None:
    """Any valid non-negative itemization is fully itemized and monotonic.

    Generative facet: for arbitrary non-negative line items where
    Recommended-with-Headroom = Recommended + non-negative reserve (and
    Recommended >= Minimum per category), every tier itemizes all six
    categories and the tier totals are monotonically non-decreasing.

    Feature: aws-saas-replatform, Property 13

    Validates: Requirements 20.1, 20.2, 20.5, 20.6
    """
    minimum, recommended, headroom, min_total, rec_total, head_total = itemization

    # All six categories itemized in every tier (R20.2), amounts non-negative.
    for tier_items in (minimum, recommended, headroom):
        assert set(tier_items) == _ALL_CATEGORIES
        assert all(amount >= 0.0 for amount in tier_items.values())

    # Headroom = Recommended + non-negative per-category reserve (R20.6).
    for category in CostCategory:
        assert headroom[category] >= recommended[category]
        assert recommended[category] >= minimum[category]

    # Monotonic tier totals (R20.5, R20.6).
    assert min_total <= rec_total <= head_total


@settings(max_examples=200)
@given(data=st.data())
def test_canonical_cost_model_itemized_and_monotonic(data: st.DataObject) -> None:
    """The shipped COST_MODEL is itemized, non-negative, and monotonic.

    Canonical facet: sampling any tier/category from the shipped model, every
    tier itemizes exactly the six categories with non-negative amounts, the
    per-category headroom reserve is non-negative, and the tier totals are
    monotonically non-decreasing. Uses the public accessors
    (``itemization_for``, ``total_for``, ``reserve_for``) and the model's own
    ``.itemization``/``.total``/``.reserve`` methods.

    Feature: aws-saas-replatform, Property 13

    Validates: Requirements 20.1, 20.2, 20.5, 20.6
    """
    tier = data.draw(st.sampled_from(list(CostTier)))
    category = data.draw(st.sampled_from(list(CostCategory)))

    itemization = itemization_for(tier)
    # Accessor and model method agree.
    assert itemization is COST_MODEL.itemization(tier)

    # All six categories itemized for the sampled tier (R20.2).
    assert set(itemization.line_items) == _ALL_CATEGORIES
    # Every line item non-negative; TierItemization.amount agrees with the map.
    for cat in CostCategory:
        amount = itemization.amount(cat)
        assert amount >= 0.0
        assert itemization.line_items[cat].monthly_usd == amount

    # total_for accessor agrees with the model and with summing the line items.
    tier_total = total_for(tier)
    assert tier_total == COST_MODEL.total(tier)
    assert tier_total == round(
        sum(item.monthly_usd for item in itemization.line_items.values()), 2
    )

    # Per-category headroom reserve is non-negative (R20.6): Headroom >= Recommended.
    reserve = reserve_for(category)
    assert reserve is not None
    assert reserve == COST_MODEL.reserve(category)
    assert reserve >= 0.0
    assert (
        itemization_for(CostTier.RECOMMENDED_WITH_HEADROOM).amount(category)
        >= itemization_for(CostTier.RECOMMENDED).amount(category)
    )

    # Monotonic tier totals across the fixed Minimum -> Recommended -> Headroom
    # order (R20.5, R20.6).
    totals = [total_for(t) for t in _ORDERED_TIERS]
    assert totals[0] <= totals[1] <= totals[2]

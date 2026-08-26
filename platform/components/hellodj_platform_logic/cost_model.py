"""Three-tier cost model for the HelloDJ AWS platform.

This module holds the pure, side-effect-free cost model consumed by the design
documentation, the CDK budgeting/alarm wiring, and the correctness property
(Property 13). It encodes the AWS **on-demand, us-east-1** unit prices and
tier estimates that were verified during the design phase on **2026-08-24**, so
infrastructure-as-code and runtime read a single source of truth and the
property test can exercise the model directly with no live AWS calls.

The model presents total estimated monthly AWS running cost in three
:class:`~hellodj_platform_logic.types.CostTier` values (Minimum, Recommended,
Recommended-with-Headroom, R20.1). Each tier itemizes all six
:class:`~hellodj_platform_logic.types.CostCategory` line items (compute, GPU,
Data_Layer, Edge_Cache, Log_Store, Observability, R20.2) as dollar amounts.

Category mapping note:
    The design's Cost Model table lists a seventh line item, **AI**
    (Bedrock/Transcribe/Polly pay-per-use). ``CostCategory`` enumerates exactly
    the six categories required by R20.2, so the AI spend is folded into
    :attr:`CostCategory.COMPUTE` (both are on-demand application-serving spend).
    Folding preserves the design's verified tier totals exactly
    (~$168 / ~$578 / ~$1,313 per month) while keeping every tier itemized
    across the six required categories.

Invariants (Property 13 / R20):
    * Every tier itemizes all six categories with non-negative amounts.
    * Recommended-with-Headroom = Recommended line items + a non-negative
      per-category reserve (R20.6).
    * Totals are monotonically non-decreasing:
      ``total(MINIMUM) <= total(RECOMMENDED) <= total(RECOMMENDED_WITH_HEADROOM)``
      (R20.5, R20.6). :func:`build_cost_model` asserts this on construction.

Design references:
    * Cost Model section (verified unit prices + three-tier estimate)
    * Correctness Property 13: Cost-tier monotonicity and itemization
    * Decision D3 (hybrid scale-to-zero GPU) and D5 (Bedrock AI)

Requirements: 20.1, 20.2, 20.3, 20.4, 20.5, 20.6
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from hellodj_platform_logic.types import CostCategory, CostTier

__all__ = [
    "PRICING_REGION",
    "PRICING_DATE",
    "UNIT_PRICES",
    "CostLineItem",
    "TierItemization",
    "CostModel",
    "build_cost_model",
    "COST_MODEL",
    "itemization_for",
    "total_for",
    "reserve_for",
]

# ---------------------------------------------------------------------------
# Region / pricing-date provenance (R20.3, R20.4)
# ---------------------------------------------------------------------------

#: AWS region for which every price in this model is valid (R20.4).
PRICING_REGION = "us-east-1"

#: Date the on-demand unit prices were verified during design (R20.4).
PRICING_DATE = "2026-08-24"


@dataclass(frozen=True)
class _UnitPrice:
    """A single verified on-demand unit price (us-east-1, ``PRICING_DATE``).

    Attributes:
        resource: Human-readable AWS resource / meter the price applies to.
        amount: The numeric unit price in US dollars.
        unit: The unit the price is measured in (e.g. ``"cluster-hr"``).
    """

    resource: str
    amount: float
    unit: str


#: Verified on-demand unit prices (us-east-1, 2026-08-24). These underpin the
#: tier estimates below and satisfy R20.3 (every price is a verified figure,
#: not an assumed value). Prices change frequently; re-verify before spend.
UNIT_PRICES: Mapping[str, _UnitPrice] = MappingProxyType(
    {
        "eks_control_plane": _UnitPrice("EKS control plane", 0.10, "cluster-hr"),
        "fargate_graviton_vcpu": _UnitPrice(
            "Fargate Graviton vCPU", 0.03238, "vCPU-hr"
        ),
        "fargate_graviton_gb": _UnitPrice(
            "Fargate Graviton memory", 0.00356, "GB-hr"
        ),
        "g5g_xlarge_on_demand": _UnitPrice(
            "g5g.xlarge (4 vCPU, 8 GiB, 1x T4G) on-demand", 0.42, "instance-hr"
        ),
        "g5g_xlarge_spot": _UnitPrice("g5g.xlarge spot", 0.33, "instance-hr"),
        "g4dn_xlarge_on_demand": _UnitPrice(
            "g4dn.xlarge (x86 fallback) on-demand", 0.526, "instance-hr"
        ),
        "dynamodb_wru": _UnitPrice(
            "DynamoDB on-demand write request units", 1.25, "M-WRU"
        ),
        "dynamodb_rru": _UnitPrice(
            "DynamoDB on-demand read request units", 0.25, "M-RRU"
        ),
        "dynamodb_storage": _UnitPrice("DynamoDB storage", 0.25, "GB-mo"),
        "dax_node": _UnitPrice("DAX node (smallest, t-class)", 0.04, "node-hr"),
        "cloudfront_egress": _UnitPrice(
            "CloudFront egress (US/EU, first 10 TB)", 0.085, "GB"
        ),
        "s3_standard": _UnitPrice("S3 Standard storage", 0.023, "GB-mo"),
        "cloudwatch_logs_ingest": _UnitPrice(
            "CloudWatch Logs ingest", 0.50, "GB"
        ),
        "athena_scanned": _UnitPrice("Athena data scanned", 5.0, "TB"),
        "quicksight_author": _UnitPrice("QuickSight author", 24.0, "author-mo"),
        "transcribe_minute": _UnitPrice("Amazon Transcribe", 0.024, "minute"),
        "polly_neural": _UnitPrice("Amazon Polly (neural)", 4.0, "M-chars"),
    }
)


# ---------------------------------------------------------------------------
# Tier itemizations (monthly USD, single region) — from the design Cost Model
# ---------------------------------------------------------------------------
#
# Each tuple is (Minimum, Recommended, Recommended-with-Headroom) monthly USD.
# The design's separate "AI" line (15 / 60 / 150) is folded into COMPUTE, so
# COMPUTE below is (design compute + design AI):
#   Minimum:  113 + 15 = 128
#   Recommended: 223 + 60 = 283
#   Recommended-with-Headroom: 353 + 150 = 503
# All other categories match the design table verbatim. Tier totals therefore
# remain exactly ~$168 / ~$578 / ~$1,313 per month.
_TIER_INDEX: Mapping[CostTier, int] = MappingProxyType(
    {
        CostTier.MINIMUM: 0,
        CostTier.RECOMMENDED: 1,
        CostTier.RECOMMENDED_WITH_HEADROOM: 2,
    }
)

_CATEGORY_AMOUNTS: Mapping[CostCategory, tuple[float, float, float]] = MappingProxyType(
    {
        # Compute = EKS control plane + Graviton app nodes/Fargate + AI (Bedrock
        # STT/intent/TTS + Transcribe/Polly, pay-per-use). AI folded in here.
        CostCategory.COMPUTE: (128.0, 283.0, 503.0),
        # GPU / transcode: hybrid scale-to-zero g5g Spot (D3).
        CostCategory.GPU: (0.0, 40.0, 180.0),
        # Data_Layer: DynamoDB + DAX.
        CostCategory.DATA_LAYER: (15.0, 50.0, 110.0),
        # Edge_Cache: CloudFront egress.
        CostCategory.EDGE_CACHE: (0.0, 100.0, 300.0),
        # Log_Store: S3 (Hive-partitioned) retention tiers.
        CostCategory.LOG_STORE: (5.0, 15.0, 40.0),
        # Observability: CloudWatch/metrics/Glue/Athena/QuickSight.
        CostCategory.OBSERVABILITY: (20.0, 90.0, 180.0),
    }
)


@dataclass(frozen=True)
class CostLineItem:
    """A single itemized category cost within one tier (R20.2).

    Attributes:
        category: The cost category this line item covers.
        monthly_usd: The estimated monthly cost in US dollars. Always
            non-negative.
    """

    category: CostCategory
    monthly_usd: float


@dataclass(frozen=True)
class TierItemization:
    """The full six-category itemization and total for one tier (R20.1, R20.2).

    Attributes:
        tier: The cost tier this itemization describes.
        line_items: Exactly one :class:`CostLineItem` per
            :class:`CostCategory`, keyed by category. All six categories are
            always present.
    """

    tier: CostTier
    line_items: Mapping[CostCategory, CostLineItem]

    @property
    def total_monthly_usd(self) -> float:
        """Sum of all six category line items for this tier (rounded cents)."""
        return round(
            sum(item.monthly_usd for item in self.line_items.values()), 2
        )

    def amount(self, category: CostCategory) -> float:
        """Return the monthly USD amount itemized for ``category``."""
        return self.line_items[category].monthly_usd


@dataclass(frozen=True)
class CostModel:
    """The complete three-tier cost model (Property 13, R20).

    Attributes:
        region: The AWS region the estimates are valid for (R20.4).
        pricing_date: The date the unit prices were verified (R20.4).
        tiers: One :class:`TierItemization` per :class:`CostTier`, keyed by
            tier. All three tiers are always present.
    """

    region: str
    pricing_date: str
    tiers: Mapping[CostTier, TierItemization]

    def itemization(self, tier: CostTier) -> TierItemization:
        """Return the full itemization for a single tier."""
        return self.tiers[tier]

    def total(self, tier: CostTier) -> float:
        """Return the total estimated monthly cost for a single tier."""
        return self.tiers[tier].total_monthly_usd

    def reserve(self, category: CostCategory) -> float:
        """Return the non-negative headroom reserve for ``category`` (R20.6).

        The reserve is the per-category delta of the Recommended-with-Headroom
        tier over the Recommended tier. It is always non-negative by
        construction.
        """
        recommended = self.tiers[CostTier.RECOMMENDED].amount(category)
        headroom = self.tiers[CostTier.RECOMMENDED_WITH_HEADROOM].amount(category)
        return round(headroom - recommended, 2)


def _amount_for(category: CostCategory, tier: CostTier) -> float:
    """Look up the raw monthly USD amount for a category within a tier."""
    return _CATEGORY_AMOUNTS[category][_TIER_INDEX[tier]]


def _build_tier(tier: CostTier) -> TierItemization:
    """Assemble the six-category itemization for a single tier."""
    line_items = {
        category: CostLineItem(
            category=category,
            monthly_usd=_amount_for(category, tier),
        )
        for category in CostCategory
    }
    return TierItemization(tier=tier, line_items=MappingProxyType(line_items))


def build_cost_model() -> CostModel:
    """Construct and validate the three-tier cost model (Property 13, R20).

    Assembles every tier's six-category itemization from the verified
    us-east-1 / 2026-08-24 estimates and enforces the model invariants before
    returning:

    * Every category amount is non-negative.
    * Every tier itemizes all six categories (R20.2).
    * The headroom reserve for every category is non-negative, i.e.
      Recommended-with-Headroom line items = Recommended line items + a
      non-negative reserve (R20.6).
    * Tier totals are monotonically non-decreasing (R20.5, R20.6):
      ``total(MINIMUM) <= total(RECOMMENDED) <= total(RECOMMENDED_WITH_HEADROOM)``.

    Returns:
        A validated :class:`CostModel`.

    Raises:
        ValueError: If any invariant is violated (a negative amount, a missing
            category, a negative reserve, or a non-monotonic total).
    """
    tiers = {tier: _build_tier(tier) for tier in CostTier}

    # Every category present and non-negative in every tier (R20.2).
    for tier, itemization in tiers.items():
        if set(itemization.line_items) != set(CostCategory):
            raise ValueError(f"tier {tier.value} is missing category line items")
        for item in itemization.line_items.values():
            if item.monthly_usd < 0:
                raise ValueError(
                    f"negative cost for {item.category.value} in {tier.value}"
                )

    # Recommended-with-Headroom = Recommended + non-negative reserve (R20.6).
    recommended = tiers[CostTier.RECOMMENDED]
    headroom = tiers[CostTier.RECOMMENDED_WITH_HEADROOM]
    for category in CostCategory:
        if headroom.amount(category) < recommended.amount(category):
            raise ValueError(
                f"headroom reserve for {category.value} is negative"
            )

    # Monotonic tier totals (R20.5, R20.6).
    minimum_total = tiers[CostTier.MINIMUM].total_monthly_usd
    recommended_total = recommended.total_monthly_usd
    headroom_total = headroom.total_monthly_usd
    if not (minimum_total <= recommended_total <= headroom_total):
        raise ValueError(
            "tier totals must be monotonically non-decreasing: "
            f"{minimum_total} <= {recommended_total} <= {headroom_total}"
        )

    return CostModel(
        region=PRICING_REGION,
        pricing_date=PRICING_DATE,
        tiers=MappingProxyType(tiers),
    )


#: The canonical, validated cost model instance (module-level singleton).
COST_MODEL: CostModel = build_cost_model()


def itemization_for(tier: CostTier) -> TierItemization:
    """Return the six-category itemization for ``tier`` from ``COST_MODEL``."""
    return COST_MODEL.itemization(tier)


def total_for(tier: CostTier) -> float:
    """Return the total estimated monthly cost for ``tier`` from ``COST_MODEL``."""
    return COST_MODEL.total(tier)


def reserve_for(category: CostCategory) -> float:
    """Return the non-negative headroom reserve for ``category`` (R20.6)."""
    return COST_MODEL.reserve(category)

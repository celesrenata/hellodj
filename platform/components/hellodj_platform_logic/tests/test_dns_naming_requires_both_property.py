"""Property-based test for the "DNS naming requires both" invariant.

Feature: hellodj-nix-native-delivery, Property 12

Property 12 (DNS naming requires both a stage and a region):
    Invoking ``dns_naming.derive_env_name`` with a valid stage but a
    missing/empty/invalid region, or with a valid region but a missing/invalid
    stage, never returns a DNS name -- it always raises. When either the stage
    or the region is absent (missing/empty), the raised error indicates that
    both a stage and a region are required (R9.4, R9.5).

Validates: Requirements 9.4, 9.5
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from hellodj_platform_logic.dns_naming import derive_env_name
from hellodj_platform_logic.types import DeploymentStage

# The exact message the derivation raises when either input is absent (R9.4/R9.5).
_BOTH_REQUIRED = "both a stage and a region are required"

# A valid single-label AWS-region-like DNS label (mirrors ``us-east-1``).
_LABEL_CHARS = "abcdefghijklmnopqrstuvwxyz0123456789"


@st.composite
def dns_region_labels(draw: st.DrawFn) -> str:
    """Generate arbitrary valid single-label AWS-region-like DNS labels."""
    first = draw(st.sampled_from(_LABEL_CHARS))
    middle = draw(st.text(alphabet=_LABEL_CHARS + "-", min_size=0, max_size=61))
    if middle:
        last = draw(st.sampled_from(_LABEL_CHARS))
        candidate = f"{first}{middle}{last}"
    else:
        candidate = first
    return candidate[:63]


# Regions that are "missing/empty" from the caller's perspective: the empty
# string and whitespace-only strings both normalize to empty (R9.4).
missing_regions = st.text(alphabet=" \t\n\r", min_size=0, max_size=8)

# Non-``DeploymentStage`` values standing in for a missing/invalid stage
# (R9.5): None, empty/blank strings, and arbitrary non-enum objects.
invalid_stages = st.one_of(
    st.none(),
    st.text(max_size=12),
    st.integers(),
    st.booleans(),
)


@settings(max_examples=200)
@given(stage=st.sampled_from(list(DeploymentStage)), region=missing_regions)
def test_valid_stage_missing_region_requires_both(
    stage: DeploymentStage, region: str
) -> None:
    """A valid stage with a missing/empty region raises "both required".

    No DNS name is returned; the error indicates both a stage and a region are
    required (R9.4).

    Feature: hellodj-nix-native-delivery, Property 12
    Validates: Requirements 9.4
    """
    with pytest.raises(ValueError) as excinfo:
        derive_env_name(stage, region)
    assert _BOTH_REQUIRED in str(excinfo.value)


@settings(max_examples=200)
@given(stage=invalid_stages, region=dns_region_labels())
def test_missing_stage_valid_region_requires_both(
    stage: object, region: str
) -> None:
    """A valid region with a missing/invalid stage raises "both required".

    No DNS name is returned; the error indicates both a stage and a region are
    required (R9.5).

    Feature: hellodj-nix-native-delivery, Property 12
    Validates: Requirements 9.5
    """
    with pytest.raises(ValueError) as excinfo:
        derive_env_name(stage, region)  # type: ignore[arg-type]
    assert _BOTH_REQUIRED in str(excinfo.value)


@settings(max_examples=200)
@given(stage=invalid_stages, region=missing_regions)
def test_both_missing_requires_both(stage: object, region: str) -> None:
    """Missing stage AND missing region always raises "both required".

    Feature: hellodj-nix-native-delivery, Property 12
    Validates: Requirements 9.4, 9.5
    """
    with pytest.raises(ValueError) as excinfo:
        derive_env_name(stage, region)  # type: ignore[arg-type]
    assert _BOTH_REQUIRED in str(excinfo.value)


@settings(max_examples=200)
@given(
    stage=st.sampled_from(list(DeploymentStage)),
    region=st.text(min_size=1, max_size=80).filter(lambda s: s.strip() != ""),
)
def test_valid_stage_invalid_region_never_returns_name(
    stage: DeploymentStage, region: str
) -> None:
    """A non-empty but otherwise-invalid region never yields a DNS name.

    A region that is present yet not a valid DNS label must raise rather than
    return a name (no name is returned for an invalid region, R9.4).

    Feature: hellodj-nix-native-delivery, Property 12
    Validates: Requirements 9.4
    """
    from hellodj_platform_logic.dns_naming import _DNS_LABEL

    normalized = region.strip().lower()
    # Only exercise the invalid-label branch; valid labels are covered by the
    # positive Property 11 test.
    if normalized and _DNS_LABEL.match(normalized):
        return

    with pytest.raises(ValueError):
        derive_env_name(stage, region)

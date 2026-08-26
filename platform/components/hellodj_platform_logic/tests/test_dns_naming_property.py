"""Property-based test for the DNS environment-naming invariant.

Feature: aws-saas-replatform, Property 1
Feature: hellodj-nix-native-delivery, Property 11

Property 1 (aws-saas-replatform, DNS environment-naming invariant):
    For any deployment stage and any AWS region, the DNS name derivation
    function produces ``<stage>.<region>.hellodj.bot``; every produced name is
    a subdomain of the ``hellodj.bot`` zone, and an apex alias from the
    production name to ``hellodj.bot`` exists. Generating arbitrary regions
    demonstrates that a new region introduces only new, non-colliding names
    (no redesign).

Property 11 (hellodj-nix-native-delivery, DNS naming yields a zone subdomain
    that includes both stage and region):
    When a deployment stage and a region are both provided, ``derive_env_name``
    returns a strict subdomain of the ``hellodj.bot`` zone whose text includes
    both the reconciled stage name (``stage.value`` — one of ``beta`` /
    ``staging`` / ``production``) and the region. Exercised over the reconciled
    stage names crossed with valid region labels.

Validates: Requirements 9.3, 12.2, 12.3, 12.4, 18.3
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from hellodj_platform_logic.dns_naming import (
    apex_alias_target,
    derive_env_name,
    is_subdomain_of_zone,
)
from hellodj_platform_logic.types import HELLODJ_ZONE, DeploymentStage

# A valid DNS label per RFC 1035 as accepted by ``dns_naming._DNS_LABEL``:
# 1-63 characters, lowercase alphanumeric with internal (never leading or
# trailing) hyphens. This mirrors the shape of real AWS regions such as
# ``us-east-1`` while exploring the full accepted input space.
_LABEL_CHARS = "abcdefghijklmnopqrstuvwxyz0123456789"


@st.composite
def dns_region_labels(draw: st.DrawFn) -> str:
    """Generate arbitrary valid single-label AWS-region-like DNS labels."""
    first = draw(st.sampled_from("abcdefghijklmnopqrstuvwxyz0123456789"))
    # Middle may contain hyphens; total length capped at 63.
    middle = draw(
        st.text(alphabet=_LABEL_CHARS + "-", min_size=0, max_size=61)
    )
    if middle:
        last = draw(st.sampled_from(_LABEL_CHARS))
        candidate = f"{first}{middle}{last}"
    else:
        candidate = first
    return candidate[:63]


@settings(max_examples=200)
@given(stage=st.sampled_from(list(DeploymentStage)), region=dns_region_labels())
def test_naming_shape_and_subdomain_invariant(
    stage: DeploymentStage, region: str
) -> None:
    """Names have the required shape and are subdomains of the zone.

    Every reconciled stage (Beta / Staging / Production) resolves to
    ``<stage>.<region>.hellodj.bot`` — a strict subdomain of the zone.

    Feature: aws-saas-replatform, Property 1
    Validates: Requirements 12.2, 12.4, 18.3
    """
    name = derive_env_name(stage, region)

    # Shape: every stage uses its reconciled ``stage.value`` label.
    normalized_region = region.strip().lower()
    assert name == f"{stage.value}.{normalized_region}.{HELLODJ_ZONE}"

    # Every produced name is a strict subdomain of the zone.
    assert is_subdomain_of_zone(name)
    assert name.endswith(f".{HELLODJ_ZONE}")
    assert len(name) > len(f".{HELLODJ_ZONE}")


@settings(max_examples=200)
@given(
    stage=st.sampled_from(list(DeploymentStage)),
    region=dns_region_labels(),
)
def test_subdomain_includes_both_stage_and_region(
    stage: DeploymentStage, region: str
) -> None:
    """The derived name is a zone subdomain that includes both stage and region.

    Property 11: when a stage and a region are both provided, the derived name
    is a strict subdomain of ``hellodj.bot`` and its text contains both the
    reconciled stage name (``stage.value``) and the region as distinct labels.

    Feature: hellodj-nix-native-delivery, Property 11: DNS naming yields a zone
    subdomain that includes both stage and region
    Validates: Requirements 9.3
    """
    name = derive_env_name(stage, region)
    normalized_region = region.strip().lower()

    # Strict subdomain of the zone (R9.3).
    assert is_subdomain_of_zone(name)
    assert name.endswith(f".{HELLODJ_ZONE}")
    assert len(name) > len(f".{HELLODJ_ZONE}")

    # The name includes BOTH the reconciled stage name and the region — as
    # distinct labels, not merely as substrings (R9.3).
    labels = name.split(".")
    assert stage.value in labels
    assert normalized_region in labels

    # The reconciled stage name is a leading label and the region follows it,
    # both to the left of the zone (``<stage>.<region>.hellodj.bot``).
    zone_labels = HELLODJ_ZONE.split(".")
    prefix_labels = labels[: len(labels) - len(zone_labels)]
    assert prefix_labels == [stage.value, normalized_region]

    # No trace of the retired ``gamma`` identifier survives in any derived name.
    assert "gamma" not in name


@settings(max_examples=200)
@given(region=dns_region_labels())
def test_prod_apex_alias_exists(region: str) -> None:
    """Production names alias to the bare ``hellodj.bot`` apex.

    Feature: aws-saas-replatform, Property 1
    Validates: Requirements 12.3
    """
    prod_name = derive_env_name(DeploymentStage.PRODUCTION, region)
    apex = apex_alias_target()

    # The apex alias target is the bare zone, and the prod name is a distinct
    # subdomain that aliases to it (R12.3).
    assert apex == HELLODJ_ZONE
    assert prod_name != apex
    assert is_subdomain_of_zone(prod_name, apex)


@settings(max_examples=200)
@given(
    stage=st.sampled_from(list(DeploymentStage)),
    region_a=dns_region_labels(),
    region_b=dns_region_labels(),
)
def test_no_cross_region_name_collisions(
    stage: DeploymentStage, region_a: str, region_b: str
) -> None:
    """Distinct regions never yield the same environment name for a stage.

    Adding a region introduces only new, non-colliding names (R18.3): for a
    fixed stage, two normalized-distinct regions must map to distinct names,
    and equal normalized regions must map to identical names (determinism).

    Feature: aws-saas-replatform, Property 1
    Validates: Requirements 18.3
    """
    name_a = derive_env_name(stage, region_a)
    name_b = derive_env_name(stage, region_b)

    if region_a.strip().lower() == region_b.strip().lower():
        assert name_a == name_b
    else:
        assert name_a != name_b


@settings(max_examples=200)
@given(
    stage_a=st.sampled_from(list(DeploymentStage)),
    stage_b=st.sampled_from(list(DeploymentStage)),
    region=dns_region_labels(),
)
def test_no_cross_stage_collisions_within_region(
    stage_a: DeploymentStage, stage_b: DeploymentStage, region: str
) -> None:
    """Distinct stages in one region never collide (R12.2 vs R12.4).

    Feature: aws-saas-replatform, Property 1
    Validates: Requirements 12.2, 12.4
    """
    name_a = derive_env_name(stage_a, region)
    name_b = derive_env_name(stage_b, region)

    if stage_a is stage_b:
        assert name_a == name_b
    else:
        assert name_a != name_b

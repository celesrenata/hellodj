"""DNS environment-name derivation for the HelloDJ Route 53 zone.

This module is the single source of truth for how a deployment stage and an
AWS region map to a DNS name under the ``hellodj.bot`` zone (Requirement 9).
Both the CDK edge stack and the runtime components import these helpers so the
infrastructure-as-code layer and the application agree on every name.

Naming scheme (Property 11 / R9):
    * Every stage -> ``<stage>.<region>.hellodj.bot``
      (e.g. ``beta.us-east-1.hellodj.bot``, ``staging.us-east-1.hellodj.bot``,
      ``production.us-east-1.hellodj.bot``). The name is always a strict
      subdomain of the zone that includes both the reconciled stage name and
      the region (R9.3).
    * The apex alias target is the bare zone ``hellodj.bot``; a CNAME/alias is
      created from the production environment name to the apex.

A stage and a region are both required (R9.4, R9.5): invoking the derivation
with either one missing or empty raises an error indicating that both a stage
and a region are required, and no DNS name is returned.

The scheme is region-parameterized, so adding a region only introduces new,
non-colliding ``<stage>.<region>.hellodj.bot`` names with no redesign.
Every derived name is asserted to be a subdomain of :data:`HELLODJ_ZONE`.

Requirements: 9.2, 9.3, 9.4, 9.5
"""

from __future__ import annotations

import re

from .types import HELLODJ_ZONE, DeploymentStage

# A DNS label per RFC 1035: 1-63 chars, alphanumeric plus internal hyphens.
# Regions (e.g. ``us-east-1``) and stage names are validated against this so a
# malformed input can never produce a name that is not a clean subdomain of the
# zone.
_DNS_LABEL = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")

# Error message used whenever either the stage or the region is missing/empty
# (R9.4, R9.5). Both are required to derive a name.
_BOTH_REQUIRED_MESSAGE = "both a stage and a region are required to derive a DNS name"


def apex_alias_target() -> str:
    """Return the apex domain the production environment aliases to.

    The production environment name (``production.<region>.hellodj.bot``) is
    aliased to this bare zone via a Route 53 CNAME/alias record.
    """
    return HELLODJ_ZONE


def _normalize_region(region: str) -> str:
    """Validate and normalize an AWS region into a single DNS label.

    Raises:
        ValueError: if ``region`` is empty or not a valid DNS label.
    """
    normalized = region.strip().lower()
    if not normalized:
        raise ValueError(_BOTH_REQUIRED_MESSAGE)
    if not _DNS_LABEL.match(normalized):
        raise ValueError(f"region is not a valid DNS label: {region!r}")
    return normalized


def derive_env_name(stage: DeploymentStage, region: str) -> str:
    """Derive the DNS environment name for a stage in a region.

    Every stage resolves to ``<stage>.<region>.hellodj.bot`` — a strict
    subdomain of :data:`HELLODJ_ZONE` that includes both the reconciled stage
    name and the region (R9.3). Both a stage and a region are required; if
    either is missing or empty, no name is returned and an error indicating
    that both are required is raised (R9.4, R9.5).

    Args:
        stage: The deployment stage (Beta, Staging, or Production).
        region: The AWS region identifier (e.g. ``us-east-1``).

    Returns:
        The fully qualified DNS name for the environment.

    Raises:
        ValueError: if ``stage`` is not a :class:`DeploymentStage` or
            ``region`` is missing/empty (both a stage and a region are
            required), or if ``region`` is not a valid DNS label.
    """
    if not isinstance(stage, DeploymentStage):
        raise ValueError(_BOTH_REQUIRED_MESSAGE)

    region_label = _normalize_region(region)
    # ``stage.value`` is a validated lowercase label
    # ("beta"/"staging"/"production").
    name = f"{stage.value}.{region_label}.{HELLODJ_ZONE}"

    # Invariant (Property 11): every derived name is a subdomain of the zone
    # that includes both the stage and the region.
    assert is_subdomain_of_zone(name), (
        f"derived name {name!r} is not a subdomain of {HELLODJ_ZONE!r}"
    )
    return name


def is_subdomain_of_zone(name: str, zone: str = HELLODJ_ZONE) -> bool:
    """Return whether ``name`` is a strict subdomain of ``zone``.

    A strict subdomain has at least one additional label to the left of the
    zone (``beta.us-east-1.hellodj.bot`` is a subdomain of ``hellodj.bot``; the
    bare zone itself is not).
    """
    suffix = f".{zone}"
    return name.endswith(suffix) and len(name) > len(suffix)

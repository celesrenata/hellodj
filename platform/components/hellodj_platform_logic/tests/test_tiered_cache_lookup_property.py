"""Property-based tests for the tiered cache-lookup decision (tasks 4.2, 4.3).

Feature: hellodj-private-source-and-toolchain, Property 3 & Property 4

These two properties exercise :func:`hellodj_platform_logic.binary_cache.
tiered_cache_lookup`, the pure decision function that composes the per-builder
``Local_Nix_Cache`` tier *in front of* the existing S3 binary cache. It decides,
purely from three presence/integrity facts -- whether a required closure is
present in the local tier, whether that local copy passes store-path integrity
verification, and whether the closure is present in S3 -- which tier serves the
closure (LOCAL_HIT / S3_HIT / BUILD) and whether the local tier is (re)populated
and the closure pushed to S3.

The design decision table (design.md §4) is:

    local_present | local_integrity_ok | s3_present | decision
    --------------+--------------------+------------+----------
    yes           | yes                | --         | LOCAL_HIT (no populate, no push)
    yes           | no                 | yes        | S3_HIT    (populate local)
    yes           | no                 | no         | BUILD     (populate local, push S3)
    no            | --                 | yes        | S3_HIT    (populate local)
    no            | --                 | no         | BUILD     (populate local, push S3)

Property 3 (task 4.2) -- tiered lookup ordering: a locally-usable closure is a
LOCAL_HIT with no rebuild and no S3 fetch; otherwise an S3-present closure is an
S3_HIT that repopulates the local tier; otherwise a BUILD populates local and
pushes S3. A reusable (present + integrity-valid) local closure is never rebuilt.
Validates: Requirements 4.2, 4.3, 4.4, 4.9.

Property 4 (task 4.3) -- integrity fallthrough: whenever a local closure is
present but fails integrity verification, the result is NEVER LOCAL_HIT; it is
treated as absent and falls through to S3_HIT (when present in S3) or BUILD.
Validates: Requirement 4.5.
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from hellodj_platform_logic.binary_cache import tiered_cache_lookup
from hellodj_platform_logic.types import CacheTier, CacheTierResolution


@settings(max_examples=200)
@given(
    local_present=st.booleans(),
    local_integrity_ok=st.booleans(),
    s3_present=st.booleans(),
)
def test_tiered_lookup_ordering(
    local_present: bool,
    local_integrity_ok: bool,
    s3_present: bool,
) -> None:
    """Feature: hellodj-private-source-and-toolchain, Property 3: tiered lookup ordering.

    A locally-usable closure resolves to LOCAL_HIT (no populate, no push); a
    closure not usable locally but present in S3 resolves to S3_HIT (local
    repopulated, S3 not written); a closure usable at neither tier resolves to
    BUILD (local populated, S3 pushed). A present + integrity-valid local closure
    is never rebuilt.

    Validates: Requirements 4.2, 4.3, 4.4, 4.9
    """
    result = tiered_cache_lookup(local_present, local_integrity_ok, s3_present)
    assert isinstance(result, CacheTierResolution)

    # A local closure is only usable when present AND integrity-valid.
    local_usable = local_present and local_integrity_ok

    if local_usable:
        # LOCAL_HIT: reuse locally -- no rebuild, no S3 fetch, no local
        # repopulation, no S3 push (R4.2/R4.4).
        assert result.tier is CacheTier.LOCAL_HIT
        assert result.populated_local is False, (
            "a LOCAL_HIT must not (re)populate the local tier"
        )
        assert result.pushed_s3 is False, (
            "a LOCAL_HIT must not fetch from or push to S3"
        )
    elif s3_present:
        # S3_HIT: not usable locally, but in S3 -- fetch from S3 and repopulate
        # the local tier; S3 is not written (R4.3/R4.5).
        assert result.tier is CacheTier.S3_HIT
        assert result.populated_local is True, (
            "an S3_HIT must repopulate the local tier"
        )
        assert result.pushed_s3 is False, (
            "an S3_HIT must not push back to S3"
        )
    else:
        # BUILD: usable at neither tier -- build, populate local, push S3
        # (R4.9).
        assert result.tier is CacheTier.BUILD
        assert result.populated_local is True, (
            "a BUILD must populate the local tier"
        )
        assert result.pushed_s3 is True, (
            "a BUILD must push the built closure to S3"
        )

    # Cross-cutting invariant: a reusable local closure (present + integrity-ok)
    # is the ONLY case that neither rebuilds nor fetches -- it is never rebuilt.
    if local_usable:
        assert result.tier is CacheTier.LOCAL_HIT
    else:
        # Every non-reusable case must consult a further tier (S3 or a build);
        # it is never a LOCAL_HIT.
        assert result.tier is not CacheTier.LOCAL_HIT


@settings(max_examples=200)
@given(
    local_present=st.booleans(),
    s3_present=st.booleans(),
)
def test_integrity_fallthrough(local_present: bool, s3_present: bool) -> None:
    """Feature: hellodj-private-source-and-toolchain, Property 4: integrity fallthrough.

    Whenever a local closure is present but fails integrity verification
    (``local_integrity_ok`` is False), the result is NEVER LOCAL_HIT: the corrupt
    local closure is treated as absent and falls through to S3_HIT (when present
    in S3) or BUILD (otherwise).

    Validates: Requirement 4.5
    """
    # Integrity always fails here -- the defining condition of the fallthrough.
    result = tiered_cache_lookup(local_present, local_integrity_ok=False, s3_present=s3_present)

    # A corrupt (or absent) local closure is never a LOCAL_HIT.
    assert result.tier is not CacheTier.LOCAL_HIT, (
        "a local closure that fails integrity must never yield LOCAL_HIT"
    )

    if s3_present:
        # Falls through to S3 and repopulates the local tier.
        assert result.tier is CacheTier.S3_HIT
        assert result.populated_local is True
        assert result.pushed_s3 is False
    else:
        # Falls through to a rebuild that populates local and pushes S3.
        assert result.tier is CacheTier.BUILD
        assert result.populated_local is True
        assert result.pushed_s3 is True

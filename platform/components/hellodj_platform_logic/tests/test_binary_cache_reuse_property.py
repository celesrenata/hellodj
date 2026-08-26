"""Property-based test for the binary-cache build-once identity (task 4.3).

Feature: hellodj-nix-native-delivery, Property 5

Property 5 (Build-once identity -- every stage resolves the same
store-path-hash and reuses it): *for any* artifact closure whose store-path hash
is present in the binary cache, each of the three deployment stages (Beta,
Staging, Production) resolving that *same* closure reference SHALL resolve the
identical store-path hash H, report the closure present in the cache, and reuse
it without a rebuild or halt. Because the resolution keys purely on the shared
store-path hash, an identical closure is built once and reused across all three
stages (R7.2/7.3).

Validates: Requirements 7.2, 7.3
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from hellodj_platform_logic.binary_cache import resolve_closure
from hellodj_platform_logic.types import ClosureRef, DeploymentStage

# The Nix store-path hash segment: the build-once identity key every stage
# shares. Kept to a lowercase-alnum charset (nixbase32-like) so datasets are
# cheap; the resolution reasons purely over set membership of this hash, never
# over its exact text, so any non-empty label exercises the logic.
_HASH = st.text(
    alphabet=st.characters(min_codepoint=ord("a"), max_codepoint=ord("z")),
    min_size=1,
    max_size=16,
)

# A closure "name" segment used to build a plausible /nix/store path; irrelevant
# to the decision but keeps the ref realistic.
_NAME = st.text(
    alphabet=st.characters(min_codepoint=ord("a"), max_codepoint=ord("z")),
    min_size=1,
    max_size=12,
)

# The three deployment stages that must all resolve the same closure identically.
_STAGES: list[DeploymentStage] = list(DeploymentStage)


@st.composite
def artifact_with_populated_cache(
    draw: st.DrawFn,
) -> tuple[ClosureRef, set[str]]:
    """Generate a closure ref plus a cache set that *contains* its hash.

    Returns ``(ref, cache_contents)`` where ``ref.store_path_hash`` is guaranteed
    to be a member of ``cache_contents`` (alongside arbitrary other hashes), so
    the closure is present -- the precondition of Property 5 (the artifact has
    been built once and pushed). This spans populated caches of varying size that
    all include the target hash.
    """
    store_path_hash = draw(_HASH)
    name = draw(_NAME)
    ref = ClosureRef(
        store_path=f"/nix/store/{store_path_hash}-{name}",
        store_path_hash=store_path_hash,
    )

    # Arbitrary other hashes present in the cache (may be empty), always plus the
    # target hash so the closure is present for every stage.
    other_hashes = draw(st.sets(_HASH, max_size=8))
    cache_contents = other_hashes | {store_path_hash}
    return ref, cache_contents


@settings(max_examples=200)
@given(scenario=artifact_with_populated_cache())
def test_all_stages_resolve_same_hash_and_reuse(
    scenario: tuple[ClosureRef, set[str]],
) -> None:
    """Feature: hellodj-nix-native-delivery, Property 5.

    Validates: Requirements 7.2, 7.3
    """
    ref, cache_contents = scenario

    # Resolve the *same* closure reference for each of the three stages. The
    # decision is stage-agnostic (keyed on the store-path hash), so passing the
    # identical ref models each stage pulling the artifact by its hash.
    resolutions = {stage: resolve_closure(ref, cache_contents) for stage in _STAGES}

    # --- Every stage resolves the identical store-path hash H -------------
    resolved_hashes = {
        stage: resolution.requested.store_path_hash
        for stage, resolution in resolutions.items()
    }
    assert set(resolved_hashes.values()) == {ref.store_path_hash}, (
        "all three stages must resolve the identical store-path hash"
    )

    # --- Every stage reuses the present closure without rebuild or halt ----
    for stage, resolution in resolutions.items():
        assert resolution.requested == ref
        assert resolution.present_in_cache is True, (
            f"{stage.value} must find the closure present in the cache (reuse)"
        )
        assert resolution.halt is False, (
            f"{stage.value} must not halt when the closure is present (reuse, no rebuild)"
        )

    # --- The resolution is identical across all three stages --------------
    # Build-once/deploy-thrice: the same closure yields the same resolution for
    # Beta, Staging, and Production -- no per-stage rebuild.
    distinct_resolutions = set(resolutions.values())
    assert len(distinct_resolutions) == 1, (
        "the same closure must resolve identically for all three stages"
    )

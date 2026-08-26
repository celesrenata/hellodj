"""Property-based test for the missing-closure halt invariant.

Feature: hellodj-nix-native-delivery, Property 6

Property 6 (A missing required closure halts the stage without substitution):
    For any required closure whose store-path hash is absent from the binary
    cache, ``binary_cache.resolve_closure`` never reports the closure as present
    and never proceeds: it halts the stage, surfaces the missing store path in
    the reason, and substitutes no artifact from any non-cache source (R7.4).

Validates: Requirements 7.4
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from hellodj_platform_logic.binary_cache import resolve_closure
from hellodj_platform_logic.types import ClosureRef

# The <hash> segment of a Nix store path is a 32-char base-32 string; any
# non-empty token from this alphabet is a faithful stand-in for the identity key.
_NIX_HASH_CHARS = "0123456789abcdfghijklmnpqrsvwxyz"


@st.composite
def store_path_hashes(draw: st.DrawFn) -> str:
    """Generate arbitrary non-empty Nix store-path hash segments."""
    return draw(
        st.text(alphabet=_NIX_HASH_CHARS, min_size=1, max_size=32)
    )


@st.composite
def closure_refs(draw: st.DrawFn) -> ClosureRef:
    """Generate a ``ClosureRef`` with a distinct store path and hash segment."""
    hash_seg = draw(store_path_hashes())
    name = draw(
        st.text(
            alphabet="abcdefghijklmnopqrstuvwxyz-0123456789",
            min_size=1,
            max_size=24,
        )
    )
    return ClosureRef(
        store_path=f"/nix/store/{hash_seg}-{name}",
        store_path_hash=hash_seg,
    )


@st.composite
def absent_cache_contents(draw: st.DrawFn, ref: ClosureRef) -> set[str]:
    """Generate a cache-contents set that never contains ``ref``'s hash."""
    others = draw(
        st.sets(
            store_path_hashes().filter(lambda h: h != ref.store_path_hash),
            max_size=8,
        )
    )
    # Defensive: the filter guarantees exclusion, but assert the invariant the
    # property depends on.
    assert ref.store_path_hash not in others
    return others


@settings(max_examples=200)
@given(ref=closure_refs(), data=st.data())
def test_missing_closure_halts_without_substitution(
    ref: ClosureRef, data: st.DataObject
) -> None:
    """A required closure absent from the cache halts the stage (no substitution).

    The resolution reports ``present_in_cache=False`` and ``halt=True``, and the
    missing store path is surfaced in the reason so the operator can identify the
    closure that stopped the stage. No artifact is drawn from any non-cache
    source (R7.4).

    Feature: hellodj-nix-native-delivery, Property 6
    Validates: Requirements 7.4
    """
    cache_contents = data.draw(absent_cache_contents(ref))

    resolution = resolve_closure(ref, cache_contents)

    # Absent from the cache -> never reported present.
    assert resolution.present_in_cache is False
    # Absent -> the stage halts (R7.4).
    assert resolution.halt is True
    # The resolution refers back to exactly the requested closure.
    assert resolution.requested == ref
    # The missing store path is surfaced so it can be identified.
    assert ref.store_path in resolution.reason


@settings(max_examples=200)
@given(ref=closure_refs())
def test_empty_cache_always_halts(ref: ClosureRef) -> None:
    """With an empty cache, every required closure halts without substitution.

    This pins the boundary case (no closure present at all) that a stage deploy
    faces before any artifact has been pushed (R7.4).

    Feature: hellodj-nix-native-delivery, Property 6
    Validates: Requirements 7.4
    """
    resolution = resolve_closure(ref, set())

    assert resolution.present_in_cache is False
    assert resolution.halt is True
    assert ref.store_path in resolution.reason

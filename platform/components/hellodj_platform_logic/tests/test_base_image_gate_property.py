"""Property test for the non-Nix base-image gate (task 4.8).

Property 6 (design "Non-Nix base-image gate", R5.4): for any container image
base descriptor, the build-stage base-image gate SHALL reject the image if and
only if the base was *not* produced by the Nix build system (for example an
Ubuntu or Debian base), and SHALL accept it only when it is Nix-produced.

The design (and the implementation under test) additionally hardens the gate so
that a well-known non-Nix base name (``ubuntu``/``debian``) is rejected
*regardless* of the ``nix_produced`` flag -- a mislabelled descriptor claiming a
Debian/Ubuntu base is Nix-produced can never slip past. Matching is
case-insensitive and tolerant of a registry prefix, tag, or digest
(``docker.io/library/Ubuntu:22.04``, ``debian@sha256:...``).

The gate function is pure, so the property is exercised directly. Generators mix
three input families -- Nix-like names (``nix-store-xxxx``, ``scratch``, ...),
forbidden Ubuntu/Debian names decorated with random registry prefixes and
tags/digests, and arbitrary text -- crossed with arbitrary ``nix_produced``
booleans, so both the accept and reject branches (including the
forbidden-base-with-``nix_produced=True`` case) are covered (>=100 iterations).

Feature: aws-saas-replatform, Property 6

Validates: Requirements 5.4

Cross-reference (reused, not rewritten):

Feature: hellodj-nix-native-delivery, Property 3: Base-image gate accepts iff
Nix-produced and not a forbidden base

Validates: Requirements 5.5
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from hellodj_platform_logic.base_image_gate import (
    FORBIDDEN_BASE_NAMES,
    check_base,
)
from hellodj_platform_logic.types import BaseImageDescriptor


def _is_forbidden_base(base_name: str) -> bool:
    """Reference oracle: does ``base_name`` name an Ubuntu/Debian base?

    Independent re-derivation of the "forbidden base" rule (strip registry
    prefix, then any ``@digest`` and ``:tag`` suffix, compare case-insensitively)
    so the test does not simply mirror the implementation's private helper.
    """
    final_segment = base_name.strip().rsplit("/", maxsplit=1)[-1]
    identifier = final_segment.split("@", maxsplit=1)[0]
    identifier = identifier.split(":", maxsplit=1)[0]
    return identifier.casefold() in FORBIDDEN_BASE_NAMES


# --- Smart generators over the base_name input space ------------------------

# Nix-like / clearly-not-forbidden bases: nix store paths, scratch, distroless.
_nix_like_names = st.one_of(
    st.just("scratch"),
    st.just("distroless"),
    st.builds(
        lambda h: f"nix-store-{h}",
        st.text(alphabet="0123456789abcdfghijklmnpqrsvwxyz", min_size=4, max_size=12),
    ),
    st.builds(
        lambda h: f"/nix/store/{h}-image",
        st.text(alphabet="0123456789abcdfghijklmnpqrsvwxyz", min_size=8, max_size=16),
    ),
)

# Forbidden Ubuntu/Debian bases decorated with optional registry prefix and an
# optional tag or digest, in mixed case -- all of which must still be rejected.
_registry_prefixes = st.sampled_from(
    ["", "library/", "docker.io/library/", "docker.io/", "registry.example.com/"]
)
_forbidden_cores = st.sampled_from(
    ["ubuntu", "Ubuntu", "UBUNTU", "debian", "Debian", "DEBIAN"]
)
_tag_or_digest = st.sampled_from(
    ["", ":22.04", ":latest", ":bookworm", "@sha256:" + "a" * 64]
)
_forbidden_names = st.builds(
    lambda prefix, core, suffix: f"{prefix}{core}{suffix}",
    _registry_prefixes,
    _forbidden_cores,
    _tag_or_digest,
)

# Arbitrary text (may occasionally, by chance, be a forbidden base -- the oracle
# handles classification either way).
_arbitrary_names = st.text(max_size=40)

_base_names = st.one_of(_nix_like_names, _forbidden_names, _arbitrary_names)


@settings(max_examples=300)
@given(base_name=_base_names, nix_produced=st.booleans())
def test_non_nix_base_image_gate(base_name: str, nix_produced: bool) -> None:
    """Accept iff Nix-produced and not a forbidden Ubuntu/Debian base.

    Feature: aws-saas-replatform, Property 6

    Validates: Requirements 5.4
    """
    descriptor = BaseImageDescriptor(base_name=base_name, nix_produced=nix_produced)
    result = check_base(descriptor)

    forbidden = _is_forbidden_base(base_name)
    expected_accept = nix_produced and not forbidden

    # Core biconditional: accept iff Nix-produced and base is not forbidden.
    assert result.accepted is expected_accept

    if expected_accept:
        # Accepted images carry no rejection reason.
        assert result.reason == ""
    else:
        # Every rejection explains itself for the build log.
        assert result.reason != ""

    # A forbidden base is rejected even when nix_produced is (mistakenly) True.
    if forbidden:
        assert result.accepted is False


@settings(max_examples=200)
@given(base_name=_forbidden_names, nix_produced=st.booleans())
def test_forbidden_base_rejected_regardless_of_flag(
    base_name: str, nix_produced: bool
) -> None:
    """Ubuntu/Debian bases are rejected regardless of the nix_produced flag.

    This isolates the hardening rule: a descriptor claiming a Debian/Ubuntu base
    is Nix-produced still fails the gate (Property 6, R5.2/R5.3/R5.4).

    Feature: aws-saas-replatform, Property 6

    Validates: Requirements 5.4
    """
    descriptor = BaseImageDescriptor(base_name=base_name, nix_produced=nix_produced)
    result = check_base(descriptor)
    assert result.accepted is False
    assert result.reason != ""


@settings(max_examples=200)
@given(base_name=_nix_like_names)
def test_nix_produced_non_forbidden_base_accepted(base_name: str) -> None:
    """A Nix-produced, non-forbidden base is always accepted.

    Feature: aws-saas-replatform, Property 6

    Validates: Requirements 5.4
    """
    descriptor = BaseImageDescriptor(base_name=base_name, nix_produced=True)
    result = check_base(descriptor)
    assert result.accepted is True
    assert result.reason == ""

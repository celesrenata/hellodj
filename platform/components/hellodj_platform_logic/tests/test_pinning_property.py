"""Property-based test for the pin-verification decision function.

Feature: hellodj-nix-native-delivery, Property 13
Feature: hellodj-private-source-and-toolchain, Property 1

Property 13 / Property 1 (Pin verification accepts equal identifiers and
otherwise retains the prior pin): *for any* captured pin -- whether derived from
a legacy ``github:owner/repo/branch`` input (:class:`FlakeInputPin`) *or* from a
private CodeCommit input (:class:`CodeCommitInput`, resolving to
``git+https://git-codecommit.<region>.amazonaws.com/v1/repos/<repo>?ref=<branch>``)
-- and any resolved upstream identifier (including ``None`` when the upstream
source could not be resolved), ``verify_pin`` SHALL:

* **accept iff equal** -- return ``accepted=True`` if and only if the upstream
  identifier is resolved *and* equals the pinned identifier (R11.1 / R3.5);
* **reject + name on mismatch** -- when the upstream is resolved but differs,
  return ``accepted=False`` with a non-empty ``reason`` naming exactly this
  input, the prior pinned revision retained by the caller (R11.5 / R3.6, R6.5);
* **fail + name on unresolved** -- when the upstream is ``None``, return
  ``accepted=False`` with a non-empty ``reason`` naming exactly this input, the
  prior pinned revision retained by the caller (R11.6 / R3.7, R6.4);

and in every case the returned ``input_name`` mirrors the pin's ``input_name``.

``verify_pin`` is form-agnostic: it reasons purely over the pinned identifier
versus the resolved upstream identifier, never over the input's *form*. This
test therefore extends the original Property 13 generation so the same pinned
identifier can equally originate from a CodeCommit input (Property 1, this spec)
without modifying ``verify_pin`` at all: a :class:`CodeCommitInput` is projected
onto the :class:`FlakeInputPin` contract ``verify_pin`` consumes, carrying its
``input_name`` and ``pinned_identifier`` through unchanged.

Validates: Requirements 11.1, 11.5, 11.6, 3.5, 3.6, 3.7, 6.4, 6.5
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from hellodj_platform_logic.pinning import verify_pin
from hellodj_platform_logic.types import (
    CodeCommitInput,
    FlakeInputPin,
    PinVerification,
)

# Identifier text (revisions/tags/versions). A small charset keeps datasets
# cheap while still producing plenty of equal and differing pairs; the decision
# reasons purely over equality, never over the text content.
_IDENTIFIERS = st.text(
    alphabet=st.characters(min_codepoint=ord("a"), max_codepoint=ord("z")),
    min_size=1,
    max_size=8,
)

# Input names. Kept non-empty; used to assert the affected input is named in
# both failure paths.
_INPUT_NAMES = st.text(
    alphabet=st.characters(min_codepoint=ord("a"), max_codepoint=ord("z")),
    min_size=1,
    max_size=8,
)

# AWS-region-shaped tokens for CodeCommit inputs. Any non-empty token works --
# the pin verification never inspects the region -- but keeping it region-shaped
# documents the CodeCommit provenance of the generated pin.
_REGIONS = st.sampled_from(
    ["us-east-1", "us-west-2", "eu-west-1", "eu-central-1", "ap-southeast-2"]
)


@st.composite
def github_pins(draw: st.DrawFn) -> FlakeInputPin:
    """Generate a legacy ``github:owner/repo/branch`` pin (Property 13)."""
    return FlakeInputPin(
        input_name=draw(_INPUT_NAMES),
        owner="hellodj",
        repo="repo",
        branch="main",
        pinned_identifier=draw(_IDENTIFIERS),
    )


@st.composite
def codecommit_pins(draw: st.DrawFn) -> FlakeInputPin:
    """Generate a pin *derived from* a CodeCommit input (Property 1, this spec).

    A :class:`CodeCommitInput` is built and then projected onto the
    :class:`FlakeInputPin` contract ``verify_pin`` consumes, carrying its
    ``input_name`` and ``pinned_identifier`` through verbatim. This proves the
    pin-verification decision is form-agnostic: a CodeCommit-provenance pin is
    verified by the *unchanged* ``verify_pin`` exactly as a github pin is.
    """
    cc = CodeCommitInput(
        input_name=draw(_INPUT_NAMES),
        region=draw(_REGIONS),
        repo=draw(
            st.sampled_from(
                ["hellodj", "Lavalink", "lavaplayer", "LavaSrc", "youtube-source"]
            )
        ),
        branch=draw(st.sampled_from(["main", "dev", "tidal-v2-api"])),
        pinned_identifier=draw(_IDENTIFIERS),
    )
    # verify_pin is form-agnostic: it reasons over input_name/pinned_identifier
    # only, so the CodeCommit input is carried through the FlakeInputPin shape
    # it consumes without changing verify_pin. The owner/repo/branch fields on
    # FlakeInputPin describe the (github) form and are irrelevant to the
    # decision; they are populated from the CodeCommit repo/branch purely so the
    # pin is well-formed.
    return FlakeInputPin(
        input_name=cc.input_name,
        owner="hellodj",
        repo=cc.repo,
        branch=cc.branch,
        pinned_identifier=cc.pinned_identifier,
    )


@st.composite
def pin_scenarios(
    draw: st.DrawFn,
) -> tuple[FlakeInputPin, str | None]:
    """Generate a captured pin plus an upstream identifier to verify against.

    The captured pin is drawn from *either* form -- a legacy github input or a
    CodeCommit-derived input -- so the property quantifies over both input forms
    while ``verify_pin`` stays unchanged (Property 13 + Property 1).

    The upstream identifier spans the full input space the property quantifies
    over: it is drawn as either ``None`` (unresolved upstream, R11.6/R3.7/R6.4),
    a value equal to the pinned identifier (accept, R11.1/R3.5), or an
    independently drawn value (which may or may not equal the pin -- covering
    both mismatch, R11.5/R3.6/R6.5, and the equal case again).
    """
    pin = draw(st.one_of(github_pins(), codecommit_pins()))
    upstream = draw(
        st.one_of(
            st.none(),
            st.just(pin.pinned_identifier),
            _IDENTIFIERS,
        )
    )
    return pin, upstream


@settings(max_examples=200)
@given(scenario=pin_scenarios())
def test_pin_verification_accepts_equal_and_retains_prior_pin(
    scenario: tuple[FlakeInputPin, str | None],
) -> None:
    """Feature: hellodj-nix-native-delivery, Property 13.

    Feature: hellodj-private-source-and-toolchain, Property 1: Pin verification
    accepts equal identifiers and otherwise retains the prior pin.

    Validates: Requirements 11.1, 11.5, 11.6, 3.5, 3.6, 3.7, 6.4, 6.5
    """
    pin, upstream = scenario

    result = verify_pin(pin, upstream)

    assert isinstance(result, PinVerification)

    # --- The affected input is always identified --------------------------
    assert result.input_name == pin.input_name

    # --- Accept iff resolved-and-equal (R11.1 / R3.5) ---------------------
    expected_accepted = upstream is not None and upstream == pin.pinned_identifier
    assert result.accepted is expected_accepted

    if expected_accepted:
        # Accepted pin carries the resolved (matching) upstream identifier and
        # an empty reason.
        assert result.upstream_identifier == upstream
        assert result.reason == ""
        return

    # --- Both failure paths: rejected/failed, prior pin retained ----------
    # accepted is False and a reason is populated naming exactly this input so
    # the caller can retain the prior pinned revision (R3.6/R3.7, R6.4/R6.5).
    assert result.accepted is False
    assert result.reason != ""
    assert repr(pin.input_name) in result.reason

    if upstream is None:
        # Failed on unresolved upstream (R11.6 / R3.7, R6.4): the outcome
        # carries None.
        assert result.upstream_identifier is None
    else:
        # Rejected on mismatch (R11.5 / R3.6, R6.5): the resolved (differing)
        # upstream identifier is carried through.
        assert upstream != pin.pinned_identifier
        assert result.upstream_identifier == upstream

"""Property-based test for the pin-verification decision function (task 6.3).

Feature: hellodj-nix-native-delivery, Property 13

Property 13 (Pin verification accepts equal identifiers and otherwise retains
the prior pin): *for any* captured :class:`FlakeInputPin` and any resolved
upstream identifier (including ``None`` when the upstream source could not be
resolved), ``verify_pin`` SHALL:

* **accept iff equal** -- return ``accepted=True`` if and only if the upstream
  identifier is resolved *and* equals the pinned identifier (R11.1);
* **reject + name on mismatch** -- when the upstream is resolved but differs,
  return ``accepted=False`` with a non-empty ``reason`` naming exactly this
  input, the prior pinned revision retained by the caller (R11.5);
* **fail + name on unresolved** -- when the upstream is ``None``, return
  ``accepted=False`` with a non-empty ``reason`` naming exactly this input, the
  prior pinned revision retained by the caller (R11.6);

and in every case the returned ``input_name`` mirrors the pin's ``input_name``.

Validates: Requirements 11.1, 11.5, 11.6
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from hellodj_platform_logic.pinning import verify_pin
from hellodj_platform_logic.types import FlakeInputPin, PinVerification

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


@st.composite
def pin_scenarios(
    draw: st.DrawFn,
) -> tuple[FlakeInputPin, str | None]:
    """Generate a ``FlakeInputPin`` plus an upstream identifier to verify against.

    The upstream identifier spans the full input space the property quantifies
    over: it is drawn as either ``None`` (unresolved upstream, R11.6), a value
    equal to the pinned identifier (accept, R11.1), or an independently drawn
    value (which may or may not equal the pin -- covering both mismatch, R11.5,
    and the equal case again).
    """
    input_name = draw(_INPUT_NAMES)
    pinned_identifier = draw(_IDENTIFIERS)
    pin = FlakeInputPin(
        input_name=input_name,
        owner="hellodj",
        repo="repo",
        branch="main",
        pinned_identifier=pinned_identifier,
    )
    upstream = draw(
        st.one_of(
            st.none(),
            st.just(pinned_identifier),
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

    Validates: Requirements 11.1, 11.5, 11.6
    """
    pin, upstream = scenario

    result = verify_pin(pin, upstream)

    assert isinstance(result, PinVerification)

    # --- The affected input is always identified --------------------------
    assert result.input_name == pin.input_name

    # --- Accept iff resolved-and-equal (R11.1) ----------------------------
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
    # the caller can retain the prior pinned revision.
    assert result.accepted is False
    assert result.reason != ""
    assert repr(pin.input_name) in result.reason

    if upstream is None:
        # Failed on unresolved upstream (R11.6): the outcome carries None.
        assert result.upstream_identifier is None
    else:
        # Rejected on mismatch (R11.5): the resolved (differing) upstream
        # identifier is carried through.
        assert upstream != pin.pinned_identifier
        assert result.upstream_identifier == upstream

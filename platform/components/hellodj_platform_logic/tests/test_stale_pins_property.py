"""Property-based test for the stale-pin report decision function (task 6.2).

Feature: hellodj-private-source-and-toolchain, Property 6: The stale-pin report
lists exactly the pins whose pinned identifier differs from upstream

Property 6 (R6.1): *for any* mapping of captured
:class:`~hellodj_platform_logic.types.FlakeInputPin` entries keyed by
``input_name`` and *any* mapping of current upstream identifiers (each value a
resolved identifier or ``None``, keys possibly missing entirely),
``stale_pins(pins, upstream)`` SHALL return exactly the entries
:func:`~hellodj_platform_logic.pinning.verify_pin` would *reject* -- the entries
whose upstream is resolved (non-``None``) *and* differs from the pinned
identifier -- and each such entry SHALL carry both the pinned identifier and the
current upstream identifier it differs from. Concretely, over the full input
space:

* an entry whose resolved upstream **equals** the pinned identifier is **not**
  in the report (``verify_pin`` accepts it);
* an entry whose resolved upstream **differs** from the pinned identifier **is**
  in the report, carrying both identifiers;
* an entry whose upstream is ``None`` -- or whose key is **missing** from the
  upstream mapping -- is **not** in the report (an unresolved upstream is a
  resolution failure, ``verify_pin`` *fails* rather than rejects, so it is
  excluded);
* the report order follows the ``pins`` iteration order.

Validates: Requirements 6.1
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from hellodj_platform_logic.pinning import verify_pin
from hellodj_platform_logic.stale_pins import stale_pins
from hellodj_platform_logic.types import FlakeInputPin, StalePin

# Identifier text (revisions/tags/versions). A small charset keeps datasets
# cheap while still producing plenty of equal and differing pairs; the decision
# reasons purely over equality, never over the text content.
_IDENTIFIERS = st.text(
    alphabet=st.characters(min_codepoint=ord("a"), max_codepoint=ord("z")),
    min_size=1,
    max_size=8,
)

# Input names (the ``pins.toml`` keys). Kept non-empty and drawn unique per
# mapping so ``pins`` is a well-formed keyed mapping.
_INPUT_NAMES = st.text(
    alphabet=st.characters(min_codepoint=ord("a"), max_codepoint=ord("z")),
    min_size=1,
    max_size=8,
)

# The four upstream-state classes the property quantifies over, per generated
# entry. Each is tagged so the test can assert the expected membership.
_EQUAL = "equal"          # upstream resolved, equals pinned  -> accept, excluded
_DIFFERS = "differs"      # upstream resolved, differs         -> reject, reported
_UNRESOLVED = "none"      # upstream present but None           -> fail, excluded
_MISSING = "missing"      # key absent from upstream mapping    -> fail, excluded


@st.composite
def pin_and_upstream_maps(
    draw: st.DrawFn,
) -> tuple[dict[str, FlakeInputPin], dict[str, str | None], dict[str, str]]:
    """Generate a ``pins`` mapping, an ``upstream`` mapping, and the expected classes.

    Produces a set of uniquely-named pins and, for each, independently assigns
    one of the four upstream-state classes (:data:`_EQUAL`, :data:`_DIFFERS`,
    :data:`_UNRESOLVED`, :data:`_MISSING`) spanning the full input space:

    * :data:`_EQUAL` -- ``upstream[name] == pinned_identifier`` (accept, excluded);
    * :data:`_DIFFERS` -- ``upstream[name]`` is a value guaranteed unequal to the
      pinned identifier (reject, reported with both identifiers);
    * :data:`_UNRESOLVED` -- ``upstream[name] is None`` (fail, excluded);
    * :data:`_MISSING` -- ``name`` absent from the upstream mapping (fail, excluded).

    Returns the ``pins`` mapping (insertion order preserved, so iteration order
    is deterministic), the ``upstream`` mapping, and a per-name class map the
    test uses to compute the expected report.
    """
    names = draw(
        st.lists(_INPUT_NAMES, min_size=0, max_size=6, unique=True)
    )

    pins: dict[str, FlakeInputPin] = {}
    upstream: dict[str, str | None] = {}
    classes: dict[str, str] = {}

    for name in names:
        pinned = draw(_IDENTIFIERS)
        pins[name] = FlakeInputPin(
            input_name=name,
            owner="hellodj",
            repo="repo",
            branch="main",
            pinned_identifier=pinned,
        )

        kind = draw(
            st.sampled_from([_EQUAL, _DIFFERS, _UNRESOLVED, _MISSING])
        )
        classes[name] = kind

        if kind == _EQUAL:
            upstream[name] = pinned
        elif kind == _DIFFERS:
            # A value guaranteed to differ from the pinned identifier: append a
            # sentinel char so it can never collide with the pinned value.
            other = draw(_IDENTIFIERS)
            upstream[name] = other + "X" if other == pinned else other
            assert upstream[name] != pinned
        elif kind == _UNRESOLVED:
            upstream[name] = None
        # _MISSING: intentionally do not add the key to ``upstream``.

    return pins, upstream, classes


@settings(max_examples=200)
@given(scenario=pin_and_upstream_maps())
def test_stale_pins_lists_exactly_the_pins_differing_from_upstream(
    scenario: tuple[dict[str, FlakeInputPin], dict[str, str | None], dict[str, str]],
) -> None:
    """Feature: hellodj-private-source-and-toolchain, Property 6.

    Validates: Requirements 6.1
    """
    pins, upstream, classes = scenario

    report = stale_pins(pins, upstream)

    # Every entry is a StalePin.
    assert all(isinstance(entry, StalePin) for entry in report)

    # --- Expected stale set == exactly what verify_pin would reject ---------
    # Independently recompute the expected report from verify_pin so the test
    # anchors on the specification (accept iff resolved-and-equal; reject on a
    # resolved mismatch; fail/exclude on unresolved) rather than re-implementing
    # stale_pins.
    expected: list[StalePin] = []
    for name, pin in pins.items():  # pins iteration order == report order
        upstream_identifier = upstream.get(name)
        verification = verify_pin(pin, upstream_identifier)
        if not verification.accepted and verification.upstream_identifier is not None:
            expected.append(
                StalePin(
                    input_name=name,
                    pinned_identifier=pin.pinned_identifier,
                    upstream_identifier=verification.upstream_identifier,
                )
            )

    # --- Report order follows pins iteration order, exact-match report ------
    assert report == expected

    reported_names = {entry.input_name for entry in report}

    for name, pin in pins.items():
        kind = classes[name]
        matching = [e for e in report if e.input_name == name]

        if kind == _DIFFERS:
            # Differing upstream IS in the report, carrying BOTH identifiers.
            assert name in reported_names
            assert len(matching) == 1
            entry = matching[0]
            assert entry.pinned_identifier == pin.pinned_identifier
            assert entry.upstream_identifier == upstream[name]
            # The report entry carries the differing upstream, never the pin.
            assert entry.upstream_identifier != entry.pinned_identifier
        elif kind == _EQUAL:
            # Equal upstream is NOT in the report.
            assert name not in reported_names
        else:
            # Unresolved (None) or missing key: excluded as a resolution failure.
            assert kind in (_UNRESOLVED, _MISSING)
            assert name not in reported_names

    # --- Report contains no name absent from pins ---------------------------
    assert reported_names <= set(pins)

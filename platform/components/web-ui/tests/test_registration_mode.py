"""Property-based tests for the pure registration-mode helper (tasks 1.1, 1.2).

These exercise the side-effect-free contract in ``registration_mode`` that both
the ``auth.register`` enforcement gate and the login banner import so display
and enforcement never drift. No AWS, Flask, or Cognito — the module is pure by
design.

Feature: registration-mode-control
Property 1: Secure default for absent or invalid values (1.1).
Property 2: Valid stored value passes through (1.2).

Validates: Requirements 1.1, 1.2, 1.3
"""

from __future__ import annotations

from typing import Any

from hypothesis import assume, given, settings
from hypothesis import strategies as st

from registration_mode import (
    CLOSED,
    CONFIG_KEY,
    OPEN,
    VALID_MODES,
    current_mode,
    normalize_mode,
)

# --- Property 1: secure default for absent or invalid values ----------------

# Arbitrary raw values that are NOT a valid mode string: None, ints, floats,
# booleans, collections, and random strings excluding the valid modes.
_invalid_raw = st.one_of(
    st.none(),
    st.integers(),
    st.floats(allow_nan=True, allow_infinity=True),
    st.booleans(),
    st.lists(st.integers(), max_size=3),
    st.dictionaries(st.text(max_size=3), st.integers(), max_size=3),
    st.text(max_size=20),
)


def _is_valid_mode_string(value: Any) -> bool:
    """Return whether ``value`` would normalize to a valid mode (not CLOSED-by-fallback)."""
    return isinstance(value, str) and value.strip().upper() in VALID_MODES


@settings(max_examples=200)
@given(raw=_invalid_raw)
def test_property1_normalize_invalid_is_closed(raw: Any) -> None:
    """Property 1 — any value that is not exactly a valid mode string
    normalizes to CLOSED.

    Feature: registration-mode-control, Property 1: Secure default for absent or
    invalid values.

    Validates: Requirements 1.1, 1.3
    """
    assume(not _is_valid_mode_string(raw))
    assert normalize_mode(raw) == CLOSED


@settings(max_examples=200)
@given(raw=_invalid_raw)
def test_property1_current_mode_invalid_is_closed(raw: Any) -> None:
    """Property 1 — a config payload whose stored value is not a valid mode
    (including the missing-key/None cases) yields CLOSED.

    Feature: registration-mode-control, Property 1: Secure default for absent or
    invalid values.

    Validates: Requirements 1.1, 1.3
    """
    assume(not _is_valid_mode_string(raw))
    assert current_mode({CONFIG_KEY: raw}) == CLOSED


@settings(max_examples=100)
@given(config=st.one_of(st.none(), st.dictionaries(st.text(max_size=5), st.text(max_size=5))))
def test_property1_missing_key_is_closed(config: dict[str, Any] | None) -> None:
    """Property 1 — an absent :data:`CONFIG_KEY` (empty/None payload) is CLOSED.

    Feature: registration-mode-control, Property 1: Secure default for absent or
    invalid values.

    Validates: Requirements 1.1
    """
    assume(CONFIG_KEY not in (config or {}))
    assert current_mode(config) == CLOSED


# --- Property 2: valid stored value passes through --------------------------


def _with_casing_and_whitespace(draw: Any, base: str) -> str:
    """Return ``base`` with random casing and surrounding whitespace applied."""
    cased = "".join(
        ch.upper() if draw(st.booleans()) else ch.lower() for ch in base
    )
    lead = draw(st.text(alphabet=" \t\n\r", max_size=4))
    trail = draw(st.text(alphabet=" \t\n\r", max_size=4))
    return f"{lead}{cased}{trail}"


@st.composite
def _valid_mode_variants(draw: Any) -> tuple[str, str]:
    """Generate a (raw, canonical) pair: a valid mode with random casing and
    surrounding whitespace, plus its expected canonical upper-case form."""
    canonical = draw(st.sampled_from(VALID_MODES))
    raw = _with_casing_and_whitespace(draw, canonical)
    return raw, canonical


@settings(max_examples=200)
@given(variant=_valid_mode_variants())
def test_property2_normalize_passthrough(variant: tuple[str, str]) -> None:
    """Property 2 — a valid mode in any casing/whitespace normalizes to its
    canonical upper-case form.

    Feature: registration-mode-control, Property 2: Valid stored value passes
    through.

    Validates: Requirements 1.2
    """
    raw, canonical = variant
    assert normalize_mode(raw) == canonical
    assert canonical in (OPEN, CLOSED)


@settings(max_examples=200)
@given(variant=_valid_mode_variants())
def test_property2_current_mode_passthrough(variant: tuple[str, str]) -> None:
    """Property 2 — a config payload storing a valid mode (any casing/whitespace)
    reports that mode in canonical upper-case form.

    Feature: registration-mode-control, Property 2: Valid stored value passes
    through.

    Validates: Requirements 1.2
    """
    raw, canonical = variant
    assert current_mode({CONFIG_KEY: raw}) == canonical

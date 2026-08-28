"""Property-based tests for the pure entitlements decision module (task 1.1).

These exercise the side-effect-free contract in ``entitlements_core`` that both
the web-ui and the bot depend on. Six named correctness properties from the
design are covered with Hypothesis, alongside a handful of concrete example
edge cases (defaults indication, disabled-but-stored>1, over-cap equality,
default-markup doubling) that make the intent unmistakable.

No AWS, Flask, or Discord — the module is pure by design.

Property 1: merge is defaults-safe and never more permissive than default.
Property 2: ``source_allowed`` matches the effective ``sources`` map.
Property 3: ``quota_reached`` / ``effective_max_bots_per_guild`` edges
            (incl. disabled-but-stored>1).
Property 4: ``validate_quota`` rejects values < 1.
Property 5: ``effective_cost`` = bedrock x (1 + markup); 2x at the default.
Property 6: ``over_cap`` true on equality.

Validates: Requirements 13.1, 13.2, 13.3, 3.2, 11.3, 11.4, 12.2, 12.3, 10.1,
10.2, 10.5
"""

from __future__ import annotations

import math
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from entitlements_core import (
    DEFAULT_ENTITLEMENTS,
    DEFAULT_MARKUP,
    effective_cost,
    effective_max_bots_per_guild,
    merge_effective,
    over_cap,
    quota_reached,
    source_allowed,
    validate_quota,
)

PROVIDERS = list(DEFAULT_ENTITLEMENTS["sources"].keys())

# The boolean-flag fields (everything except sources / numeric quotas / cap).
BOOL_FLAGS = [
    "custom_avatar",
    "custom_name",
    "audio_above_96k",
    "video_activities",
    "visualizations",
    "wakeword",
    "ai_integration",
    "max_bots_per_guild_enabled",
]


def _is_more_permissive(field: str, value: Any, default: Any) -> bool:
    """Return True when ``value`` grants more than ``default`` for ``field``.

    Used only to assert that an *absent* field never resolves to something more
    permissive than the secure default. For booleans, True is more permissive
    than False. For numeric quotas, a larger number is more permissive. For the
    spend cap, ``None`` (no cap) is more permissive than any finite cap.
    """
    if isinstance(default, bool):
        return bool(value) and not default
    if field in ("max_bots_per_guild", "max_guilds"):
        return int(value) > int(default)
    if field == "ai_spend_cap":
        return default is not None and value is None
    return False


# --- strategies -------------------------------------------------------------

_sources_strategy = st.dictionaries(
    keys=st.sampled_from(PROVIDERS),
    values=st.booleans(),
    max_size=len(PROVIDERS),
)

_stored_strategy = st.fixed_dictionaries(
    {},
    optional={
        "sources": _sources_strategy,
        "custom_avatar": st.booleans(),
        "custom_name": st.booleans(),
        "audio_above_96k": st.booleans(),
        "video_activities": st.booleans(),
        "visualizations": st.booleans(),
        "wakeword": st.booleans(),
        "ai_integration": st.booleans(),
        "max_bots_per_guild": st.integers(min_value=1, max_value=50),
        "max_bots_per_guild_enabled": st.booleans(),
        "max_guilds": st.integers(min_value=1, max_value=50),
        "ai_spend_cap": st.one_of(st.none(), st.floats(min_value=0, max_value=1e6)),
    },
)


# --- Property 1: merge is defaults-safe --------------------------------------


@settings(max_examples=300)
@given(stored=st.one_of(st.none(), _stored_strategy))
def test_property1_merge_defaults_safe(stored: dict[str, Any] | None) -> None:
    """Property 1 — present fields equal the stored value; absent fields equal
    their default and are never more permissive than the default.

    Validates: Requirements 2.2, 13.1, 13.2, 13.3
    """
    effective = merge_effective(stored)
    stored_map = stored or {}

    for field, default in DEFAULT_ENTITLEMENTS.items():
        if field == "sources":
            continue
        if field in stored_map:
            assert effective[field] == stored_map[field]
        else:
            assert effective[field] == default
            assert not _is_more_permissive(field, effective[field], default)

    # sources: per-key merge — present providers keep their stored value,
    # absent providers fall back to (never more permissive than) the default.
    stored_sources = stored_map.get("sources", {})
    for provider, default_on in DEFAULT_ENTITLEMENTS["sources"].items():
        if provider in stored_sources:
            assert effective["sources"][provider] == stored_sources[provider]
        else:
            assert effective["sources"][provider] == default_on
            assert not (effective["sources"][provider] and not default_on)


def test_property1_none_yields_defaults_copy() -> None:
    """A ``None`` record yields the defaults, and mutating the result does not
    corrupt the shared ``DEFAULT_ENTITLEMENTS`` constant."""
    effective = merge_effective(None)
    assert effective == DEFAULT_ENTITLEMENTS
    effective["sources"]["youtube"] = True
    effective["ai_integration"] = True
    assert DEFAULT_ENTITLEMENTS["sources"]["youtube"] is False
    assert DEFAULT_ENTITLEMENTS["ai_integration"] is False


def test_property1_defaults_are_restrictive() -> None:
    """R13.2 — custom identity and the other gated flags default to restricted."""
    assert DEFAULT_ENTITLEMENTS["custom_avatar"] is False
    assert DEFAULT_ENTITLEMENTS["custom_name"] is False
    for flag in BOOL_FLAGS:
        assert DEFAULT_ENTITLEMENTS[flag] is False


# --- Property 2: source gate matches effective map ---------------------------


@settings(max_examples=200)
@given(stored=st.one_of(st.none(), _stored_strategy), provider=st.sampled_from(PROVIDERS))
def test_property2_source_allowed_matches_map(
    stored: dict[str, Any] | None, provider: str
) -> None:
    """Property 2 — ``source_allowed`` is exactly the effective map value.

    Validates: Requirements 3.2, 3.3, 3.4
    """
    effective = merge_effective(stored)
    assert source_allowed(effective, provider) == bool(effective["sources"][provider])


@given(provider=st.text(max_size=12))
def test_property2_unknown_provider_denied(provider: str) -> None:
    """An unknown provider is denied (secure by default)."""
    from hypothesis import assume

    assume(provider not in PROVIDERS)
    assert source_allowed(merge_effective(None), provider) is False


# --- Property 3: quota edges -------------------------------------------------


@settings(max_examples=300)
@given(
    current=st.integers(min_value=0, max_value=100),
    limit=st.integers(min_value=1, max_value=100),
)
def test_property3_quota_reached(current: int, limit: int) -> None:
    """Property 3 (quota_reached) — reached iff ``current >= limit``.

    Validates: Requirements 11.2, 12.3
    """
    assert quota_reached(current, limit) == (current >= limit)


@settings(max_examples=300)
@given(
    stored_value=st.integers(min_value=1, max_value=50),
    enabled=st.booleans(),
)
def test_property3_effective_max_bots(stored_value: int, enabled: bool) -> None:
    """Property 3 (effective_max_bots_per_guild) — enabled uses the stored
    value; disabled-but-stored>1 still uses the stored value; else 1.

    Validates: Requirements 11.3, 11.4
    """
    effective = merge_effective(
        {"max_bots_per_guild": stored_value, "max_bots_per_guild_enabled": enabled}
    )
    result = effective_max_bots_per_guild(effective)
    if enabled:
        assert result == stored_value
    elif stored_value > 1:
        assert result == stored_value
    else:
        assert result == 1


def test_property3_disabled_but_stored_gt_one_edge() -> None:
    """Explicit edge: disabled marker with a provisioned value > 1 still
    applies the stored value (R11.3)."""
    effective = merge_effective(
        {"max_bots_per_guild": 4, "max_bots_per_guild_enabled": False}
    )
    assert effective_max_bots_per_guild(effective) == 4


def test_property3_default_is_one() -> None:
    """R11.4 — the baseline per-guild bot limit is 1."""
    assert effective_max_bots_per_guild(merge_effective(None)) == 1


# --- Property 4: validate_quota rejects < 1 ----------------------------------


@settings(max_examples=200)
@given(value=st.integers(min_value=1, max_value=10**6))
def test_property4_validate_quota_accepts_ge_one(value: int) -> None:
    """Property 4 — values >= 1 return unchanged.

    Validates: Requirements 12.2
    """
    assert validate_quota(value) == value


@settings(max_examples=200)
@given(value=st.integers(max_value=0))
def test_property4_validate_quota_rejects_lt_one(value: int) -> None:
    """Property 4 — values < 1 raise ``ValueError``.

    Validates: Requirements 12.2
    """
    with pytest.raises(ValueError):
        validate_quota(value)


# --- Property 5: effective cost with markup ----------------------------------


@settings(max_examples=300)
@given(
    bedrock=st.floats(min_value=0, max_value=1e6, allow_nan=False, allow_infinity=False),
    markup=st.floats(min_value=0, max_value=10, allow_nan=False, allow_infinity=False),
)
def test_property5_effective_cost(bedrock: float, markup: float) -> None:
    """Property 5 — cost == bedrock * (1 + markup).

    Validates: Requirements 10.1, 10.2
    """
    assert math.isclose(
        effective_cost(bedrock, markup), bedrock * (1.0 + markup), rel_tol=1e-9, abs_tol=1e-12
    )


@settings(max_examples=200)
@given(bedrock=st.floats(min_value=0, max_value=1e6, allow_nan=False, allow_infinity=False))
def test_property5_default_markup_doubles(bedrock: float) -> None:
    """Property 5 — with the default markup (1.0) the cost is 2x bedrock.

    Validates: Requirements 10.2
    """
    assert DEFAULT_MARKUP == 1.0
    assert math.isclose(effective_cost(bedrock), 2.0 * bedrock, rel_tol=1e-9, abs_tol=1e-12)


# --- Property 6: over-cap on equality ----------------------------------------


@settings(max_examples=300)
@given(
    accumulated=st.floats(min_value=0, max_value=1e6, allow_nan=False, allow_infinity=False),
    cap=st.one_of(
        st.none(),
        st.floats(min_value=0, max_value=1e6, allow_nan=False, allow_infinity=False),
    ),
)
def test_property6_over_cap(accumulated: float, cap: float | None) -> None:
    """Property 6 — over-cap iff a cap is set and ``accumulated >= cap``.

    Validates: Requirements 10.5
    """
    expected = cap is not None and accumulated >= cap
    assert over_cap(accumulated, cap) is expected


@settings(max_examples=200)
@given(cap=st.floats(min_value=0, max_value=1e6, allow_nan=False, allow_infinity=False))
def test_property6_equality_counts_as_over_cap(cap: float) -> None:
    """Property 6 — equality counts as over-cap.

    Validates: Requirements 10.5
    """
    assert over_cap(cap, cap) is True


def test_property6_no_cap_never_over() -> None:
    """No configured cap is never over-cap regardless of the tally."""
    assert over_cap(1e12, None) is False

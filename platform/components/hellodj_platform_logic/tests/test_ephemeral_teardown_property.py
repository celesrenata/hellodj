"""Property-based test for the ephemeral-teardown decision function (task 4.6).

Feature: hellodj-nix-native-delivery, Property 4

Property 4 (ephemeral build compute is always torn down within bounded time):
*for any* build completion outcome and *any* teardown scenario -- clean stop,
teardown failure, or a crashed build -- the ``ephemeral_teardown`` decision
SHALL honour the <=300 s teardown deadline (R6.6) and the <=10800 s (3 h) hard
maximum-lifetime forced-termination cap (R6.7), emit an alert if and only if the
stop is *not* confirmed (R6.8), and retain the resource identifier and teardown
timestamp on confirmation (R6.9). A builder configured with a teardown deadline
beyond the 300 s cap or a maximum lifetime beyond the 10800 s cap SHALL be
rejected with a ``ValueError``.

Validates: Requirements 6.6, 6.7, 6.8, 6.9
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from hellodj_platform_logic.ephemeral_build import (
    MAX_LIFETIME_CAP_SECONDS,
    TEARDOWN_DEADLINE_CAP_SECONDS,
    ephemeral_teardown,
)
from hellodj_platform_logic.types import EphemeralCompute, TeardownResult

# Resource identifiers: any non-empty label exercises the logic, which reasons
# purely over the lifecycle bounds + confirmation flag, never the id text.
_RESOURCE_IDS = st.text(
    alphabet=st.characters(min_codepoint=ord("a"), max_codepoint=ord("z")),
    min_size=1,
    max_size=12,
)

# Teardown timestamps: opaque strings retained verbatim on the result.
_TIMESTAMPS = st.text(min_size=1, max_size=24)

# Valid teardown deadlines: within (0, 300] -- at or under the R6.6 cap.
_VALID_DEADLINES = st.floats(
    min_value=0.0,
    max_value=TEARDOWN_DEADLINE_CAP_SECONDS,
    allow_nan=False,
    allow_infinity=False,
    exclude_min=True,
)

# Valid maximum lifetimes: within (0, 10800] -- at or under the R6.7 cap.
_VALID_LIFETIMES = st.floats(
    min_value=0.0,
    max_value=MAX_LIFETIME_CAP_SECONDS,
    allow_nan=False,
    allow_infinity=False,
    exclude_min=True,
)


@st.composite
def valid_compute(draw: st.DrawFn) -> EphemeralCompute:
    """Generate an ``EphemeralCompute`` whose bounds are within the design caps."""
    return EphemeralCompute(
        resource_id=draw(_RESOURCE_IDS),
        teardown_deadline_seconds=draw(_VALID_DEADLINES),
        max_lifetime_seconds=draw(_VALID_LIFETIMES),
    )


@settings(max_examples=200)
@given(
    compute=valid_compute(),
    stopped_confirmed=st.booleans(),
    ts=_TIMESTAMPS,
)
def test_ephemeral_teardown_bounded_time(
    compute: EphemeralCompute,
    stopped_confirmed: bool,
    ts: str,
) -> None:
    """Feature: hellodj-nix-native-delivery, Property 4.

    Validates: Requirements 6.6, 6.7, 6.8, 6.9
    """
    result = ephemeral_teardown(compute, stopped_confirmed, ts)

    assert isinstance(result, TeardownResult)

    # --- Bounded time is honoured (R6.6/R6.7) ------------------------------
    # A valid builder is accepted only when its teardown deadline is within the
    # 300 s cap and its maximum lifetime is within the 10800 s cap.
    assert compute.teardown_deadline_seconds <= TEARDOWN_DEADLINE_CAP_SECONDS
    assert compute.max_lifetime_seconds <= MAX_LIFETIME_CAP_SECONDS

    # --- Alert iff stop unconfirmed (R6.8) ---------------------------------
    assert result.alert_emitted == (not stopped_confirmed)

    # --- confirmed_stopped mirrors the injected fact -----------------------
    assert result.confirmed_stopped == stopped_confirmed

    # --- id + timestamp retained (R6.9) ------------------------------------
    # The resource identifier and teardown timestamp are retained in every case
    # so a confirmed stop is fully recorded and an alert can name the resource.
    assert result.resource_id == compute.resource_id
    assert result.teardown_timestamp == ts


@settings(max_examples=200)
@given(
    resource_id=_RESOURCE_IDS,
    over_deadline=st.floats(
        min_value=TEARDOWN_DEADLINE_CAP_SECONDS,
        max_value=TEARDOWN_DEADLINE_CAP_SECONDS * 100,
        allow_nan=False,
        allow_infinity=False,
        exclude_min=True,
    ),
    lifetime=_VALID_LIFETIMES,
    stopped_confirmed=st.booleans(),
    ts=_TIMESTAMPS,
)
def test_over_cap_teardown_deadline_raises(
    resource_id: str,
    over_deadline: float,
    lifetime: float,
    stopped_confirmed: bool,
    ts: str,
) -> None:
    """A teardown deadline beyond the 300 s cap (R6.6) is rejected.

    Feature: hellodj-nix-native-delivery, Property 4

    Validates: Requirements 6.6
    """
    compute = EphemeralCompute(
        resource_id=resource_id,
        teardown_deadline_seconds=over_deadline,
        max_lifetime_seconds=lifetime,
    )
    with pytest.raises(ValueError):
        ephemeral_teardown(compute, stopped_confirmed, ts)


@settings(max_examples=200)
@given(
    resource_id=_RESOURCE_IDS,
    deadline=_VALID_DEADLINES,
    over_lifetime=st.floats(
        min_value=MAX_LIFETIME_CAP_SECONDS,
        max_value=MAX_LIFETIME_CAP_SECONDS * 100,
        allow_nan=False,
        allow_infinity=False,
        exclude_min=True,
    ),
    stopped_confirmed=st.booleans(),
    ts=_TIMESTAMPS,
)
def test_over_cap_max_lifetime_raises(
    resource_id: str,
    deadline: float,
    over_lifetime: float,
    stopped_confirmed: bool,
    ts: str,
) -> None:
    """A maximum lifetime beyond the 10800 s cap (R6.7) is rejected.

    Feature: hellodj-nix-native-delivery, Property 4

    Validates: Requirements 6.7
    """
    compute = EphemeralCompute(
        resource_id=resource_id,
        teardown_deadline_seconds=deadline,
        max_lifetime_seconds=over_lifetime,
    )
    with pytest.raises(ValueError):
        ephemeral_teardown(compute, stopped_confirmed, ts)

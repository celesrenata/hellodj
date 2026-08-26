"""Property-based test for the GPU scale-to-zero decision function (task 5.2).

Feature: hellodj-nix-native-delivery, Property 8

Property 8 (GPU scales to zero exactly when idle beyond the window with no
active work): *for any* validated :class:`GpuIdleConfig`, continuous idle
elapsed time, and active-job count, ``gpu_idle_decision`` SHALL return
scale-to-zero *if and only if* there are zero active jobs *and* the elapsed
idle time is at least the configured idle window (R8.5); it SHALL never scale
to zero while a GPU-requiring workload is present (``active_jobs > 0``, R8.6).
In addition, a :class:`GpuIdleConfig` constructed with an idle window outside
the inclusive ``[60, 900]``-second range SHALL be rejected at construction.

Validates: Requirements 8.5, 8.6
"""

from __future__ import annotations

import math

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from hellodj_platform_logic.gpu_idle import gpu_idle_decision
from hellodj_platform_logic.types import GpuIdleConfig

# Idle windows strictly inside the valid inclusive range [60, 900] so a
# GpuIdleConfig can always be constructed for the iff-condition assertions.
_VALID_WINDOWS = st.floats(
    min_value=60.0,
    max_value=900.0,
    allow_nan=False,
    allow_infinity=False,
)

# Continuous idle elapsed time. Spans below, at, and above any window in the
# valid range (including 0 and values well past 900) so the >= boundary is
# exercised on both sides.
_ELAPSED = st.floats(
    min_value=0.0,
    max_value=2000.0,
    allow_nan=False,
    allow_infinity=False,
)

# Active transcode job counts: zero (permits scale-to-zero) and positive
# (active work, forbids it). Negatives are included because the function treats
# any non-positive count as "no active work" (active_jobs <= 0).
_ACTIVE_JOBS = st.integers(min_value=-5, max_value=100)

# Idle windows strictly OUTSIDE the valid [60, 900] range, both below and above,
# used to assert construction rejection.
_OUT_OF_RANGE_WINDOWS = st.one_of(
    st.floats(
        min_value=-1000.0,
        max_value=59.0,
        allow_nan=False,
        allow_infinity=False,
        exclude_max=False,
    ),
    st.floats(
        min_value=901.0,
        max_value=100000.0,
        allow_nan=False,
        allow_infinity=False,
    ),
)


@settings(max_examples=200)
@given(window=_VALID_WINDOWS, idle_elapsed_s=_ELAPSED, active_jobs=_ACTIVE_JOBS)
def test_gpu_scales_to_zero_iff_idle_beyond_window_with_no_active_work(
    window: float,
    idle_elapsed_s: float,
    active_jobs: int,
) -> None:
    """Feature: hellodj-nix-native-delivery, Property 8.

    Validates: Requirements 8.5, 8.6
    """
    cfg = GpuIdleConfig(idle_window_seconds=window)

    result = gpu_idle_decision(cfg, idle_elapsed_s, active_jobs)

    # --- The exact iff-condition (R8.5) -----------------------------------
    # Scale-to-zero holds if and only if there is no active work AND the idle
    # time has reached at least the configured window.
    expected = active_jobs <= 0 and idle_elapsed_s >= window
    assert result is expected

    # --- Never scale to zero with active work (R8.6) ----------------------
    # Regardless of elapsed idle time, a present GPU-requiring workload forbids
    # scale-to-zero.
    if active_jobs > 0:
        assert result is False


@settings(max_examples=200)
@given(window=_OUT_OF_RANGE_WINDOWS)
def test_out_of_range_idle_window_is_rejected_at_construction(
    window: float,
) -> None:
    """Feature: hellodj-nix-native-delivery, Property 8.

    Validates: Requirements 8.5, 8.6
    """
    # A window outside [60, 900] must never yield a usable config; construction
    # itself rejects it so no out-of-range window can reach gpu_idle_decision.
    assert not (60.0 <= window <= 900.0) and not math.isnan(window)
    with pytest.raises(ValueError):
        GpuIdleConfig(idle_window_seconds=window)

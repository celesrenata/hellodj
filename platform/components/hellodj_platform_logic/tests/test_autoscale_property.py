"""Property test for the autoscaling decision function (task 5.2).

Property 7 (design "Autoscaling decision over CPU, RAM, and GPU pressure"):
for any triple of utilization readings ``(cpu, ram, gpu)`` and their configured
per-signal scale-out and scale-in thresholds, the decision function
(:func:`hellodj_platform_logic.autoscale.decide`) SHALL:

* decide :attr:`AutoscaleDecision.SCALE_OUT` when *any* signal strictly exceeds
  its scale-out threshold (R16.2-R16.4);
* decide :attr:`AutoscaleDecision.SCALE_IN` only when it is *not* scaling out
  and *all* signals are strictly below their scale-in thresholds (R16.5);
* otherwise :attr:`AutoscaleDecision.HOLD`; and
* be *monotonic* -- raising any single signal never downgrades a scale-out
  decision (once SCALE_OUT, increasing any of cpu/ram/gpu keeps it SCALE_OUT).

The decision function is pure, so the property is exercised directly over
utilization triples and thresholds generated with Hypothesis floats in
``[0.0, 1.0]`` (>=100 iterations). Thresholds are generated with per-signal
``scale_in <= scale_out`` to model sensible autoscaler configurations, and the
default-threshold path (scale out 70% / scale in 40%) is exercised too.

Feature: aws-saas-replatform, Property 7

Validates: Requirements 3.2, 3.3, 16.2, 16.3, 16.4, 16.5
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from hellodj_platform_logic.autoscale import decide
from hellodj_platform_logic.types import AutoscaleDecision, UtilizationReading

# Utilization / threshold values are fractions in [0.0, 1.0].
_fraction = st.floats(
    min_value=0.0,
    max_value=1.0,
    allow_nan=False,
    allow_infinity=False,
)


@st.composite
def _sensible_thresholds(draw: st.DrawFn) -> tuple[
    UtilizationReading, UtilizationReading
]:
    """Draw a (scale_out, scale_in) pair with per-signal scale_in <= scale_out.

    A sensible autoscaler config keeps each scale-in threshold at or below its
    scale-out threshold so a hold band exists between them (hysteresis). We draw
    the scale-out threshold first, then the scale-in threshold within
    ``[0, scale_out]`` per signal.
    """
    out_cpu = draw(_fraction)
    out_ram = draw(_fraction)
    out_gpu = draw(_fraction)
    in_cpu = draw(st.floats(0.0, out_cpu, allow_nan=False, allow_infinity=False))
    in_ram = draw(st.floats(0.0, out_ram, allow_nan=False, allow_infinity=False))
    in_gpu = draw(st.floats(0.0, out_gpu, allow_nan=False, allow_infinity=False))
    scale_out = UtilizationReading(cpu=out_cpu, ram=out_ram, gpu=out_gpu)
    scale_in = UtilizationReading(cpu=in_cpu, ram=in_ram, gpu=in_gpu)
    return scale_out, scale_in


def _expected(
    cpu: float,
    ram: float,
    gpu: float,
    scale_out: UtilizationReading,
    scale_in: UtilizationReading,
) -> AutoscaleDecision:
    """Reference implementation of the Property 7 rules for cross-checking."""
    any_over_out = (
        cpu > scale_out.cpu or ram > scale_out.ram or gpu > scale_out.gpu
    )
    all_under_in = (
        cpu < scale_in.cpu and ram < scale_in.ram and gpu < scale_in.gpu
    )
    if any_over_out:
        return AutoscaleDecision.SCALE_OUT
    if all_under_in:
        return AutoscaleDecision.SCALE_IN
    return AutoscaleDecision.HOLD


@settings(max_examples=300)
@given(
    cpu=_fraction,
    ram=_fraction,
    gpu=_fraction,
    thresholds=_sensible_thresholds(),
)
def test_autoscale_decision_rules(
    cpu: float,
    ram: float,
    gpu: float,
    thresholds: tuple[UtilizationReading, UtilizationReading],
) -> None:
    """scale-out iff any > out; scale-in iff (not out) and all < in; else hold.

    Feature: aws-saas-replatform, Property 7

    Validates: Requirements 3.2, 3.3, 16.2, 16.3, 16.4, 16.5
    """
    scale_out, scale_in = thresholds
    decision = decide(cpu, ram, gpu, scale_out, scale_in)

    any_over_out = (
        cpu > scale_out.cpu or ram > scale_out.ram or gpu > scale_out.gpu
    )
    all_under_in = (
        cpu < scale_in.cpu and ram < scale_in.ram and gpu < scale_in.gpu
    )

    # scale-out iff any signal strictly exceeds its scale-out threshold.
    assert (decision is AutoscaleDecision.SCALE_OUT) == any_over_out
    # scale-in iff not scaling out and all signals below scale-in thresholds.
    assert (decision is AutoscaleDecision.SCALE_IN) == (
        not any_over_out and all_under_in
    )
    # else hold.
    assert (decision is AutoscaleDecision.HOLD) == (
        not any_over_out and not all_under_in
    )
    # Cross-check against the reference rules.
    assert decision is _expected(cpu, ram, gpu, scale_out, scale_in)


@settings(max_examples=300)
@given(
    cpu=_fraction,
    ram=_fraction,
    gpu=_fraction,
    thresholds=_sensible_thresholds(),
    d_cpu=_fraction,
    d_ram=_fraction,
    d_gpu=_fraction,
)
def test_autoscale_monotonic_scale_out(
    cpu: float,
    ram: float,
    gpu: float,
    thresholds: tuple[UtilizationReading, UtilizationReading],
    d_cpu: float,
    d_ram: float,
    d_gpu: float,
) -> None:
    """Raising any signal never downgrades a scale-out decision.

    Starting from an arbitrary base triple, applying non-negative deltas to any
    subset of cpu/ram/gpu (clamped to <= 1.0) preserves a SCALE_OUT decision.

    Feature: aws-saas-replatform, Property 7

    Validates: Requirements 16.2, 16.3, 16.4
    """
    scale_out, scale_in = thresholds
    base = decide(cpu, ram, gpu, scale_out, scale_in)

    raised_cpu = min(1.0, cpu + d_cpu)
    raised_ram = min(1.0, ram + d_ram)
    raised_gpu = min(1.0, gpu + d_gpu)
    raised = decide(raised_cpu, raised_ram, raised_gpu, scale_out, scale_in)

    if base is AutoscaleDecision.SCALE_OUT:
        assert raised is AutoscaleDecision.SCALE_OUT


@settings(max_examples=200)
@given(cpu=_fraction, ram=_fraction, gpu=_fraction)
def test_autoscale_default_thresholds(
    cpu: float, ram: float, gpu: float
) -> None:
    """The default-threshold path obeys the 70% out / 40% in rules.

    Feature: aws-saas-replatform, Property 7

    Validates: Requirements 16.2, 16.3, 16.4, 16.5
    """
    decision = decide(cpu, ram, gpu)

    any_over_out = cpu > 0.70 or ram > 0.70 or gpu > 0.70
    all_under_in = cpu < 0.40 and ram < 0.40 and gpu < 0.40

    if any_over_out:
        assert decision is AutoscaleDecision.SCALE_OUT
    elif all_under_in:
        assert decision is AutoscaleDecision.SCALE_IN
    else:
        assert decision is AutoscaleDecision.HOLD

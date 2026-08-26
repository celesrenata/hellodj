"""Property-based test for the pipeline promotion controller.

Feature: hellodj-nix-native-delivery, Property 10: Promotion runs in fixed order
and halts on the first failure

Property 10 (promotion ordering and halt-on-failure): *for any* mapping of
per-stage deploy outcomes, the promotion controller realizes stages in the fixed
order Beta -> Staging -> Production; the first stage (Beta) is always deployed
with its own outcome; a stage is deployed only when every earlier stage
succeeded; and as soon as a deployed stage fails, promotion halts and every
later stage is recorded as SKIPPED and never deployed.

This test extends the original ``aws-saas-replatform`` Property 9 ``promote``
test to the reconciled stage names (Beta / Staging / Production), replacing the
prior Beta / Gamma / Prod labels.

Validates: Requirements 9.6, 10.3, 10.4, 10.5
"""

from __future__ import annotations

from collections.abc import Mapping

from hypothesis import given, settings
from hypothesis import strategies as st

from hellodj_platform_logic.promotion import PROMOTION_ORDER, promote
from hellodj_platform_logic.types import DeploymentStage, StageResult

# Per-stage inputs are only ever SUCCEEDED or FAILED; SKIPPED is a decision the
# controller makes, never an input (see promotion._validate_outcomes).
_INPUT_RESULTS = st.sampled_from([StageResult.SUCCEEDED, StageResult.FAILED])


@st.composite
def stage_outcome_maps(
    draw: st.DrawFn,
) -> Mapping[DeploymentStage, StageResult]:
    """Generate a complete SUCCEEDED/FAILED outcome for every stage.

    Every stage in :data:`PROMOTION_ORDER` is assigned an independent
    SUCCEEDED/FAILED outcome, spanning the full 2**3 = 8 input space of
    per-stage result sequences the property quantifies over.
    """
    return {stage: draw(_INPUT_RESULTS) for stage in PROMOTION_ORDER}


@settings(max_examples=200)
@given(outcomes=stage_outcome_maps())
def test_promotion_ordering_and_halt_on_failure(
    outcomes: Mapping[DeploymentStage, StageResult],
) -> None:
    """Feature: hellodj-nix-native-delivery, Property 10: Promotion runs in
    fixed order and halts on the first failure.

    Validates: Requirements 9.6, 10.3, 10.4, 10.5
    """
    realized = promote(outcomes)

    # The controller reports a realized result for every stage, and for no
    # stage outside the fixed pipeline.
    assert set(realized) == set(PROMOTION_ORDER)

    # --- Fixed order Beta -> Staging -> Production (R9.6, R10.3) -----------
    # PROMOTION_ORDER is the single source of truth for sequencing; pin it to
    # the mandatory Beta -> Staging -> Production order so the property cannot
    # pass under a reordered pipeline, and confirm zero GAMMA reference remains.
    assert PROMOTION_ORDER == (
        DeploymentStage.BETA,
        DeploymentStage.STAGING,
        DeploymentStage.PRODUCTION,
    )

    # --- Beta is always attempted (R10.5) ----------------------------------
    # Beta has no predecessor, so it is always deployed and carries its own
    # input outcome (never SKIPPED).
    assert realized[DeploymentStage.BETA] is outcomes[DeploymentStage.BETA]
    assert realized[DeploymentStage.BETA] is not StageResult.SKIPPED

    # Locate the first failing stage (if any) in pipeline order.
    first_failure_index: int | None = None
    for index, stage in enumerate(PROMOTION_ORDER):
        if realized[stage] is StageResult.FAILED:
            first_failure_index = index
            break

    for index, stage in enumerate(PROMOTION_ORDER):
        result = realized[stage]

        if first_failure_index is None:
            # No failure anywhere: every stage deployed and succeeded, matching
            # its input outcome (full Beta -> Staging -> Production promotion).
            assert result is StageResult.SUCCEEDED
            assert result is outcomes[stage]
            continue

        if index < first_failure_index:
            # --- No deploy without predecessor success (R10.3) -------------
            # Every stage before the first failure was deployed and its
            # predecessor(s) all succeeded, so it carries its actual outcome.
            assert result is StageResult.SUCCEEDED
            assert result is outcomes[stage]
        elif index == first_failure_index:
            # The first failing stage was deployed and recorded as FAILED.
            assert result is StageResult.FAILED
            assert result is outcomes[stage]
        else:
            # --- Halt on first failure (R10.4) -----------------------------
            # Every stage after the first failure is never deployed: it is
            # SKIPPED regardless of the outcome it would have produced.
            assert result is StageResult.SKIPPED

    # --- Predecessor-success invariant, stated directly (R10.3, R10.4) -----
    # Any stage that was actually deployed (SUCCEEDED or FAILED, i.e. not
    # SKIPPED) implies every earlier stage succeeded; and once a stage is
    # SKIPPED every later stage is SKIPPED too (halt is permanent).
    for index, stage in enumerate(PROMOTION_ORDER):
        if realized[stage] is not StageResult.SKIPPED:
            for predecessor in PROMOTION_ORDER[:index]:
                assert realized[predecessor] is StageResult.SUCCEEDED
        else:
            for later in PROMOTION_ORDER[index + 1:]:
                assert realized[later] is StageResult.SKIPPED

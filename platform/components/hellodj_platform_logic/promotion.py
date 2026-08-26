"""Pipeline promotion controller for the Beta -> Staging -> Production pipeline.

This module implements the pure decision logic behind the multi-stage
deployment pipeline (Requirement 10). It is deliberately free of any AWS or
CodePipeline dependency so that both the CDK ``pipeline-stack`` construct and
the property tests can import a single source of truth.

The controller answers one question: *given the deploy outcome each stage would
produce, in what order do stages actually run and where does promotion halt?*
It encodes three invariants drawn from the requirements and design:

* **Fixed order (R9.6, R10.3, R10.5).** Stages are always evaluated in the
  order Beta -> Staging -> Production, taken directly from the declaration
  order of :class:`~hellodj_platform_logic.types.DeploymentStage`.
* **No deploy without predecessor success (R10.3, R10.5).** A stage is only
  deployed when every earlier stage succeeded. The first stage (Beta) has no
  predecessor and is therefore always deployed.
* **Halt on first failure (R10.4).** Once a stage fails, promotion stops: that
  stage's failure is recorded and every later stage is marked
  :attr:`~hellodj_platform_logic.types.StageResult.SKIPPED` (never deployed).

Design references:
    * Deployment Pipeline (Beta -> Staging -> Production) and its
      halt-on-failure edges
    * Correctness Property 10 (pipeline promotion ordering and halt-on-failure)

Requirements: 9.2, 9.6, 10.3, 10.4, 10.5
"""

from __future__ import annotations

from collections.abc import Mapping

from hellodj_platform_logic.types import DeploymentStage, StageResult

__all__ = [
    "PROMOTION_ORDER",
    "PromotionError",
    "promote",
]

#: The fixed promotion order Beta -> Staging -> Production (R9.6). Derived from
#: the declaration order of :class:`DeploymentStage` so there is one source of
#: truth for the sequence.
PROMOTION_ORDER: tuple[DeploymentStage, ...] = tuple(
    sorted(DeploymentStage, key=lambda stage: stage.order)
)


class PromotionError(ValueError):
    """Raised when the promotion inputs are malformed.

    This signals a programming error in how the controller is invoked (for
    example an outcome given for a stage that does not exist), not a normal
    deploy failure. A deploy failure is expressed as
    :attr:`StageResult.FAILED` in ``stage_outcomes`` and handled by halting
    promotion, never by raising.
    """


def promote(
    stage_outcomes: Mapping[DeploymentStage, StageResult],
) -> dict[DeploymentStage, StageResult]:
    """Compute the realized per-stage result of one promotion run.

    The pipeline is walked in the fixed order Beta -> Staging -> Production.
    Each stage is deployed only if every predecessor succeeded; the outcome of a
    deployed stage is taken from ``stage_outcomes``. As soon as a deployed stage
    fails, promotion halts and every remaining stage is marked
    :attr:`StageResult.SKIPPED` without being deployed.

    Args:
        stage_outcomes: The deploy outcome each stage *would* produce if it were
            reached, keyed by :class:`DeploymentStage`. Every stage in
            :data:`PROMOTION_ORDER` must be present, and each value must be
            :attr:`StageResult.SUCCEEDED` or :attr:`StageResult.FAILED`
            (a caller cannot pre-declare a stage as ``SKIPPED``; skipping is a
            decision this controller makes).

    Returns:
        A mapping from every stage to its realized :class:`StageResult`: the
        stages that ran carry their actual outcome, and any stage that was
        never reached (because an earlier stage failed) carries
        :attr:`StageResult.SKIPPED`.

    Raises:
        PromotionError: If ``stage_outcomes`` is missing a stage, contains an
            unknown key, or supplies a ``SKIPPED`` outcome as an input.

    Requirements: 9.6, 10.3, 10.4, 10.5
    """
    _validate_outcomes(stage_outcomes)

    realized: dict[DeploymentStage, StageResult] = {}
    predecessor_succeeded = True

    for stage in PROMOTION_ORDER:
        if not predecessor_succeeded:
            # A prior stage failed: this stage is never deployed (R10.4).
            realized[stage] = StageResult.SKIPPED
            continue

        outcome = stage_outcomes[stage]
        realized[stage] = outcome
        if outcome is StageResult.FAILED:
            # Halt promotion; all later stages will be skipped (R10.4).
            predecessor_succeeded = False

    return realized


def _validate_outcomes(
    stage_outcomes: Mapping[DeploymentStage, StageResult],
) -> None:
    """Validate that ``stage_outcomes`` is a complete, well-formed input.

    Args:
        stage_outcomes: Candidate per-stage deploy outcomes to validate.

    Raises:
        PromotionError: If a stage is missing, an unexpected key is present, or
            a ``SKIPPED`` value is supplied as an input outcome.
    """
    expected = set(PROMOTION_ORDER)
    provided = set(stage_outcomes)

    missing = expected - provided
    if missing:
        names = ", ".join(sorted(stage.value for stage in missing))
        raise PromotionError(f"missing deploy outcome for stage(s): {names}")

    unexpected = provided - expected
    if unexpected:
        names = ", ".join(
            sorted(getattr(key, "value", repr(key)) for key in unexpected)
        )
        raise PromotionError(f"unknown stage(s) in outcomes: {names}")

    for stage in PROMOTION_ORDER:
        outcome = stage_outcomes[stage]
        if outcome is StageResult.SKIPPED:
            raise PromotionError(
                f"stage {stage.value!r} cannot be pre-declared SKIPPED; "
                "skipping is decided by the controller"
            )

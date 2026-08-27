#!/usr/bin/env python3
"""Cache push + verify-retrievable-before-available check (task 20.4).

This is the executable the verification harness (``tools/verify_all.py``) drives
to exercise the integration criteria that a built closure is **pushed to the
S3-backed Nix binary cache and confirmed retrievable there BEFORE it is marked
available for stage deployment** (Requirement 7.7), and that on a build the
closure is published to the cache and the image to the Container_Registry
(Requirement 6.2). It is the executable counterpart of the ``publish-cache`` job
in ``.github/workflows/nix-build.yml`` (task 16.2): that CI job performs the real
``nix store sign`` / ``nix copy --to s3://…`` / ``nix path-info`` narinfo
read-back / ``record_closure.py`` sequence; this tool models and checks that the
ORDERING is honored so a reviewer can run it locally without a real cache.

The ordered push→verify→available contract (R7.7 / R6.2)
--------------------------------------------------------

For every artifact, the publish path performs, strictly in order:

1. **sign** the closure with the cache's secret key (R7.1),
2. **push** (``nix copy --to``) the closure to the S3 cache (R7.2, R6.2),
3. **verify retrievable** — a ``narinfo`` read-back (``nix path-info --store``)
   confirming the pushed closure can be pulled back (R7.7),
4. **mark available** — record the build-once store-path hash in
   ``closures.toml`` (``record_closure.py``) so the deploy path pulls it (R7.3).

The single invariant this tool enforces (R7.7): **step 4 (mark available) never
happens for an artifact whose step 3 (verify retrievable) did not succeed.** If a
read-back fails, the artifact must NOT be recorded available, and the overall
check fails, naming the artifact whose closure was not retrievable.

Because ``tools/verify_all.py`` classifies a command as FAILED when it exits
non-zero OR its output contains a failure marker, this tool exits non-zero and
prints an ``error:`` marker naming the offending artifact on any violation, so
the harness's R12.7 aggregation reports the failing command + artifact.

Usage::

    python tools/verify_cache.py               # self-check the push→verify→available ordering
    python tools/verify_cache.py --list         # describe the modeled publish steps

Design references:
    * Requirement 7.7 — push the closure and confirm it is retrievable from the
      cache before the artifact is marked available for stage deployment.
    * Requirement 6.2 — the build path publishes closures to the cache and images
      to the registry.
    * Design §Testing Strategy — "Cache push+verify: push a closure and confirm
      read-back before marking available (7.7); closure published to cache and
      image to ECR on a build (6.2)."

Requirements: 6.2, 7.7
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

__all__ = [
    "PublishStep",
    "PublishOutcome",
    "ArtifactPublish",
    "publish_artifact",
    "verify_publish_plan",
    "main",
]


class PublishStep(StrEnum):
    """The ordered steps a build's publish path performs per artifact (R7.7)."""

    SIGN = "sign"
    PUSH = "push"
    VERIFY_RETRIEVABLE = "verify-retrievable"
    MARK_AVAILABLE = "mark-available"


#: The fixed order the publish path must follow (R7.7). ``mark-available`` is
#: strictly last, and reachable only when ``verify-retrievable`` succeeds.
PUBLISH_ORDER: tuple[PublishStep, ...] = (
    PublishStep.SIGN,
    PublishStep.PUSH,
    PublishStep.VERIFY_RETRIEVABLE,
    PublishStep.MARK_AVAILABLE,
)


@dataclass(frozen=True)
class PublishOutcome:
    """The result of running the publish path for one artifact.

    Attributes:
        artifact: The artifact name (fork / component / ``gpu-ami``).
        steps_run: The publish steps that were executed, in order.
        marked_available: Whether the artifact was recorded available for deploy.
        error: A short reason string when the publish failed, else ``""``.
    """

    artifact: str
    steps_run: tuple[PublishStep, ...]
    marked_available: bool
    error: str = ""

    @property
    def ok(self) -> bool:
        """Whether the artifact was published+verified+recorded correctly."""
        return self.marked_available and not self.error


#: A push function: given an artifact + its store path, return whether the push
#: to the cache succeeded. Injected so the self-test can model push failure.
PushFn = Callable[[str, str], bool]

#: A read-back verifier: given an artifact + store path, return whether the
#: closure is retrievable from the cache (narinfo read-back). Injected.
VerifyFn = Callable[[str, str], bool]


@dataclass
class ArtifactPublish:
    """One artifact to publish: its name and the store path built for it."""

    artifact: str
    store_path: str


def publish_artifact(
    artifact: ArtifactPublish,
    *,
    push: PushFn,
    verify_retrievable: VerifyFn,
) -> PublishOutcome:
    """Run the ordered sign→push→verify→available path for one artifact (R7.7).

    Enforces the single invariant: the artifact is marked available ONLY when the
    push succeeded AND the retrievability read-back succeeded. On any failure the
    artifact is NOT marked available and the outcome carries an ``error`` naming
    the artifact and the failed step, so the harness can report it (R12.7).

    Args:
        artifact: The artifact + its built store path.
        push: Injected push (``nix copy --to``) function (R7.2 / R6.2).
        verify_retrievable: Injected narinfo read-back verifier (R7.7).

    Returns:
        The :class:`PublishOutcome` recording steps run, availability, and error.
    """
    steps: list[PublishStep] = [PublishStep.SIGN]

    # 2. push to cache (R7.2/R6.2)
    steps.append(PublishStep.PUSH)
    if not push(artifact.artifact, artifact.store_path):
        return PublishOutcome(
            artifact=artifact.artifact,
            steps_run=tuple(steps),
            marked_available=False,
            error=(
                f"error: push to cache failed for {artifact.artifact!r} "
                f"({artifact.store_path}); artifact NOT marked available (R6.2)"
            ),
        )

    # 3. verify retrievable — narinfo read-back (R7.7)
    steps.append(PublishStep.VERIFY_RETRIEVABLE)
    if not verify_retrievable(artifact.artifact, artifact.store_path):
        # The invariant: a closure that is not retrievable is NEVER marked
        # available — mark-available is not reached (R7.7).
        return PublishOutcome(
            artifact=artifact.artifact,
            steps_run=tuple(steps),
            marked_available=False,
            error=(
                f"error: closure not retrievable from cache for "
                f"{artifact.artifact!r} ({artifact.store_path}); artifact NOT "
                "marked available (R7.7)"
            ),
        )

    # 4. mark available — strictly after a successful read-back (R7.7)
    steps.append(PublishStep.MARK_AVAILABLE)
    return PublishOutcome(
        artifact=artifact.artifact,
        steps_run=tuple(steps),
        marked_available=True,
        error="",
    )


@dataclass
class _PlanReport:
    """The aggregated result of verifying a whole publish plan."""

    outcomes: list[PublishOutcome] = field(default_factory=list)

    @property
    def failures(self) -> list[PublishOutcome]:
        return [o for o in self.outcomes if not o.ok]

    @property
    def ok(self) -> bool:
        return not self.failures


def verify_publish_plan(
    plan: list[ArtifactPublish],
    *,
    push: PushFn,
    verify_retrievable: VerifyFn,
) -> _PlanReport:
    """Publish every artifact and aggregate the outcomes."""
    report = _PlanReport()
    for artifact in plan:
        report.outcomes.append(
            publish_artifact(artifact, push=push, verify_retrievable=verify_retrievable)
        )
    return report


# ---------------------------------------------------------------------------
# Self-check — models the push→verify→available ordering with no real cache
# ---------------------------------------------------------------------------


def _run_self_check() -> int:
    """Exercise the R7.7 ordering invariant without any real Nix / S3 (R6.2/7.7).

    Asserts three things:

    * a healthy publish signs → pushes → verifies → marks available, in order,
      and the artifact ends up available (R7.7 happy path);
    * a closure whose read-back FAILS is NEVER marked available, and the failure
      names the artifact (R7.7 invariant);
    * a push failure is surfaced (R6.2) and likewise blocks availability.
    """
    ok = True

    healthy_plan = [
        ArtifactPublish("lavaplayer", "/nix/store/aaaa1111-lavaplayer-jar"),
        ArtifactPublish("lavalink-image", "/nix/store/bbbb2222-lavalink-image.tar.gz"),
        ArtifactPublish("gpu-ami", "/nix/store/cccc3333-nixos-amazon-image.vhd"),
    ]

    # 1. Healthy publish: every step runs in order and each is available.
    healthy = verify_publish_plan(
        healthy_plan,
        push=lambda _a, _p: True,
        verify_retrievable=lambda _a, _p: True,
    )
    if not healthy.ok:
        print("error: self-check FAILED — a healthy publish should succeed")
        ok = False
    for outcome in healthy.outcomes:
        if outcome.steps_run != PUBLISH_ORDER:
            print(
                f"error: self-check FAILED — {outcome.artifact} did not run the "
                f"steps in order {tuple(s.value for s in PUBLISH_ORDER)}"
            )
            ok = False
        if not outcome.marked_available:
            print(f"error: self-check FAILED — {outcome.artifact} not marked available")
            ok = False

    # 2. Read-back fails for exactly one artifact -> that artifact is NOT marked
    #    available and mark-available is not reached (R7.7 invariant).
    def only_lavalink_unretrievable(artifact: str, _p: str) -> bool:
        return artifact != "lavalink-image"

    unretrievable = verify_publish_plan(
        healthy_plan,
        push=lambda _a, _p: True,
        verify_retrievable=only_lavalink_unretrievable,
    )
    failing = [o for o in unretrievable.outcomes if not o.ok]
    if len(failing) != 1 or failing[0].artifact != "lavalink-image":
        print("error: self-check FAILED — the unretrievable artifact must fail (R7.7)")
        ok = False
    else:
        bad = failing[0]
        if bad.marked_available:
            print(
                "error: self-check FAILED — an unretrievable closure must NOT be "
                "marked available (R7.7)"
            )
            ok = False
        if PublishStep.MARK_AVAILABLE in bad.steps_run:
            print(
                "error: self-check FAILED — mark-available must not run after a "
                "failed read-back (R7.7)"
            )
            ok = False
        if "not retrievable" not in bad.error or bad.artifact not in bad.error:
            print("error: self-check FAILED — the error must name the artifact (R7.7)")
            ok = False

    # 3. A push failure blocks availability and is surfaced (R6.2).
    push_failed = verify_publish_plan(
        [ArtifactPublish("web-ui", "/nix/store/dddd4444-web-ui-image.tar.gz")],
        push=lambda _a, _p: False,
        verify_retrievable=lambda _a, _p: True,
    )
    if push_failed.ok or push_failed.outcomes[0].marked_available:
        print("error: self-check FAILED — a push failure must block availability (R6.2)")
        ok = False
    if PublishStep.VERIFY_RETRIEVABLE in push_failed.outcomes[0].steps_run:
        print("error: self-check FAILED — no read-back after a failed push")
        ok = False

    if ok:
        print(
            "verify-cache self-check passed: publish signs → pushes → verifies "
            "retrievable → marks available in order; a non-retrievable closure is "
            "NEVER marked available and the failure names the artifact (R7.7); a "
            "push failure blocks availability (R6.2)."
        )
        return 0
    return 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _usage() -> str:
    return (
        "usage: verify_cache.py [--list]\n"
        "  (no flags)  self-check the R7.7 push→verify→available ordering (R6.2/7.7)\n"
        "  --list      describe the modeled publish steps and exit"
    )


def main(argv: list[str]) -> int:
    """Entry point: run the cache push+verify check, returning an exit code."""
    args = list(argv)
    if "--list" in args:
        args = [a for a in args if a != "--list"]
        if args:
            print(_usage())
            return 2
        print("cache push+verify publish steps (R7.7 order):")
        for i, step in enumerate(PUBLISH_ORDER, start=1):
            print(f"  {i}. {step.value}")
        print(
            "invariant: 'mark-available' is reached ONLY after 'verify-retrievable' "
            "succeeds (R7.7); closures→cache and images→ECR on a build (R6.2)."
        )
        return 0

    if args:
        print(_usage())
        return 2

    return _run_self_check()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

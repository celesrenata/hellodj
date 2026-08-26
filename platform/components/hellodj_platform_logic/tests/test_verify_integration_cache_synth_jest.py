"""Integration verification for task 20.4 — cache push+verify, cdk synth, jest, R12.7.

Feature: hellodj-nix-native-delivery, Requirements 6.2, 7.7, 12.5, 12.6, 12.7.

Task 20.4 adds the integration-verification entries for:

* **cache push + verify-retrievable-before-available** (R7.7 / R6.2) — a built
  closure is pushed to the S3-backed Nix binary cache and confirmed retrievable
  (narinfo read-back) BEFORE it is marked available for stage deploy; closures
  publish to the cache and images to ECR on a build. Exercised via
  ``tools/verify_cache.py`` (which models the ordering with no real Nix/S3).
* **``npx cdk synth`` with reconciled stage names** (R12.5) — the harness plans
  a ``cdk synth`` in ``platform/infra`` and the pipeline uses the reconciled
  Beta/Staging/Production stage names (zero ``gamma``).
* **``jest`` green** (R12.6) — the harness plans the jest suite.
* **12.7 failure aggregation** — inducing a failing command (here the new
  ``cache-push-verify`` command) makes the harness report the failing command
  AND artifact.

These are example/integration tests (not property tests): they drive the pure,
injectable aggregation core through the ``tools/verify_all.py`` wrapper and the
``tools/verify_cache.py`` self-check, so they run without a real Nix / CDK / jest
toolchain.

Validates: Requirements 6.2, 7.7, 12.5, 12.6, 12.7
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_PLATFORM_ROOT = Path(__file__).resolve().parents[3]
_COMPONENTS_ROOT = _PLATFORM_ROOT / "components"
_INFRA_ROOT = _PLATFORM_ROOT / "infra"
if str(_COMPONENTS_ROOT) not in sys.path:
    sys.path.insert(0, str(_COMPONENTS_ROOT))


def _load(mod_name: str) -> object:
    path = _PLATFORM_ROOT / "tools" / f"{mod_name}.py"
    spec = importlib.util.spec_from_file_location(mod_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


tool = _load("verify_all")
cache_tool = _load("verify_cache")


def _always_available(_exe: str) -> bool:
    return True


# ---------------------------------------------------------------------------
# R7.7 / R6.2 — the cache push + verify-retrievable-before-available tool
# ---------------------------------------------------------------------------


def test_verify_cache_self_check_passes() -> None:
    """`tools/verify_cache.py` self-check passes (push→verify→available order).

    Validates: Requirements 6.2, 7.7
    """
    assert cache_tool.main([]) == 0


def test_verify_cache_list_describes_ordered_steps() -> None:
    """--list describes the ordered publish steps ending in mark-available (R7.7)."""
    assert cache_tool.main(["--list"]) == 0


def test_healthy_publish_marks_available_in_order() -> None:
    """A healthy publish signs→pushes→verifies→marks-available, in order (R7.7).

    Validates: Requirements 7.7
    """
    plan = [cache_tool.ArtifactPublish("lavaplayer", "/nix/store/aaaa1111-lavaplayer-jar")]
    report = cache_tool.verify_publish_plan(
        plan,
        push=lambda _a, _p: True,
        verify_retrievable=lambda _a, _p: True,
    )
    assert report.ok
    outcome = report.outcomes[0]
    assert outcome.marked_available is True
    assert outcome.steps_run == cache_tool.PUBLISH_ORDER
    # mark-available is strictly last.
    assert outcome.steps_run[-1] == cache_tool.PublishStep.MARK_AVAILABLE


def test_unretrievable_closure_is_never_marked_available() -> None:
    """A closure failing the read-back is NEVER marked available (R7.7 invariant).

    Validates: Requirements 7.7
    """
    plan = [
        cache_tool.ArtifactPublish("web-ui", "/nix/store/bbbb2222-web-ui-image.tar.gz")
    ]
    report = cache_tool.verify_publish_plan(
        plan,
        push=lambda _a, _p: True,
        verify_retrievable=lambda _a, _p: False,  # read-back fails
    )
    assert report.ok is False
    outcome = report.outcomes[0]
    assert outcome.marked_available is False
    # mark-available must NOT be reached after a failed read-back.
    assert cache_tool.PublishStep.MARK_AVAILABLE not in outcome.steps_run
    # The error names the offending artifact (so the harness can report it).
    assert "web-ui" in outcome.error
    assert "not retrievable" in outcome.error


def test_push_failure_blocks_availability_and_skips_readback() -> None:
    """A push failure blocks availability and no read-back is attempted (R6.2).

    Validates: Requirements 6.2
    """
    plan = [cache_tool.ArtifactPublish("gpu-ami", "/nix/store/cccc3333-amazon-image.vhd")]
    report = cache_tool.verify_publish_plan(
        plan,
        push=lambda _a, _p: False,  # push fails
        verify_retrievable=lambda _a, _p: True,
    )
    assert report.ok is False
    outcome = report.outcomes[0]
    assert outcome.marked_available is False
    assert cache_tool.PublishStep.VERIFY_RETRIEVABLE not in outcome.steps_run
    assert "gpu-ami" in outcome.error


# ---------------------------------------------------------------------------
# The harness plan includes the cache push+verify, cdk synth, and jest commands
# ---------------------------------------------------------------------------


def test_plan_includes_cache_push_verify_command() -> None:
    """The plan includes the R7.7 cache push+verify command in platform/ (R7.7/R6.2).

    Validates: Requirements 6.2, 7.7
    """
    plan = tool.build_command_plan()
    cache_cmds = [c for c in plan if c.artifact == "cache-push-verify"]
    assert len(cache_cmds) == 1
    cmd = cache_cmds[0]
    assert cmd.requirement == "7.7"
    assert cmd.argv[0] == sys.executable
    assert cmd.argv[1].endswith("verify_cache.py")
    # Requires only the host Python (runs in every environment, no real cache).
    assert cmd.needs == sys.executable


def test_plan_cdk_synth_targets_infra_and_jest_present() -> None:
    """The plan runs `npx cdk synth` in platform/infra and includes jest (12.5/12.6).

    Validates: Requirements 12.5, 12.6
    """
    plan = tool.build_command_plan()
    by_artifact = {c.artifact: c for c in plan}

    cdk = by_artifact["cdk-app"]
    assert cdk.requirement == "12.5"
    assert cdk.argv == ("npx", "cdk", "synth")
    assert cdk.cwd == _INFRA_ROOT
    assert cdk.needs == "npx"

    jest = by_artifact["jest-suite"]
    assert jest.requirement == "12.6"
    assert jest.argv[:2] == ("npx", "jest")
    assert jest.cwd == _INFRA_ROOT


def test_cdk_synth_uses_reconciled_stage_names_zero_gamma() -> None:
    """The CDK pipeline `cdk synth` targets reconciled Beta/Staging/Production (12.5).

    Static check: pipeline-stack.ts PROMOTION_ORDER is the reconciled tuple and
    carries zero `gamma` references, so `npx cdk synth` synthesizes with the
    reconciled stage names (single-host endpoints wired elsewhere).

    Validates: Requirements 12.5
    """
    pipeline = (_INFRA_ROOT / "lib" / "pipeline-stack.ts").read_text(encoding="utf-8")
    assert "['beta', 'staging', 'production']" in pipeline
    assert "gamma" not in pipeline.lower()


# ---------------------------------------------------------------------------
# R12.7 — inducing a failing command reports the command + artifact
# ---------------------------------------------------------------------------


def test_failing_cache_push_verify_is_reported_with_command_and_artifact() -> None:
    """Inducing a cache push+verify failure names the command + artifact (R12.7).

    Validates: Requirements 12.7
    """
    plan = tool.build_command_plan()

    def runner(command: object) -> tuple[int, str]:
        if command.artifact == "cache-push-verify":
            # Model the tool's own failure output on a non-retrievable closure.
            return (
                1,
                "error: closure not retrievable from cache for 'lavalink-image'; "
                "artifact NOT marked available (R7.7)",
            )
        return (0, "ok")

    exit_code, report = tool.run_plan(
        plan,
        require_all=False,
        runner=runner,
        is_available=_always_available,
    )

    assert report.failed is True
    assert exit_code == 1
    failing_artifacts = {r.command.artifact for r in report.failures}
    assert failing_artifacts == {"cache-push-verify"}

    rendered = tool.format_report(report, require_all=False)
    assert "VERIFICATION FAILED" in rendered
    # Names the command AND the artifact it was verifying (R12.7).
    assert "cache-push-verify" in rendered
    assert "cache push + verify-retrievable before available (R7.7/R6.2)" in rendered


def test_failing_cdk_synth_is_reported_with_command_and_artifact() -> None:
    """Inducing a cdk synth failure names the command + artifact (R12.5/R12.7).

    Validates: Requirements 12.5, 12.7
    """
    plan = tool.build_command_plan()

    def runner(command: object) -> tuple[int, str]:
        return (2, "boom") if command.artifact == "cdk-app" else (0, "ok")

    report = tool.aggregate(plan, runner=runner, is_available=_always_available)
    assert report.failed is True
    assert {r.command.artifact for r in report.failures} == {"cdk-app"}
    rendered = tool.format_report(report, require_all=False)
    assert "cdk-app" in rendered
    assert "npx cdk synth" in rendered


def test_all_pass_including_cache_and_cdk_and_jest_succeeds() -> None:
    """When the full plan (incl. cache/cdk/jest) all passes, the run succeeds.

    Validates: Requirements 6.2, 7.7, 12.5, 12.6
    """
    plan = tool.build_command_plan()
    report = tool.aggregate(
        plan,
        runner=lambda _c: (0, "ok"),
        is_available=_always_available,
    )
    assert report.failed is False
    covered = {c.artifact for c in plan}
    assert {"cache-push-verify", "cdk-app", "jest-suite", "gpu-ami", "base-image-gate"} <= covered
    assert tool.decide_exit_code(report, require_all=False) == 0

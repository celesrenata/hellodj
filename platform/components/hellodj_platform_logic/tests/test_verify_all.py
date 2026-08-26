"""Unit / example tests for the verification-harness failure aggregation (task 20.2).

Feature: hellodj-nix-native-delivery, Requirement 12.7.

These tests exercise ``tools/verify_all.py`` — the executable a reviewer / CI
runs to drive the whole R12.1-6 verification path and get one aggregated
verdict. Requirement 12.7 says: *if any of the verification commands in criteria
1-6 exits non-zero or reports a failure, the verification is treated as failed
and identifies the failing command and artifact.*

The harness takes an injected command ``runner`` and ``is_available`` predicate,
so these example tests exercise the aggregation contract with no real Nix /
CDK / jest dependency:

* an all-pass run succeeds (exit 0) — R12.7 (the pass side);
* a single failing command (exit non-zero) fails the whole run and the report
  names the failing command AND the artifact it verified — R12.7;
* a command that exits 0 but *reports* a failure in its output is still a FAIL
  — the "reports a failure" clause of R12.7;
* a command whose builder/toolchain is unavailable is SKIPPED — a skip is
  distinct from a pass and is NOT a verification failure by default, but IS
  treated as incomplete under ``--require-all``.

Validates: Requirements 12.7
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

# The tools live under platform/tools/ (a sibling of components/). Load them by
# path so the tests do not depend on tools/ being an installed package.
_PLATFORM_ROOT = Path(__file__).resolve().parents[3]
_COMPONENTS_ROOT = _PLATFORM_ROOT / "components"
if str(_COMPONENTS_ROOT) not in sys.path:
    sys.path.insert(0, str(_COMPONENTS_ROOT))


def _load(mod_name: str) -> object:
    path = _PLATFORM_ROOT / "tools" / f"{mod_name}.py"
    spec = importlib.util.spec_from_file_location(mod_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Register before exec so dataclass field-type resolution can find the
    # module's namespace (the tool defines frozen dataclasses).
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


tool = _load("verify_all")


def _cmd(
    requirement: str,
    artifact: str,
    *,
    needs: str = "always",
    description: str | None = None,
) -> object:
    """Build a synthetic VerifyCommand for the injected runner to classify."""
    return tool.VerifyCommand(
        requirement=requirement,
        artifact=artifact,
        argv=("true",),
        cwd=_PLATFORM_ROOT,
        needs=needs,
        description=description or f"{artifact} check",
    )


def _always_available(_exe: str) -> bool:
    return True


# ---------------------------------------------------------------------------
# All-pass run succeeds
# ---------------------------------------------------------------------------


def test_all_pass_run_succeeds() -> None:
    """When every command passes, the run succeeds with exit 0 (R12.7 pass side).

    Validates: Requirements 12.7
    """
    commands = [
        _cmd("12.1", "fork:Lavalink"),
        _cmd("12.3", "base-image-gate"),
        _cmd("12.6", "jest-suite"),
    ]
    report = tool.aggregate(
        commands,
        runner=lambda _c: (0, "ok"),
        is_available=_always_available,
    )
    assert report.failed is False
    assert len(report.passes) == 3
    assert report.failures == []
    assert tool.decide_exit_code(report, require_all=False) == 0
    assert tool.decide_exit_code(report, require_all=True) == 0


# ---------------------------------------------------------------------------
# A single failing command fails the whole run and names command + artifact
# ---------------------------------------------------------------------------


def test_single_nonzero_exit_fails_run_and_names_command_and_artifact() -> None:
    """One command exiting non-zero fails the whole run and is named (R12.7).

    Validates: Requirements 12.7
    """
    failing_artifact = "component:web-ui:.#image"
    commands = [
        _cmd("12.1", "fork:Lavalink"),
        _cmd("12.2", failing_artifact, description="nix build .#image for web-ui"),
        _cmd("12.6", "jest-suite"),
    ]

    def runner(command: object) -> tuple[int, str]:
        return (2, "boom") if command.artifact == failing_artifact else (0, "ok")

    exit_code, report = tool.run_plan(
        commands,
        require_all=False,
        runner=runner,
        is_available=_always_available,
    )

    # Verification is treated as FAILED (R12.7).
    assert report.failed is True
    assert exit_code == 1
    # Exactly the one failing command is a failure; the others still passed.
    assert len(report.failures) == 1
    failure = report.failures[0]
    assert failure.command.artifact == failing_artifact
    assert failure.exit_code == 2

    # The rendered report NAMES the failing command AND the artifact (R12.7).
    rendered = tool.format_report(report, require_all=False)
    assert failing_artifact in rendered
    assert "nix build .#image for web-ui" in rendered
    assert "VERIFICATION FAILED" in rendered


def test_multiple_failures_all_named() -> None:
    """Every failing command is aggregated and named, not just the first (R12.7).

    Validates: Requirements 12.7
    """
    commands = [
        _cmd("12.1", "fork:lavaplayer"),
        _cmd("12.2", "fork:Lavalink:.#image"),
        _cmd("12.4", "gpu-ami"),
    ]

    def runner(command: object) -> tuple[int, str]:
        # Two of the three fail.
        if command.artifact in {"fork:Lavalink:.#image", "gpu-ami"}:
            return (1, "build failed")
        return (0, "ok")

    report = tool.aggregate(commands, runner=runner, is_available=_always_available)
    assert report.failed is True
    failed_artifacts = {r.command.artifact for r in report.failures}
    assert failed_artifacts == {"fork:Lavalink:.#image", "gpu-ami"}

    rendered = tool.format_report(report, require_all=False)
    assert "fork:Lavalink:.#image" in rendered
    assert "gpu-ami" in rendered


# ---------------------------------------------------------------------------
# Exit 0 but output reports a failure is still a FAIL (R12.7 "reports a failure")
# ---------------------------------------------------------------------------


def test_exit_zero_but_reported_failure_is_a_fail() -> None:
    """A command that exits 0 but prints a failure marker is still FAIL (R12.7).

    Validates: Requirements 12.7
    """
    commands = [_cmd("12.3", "base-image-gate")]
    report = tool.aggregate(
        commands,
        runner=lambda _c: (0, "base-image gate FAILED: 1 non-Nix base(s) detected."),
        is_available=_always_available,
    )
    assert report.failed is True
    assert report.failures[0].command.artifact == "base-image-gate"
    assert "reported a failure" in report.failures[0].detail


def test_benign_zero_exit_output_is_a_pass() -> None:
    """A clean exit-0 command with benign output is a PASS (no false positives)."""
    commands = [_cmd("12.1", "fork:LavaSrc")]
    report = tool.aggregate(
        commands,
        runner=lambda _c: (0, "checking flake output 'lavasrcPlugin'... ok"),
        is_available=_always_available,
    )
    assert report.failed is False
    assert len(report.passes) == 1


# ---------------------------------------------------------------------------
# Unavailable builder -> SKIP (distinct from pass; not a failure by default)
# ---------------------------------------------------------------------------


def test_unavailable_builder_is_skipped_not_failed() -> None:
    """A command whose builder is unavailable is SKIPPED, not failed (R12.7).

    A skip is reported distinctly and does not fail the run by default — several
    of the spec's own integration criteria are gated on builder availability.

    Validates: Requirements 12.7
    """
    commands = [
        _cmd("12.1", "fork:Lavalink", needs="nix"),
        _cmd("12.3", "base-image-gate", needs="always"),
    ]

    def only_missing_nix(exe: str) -> bool:
        return exe != "nix"

    report = tool.aggregate(
        commands,
        runner=lambda _c: (0, "ok"),
        is_available=only_missing_nix,
    )
    assert report.failed is False
    assert len(report.skips) == 1
    assert report.skips[0].command.artifact == "fork:Lavalink"
    assert len(report.passes) == 1
    # Skips alone do not fail the run by default...
    assert tool.decide_exit_code(report, require_all=False) == 0
    # ...but --require-all treats an unavailable builder as incomplete (exit 2).
    assert tool.decide_exit_code(report, require_all=True) == 2

    rendered = tool.format_report(report, require_all=False)
    assert "skipped" in rendered.lower()


def test_skip_report_is_distinct_from_pass() -> None:
    """The report distinguishes SKIP from PASS so an un-run builder isn't green."""
    commands = [_cmd("12.2", "fork:Lavalink:.#image", needs="nix")]
    report = tool.aggregate(
        commands,
        runner=lambda _c: (0, "ok"),
        is_available=lambda _exe: False,
    )
    rendered = tool.format_report(report, require_all=False)
    # The one command's outcome label is SKIP, not the PASS outcome label.
    assert "SKIP R12.2 fork:Lavalink:.#image" in rendered
    assert "PASS R12.2" not in rendered
    assert len(report.passes) == 0


# ---------------------------------------------------------------------------
# The concrete command plan covers R12.1-6
# ---------------------------------------------------------------------------


def test_command_plan_covers_all_of_r12_1_through_6() -> None:
    """The built plan includes a command for each of R12.1-6."""
    plan = tool.build_command_plan()
    requirements = {c.requirement for c in plan}
    assert {"12.1", "12.2", "12.3", "12.4", "12.5", "12.6"} <= requirements
    # The four forks appear in the flake-check segment (R12.1).
    r121_artifacts = {c.artifact for c in plan if c.requirement == "12.1"}
    for fork in ("Lavalink", "lavaplayer", "LavaSrc", "youtube-source"):
        assert f"fork:{fork}" in r121_artifacts
    # The gate, AMI, cdk synth, and jest are all present.
    all_artifacts = {c.artifact for c in plan}
    assert "base-image-gate" in all_artifacts
    assert "gpu-ami" in all_artifacts
    assert "cdk-app" in all_artifacts
    assert "jest-suite" in all_artifacts


def test_plan_commands_are_fixed_argv_never_shell_strings() -> None:
    """Every planned command is a fixed argv tuple (no shell string injection)."""
    for command in tool.build_command_plan():
        assert isinstance(command.argv, tuple)
        assert all(isinstance(part, str) for part in command.argv)
        assert command.argv  # non-empty


# ---------------------------------------------------------------------------
# Task 20.3 — integration verification across all flakes
# (flake check exit 0 (12.1/2.7); real jar/image no-placeholder (12.2/2.6);
#  hermetic build (2.5); Temurin/Java feature version 25 (3.x))
# ---------------------------------------------------------------------------


def test_integration_plan_covers_hermetic_realjar_and_temurin25() -> None:
    """The 20.3 integration plan names the 2.6, 2.5, and 3.x concerns distinctly.

    Validates: Requirements 12.1, 12.2, 2.5, 2.6, 2.7, 3.x
    """
    plan = tool.build_integration_plan("x86_64-linux")
    requirements = {c.requirement for c in plan}
    # Real jar / no-placeholder (2.6), hermetic build (2.5), Temurin-25 (3.x).
    assert {"2.6", "2.5", "3.5"} <= requirements

    # 2.6: every fork has a real-artifact (no-placeholder) verification.
    real_artifact = {
        c.artifact for c in plan if c.requirement == "2.6"
    }
    for fork in ("Lavalink", "lavaplayer", "LavaSrc", "youtube-source"):
        assert f"fork:{fork}:real-artifact" in real_artifact

    # 2.5: the three plugin forks have an explicit hermetic-build verification.
    hermetic = {c.artifact for c in plan if c.requirement == "2.5"}
    assert hermetic == {
        "fork:lavaplayer:hermetic-build",
        "fork:LavaSrc:hermetic-build",
        "fork:youtube-source:hermetic-build",
    }

    # 3.x: the Lavalink image JRE reports Java feature version 25.
    temurin = [c for c in plan if c.requirement == "3.5"]
    assert len(temurin) == 1
    assert temurin[0].artifact == "fork:Lavalink:temurin-25"
    assert "25" in temurin[0].description


def test_integration_commands_are_nix_gated_and_system_keyed() -> None:
    """Every integration command is nix-gated and targets a system-keyed check.

    Validates: Requirements 12.2, 2.5, 2.6, 3.x
    """
    plan = tool.build_integration_plan("aarch64-linux")
    for command in plan:
        # Gated on the Nix builder (skips cleanly when absent).
        assert command.needs == "nix"
        # A `nix build .#checks.<system>.<name> --no-link` invocation.
        assert command.argv[0] == "nix"
        assert command.argv[1] == "build"
        assert command.argv[2].startswith(".#checks.aarch64-linux.")
        assert "--no-link" in command.argv


def test_integration_checks_skip_cleanly_without_nix_builder() -> None:
    """Without a Nix builder the integration checks SKIP, never fail (R12.2 gate).

    The spec gates nix-build verification on builder availability ("WHERE a Nix
    builder is available"); an absent builder must be a distinct SKIP, not a
    verification failure.

    Validates: Requirements 12.2, 2.5, 2.6, 3.x
    """
    plan = tool.build_integration_plan("x86_64-linux")
    report = tool.aggregate(
        plan,
        runner=lambda _c: (0, "ok"),
        is_available=lambda exe: exe != "nix",
    )
    assert report.failed is False
    assert len(report.skips) == len(plan)
    assert len(report.passes) == 0
    assert tool.decide_exit_code(report, require_all=False) == 0
    # Strict mode surfaces the un-run builder as incomplete (exit 2).
    assert tool.decide_exit_code(report, require_all=True) == 2


def test_integration_checks_pass_when_builder_available_and_green() -> None:
    """With a Nix builder and green checks, the integration plan passes.

    Validates: Requirements 12.1, 12.2, 2.5, 2.6, 3.x
    """
    plan = tool.build_integration_plan("x86_64-linux")
    report = tool.aggregate(
        plan,
        runner=lambda _c: (0, "checking flake output... ok"),
        is_available=_always_available,
    )
    assert report.failed is False
    assert len(report.passes) == len(plan)


def test_integration_placeholder_marker_in_output_is_a_failure() -> None:
    """A real-artifact check whose output reports the placeholder marker FAILs.

    R2.6 forbids the ``PLACEHOLDER ARTIFACT`` marker in a built jar; if the
    verifying check surfaces it, verification must be treated as failed and name
    the artifact.

    Validates: Requirements 2.6
    """
    plan = tool.build_integration_plan("x86_64-linux")
    lavaplayer_real = next(
        c for c in plan if c.artifact == "fork:lavaplayer:real-artifact"
    )

    def runner(command: object) -> tuple[int, str]:
        if command.artifact == "fork:lavaplayer:real-artifact":
            return (1, "FAIL: jar contains PLACEHOLDER ARTIFACT marker")
        return (0, "ok")

    report = tool.aggregate(plan, runner=runner, is_available=_always_available)
    assert report.failed is True
    failed_artifacts = {r.command.artifact for r in report.failures}
    assert failed_artifacts == {"fork:lavaplayer:real-artifact"}
    rendered = tool.format_report(report, require_all=False)
    assert lavaplayer_real.artifact in rendered


def test_temurin25_check_failure_is_reported_with_artifact() -> None:
    """A wrong Java feature version fails the Temurin-25 verification (R3.x).

    Validates: Requirements 3.x
    """
    plan = tool.build_integration_plan("x86_64-linux")

    def runner(command: object) -> tuple[int, str]:
        if command.artifact == "fork:Lavalink:temurin-25":
            return (1, "FAIL (R3.5): image JRE reports Java feature version '21'")
        return (0, "ok")

    report = tool.aggregate(plan, runner=runner, is_available=_always_available)
    assert report.failed is True
    assert report.failures[0].command.artifact == "fork:Lavalink:temurin-25"


def test_full_plan_includes_integration_verifications() -> None:
    """The full R12 plan now folds in the 20.3 integration verifications."""
    plan = tool.build_command_plan()
    artifacts = {c.artifact for c in plan}
    assert "fork:Lavalink:temurin-25" in artifacts
    assert "fork:lavaplayer:hermetic-build" in artifacts
    assert "fork:LavaSrc:real-artifact" in artifacts


# ---------------------------------------------------------------------------
# CLI: self-test, list, usage
# ---------------------------------------------------------------------------


def test_self_test_passes() -> None:
    """The harness's built-in --self-test smoke check passes."""
    assert tool.main(["--self-test"]) == 0


def test_list_prints_plan_and_runs_nothing(capsys) -> None:
    """--list prints the plan and exits 0 without running any command."""
    rc = tool.main(["--list"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "verification command plan" in out
    assert "R12.1" in out
    assert "R12.6" in out


def test_unknown_arg_is_usage_error() -> None:
    """An unknown argument is an operational error (exit 2)."""
    assert tool.main(["--frobnicate"]) == 2

#!/usr/bin/env python3
"""End-to-end verification harness with failure aggregation (task 20.2).

This is the executable a reviewer (or CI) runs to exercise the whole
`hellodj-nix-native-delivery` verification path — the R12.1-6 command set — and
get a single aggregated verdict. It implements Requirement 12.7:

    IF any of the verification commands in criteria 1-6 exits non-zero or
    reports a failure, THEN the verification SHALL be treated as failed and
    SHALL identify the failing command and artifact.

Aggregation contract (R12.7)
----------------------------

The harness runs each command and classifies its outcome into exactly one of
PASS / FAIL / SKIP:

* **FAIL** — the command exited non-zero OR its output reported a failure.
* **SKIP** — the command's builder/toolchain is unavailable in this environment.

**Overall verdict:** verification FAILS iff **any** command is classified FAIL.
Skips alone do not fail the run by default; use ``--require-all`` to treat any
SKIP as a failure (strict CI mode).

Exit codes
----------

* ``0`` — every runnable command passed.
* ``1`` — at least one command FAILED (R12.7).
* ``2`` — operational error, or under ``--require-all``, a SKIP.

Usage::

    python tools/verify_all.py                 # run the real R12.1-6 path
    python tools/verify_all.py --require-all    # strict: skips count as failures
    python tools/verify_all.py --list           # print the command plan, run nothing
    python tools/verify_all.py --self-test      # exercise the aggregation logic

Requirements: 12.7, 12.1, 12.2, 2.5, 2.6, 2.7, 3.x
"""

from __future__ import annotations

import shutil
import subprocess  # noqa: S404 - fixed argv, never shell=True
import sys
from collections.abc import Sequence
from pathlib import Path

PLATFORM_ROOT = Path(__file__).resolve().parent.parent
COMPONENTS_ROOT = PLATFORM_ROOT / "components"
INFRA_ROOT = PLATFORM_ROOT / "infra"
AMI_ROOT = INFRA_ROOT / "ami"
TOOLS_ROOT = PLATFORM_ROOT / "tools"

# Make the shared pure-logic package importable without installation.
if str(COMPONENTS_ROOT) not in sys.path:
    sys.path.insert(0, str(COMPONENTS_ROOT))

from _verify_helpers import (  # noqa: E402
    FORK_BUILD_TARGETS,
    FORK_REPOS,
    build_integration_plan,
    component_dirs_with_flakes,
    fork_dir,
    run_self_test,
)
from hellodj_platform_logic.verification_harness import (  # noqa: E402
    AvailabilityCheck,
    CommandRunner,
    VerifyCommand,
    VerifyReport,
    aggregate,
    decide_exit_code,
    format_report,
)

#: Per-command timeout budget (seconds).
DEFAULT_COMMAND_TIMEOUT = 3600.0


def _default_availability(executable: str) -> bool:
    """Return whether ``executable`` is on ``PATH`` (builder availability)."""
    return shutil.which(executable) is not None


def _default_runner(command: VerifyCommand) -> tuple[int, str]:
    """Run one verification command via subprocess, returning (code, output)."""
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            list(command.argv),
            cwd=str(command.cwd),
            capture_output=True,
            text=True,
            check=False,
            timeout=DEFAULT_COMMAND_TIMEOUT,
        )
    except FileNotFoundError:
        return 127, f"executable not found: {command.argv[0]}"
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {DEFAULT_COMMAND_TIMEOUT:.0f}s"
    return completed.returncode, (completed.stdout or "") + (completed.stderr or "")


# ---------------------------------------------------------------------------
# The concrete R12.1-6 command plan
# ---------------------------------------------------------------------------


def build_command_plan() -> list[VerifyCommand]:
    """Construct the full R12.1-6 verification command plan.

    Enumerates, in requirement order: R12.1 ``nix flake check`` for each fork +
    component flake; R12.2 ``nix build`` for each fork jar/image + component
    image; R12.3 the base-image gate; R12.4 the GPU AMI build; R12.5 ``npx cdk
    synth``; R12.6 the jest suite.
    """
    plan: list[VerifyCommand] = []
    components = sorted(component_dirs_with_flakes())

    # -- R12.1: nix flake check for every Fork_Flake and Component_Flake --------
    for fork in FORK_REPOS:
        plan.append(
            VerifyCommand(
                requirement="12.1",
                artifact=f"fork:{fork}",
                argv=("nix", "flake", "check"),
                cwd=fork_dir(fork),
                needs="nix",
                description=f"nix flake check for fork {fork}",
            )
        )
    for component in components:
        plan.append(
            VerifyCommand(
                requirement="12.1",
                artifact=f"component:{component.name}",
                argv=("nix", "flake", "check"),
                cwd=component,
                needs="nix",
                description=f"nix flake check for component {component.name}",
            )
        )

    # -- R12.2: nix build .#<jar>/.#image for each fork + component -------------
    for fork, target in FORK_BUILD_TARGETS.items():
        plan.append(
            VerifyCommand(
                requirement="12.2",
                artifact=f"fork:{fork}:{target}",
                argv=("nix", "build", target, "--no-link"),
                cwd=fork_dir(fork),
                needs="nix",
                description=f"nix build {target} for fork {fork}",
            )
        )
    for component in components:
        plan.append(
            VerifyCommand(
                requirement="12.2",
                artifact=f"component:{component.name}:.#image",
                argv=("nix", "build", ".#image", "--no-link"),
                cwd=component,
                needs="nix",
                description=f"nix build .#image for component {component.name}",
            )
        )

    # -- R12.3: base-image gate -------------------------------------------------
    plan.append(
        VerifyCommand(
            requirement="12.3",
            artifact="base-image-gate",
            argv=(sys.executable, str(TOOLS_ROOT / "gate_base_image.py")),
            cwd=PLATFORM_ROOT,
            needs=sys.executable,
            description="python3 tools/gate_base_image.py",
        )
    )

    # -- R12.4: GPU AMI build (nixos-generate -f amazon / infra/ami flake) ------
    plan.append(
        VerifyCommand(
            requirement="12.4",
            artifact="gpu-ami",
            argv=("nix", "build", ".#amazonImage", "--no-link"),
            cwd=AMI_ROOT,
            needs="nix",
            description="GPU AMI build (infra/ami flake .#amazonImage)",
        )
    )

    # -- R12.5: npx cdk synth ---------------------------------------------------
    plan.append(
        VerifyCommand(
            requirement="12.5",
            artifact="cdk-app",
            argv=("npx", "cdk", "synth"),
            cwd=INFRA_ROOT,
            needs="npx",
            description="npx cdk synth",
        )
    )

    # -- R12.6: jest ------------------------------------------------------------
    plan.append(
        VerifyCommand(
            requirement="12.6",
            artifact="jest-suite",
            argv=("npx", "jest", "--ci"),
            cwd=INFRA_ROOT,
            needs="npx",
            description="jest suite",
        )
    )

    # -- R7.7 / R6.2: cache push + verify-retrievable-before-available ----------
    plan.append(
        VerifyCommand(
            requirement="7.7",
            artifact="cache-push-verify",
            argv=(sys.executable, str(TOOLS_ROOT / "verify_cache.py")),
            cwd=PLATFORM_ROOT,
            needs=sys.executable,
            description="cache push + verify-retrievable before available (R7.7/R6.2)",
        )
    )

    # -- Task 20.3: named integration verifications across all flakes ----------
    plan.extend(build_integration_plan())

    return plan


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _usage() -> str:
    """Return the CLI usage string."""
    return (
        "usage: verify_all.py [--require-all] [--list] [--self-test]\n"
        "  (no flags)     run the R12.1-6 verification path and aggregate\n"
        "  --require-all  treat any SKIP (unavailable builder) as a failure\n"
        "  --list         print the command plan and exit without running\n"
        "  --self-test    exercise the aggregation logic without a real builder"
    )


def run_plan(
    commands: Sequence[VerifyCommand],
    *,
    require_all: bool,
    runner: CommandRunner | None = None,
    is_available: AvailabilityCheck | None = None,
) -> tuple[int, VerifyReport]:
    """Run the plan, print the report, and return ``(exit_code, report)``."""
    report = aggregate(
        commands,
        runner=runner or _default_runner,
        is_available=is_available or _default_availability,
    )
    print(format_report(report, require_all=require_all))
    return decide_exit_code(report, require_all=require_all), report


def main(argv: list[str]) -> int:
    """Entry point: run the harness, returning a process exit code."""
    args = list(argv)

    self_test = "--self-test" in args
    args = [a for a in args if a != "--self-test"]
    require_all = "--require-all" in args
    args = [a for a in args if a != "--require-all"]
    list_only = "--list" in args
    args = [a for a in args if a != "--list"]

    if args:
        print(_usage())
        return 2

    if self_test:
        rc = run_self_test()
        if rc != 0:
            return rc

    plan = build_command_plan()

    if list_only:
        print("verification command plan (R12.1-6):")
        for command in plan:
            print(
                f"  R{command.requirement} {command.artifact}: "
                f"{' '.join(command.argv)}  (cwd={command.cwd}, needs={command.needs})"
            )
        return 0

    if self_test:
        # --self-test alone (no run requested) is a valid smoke-only invocation.
        return 0

    exit_code, _report = run_plan(plan, require_all=require_all)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

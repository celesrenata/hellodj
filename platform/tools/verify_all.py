#!/usr/bin/env python3
"""End-to-end verification harness with failure aggregation (task 20.2).

This is the executable a reviewer (or CI) runs to exercise the whole
`hellodj-nix-native-delivery` verification path — the R12.1-6 command set — and
get a single aggregated verdict. It is the thin wrapper around the pure,
property-tested aggregation core in
:mod:`hellodj_platform_logic.verification_harness`, so the CLI and the shared
decision logic reason over one source of truth. It implements Requirement 12.7:

    IF any of the verification commands in criteria 1-6 exits non-zero or
    reports a failure, THEN the verification SHALL be treated as failed and
    SHALL identify the failing command and artifact.

The R12.1-6 command set (design §Testing Strategy / Requirement 12)
-------------------------------------------------------------------

============  ==============================================================
Requirement   Command (per fork / component / artifact)
============  ==============================================================
R12.1         ``nix flake check`` for every Fork_Flake + Component_Flake
R12.2         ``nix build .#<jar>`` / ``.#image`` for each fork + component
R12.3         ``python3 tools/gate_base_image.py``
R12.4         ``nixos-generate -f amazon`` (or the ``infra/ami`` flake build)
R12.5         ``npx cdk synth`` (reconciled Beta/Staging/Production stages)
R12.6         ``jest`` (the CDK/infra suite)
R7.7/R6.2     ``python3 tools/verify_cache.py`` — push a closure + confirm it
              is retrievable BEFORE marking it available (closures→cache,
              images→ECR on a build)
============  ==============================================================

Integration verification across all flakes (task 20.3)
------------------------------------------------------

On top of the requirement-level R12.1/R12.2 commands above, the harness also
runs a set of *named integration verifications* that make the individual
correctness concerns of Requirements 2.5/2.6/2.7 and 3.x visible to a reviewer
as distinct, independently-reported checks (rather than folding them into a
single opaque ``nix flake check``). Each fork flake ships a ``checks`` set the
integration plan builds directly:

============  ==============================================================
Concern       Command (per fork / component)
============  ==============================================================
2.7 / 12.1    ``nix flake check`` exits 0 for every Fork_Flake + Component_Flake
2.6           build each fork's jar/plugin ``checks`` output — asserts a real
              jar/plugin (manifest + ``.class`` entries) with **no**
              ``PLACEHOLDER ARTIFACT`` marker (and the Lavalink image-layout /
              image-builds checks for the OCI image)
2.5           build each fork's ``hermeticBuild`` check — the jar built in the
              network-disabled Nix sandbox
3.x           build the Lavalink fork's ``jreVersion`` check — the assembled
              image's bundled JRE reports Java feature version **25**
============  ==============================================================

Like every ``nix``-dependent command, these integration verifications are
**gated on builder availability**: when ``nix`` is not on ``PATH`` they are
SKIPPED cleanly (reported distinctly, never failing the run by default),
mirroring the spec's own "WHERE a Nix builder is available" gating (R12.2).

Aggregation contract (R12.7)
----------------------------

The harness runs each command and classifies its outcome into exactly one of
PASS / FAIL / SKIP (see :mod:`hellodj_platform_logic.verification_harness`):

* **FAIL** — the command exited non-zero OR its output reported a failure. Each
  fail carries the failing *command* and the *artifact* it was verifying so the
  aggregate report names both (R12.7).
* **SKIP** — the command's builder/toolchain is unavailable in this environment
  (e.g. no ``nix`` binary for the flake/AMI builds, no ``npx`` for cdk synth /
  jest). A skip is **not** a verification failure — several of the spec's own
  integration criteria are gated on builder availability ("WHERE a Nix builder
  is available", R12.2) — but it is reported *distinctly* from a pass so a
  reviewer never mistakes an un-run builder for a green run.

**Overall verdict:** verification FAILS iff **any** command is classified FAIL.
Skips alone do not fail the run by default; use ``--require-all`` to additionally
treat any SKIP as a failure (strict CI mode demanding a fully-provisioned
builder environment).

Exit codes
----------

* ``0`` — every runnable command passed; no fails (skips allowed by default).
* ``1`` — at least one command FAILED (R12.7); the report names each failing
  command + artifact.
* ``2`` — an operational error in the harness itself (bad arguments), or, under
  ``--require-all``, at least one command was SKIPPED.

Usage::

    python tools/verify_all.py                 # run the real R12.1-6 path
    python tools/verify_all.py --require-all    # strict: skips count as failures
    python tools/verify_all.py --list           # print the command plan, run nothing
    python tools/verify_all.py --self-test      # exercise the aggregation logic

Design references:
    * Requirement 12.7 — failure aggregation identifies the failing command +
      artifact.
    * Design §Testing Strategy — "Verification-harness failure aggregation:
      induce one failing command and assert it is reported as failed with the
      command/artifact named (12.7)."
    * Design §Testing Strategy / Integration tests — "`nix flake check` … exits
      0 (12.1, 2.7); `nix build .#<jar>`/`.#image` … produces a real jar/OCI
      image (12.2); Hermetic build: build each Fork_Flake in the
      network-disabled Nix sandbox and assert success (2.5); Temurin 25: … run
      the Lavalink image's JRE and assert Java feature version 25 (3.5)."

Requirements: 12.7, 12.1, 12.2, 2.5, 2.6, 2.7, 3.x
"""

from __future__ import annotations

import platform
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

# Make the shared pure-logic package importable without installation, mirroring
# the layout used by the other platform tools (resolve_closure.py etc.).
if str(COMPONENTS_ROOT) not in sys.path:
    sys.path.insert(0, str(COMPONENTS_ROOT))

from hellodj_platform_logic.verification_harness import (  # noqa: E402
    AvailabilityCheck,
    CommandRunner,
    Outcome,
    VerifyCommand,
    VerifyReport,
    aggregate,
    decide_exit_code,
    format_report,
    reports_failure,
)

#: The four JVM fork repos, checked out as siblings of the hellodj repo. Their
#: flakes live at each repo root. Referenced for `nix flake check` / `nix build`.
FORK_ROOT = PLATFORM_ROOT.parent.parent  # /home/celes/sources/celesrenata
FORK_REPOS = ("Lavalink", "lavaplayer", "LavaSrc", "youtube-source")

#: Per-command timeout budget (seconds). Nix builds are slow; this only guards
#: against a hung command, not normal build time.
DEFAULT_COMMAND_TIMEOUT = 3600.0

#: The `nix build .#<jar>`/`.#image` target for each fork (R12.2). The real
#: artifact each fork produces; building it proves a real jar/OCI image exists.
FORK_BUILD_TARGETS = {
    "Lavalink": ".#image",
    "lavaplayer": ".#lavaplayerJar",
    "LavaSrc": ".#lavasrcPlugin",
    "youtube-source": ".#youtubeSabrPlugin",
}

#: The fork `checks.<system>.<name>` attribute that asserts the built jar/plugin
#: (or, for Lavalink, the image) is REAL — a manifest + compiled `.class`
#: entries and NO `PLACEHOLDER ARTIFACT` marker (R2.6). Building the check
#: derivation forces the offline Gradle build to have succeeded first.
FORK_REAL_ARTIFACT_CHECKS = {
    "Lavalink": "imageLayout",
    "lavaplayer": "lavaplayerJar",
    "LavaSrc": "lavasrcPlugin",
    "youtube-source": "youtubeSabrPlugin",
}

#: The three JVM plugin/jar forks that ship an explicit `hermeticBuild` check —
#: the jar built in the network-disabled Nix sandbox (R2.5). Lavalink's jar is
#: exercised hermetically via its own build chain (its `image`/`lavalinkJar`
#: checks force the offline Gradle build), so it is not double-listed here.
FORK_HERMETIC_CHECKS = {
    "lavaplayer": "hermeticBuild",
    "LavaSrc": "hermeticBuild",
    "youtube-source": "hermeticBuild",
}

#: The Lavalink fork check that runs the assembled image's bundled JRE and
#: asserts it reports Java feature version 25 (R3.5 / R3.x).
LAVALINK_TEMURIN_CHECK = "jreVersion"


def _current_system() -> str:
    """Return the Nix system double for this host (e.g. ``x86_64-linux``).

    Fork flake ``checks`` are keyed by system (``checks.<system>.<name>``), so a
    ``nix build .#checks.<system>.<name>`` command must name the current system.
    Falls back to ``x86_64-linux`` for unrecognised machine strings; the
    commands are only ever *run* when a Nix builder is available, and are
    otherwise skipped, so the fallback is harmless for a list-only invocation.
    """
    machine = platform.machine().lower()
    arch = {
        "x86_64": "x86_64",
        "amd64": "x86_64",
        "aarch64": "aarch64",
        "arm64": "aarch64",
    }.get(machine, "x86_64")
    system_os = "darwin" if sys.platform == "darwin" else "linux"
    return f"{arch}-{system_os}"


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
        # The executable vanished between the availability check and the run.
        return 127, f"executable not found: {command.argv[0]}"
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {DEFAULT_COMMAND_TIMEOUT:.0f}s"
    return completed.returncode, (completed.stdout or "") + (completed.stderr or "")


# ---------------------------------------------------------------------------
# The concrete R12.1-6 command plan
# ---------------------------------------------------------------------------


def _fork_dir(name: str) -> Path:
    """Return the checkout directory for a fork repo (sibling of hellodj)."""
    return FORK_ROOT / name


def _component_dirs_with_flakes() -> list[Path]:
    """Return component directories that carry a ``flake.nix`` (image builders).

    The pure-logic package ships no image and has no flake, so it is naturally
    excluded. A missing ``components/`` directory yields an empty segment.
    """
    if not COMPONENTS_ROOT.is_dir():
        return []
    return [
        child
        for child in COMPONENTS_ROOT.iterdir()
        if child.is_dir() and (child / "flake.nix").is_file()
    ]


def build_integration_plan(system: str | None = None) -> list[VerifyCommand]:
    """Construct the task 20.3 integration-verification command plan.

    These commands make the individual correctness concerns of Requirements
    2.5/2.6/2.7 and 3.x visible as distinct, independently-reported checks
    across every Fork_Flake (and the Lavalink Component_Flake image), beyond the
    single ``nix flake check`` / ``nix build`` commands of R12.1/R12.2:

    * **2.7 / 12.1** — ``nix flake check`` exits 0 for every Fork_Flake and the
      Lavalink Component_Flake (already enumerated by :func:`build_command_plan`
      under R12.1; not duplicated here).
    * **2.6** — build each fork's real-artifact ``checks`` output, asserting a
      real jar/plugin (manifest + ``.class`` entries) with **no**
      ``PLACEHOLDER ARTIFACT`` marker, and the Lavalink image-layout check for
      the assembled OCI image.
    * **2.5** — build each plugin fork's ``hermeticBuild`` check: the jar built
      in the network-disabled Nix sandbox.
    * **3.x** — build the Lavalink fork's ``jreVersion`` check: the assembled
      image's bundled JRE reports Java feature version 25.

    Every command is ``nix``-gated (``needs="nix"``): when no Nix builder is on
    ``PATH`` the command is SKIPPED cleanly rather than failing (R12.2's
    "WHERE a Nix builder is available" gating), exactly like the R12.1/R12.2
    commands.

    Args:
        system: The Nix system double (``checks.<system>.<name>`` is
            system-keyed). Defaults to the current host's system.

    Returns:
        The integration-verification command plan (task 20.3).
    """
    sys_double = system or _current_system()
    plan: list[VerifyCommand] = []

    # -- 2.6: real jar/plugin (no placeholder) for every fork ------------------
    # Building the fork's real-artifact check derivation forces its offline
    # Gradle build to have succeeded and re-asserts the output is a real jar
    # (manifest + compiled .class entries) with no PLACEHOLDER ARTIFACT marker.
    for fork in FORK_REPOS:
        check = FORK_REAL_ARTIFACT_CHECKS[fork]
        plan.append(
            VerifyCommand(
                requirement="2.6",
                artifact=f"fork:{fork}:real-artifact",
                argv=(
                    "nix",
                    "build",
                    f".#checks.{sys_double}.{check}",
                    "--no-link",
                ),
                cwd=_fork_dir(fork),
                needs="nix",
                description=(
                    f"real jar/image (no PLACEHOLDER marker) for fork {fork} "
                    f"(checks.{sys_double}.{check})"
                ),
            )
        )

    # -- 2.5: hermetic (network-disabled sandbox) build for each plugin fork ---
    for fork, check in FORK_HERMETIC_CHECKS.items():
        plan.append(
            VerifyCommand(
                requirement="2.5",
                artifact=f"fork:{fork}:hermetic-build",
                argv=(
                    "nix",
                    "build",
                    f".#checks.{sys_double}.{check}",
                    "--no-link",
                ),
                cwd=_fork_dir(fork),
                needs="nix",
                description=(
                    f"hermetic (network-disabled sandbox) build for fork {fork} "
                    f"(checks.{sys_double}.{check})"
                ),
            )
        )

    # -- 3.x: the Lavalink image's bundled JRE reports Java feature version 25 -
    plan.append(
        VerifyCommand(
            requirement="3.5",
            artifact="fork:Lavalink:temurin-25",
            argv=(
                "nix",
                "build",
                f".#checks.{sys_double}.{LAVALINK_TEMURIN_CHECK}",
                "--no-link",
            ),
            cwd=_fork_dir("Lavalink"),
            needs="nix",
            description=(
                "Lavalink image JRE reports Java feature version 25 "
                f"(checks.{sys_double}.{LAVALINK_TEMURIN_CHECK})"
            ),
        )
    )

    return plan


def build_command_plan() -> list[VerifyCommand]:
    """Construct the full R12.1-6 verification command plan.

    Enumerates, in requirement order: R12.1 ``nix flake check`` for each fork +
    component flake; R12.2 ``nix build`` for each fork jar/image + component
    image; R12.3 the base-image gate; R12.4 the GPU AMI build; R12.5 ``npx cdk
    synth``; R12.6 the jest suite. Every command records the artifact it
    verifies so a failure names it (R12.7).
    """
    plan: list[VerifyCommand] = []
    components = sorted(_component_dirs_with_flakes())

    # -- R12.1: nix flake check for every Fork_Flake and Component_Flake --------
    for fork in FORK_REPOS:
        plan.append(
            VerifyCommand(
                requirement="12.1",
                artifact=f"fork:{fork}",
                argv=("nix", "flake", "check"),
                cwd=_fork_dir(fork),
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
                cwd=_fork_dir(fork),
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
    # The build path pushes each built closure to the S3-backed Nix binary cache
    # and confirms it is retrievable (narinfo read-back) BEFORE the artifact is
    # marked available for stage deploy (R7.7), publishing closures→cache and
    # images→ECR on a build (R6.2). `tools/verify_cache.py` models and checks
    # that push→verify→available ordering, so this integration criterion runs in
    # every environment (no real Nix/S3 needed — it uses the host Python).
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
    # On top of the requirement-level flake-check / nix-build commands above,
    # add distinctly-reported integration verifications for the real-jar /
    # no-placeholder (2.6), hermetic-build (2.5), and Temurin-25 (3.x) concerns.
    # Each is nix-gated, so it SKIPs cleanly when no builder is available.
    plan.extend(build_integration_plan())

    return plan


# ---------------------------------------------------------------------------
# Self-test — exercises the aggregation logic without any real builder
# ---------------------------------------------------------------------------


def _run_self_test() -> int:
    """Verify the aggregation core end to end without any real command (R12.7).

    Exercises the four outcomes the harness must distinguish:

    * an all-pass run succeeds (exit 0);
    * a single failing command fails the whole run and names its command +
      artifact (R12.7);
    * a command that exits 0 but *reports* a failure is still a FAIL;
    * a skipped (unavailable-builder) command does not fail the run by default,
      but does under ``--require-all``.
    """
    ok = True

    def cmd(requirement: str, artifact: str, needs: str = "always") -> VerifyCommand:
        return VerifyCommand(
            requirement=requirement,
            artifact=artifact,
            argv=("true",),
            cwd=PLATFORM_ROOT,
            needs=needs,
            description=f"{artifact} check",
        )

    available_all: AvailabilityCheck = lambda _exe: True

    # 1. All-pass run succeeds.
    all_pass = aggregate(
        [cmd("12.1", "fork:Lavalink"), cmd("12.3", "base-image-gate")],
        runner=lambda _c: (0, "ok"),
        is_available=available_all,
    )
    if all_pass.failed or decide_exit_code(all_pass, require_all=False) != 0:
        print("self-test FAILED: an all-pass run should succeed")
        ok = False

    # 2. A single failing command fails the whole run and names command+artifact.
    def one_fails(command: VerifyCommand) -> tuple[int, str]:
        if command.artifact == "component:web-ui:.#image":
            return (1, "boom")
        return (0, "ok")

    mixed = aggregate(
        [
            cmd("12.1", "fork:Lavalink"),
            cmd("12.2", "component:web-ui:.#image"),
            cmd("12.6", "jest-suite"),
        ],
        runner=one_fails,
        is_available=available_all,
    )
    if not mixed.failed:
        print("self-test FAILED: one failing command must fail the whole run (R12.7)")
        ok = False
    if len(mixed.failures) != 1:
        print("self-test FAILED: exactly one failure expected")
        ok = False
    else:
        rendered = format_report(mixed, require_all=False)
        failing = mixed.failures[0].command
        if failing.artifact not in rendered or failing.description not in rendered:
            print("self-test FAILED: report must name the failing command + artifact")
            ok = False
    if decide_exit_code(mixed, require_all=False) != 1:
        print("self-test FAILED: a failing run must exit 1")
        ok = False

    # 3. Exit 0 but reported failure in output is still a FAIL.
    reported = aggregate(
        [cmd("12.3", "base-image-gate")],
        runner=lambda _c: (0, "base-image gate FAILED: 1 non-Nix base(s) detected."),
        is_available=available_all,
    )
    if not reported.failed:
        print("self-test FAILED: exit 0 with a failure marker must be a FAIL (R12.7)")
        ok = False

    # 4. A skip does not fail by default, but does under --require-all.
    def only_missing_nix(exe: str) -> bool:
        return exe != "nix"

    skipped = aggregate(
        [cmd("12.1", "fork:Lavalink", needs="nix"), cmd("12.3", "gate", needs="always")],
        runner=lambda _c: (0, "ok"),
        is_available=only_missing_nix,
    )
    if skipped.failed:
        print("self-test FAILED: a skip is not a verification failure")
        ok = False
    if len(skipped.skips) != 1:
        print("self-test FAILED: the unavailable-builder command must be SKIPPED")
        ok = False
    if decide_exit_code(skipped, require_all=False) != 0:
        print("self-test FAILED: skips alone must not fail the run by default")
        ok = False
    if decide_exit_code(skipped, require_all=True) != 2:
        print("self-test FAILED: --require-all must treat a skip as incomplete (exit 2)")
        ok = False

    # 5. Task 20.3 integration verifications are present, nix-gated, and SKIP
    #    cleanly when no Nix builder is available (never fail the run).
    integration = build_integration_plan("x86_64-linux")
    if not all(c.needs == "nix" for c in integration):
        print("self-test FAILED: integration verifications must be nix-gated")
        ok = False
    integration_reqs = {c.requirement for c in integration}
    if not ({"2.5", "2.6", "3.5"} <= integration_reqs):
        print("self-test FAILED: integration plan must cover 2.5, 2.6, and 3.x")
        ok = False
    no_nix = aggregate(
        integration,
        runner=lambda _c: (0, "ok"),
        is_available=lambda exe: exe != "nix",
    )
    if no_nix.failed or len(no_nix.skips) != len(integration):
        print("self-test FAILED: integration checks must SKIP without a Nix builder")
        ok = False

    # Guard against dead imports the tool re-exports for its tests.
    assert Outcome.PASS.value == "PASS"
    assert reports_failure("all good") is None
    assert isinstance(VerifyReport().results, list)

    if ok:
        print(
            "self-test passed: all-pass succeeds; a single fail (exit non-zero OR "
            "reported failure) fails the run and names command+artifact; skips are "
            "distinct and non-failing by default (R12.7); the task 20.3 integration "
            "verifications (2.5/2.6/3.x) are nix-gated and skip cleanly without a "
            "builder."
        )
        return 0
    return 1


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
    """Run the plan, print the report, and return ``(exit_code, report)``.

    Args:
        commands: The verification command plan.
        require_all: Whether a SKIP should count as a failure (strict CI mode).
        runner: Override runner (tests); defaults to the subprocess runner.
        is_available: Override availability check (tests); defaults to PATH.

    Returns:
        The ``(exit_code, report)`` pair.
    """
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
        rc = _run_self_test()
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

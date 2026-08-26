"""Private helpers for the verification harness (verify_all.py).

Extracted from the main runner to keep it under 500 lines. Contains:
- Fork/component discovery helpers
- The integration verification plan builder (task 20.3)
- The self-test logic
"""

from __future__ import annotations

import platform
import sys
from pathlib import Path

PLATFORM_ROOT = Path(__file__).resolve().parent.parent
COMPONENTS_ROOT = PLATFORM_ROOT / "components"
INFRA_ROOT = PLATFORM_ROOT / "infra"
TOOLS_ROOT = PLATFORM_ROOT / "tools"

# Make the shared pure-logic package importable without installation.
if str(COMPONENTS_ROOT) not in sys.path:
    sys.path.insert(0, str(COMPONENTS_ROOT))

from hellodj_platform_logic.verification_harness import (  # noqa: E402
    Outcome,
    VerifyCommand,
    VerifyReport,
    aggregate,
    decide_exit_code,
    format_report,
    reports_failure,
)

#: The four JVM fork repos, checked out as siblings of the hellodj repo.
FORK_ROOT = PLATFORM_ROOT.parent.parent  # /home/celes/sources/celesrenata
FORK_REPOS = ("Lavalink", "lavaplayer", "LavaSrc", "youtube-source")

#: The `nix build .#<jar>`/`.#image` target for each fork (R12.2).
FORK_BUILD_TARGETS = {
    "Lavalink": ".#image",
    "lavaplayer": ".#lavaplayerJar",
    "LavaSrc": ".#lavasrcPlugin",
    "youtube-source": ".#youtubeSabrPlugin",
}

#: The fork `checks.<system>.<name>` attribute that asserts the built jar/plugin
#: is REAL (R2.6).
FORK_REAL_ARTIFACT_CHECKS = {
    "Lavalink": "imageLayout",
    "lavaplayer": "lavaplayerJar",
    "LavaSrc": "lavasrcPlugin",
    "youtube-source": "youtubeSabrPlugin",
}

#: Three JVM plugin/jar forks with an explicit `hermeticBuild` check (R2.5).
FORK_HERMETIC_CHECKS = {
    "lavaplayer": "hermeticBuild",
    "LavaSrc": "hermeticBuild",
    "youtube-source": "hermeticBuild",
}

#: Lavalink fork check for Java feature version 25 (R3.5 / R3.x).
LAVALINK_TEMURIN_CHECK = "jreVersion"


def current_system() -> str:
    """Return the Nix system double for this host (e.g. ``x86_64-linux``)."""
    machine = platform.machine().lower()
    arch = {
        "x86_64": "x86_64",
        "amd64": "x86_64",
        "aarch64": "aarch64",
        "arm64": "aarch64",
    }.get(machine, "x86_64")
    system_os = "darwin" if sys.platform == "darwin" else "linux"
    return f"{arch}-{system_os}"


def fork_dir(name: str) -> Path:
    """Return the checkout directory for a fork repo (sibling of hellodj)."""
    return FORK_ROOT / name


def component_dirs_with_flakes() -> list[Path]:
    """Return component directories that carry a ``flake.nix`` (image builders)."""
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

    * **2.6** — build each fork's real-artifact ``checks`` output.
    * **2.5** — build each plugin fork's ``hermeticBuild`` check.
    * **3.x** — build the Lavalink fork's ``jreVersion`` check.

    Every command is ``nix``-gated: when no Nix builder is on ``PATH`` the
    command is SKIPPED cleanly rather than failing.
    """
    sys_double = system or current_system()
    plan: list[VerifyCommand] = []

    # -- 2.6: real jar/plugin (no placeholder) for every fork ------------------
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
                cwd=fork_dir(fork),
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
                cwd=fork_dir(fork),
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
            cwd=fork_dir("Lavalink"),
            needs="nix",
            description=(
                "Lavalink image JRE reports Java feature version 25 "
                f"(checks.{sys_double}.{LAVALINK_TEMURIN_CHECK})"
            ),
        )
    )

    return plan


def run_self_test() -> int:
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

    def available_all(_exe):
        return True

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

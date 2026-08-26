"""Verification-harness failure aggregation logic (R12.7).

This module holds the pure decision core that governs the end-to-end
verification harness (``tools/verify_all.py``), so the "run the R12.1-6 command
set and aggregate the results" behaviour is free of any Nix / CDK / jest /
subprocess dependency and can be exercised directly by the property/example
tests. Requirement 12.7 states:

    IF any of the verification commands in criteria 1-6 exits non-zero or
    reports a failure, THEN the verification SHALL be treated as failed and
    SHALL identify the failing command and artifact.

The harness runs each :class:`VerifyCommand` and classifies its result into one
of three :class:`Outcome` values:

* **PASS** — exited 0 and reported no failure.
* **FAIL** — exited non-zero OR its output reported a failure (the two halves of
  R12.7). Each fail carries the failing command + the artifact it verified so
  the aggregate report names both.
* **SKIP** — the command's builder/toolchain was unavailable in this environment
  (e.g. no ``nix``/``npx``). A skip is **not** a verification failure — several
  of the spec's own integration criteria are gated on builder availability
  ("WHERE a Nix builder is available", R12.2) — but it is reported *distinctly*
  from a pass so an un-run builder is never mistaken for a green run.

The concrete command plan, the subprocess runner, the ``PATH`` availability
check, and the CLI live in the thin ``tools/verify_all.py`` wrapper; this module
is the shared, testable decision logic (mirroring how ``binary_cache`` /
``ephemeral_build`` back their ``tools/`` wrappers).

Design references:
    * Requirement 12.7 — failure aggregation identifies the failing command +
      artifact.
    * Design §Testing Strategy — "Verification-harness failure aggregation:
      induce one failing command and assert it is reported as failed with the
      command/artifact named (12.7)."

Requirements: 12.7
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

#: Case-insensitive substrings that, if present in a command's OUTPUT, indicate
#: a reported failure even when the process happened to exit 0. This satisfies
#: the "exits non-zero OR reports a failure" clause of R12.7. Kept deliberately
#: conservative to avoid false positives on benign words.
FAILURE_MARKERS: tuple[str, ...] = (
    "error:",
    "evaluation error",
    "failed to build",
    "build failed",
    "PLACEHOLDER ARTIFACT",
    "base-image gate FAILED",
    "REJECT ",
    "FAILED:",
    " failing test",
    "tests failed",
)


class Outcome(str, Enum):
    """The classification of a single verification command's result (R12.7)."""

    PASS = "PASS"
    FAIL = "FAIL"
    SKIP = "SKIP"


@dataclass(frozen=True)
class VerifyCommand:
    """One R12.1-6 verification command and the artifact it verifies.

    Attributes:
        requirement: The Requirement 12 sub-number this command satisfies
            (e.g. ``"12.1"``), for traceability in the aggregate report.
        artifact: The specific artifact under verification (e.g. a fork /
            component name, ``"gpu-ami"``, ``"cdk-app"``, ``"jest-suite"``) so a
            failure names exactly what failed (R12.7).
        argv: The command to run, as a fixed argument vector (never a shell
            string).
        cwd: The working directory to run the command in.
        needs: The executable this command requires; when it is not available
            the command is SKIPPED (builder/toolchain unavailable), not failed.
        description: A short human label for the report.
    """

    requirement: str
    artifact: str
    argv: tuple[str, ...]
    cwd: Path
    needs: str
    description: str


@dataclass(frozen=True)
class CommandResult:
    """The classified result of running one :class:`VerifyCommand`.

    Attributes:
        command: The command that produced this result.
        outcome: PASS / FAIL / SKIP (R12.7).
        exit_code: The process exit code (``None`` when skipped / not run).
        detail: A short reason string (why it failed / was skipped, or the
            failure marker found in output).
    """

    command: VerifyCommand
    outcome: Outcome
    exit_code: int | None = None
    detail: str = ""


@dataclass
class VerifyReport:
    """The aggregated verdict over all R12.1-6 commands (R12.7).

    ``failed`` is truthy iff at least one command was classified FAIL. Under the
    strict ``require_all`` policy a SKIP also makes the run non-successful, but
    it is tracked separately from a genuine FAIL so the report never conflates
    "builder unavailable" with "verification failed".
    """

    results: list[CommandResult] = field(default_factory=list)

    @property
    def failures(self) -> list[CommandResult]:
        """Every command classified FAIL (exit non-zero or reported failure)."""
        return [r for r in self.results if r.outcome is Outcome.FAIL]

    @property
    def skips(self) -> list[CommandResult]:
        """Every command SKIPPED because its builder/toolchain was unavailable."""
        return [r for r in self.results if r.outcome is Outcome.SKIP]

    @property
    def passes(self) -> list[CommandResult]:
        """Every command that passed."""
        return [r for r in self.results if r.outcome is Outcome.PASS]

    @property
    def failed(self) -> bool:
        """Whether verification is treated as FAILED — any single FAIL (R12.7)."""
        return bool(self.failures)


#: A runner injected by the tool / tests. Given a command, return
#: ``(exit_code, combined_output)``.
CommandRunner = Callable[[VerifyCommand], "tuple[int, str]"]

#: A predicate deciding whether a required executable is available.
AvailabilityCheck = Callable[[str], bool]


def reports_failure(output: str) -> str | None:
    """Return the failure marker found in ``output``, or ``None`` if clean.

    Implements the "reports a failure" half of R12.7: a command that exits 0 but
    prints a recognised failure marker is still a FAIL.
    """
    lowered = output.lower()
    for marker in FAILURE_MARKERS:
        if marker.lower() in lowered:
            return marker.strip()
    return None


def classify(
    command: VerifyCommand,
    *,
    runner: CommandRunner,
    is_available: AvailabilityCheck,
) -> CommandResult:
    """Run one command and classify it PASS / FAIL / SKIP (R12.7).

    Ordering of the decision:

    1. If the command's required builder/toolchain is unavailable, SKIP it (a
       skip is not a verification failure, but it is reported distinctly).
    2. Otherwise run it. A non-zero exit is a FAIL naming the command + artifact.
    3. A zero exit whose output contains a failure marker is also a FAIL (the
       "reports a failure" clause of R12.7).
    4. Otherwise PASS.

    Args:
        command: The verification command to run.
        runner: Injected process runner returning ``(exit_code, output)``.
        is_available: Injected predicate for builder/toolchain availability.

    Returns:
        The classified :class:`CommandResult`.
    """
    if not is_available(command.needs):
        return CommandResult(
            command=command,
            outcome=Outcome.SKIP,
            exit_code=None,
            detail=(
                f"builder/toolchain '{command.needs}' unavailable in this "
                "environment — skipped (not a verification failure)"
            ),
        )

    exit_code, output = runner(command)
    if exit_code != 0:
        return CommandResult(
            command=command,
            outcome=Outcome.FAIL,
            exit_code=exit_code,
            detail=f"exited {exit_code}",
        )

    marker = reports_failure(output)
    if marker is not None:
        return CommandResult(
            command=command,
            outcome=Outcome.FAIL,
            exit_code=exit_code,
            detail=f"exit 0 but output reported a failure: {marker!r}",
        )

    return CommandResult(command=command, outcome=Outcome.PASS, exit_code=exit_code)


def aggregate(
    commands: Iterable[VerifyCommand],
    *,
    runner: CommandRunner,
    is_available: AvailabilityCheck,
) -> VerifyReport:
    """Run and classify every command, aggregating into a :class:`VerifyReport`.

    This is the pure aggregation core (R12.7): it does not itself decide the
    process exit code or the skip policy — it only produces the classified
    results. :func:`decide_exit_code` turns the report + policy into an exit
    code, and :func:`format_report` renders it.
    """
    report = VerifyReport()
    for command in commands:
        report.results.append(
            classify(command, runner=runner, is_available=is_available)
        )
    return report


def decide_exit_code(report: VerifyReport, *, require_all: bool) -> int:
    """Turn an aggregated report + skip policy into a process exit code.

    * Any FAIL -> ``1`` (verification failed — R12.7).
    * Else, under ``require_all``, any SKIP -> ``2`` (strict: a skipped builder
      means the environment could not fully verify).
    * Else -> ``0``.
    """
    if report.failed:
        return 1
    if require_all and report.skips:
        return 2
    return 0


def format_report(report: VerifyReport, *, require_all: bool) -> str:
    """Render the aggregated report as human-readable text (R12.7).

    Lists every command's outcome, then — if verification failed — names each
    failing command and the artifact it was verifying. Skips are summarised
    distinctly so an un-run builder is never mistaken for a green run.
    """
    lines: list[str] = ["verification harness (R12.1-6) — aggregated result:"]
    for result in report.results:
        cmd = result.command
        lines.append(
            f"  {result.outcome.value:4} R{cmd.requirement} {cmd.artifact} "
            f"— {cmd.description}"
            + (f" [{result.detail}]" if result.detail else "")
        )

    lines.append("")
    lines.append(
        f"summary: {len(report.passes)} passed, {len(report.failures)} failed, "
        f"{len(report.skips)} skipped (builder unavailable)."
    )

    if report.failed:
        lines.append("")
        lines.append(
            "VERIFICATION FAILED (R12.7) — the following command(s) exited "
            "non-zero or reported a failure:"
        )
        for result in report.failures:
            cmd = result.command
            lines.append(
                f"  FAIL R{cmd.requirement}: command '{cmd.description}' "
                f"(argv={' '.join(cmd.argv)}) verifying artifact "
                f"'{cmd.artifact}' — {result.detail}"
            )
    elif require_all and report.skips:
        lines.append("")
        lines.append(
            "VERIFICATION INCOMPLETE (--require-all): the following builder(s) "
            "were unavailable so their commands could not run:"
        )
        for result in report.skips:
            cmd = result.command
            lines.append(
                f"  SKIP R{cmd.requirement}: '{cmd.description}' verifying "
                f"'{cmd.artifact}' — {result.detail}"
            )
    else:
        lines.append("")
        lines.append(
            "VERIFICATION PASSED — every runnable command passed; no command "
            "exited non-zero or reported a failure."
        )
        if report.skips:
            lines.append(
                f"  ({len(report.skips)} command(s) skipped because their "
                "builder/toolchain was unavailable; rerun with --require-all in "
                "a fully provisioned environment to enforce them.)"
            )

    return "\n".join(lines)

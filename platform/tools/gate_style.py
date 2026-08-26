#!/usr/bin/env python3
"""Build-stage PEP 8 / line-count gate runner (task 18.3).

This is the executable the Beta -> Gamma -> Prod deployment pipeline invokes in
its build stage to enforce Requirements 13.2-13.4: *the platform complies with
PEP 8 for all Python sources, keeps each Python source file within the design's
maximum line count, and the pipeline reports (fails on) violations during the
build stage.* It is the thin CI wrapper that runs the two style checks the
design's "PEP8/line-count gate (ruff + max-line-count check)" calls for, so the
pipeline and the developer-facing tooling share a single source of truth:

  1. **PEP 8 / style** — ``ruff check`` over the platform's Python sources,
     using the ``[tool.ruff]`` config in ``pyproject.toml`` (E/W/F/I/N/UP/B).
  2. **Per-file line-count** — ``tools/check_line_count.py``, the 500-line-max
     hook that reads ``[tool.hellodj.line_count] max-line-count`` from
     ``pyproject.toml``.

Either check failing makes this runner exit non-zero, which fails the build
(R13.4). Both are run every invocation (the line-count check is not skipped
when ruff fails) so a single build surfaces every style and line-count
violation at once.

Running ruff
------------

Ruff is invoked via a small candidate cascade so the gate works both in CI
(where ruff is pinned) and locally: ``uvx ruff@<pin> check`` first (the repo's
pinned runner), then a ``ruff`` already on ``PATH``, then ``python -m ruff``.
The first runner that is actually available is used; if none is available the
gate fails closed (a missing linter must not silently pass the build).

Usage::

    python tools/gate_style.py [PATH ...]

With no arguments it checks the whole platform tree (ruff honours its
``extend-exclude`` config and the line-count hook skips vendored/venv dirs).
Explicit paths are forwarded to both checks, e.g. to gate a single component.

Design references:
    * Deployment Pipeline build-stage PEP8/line-count gate (R13.2-R13.4)
    * Correctness properties are not applicable (this is a CI runner over the
      already-tested line-count hook + the external ruff linter).

Requirements: 13.2, 13.3, 13.4
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

PLATFORM_ROOT = Path(__file__).resolve().parent.parent
CHECK_LINE_COUNT = PLATFORM_ROOT / "tools" / "check_line_count.py"

#: The ruff version the pipeline pins via ``uvx`` so CI and local runs agree.
RUFF_PIN = "0.6.9"


def _ruff_command() -> list[str] | None:
    """Return the ruff invocation prefix to use, or ``None`` if unavailable.

    Tries, in order: the pinned ``uvx ruff@<pin>`` runner (CI's canonical
    entry point), a ``ruff`` binary already on ``PATH``, and finally
    ``python -m ruff``. The first available runner wins; ``None`` means ruff
    could not be located at all and the gate must fail closed.
    """
    if shutil.which("uvx") is not None:
        return ["uvx", f"ruff@{RUFF_PIN}", "check"]
    if shutil.which("ruff") is not None:
        return ["ruff", "check"]
    try:
        import ruff  # noqa: F401  (import-only availability probe)

        return [sys.executable, "-m", "ruff", "check"]
    except ImportError:
        return None


def run_ruff(paths: list[str]) -> int:
    """Run ``ruff check`` over ``paths`` (or the whole tree), return exit code.

    Returns 0 when ruff reports no violations, non-zero when ruff reports style
    violations or when no ruff runner is available (fail closed).
    """
    prefix = _ruff_command()
    if prefix is None:
        print("PEP 8 gate FAILED: no ruff runner available (uvx/ruff/python -m ruff).")
        return 1
    command = [*prefix, *(paths or ["."])]
    print(f"running: {' '.join(command)}")
    result = subprocess.run(command, cwd=str(PLATFORM_ROOT), check=False)  # noqa: S603
    if result.returncode == 0:
        print("PEP 8 (ruff) check passed.")
    else:
        print("PEP 8 (ruff) check FAILED.")
    return result.returncode


def run_line_count(paths: list[str]) -> int:
    """Run the 500-line-max hook over ``paths`` (or the tree), return exit code."""
    command = [sys.executable, str(CHECK_LINE_COUNT), *paths]
    print(f"running: {' '.join(command)}")
    result = subprocess.run(command, cwd=str(PLATFORM_ROOT), check=False)  # noqa: S603
    return result.returncode


def main(argv: list[str]) -> int:
    """Run both style checks and return a combined non-zero code on any failure.

    Both checks always run so one build reports every violation (R13.4).
    """
    paths = list(argv)
    ruff_rc = run_ruff(paths)
    line_rc = run_line_count(paths)

    if ruff_rc == 0 and line_rc == 0:
        print("style gate passed: PEP 8 (ruff) and max-line-count both clean.")
        return 0
    print("style gate FAILED: fix the reported PEP 8 and/or line-count violations.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

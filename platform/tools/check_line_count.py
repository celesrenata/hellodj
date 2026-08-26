#!/usr/bin/env python3
"""Enforce the per-file maximum line count for Python sources.

This is the ``max-line-count`` check hook referenced by the platform tooling
config (Requirement 13.3). It reads the configured ceiling from
``pyproject.toml`` (``[tool.hellodj.line_count] max-line-count``) so that this
tool and the deployment-pipeline PEP 8 / line-count gate share a single source
of truth.

Usage::

    python tools/check_line_count.py [PATH ...]

With no arguments it scans every ``*.py`` file under the platform root
(excluding vendored/build directories). Exits non-zero and prints each
offending file when any file exceeds the configured maximum.
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

PLATFORM_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = PLATFORM_ROOT / "pyproject.toml"

# Directories that hold vendored, generated, or virtual-env content and must
# not be counted against the source-file ceiling.
EXCLUDED_DIR_NAMES = {
    "__pycache__",
    ".venv",
    "venv",
    "node_modules",
    "cdk.out",
    ".git",
    ".pytest_cache",
    ".hypothesis",
    ".ruff_cache",
}

DEFAULT_MAX_LINE_COUNT = 500


def load_max_line_count() -> int:
    """Read the configured maximum line count from pyproject.toml."""
    if not PYPROJECT.is_file():
        return DEFAULT_MAX_LINE_COUNT
    with PYPROJECT.open("rb") as handle:
        data = tomllib.load(handle)
    return int(
        data.get("tool", {})
        .get("hellodj", {})
        .get("line_count", {})
        .get("max-line-count", DEFAULT_MAX_LINE_COUNT)
    )


def _is_virtualenv_dir(candidate: Path) -> bool:
    """Return True when ``candidate`` is a Python virtual environment root.

    A virtual environment always carries a ``pyvenv.cfg`` marker at its root.
    Detecting that marker excludes vendored ``site-packages`` regardless of the
    env's directory name (``.venv``, ``venv``, ``.venv-pbt``, ...), so the gate
    only ever counts the platform's own sources, never third-party packages.
    """
    return (candidate / "pyvenv.cfg").is_file()


def is_excluded(path: Path) -> bool:
    """Return True when a path lies inside an excluded or virtual-env directory.

    A path is excluded when any of its segments is a known excluded directory
    name, or when any of its ancestor directories is a Python virtual
    environment (identified by a ``pyvenv.cfg`` marker), so vendored
    dependencies under an arbitrarily-named venv never trip the ceiling.
    """
    if any(part in EXCLUDED_DIR_NAMES for part in path.parts):
        return True
    for ancestor in path.parents:
        if _is_virtualenv_dir(ancestor):
            return True
    return False


def iter_python_files(roots: list[Path]) -> list[Path]:
    """Yield all non-excluded ``*.py`` files under the given roots."""
    files: list[Path] = []
    for root in roots:
        if root.is_file() and root.suffix == ".py":
            if not is_excluded(root):
                files.append(root)
            continue
        for candidate in root.rglob("*.py"):
            if not is_excluded(candidate):
                files.append(candidate)
    return files


def count_lines(path: Path) -> int:
    """Count the number of lines in a file."""
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        return sum(1 for _ in handle)


def main(argv: list[str]) -> int:
    """Run the line-count check and return a process exit code."""
    max_lines = load_max_line_count()
    roots = [Path(arg).resolve() for arg in argv] or [PLATFORM_ROOT]
    violations: list[tuple[Path, int]] = []

    for py_file in iter_python_files(roots):
        line_count = count_lines(py_file)
        if line_count > max_lines:
            violations.append((py_file, line_count))

    if violations:
        print(f"max-line-count check FAILED (limit={max_lines}):")
        for path, line_count in sorted(violations):
            rel = path.relative_to(PLATFORM_ROOT)
            print(f"  {rel}: {line_count} lines")
        return 1

    print(f"max-line-count check passed (limit={max_lines}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

#!/usr/bin/env python3
"""Python 3.14 migration-readiness gate for platform components (task 15.2).

Gates each Python component's migration from 3.11 to 3.14 on the pure
``python_migration_ready`` decision function (Property 5 / R5.3, R5.4).

For each of the seven enumerated Python components, this tool:
1. Attempts to import each dependency under the component's Python 3.14 venv
2. Runs the component's test suite under Python 3.14
3. Feeds results into ``python_migration_ready``
4. Reports the outcome: migrated (ready) or blocked (naming the dependency)

Usage::

    python tools/gate_python_migration.py                # check all components
    python tools/gate_python_migration.py discord-bot-core  # check one component
    python tools/gate_python_migration.py --dry-run      # show what would be checked

Requirements: 5.3, 5.4
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

PLATFORM_ROOT = Path(__file__).resolve().parent.parent
COMPONENTS_ROOT = PLATFORM_ROOT / "components"

if str(COMPONENTS_ROOT) not in sys.path:
    sys.path.insert(0, str(COMPONENTS_ROOT))

from hellodj_platform_logic.python_migration import (  # noqa: E402
    python_migration_ready,
)
from hellodj_platform_logic.types import DependencyCheck  # noqa: E402

# ---------------------------------------------------------------------------
# Component definitions
# ---------------------------------------------------------------------------

#: The seven Python components currently on 3.11, with their runtime deps.
COMPONENT_DEPENDENCIES: dict[str, list[str]] = {
    "discord-bot-core": ["discord.py", "wavelink", "aiohttp", "cryptography"],
    "playback-orchestrator": ["wavelink", "aiohttp", "cryptography"],
    "config-renderer": ["cryptography", "boto3"],
    "activity-backend": ["aiohttp", "flask"],
    "voice-pipeline": ["onnxruntime", "torch", "numpy", "discord.py"],
    "web-ui": ["flask", "gunicorn", "boto3"],
    "migration": ["boto3", "cryptography"],
}

#: Python 3.14 interpreter path (from the component flake's devShell or system).
PYTHON314 = "python3.14"


@dataclass(frozen=True)
class MigrationResult:
    """Result of checking one component's migration readiness."""

    component: str
    ready: bool
    blocker: str | None
    checks: list[DependencyCheck]
    test_passed: bool


# ---------------------------------------------------------------------------
# Import checking
# ---------------------------------------------------------------------------


def _check_import(dep_name: str, component_dir: Path) -> bool:
    """Check if a dependency imports under Python 3.14.

    Attempts to import the package in a subprocess using python3.14.
    Maps package names to their importable module names.
    """
    # Map package names to importable module names
    import_map: dict[str, str] = {
        "discord.py": "discord",
        "wavelink": "wavelink",
        "aiohttp": "aiohttp",
        "cryptography": "cryptography",
        "boto3": "boto3",
        "flask": "flask",
        "gunicorn": "gunicorn",
        "onnxruntime": "onnxruntime",
        "torch": "torch",
        "numpy": "numpy",
    }
    module_name = import_map.get(dep_name, dep_name)

    try:
        result = subprocess.run(
            [PYTHON314, "-c", f"import {module_name}"],
            cwd=component_dir,
            capture_output=True,
            text=True,
            timeout=30,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def _run_tests(component: str, component_dir: Path) -> bool:
    """Run the component's test suite under Python 3.14.

    Returns True if the test suite passes, False otherwise.
    """
    # Look for pytest or unittest in the component directory
    test_dir = component_dir / "tests"
    if not test_dir.exists():
        # No tests directory — treat as passing (nothing to block on)
        return True

    try:
        result = subprocess.run(
            [PYTHON314, "-m", "pytest", str(test_dir), "-x", "--tb=short", "-q"],
            cwd=component_dir,
            capture_output=True,
            text=True,
            timeout=120,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


# ---------------------------------------------------------------------------
# Main logic
# ---------------------------------------------------------------------------


def check_component(component: str) -> MigrationResult:
    """Check a single component's migration readiness."""
    deps = COMPONENT_DEPENDENCIES.get(component)
    if deps is None:
        print(f"  ERROR: unknown component '{component}'")
        return MigrationResult(
            component=component,
            ready=False,
            blocker=f"unknown-component:{component}",
            checks=[],
            test_passed=False,
        )

    component_dir = COMPONENTS_ROOT / component
    print(f"  [{component}] checking {len(deps)} dependencies ...")

    # Check each dependency import under 3.14
    checks: list[DependencyCheck] = []
    for dep in deps:
        imports_ok = _check_import(dep, component_dir)
        checks.append(DependencyCheck(name=dep, imports_ok=imports_ok))
        status = "✓" if imports_ok else "✗"
        print(f"    {status} {dep}")

    # Run test suite
    print(f"  [{component}] running test suite ...")
    test_passed = _run_tests(component, component_dir)
    test_status = "✓" if test_passed else "✗"
    print(f"    {test_status} test suite")

    # Feed into the pure decision function
    ready, blocker = python_migration_ready(checks, test_passed)
    return MigrationResult(
        component=component,
        ready=ready,
        blocker=blocker,
        checks=checks,
        test_passed=test_passed,
    )


def main(argv: list[str] | None = None) -> int:
    """Entry point: gate Python 3.14 migration for platform components.

    Returns 0 when all checked components are ready, 1 when any is blocked.
    """
    if argv is None:
        argv = sys.argv[1:]

    dry_run = "--dry-run" in argv
    argv = [a for a in argv if a != "--dry-run"]

    # Determine which components to check
    if argv:
        components = argv
    else:
        components = list(COMPONENT_DEPENDENCIES.keys())

    if dry_run:
        print("DRY RUN — would check these components for Python 3.14 readiness:")
        for comp in components:
            deps = COMPONENT_DEPENDENCIES.get(comp, [])
            print(f"  {comp}: {', '.join(deps) or '(no deps)'}")
        return 0

    print(f"Python 3.14 migration gate: checking {len(components)} component(s)\n")

    results: list[MigrationResult] = []
    for comp in components:
        result = check_component(comp)
        results.append(result)
        print()

    # Summary
    print("=" * 60)
    print("MIGRATION READINESS SUMMARY")
    print("=" * 60)

    blocked_count = 0
    for r in results:
        if r.ready:
            print(f"  READY   {r.component}")
        else:
            print(f"  BLOCKED {r.component}: {r.blocker}")
            blocked_count += 1

    print("=" * 60)

    if blocked_count == 0:
        print(f"All {len(results)} component(s) are ready for Python 3.14 migration.")
        return 0
    else:
        print(
            f"{blocked_count} of {len(results)} component(s) blocked — "
            "resolve blocking dependencies before marking migrated (R5.4)."
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

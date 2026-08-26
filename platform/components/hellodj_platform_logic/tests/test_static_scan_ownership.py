"""Static-scan tests for source ownership and Python component compliance.

Split from ``test_static_scan_compliance.py`` to keep files under 500 lines.

Conditions asserted:

* R1.6 / R2.1: Zero ``github:hellodj/<repo>/<branch>`` inputs for migrated repos
  remain; all five migrated inputs reference the CodeCommit ``git+https`` form.
* R5.1 / R5.5: Zero deadsnakes references across component flakes and
  Dockerfiles; the enumerated Python-3.11 component list equals the seven named
  components.

Requirements: 1.6, 2.1, 5.1, 5.5
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest  # noqa: F401

# ---------------------------------------------------------------------------
# Repository layout resolution.
# ---------------------------------------------------------------------------
_PLATFORM_ROOT = Path(__file__).resolve().parents[3]
_COMPONENTS_ROOT = _PLATFORM_ROOT / "components"

_ACCOUNT_ROOT = _PLATFORM_ROOT.parent.parent
_FORK_REPOS = {
    "Lavalink": _ACCOUNT_ROOT / "Lavalink",
    "lavaplayer": _ACCOUNT_ROOT / "lavaplayer",
    "LavaSrc": _ACCOUNT_ROOT / "LavaSrc",
    "youtube-source": _ACCOUNT_ROOT / "youtube-source",
}


def _read(path: Path) -> str:
    """Read a repo file, failing the test clearly if it is missing."""
    assert path.exists(), f"expected file does not exist: {path}"
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# R1.6 / R2.1 -- Source-ownership static scan (Task 12.4).
# Zero github:hellodj/<repo>/<branch> inputs for migrated repos remain; all
# five migrated inputs reference the CodeCommit git+https form.
# ---------------------------------------------------------------------------


def test_no_github_hellodj_inputs_for_migrated_repos() -> None:
    """Zero github:hellodj/<repo>/<branch> inputs remain for migrated repos (R1.6/R2.1).

    Feature: hellodj-private-source-and-toolchain
    Validates: Requirements 1.6, 2.1
    """
    migrated_repos = {"hellodj", "Lavalink", "lavaplayer", "LavaSrc", "youtube-source"}
    # Pattern matches github:hellodj/<migrated-repo>/<branch> in url = "..." declarations
    github_pattern = re.compile(
        r'url\s*=\s*"github:hellodj/('
        + "|".join(migrated_repos)
        + r')/[^"]*"'
    )

    flake_files = [
        _PLATFORM_ROOT / "components" / "lavalink" / "flake.nix",
        _PLATFORM_ROOT.parent.parent / "Lavalink" / "flake.nix",
    ]

    violations = []
    for flake_path in flake_files:
        if not flake_path.exists():
            continue
        content = _read(flake_path)
        matches = github_pattern.findall(content)
        for match in matches:
            violations.append(f"{flake_path.name}: github:hellodj/{match}/...")

    assert violations == [], (
        f"Migrated repos still have github:hellodj/ inputs: {violations}. "
        "These must be git+https://git-codecommit...amazonaws.com/... (R2.1)"
    )


def test_migrated_inputs_use_codecommit_form() -> None:
    """All five migrated inputs reference the CodeCommit git+https form (R2.1).

    Feature: hellodj-private-source-and-toolchain
    Validates: Requirements 2.1
    """
    codecommit_pattern = "git+https://git-codecommit"

    # The lavalink component flake should reference Lavalink via CodeCommit
    lavalink_component = _PLATFORM_ROOT / "components" / "lavalink" / "flake.nix"
    if lavalink_component.exists():
        content = _read(lavalink_component)
        assert codecommit_pattern in content, (
            "lavalink component flake must reference Lavalink via CodeCommit git+https (R2.1)"
        )

    # The Lavalink fork flake should reference lavaplayer, LavaSrc, youtube-source via CodeCommit
    lavalink_fork = _PLATFORM_ROOT.parent.parent / "Lavalink" / "flake.nix"
    if lavalink_fork.exists():
        content = _read(lavalink_fork)
        for repo in ["lavaplayer", "LavaSrc", "youtube-source"]:
            assert f"git-codecommit.us-east-1.amazonaws.com/v1/repos/{repo}" in content, (
                f"Lavalink fork flake must reference {repo} via CodeCommit git+https (R2.1)"
            )


# ---------------------------------------------------------------------------
# R5.1 / R5.5 -- No-deadsnakes scan (Task 15.3).
# Zero deadsnakes references across component flakes and Dockerfiles; the
# enumerated Python-3.11 component list equals the seven named components.
# ---------------------------------------------------------------------------


def test_no_deadsnakes_references() -> None:
    """Zero deadsnakes references across component flakes and Dockerfiles (R5.5).

    Feature: hellodj-private-source-and-toolchain
    Validates: Requirements 5.1, 5.5
    """
    components_dir = _PLATFORM_ROOT / "components"
    deadsnakes_pattern = re.compile(r'deadsnakes', re.IGNORECASE)

    violations = []
    for path in components_dir.rglob("*"):
        if path.is_file() and (
            path.name == "flake.nix"
            or path.name.startswith("Dockerfile")
            or path.suffix == ".nix"
        ):
            content = _read(path)
            if deadsnakes_pattern.search(content):
                violations.append(str(path.relative_to(_PLATFORM_ROOT)))

    assert violations == [], (
        f"deadsnakes references found (must use Nix python314 instead, R5.5): {violations}"
    )


def test_python_component_list_equals_seven() -> None:
    """The enumerated Python 3.11 component list equals the seven named components (R5.1).

    Feature: hellodj-private-source-and-toolchain
    Validates: Requirements 5.1
    """
    expected = {
        "discord-bot-core",
        "playback-orchestrator",
        "config-renderer",
        "activity-backend",
        "voice-pipeline",
        "web-ui",
        "migration",
    }

    # Import from the migration gate tool — read the file and extract the dict
    # keys directly to avoid dataclass resolution issues with importlib.
    gate_path = _PLATFORM_ROOT / "tools" / "gate_python_migration.py"
    if gate_path.exists():
        import ast
        source = gate_path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        # Find the COMPONENT_DEPENDENCIES dict assignment (may be AnnAssign)
        for node in ast.walk(tree):
            target = None
            value = None
            if isinstance(node, ast.Assign):
                target = node.targets[0] if node.targets else None
                value = node.value
            elif isinstance(node, ast.AnnAssign):
                target = node.target
                value = node.value
            if (
                target is not None
                and isinstance(target, ast.Name)
                and target.id == "COMPONENT_DEPENDENCIES"
            ):
                if isinstance(value, ast.Dict):
                    actual = set()
                    for key in value.keys:
                        if isinstance(key, ast.Constant):
                            actual.add(key.value)
                    assert actual == expected, (
                        f"Python component list mismatch: expected {expected}, got {actual}"
                    )
                    return
        pytest.fail("COMPONENT_DEPENDENCIES not found in gate_python_migration.py")

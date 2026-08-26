"""Private helpers for the CodeCommit migration tool (migrate_repos.py).

Extracted from the main runner to keep it under 500 lines. Contains:
- Subprocess helper (_run)
- History snapshot / verification logic
- Local repo location helper
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from typing import NamedTuple

PLATFORM_ROOT = Path(__file__).resolve().parent.parent


def _run(
    cmd: list[str],
    *,
    cwd: str | Path | None = None,
    check: bool = True,
    capture: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run a subprocess, capturing stdout/stderr as text."""
    return subprocess.run(
        cmd,
        cwd=cwd,
        check=check,
        capture_output=capture,
        text=True,
    )


# ---------------------------------------------------------------------------
# History verification
# ---------------------------------------------------------------------------


class _HistorySnapshot(NamedTuple):
    """Snapshot of a repo's history for preservation comparison."""

    branch_tips: dict[str, str]       # branch_name -> tip SHA
    tag_names: set[str]
    ancestor_sets: dict[str, list[str]]  # branch_name -> ordered SHAs


def _snapshot_local(repo_dir: Path) -> _HistorySnapshot:
    """Take a history snapshot of the local repository."""
    # Branch tips
    result = _run(
        ["git", "for-each-ref", "--format=%(refname:short) %(objectname)", "refs/heads/"],
        cwd=repo_dir,
    )
    branch_tips: dict[str, str] = {}
    for line in result.stdout.strip().splitlines():
        if line.strip():
            parts = line.strip().split()
            if len(parts) == 2:
                branch_tips[parts[0]] = parts[1]

    # Tag names
    result = _run(
        ["git", "for-each-ref", "--format=%(refname:short)", "refs/tags/"],
        cwd=repo_dir,
    )
    tag_names = {
        line.strip()
        for line in result.stdout.strip().splitlines()
        if line.strip()
    }

    # Ancestor SHA sets per branch
    ancestor_sets: dict[str, list[str]] = {}
    for branch in branch_tips:
        result = _run(
            ["git", "log", "--format=%H", branch],
            cwd=repo_dir,
        )
        ancestor_sets[branch] = [
            line.strip()
            for line in result.stdout.strip().splitlines()
            if line.strip()
        ]

    return _HistorySnapshot(
        branch_tips=branch_tips,
        tag_names=tag_names,
        ancestor_sets=ancestor_sets,
    )


def _snapshot_remote(remote_url: str) -> _HistorySnapshot:
    """Take a history snapshot by cloning the remote into a temp directory."""
    with tempfile.TemporaryDirectory(prefix="migrate_verify_") as tmpdir:
        clone_dir = Path(tmpdir) / "clone"
        _run(["git", "clone", "--mirror", remote_url, str(clone_dir)])

        # Branch tips (mirror clone uses refs/heads/)
        result = _run(
            ["git", "for-each-ref", "--format=%(refname:short) %(objectname)", "refs/heads/"],
            cwd=clone_dir,
        )
        branch_tips: dict[str, str] = {}
        for line in result.stdout.strip().splitlines():
            if line.strip():
                parts = line.strip().split()
                if len(parts) == 2:
                    branch_tips[parts[0]] = parts[1]

        # Tag names
        result = _run(
            ["git", "for-each-ref", "--format=%(refname:short)", "refs/tags/"],
            cwd=clone_dir,
        )
        tag_names = {
            line.strip()
            for line in result.stdout.strip().splitlines()
            if line.strip()
        }

        # Ancestor SHA sets per branch
        ancestor_sets: dict[str, list[str]] = {}
        for branch in branch_tips:
            result = _run(
                ["git", "log", "--format=%H", branch],
                cwd=clone_dir,
            )
            ancestor_sets[branch] = [
                line.strip()
                for line in result.stdout.strip().splitlines()
                if line.strip()
            ]

    return _HistorySnapshot(
        branch_tips=branch_tips,
        tag_names=tag_names,
        ancestor_sets=ancestor_sets,
    )


def verify_history(local_dir: Path, remote_url: str) -> bool:
    """Verify history preservation: tips, ancestors, branches, tags match."""
    try:
        local_snap = _snapshot_local(local_dir)
        remote_snap = _snapshot_remote(remote_url)
    except subprocess.CalledProcessError as exc:
        print(f"    history verification failed (subprocess error): {exc}")
        return False

    # Compare branch tips
    if local_snap.branch_tips != remote_snap.branch_tips:
        diff_branches = set(local_snap.branch_tips) ^ set(remote_snap.branch_tips)
        tip_mismatches = {
            b
            for b in local_snap.branch_tips
            if local_snap.branch_tips.get(b) != remote_snap.branch_tips.get(b)
        }
        print(f"    branch tip mismatch: diff_branches={diff_branches}, "
              f"tip_mismatches={tip_mismatches}")
        return False

    # Compare tag names
    if local_snap.tag_names != remote_snap.tag_names:
        missing = local_snap.tag_names - remote_snap.tag_names
        extra = remote_snap.tag_names - local_snap.tag_names
        print(f"    tag mismatch: missing_on_remote={missing}, extra_on_remote={extra}")
        return False

    # Compare ancestor sets per branch
    for branch in local_snap.branch_tips:
        local_ancestors = local_snap.ancestor_sets.get(branch, [])
        remote_ancestors = remote_snap.ancestor_sets.get(branch, [])
        if local_ancestors != remote_ancestors:
            print(f"    ancestor set mismatch on branch '{branch}': "
                  f"local has {len(local_ancestors)} commits, "
                  f"remote has {len(remote_ancestors)} commits")
            return False

    return True


def locate_local_repo(repo_name: str) -> Path:
    """Locate the local clone of a Source_Repo.

    Checks sibling directories of the hellodj project root (the standard
    ``celesrenata/`` working tree layout).
    """
    # hellodj project root is two levels above platform/tools/
    project_root = PLATFORM_ROOT.parent
    workspace_root = project_root.parent  # celesrenata/

    # The repo should be a sibling of hellodj under celesrenata/
    if repo_name == "hellodj":
        repo_dir = project_root
    else:
        repo_dir = workspace_root / repo_name

    if not (repo_dir / ".git").exists():
        raise FileNotFoundError(
            f"Local repo not found at {repo_dir} — ensure it is cloned "
            f"as a sibling of the hellodj project"
        )
    return repo_dir

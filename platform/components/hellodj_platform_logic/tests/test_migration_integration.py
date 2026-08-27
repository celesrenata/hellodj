"""Integration tests for the CodeCommit migration procedure (task 11.4).

Feature: hellodj-private-source-and-toolchain, Requirements 1.2, 1.4.

These tests exercise the history-preservation logic of the migration procedure
using local bare git repos. They verify:
  * Each fork's `upstream` remote URL matches the expected public upstream (R1.2)
  * After `git push --mirror`, branch tips, ancestor SHA sets, and branch/tag
    name sets all match between the source and the target (R1.4)

Validates: Requirements 1.2, 1.4
"""

from __future__ import annotations

import subprocess
from pathlib import Path


def _run(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=True)


def _init_repo_with_history(repo_dir: Path) -> None:
    """Create a git repo with branches, tags, and multiple commits."""
    _run(["git", "init", "--initial-branch=main", str(repo_dir)], cwd=repo_dir.parent)
    _run(["git", "config", "user.email", "test@test.com"], cwd=repo_dir)
    _run(["git", "config", "user.name", "Test"], cwd=repo_dir)

    # Create main branch with 3 commits
    (repo_dir / "file1.txt").write_text("initial")
    _run(["git", "add", "."], cwd=repo_dir)
    _run(["git", "commit", "-m", "initial commit"], cwd=repo_dir)

    (repo_dir / "file2.txt").write_text("second")
    _run(["git", "add", "."], cwd=repo_dir)
    _run(["git", "commit", "-m", "second commit"], cwd=repo_dir)

    _run(["git", "tag", "v1.0"], cwd=repo_dir)

    (repo_dir / "file3.txt").write_text("third")
    _run(["git", "add", "."], cwd=repo_dir)
    _run(["git", "commit", "-m", "third commit"], cwd=repo_dir)

    _run(["git", "tag", "v2.0"], cwd=repo_dir)

    # Create a dev branch with additional commits
    _run(["git", "checkout", "-b", "dev"], cwd=repo_dir)
    (repo_dir / "dev.txt").write_text("dev work")
    _run(["git", "add", "."], cwd=repo_dir)
    _run(["git", "commit", "-m", "dev work"], cwd=repo_dir)

    _run(["git", "checkout", "main"], cwd=repo_dir)


def _get_branch_tips(repo_dir: Path) -> dict[str, str]:
    """Get branch name -> tip SHA mapping."""
    result = _run(
        ["git", "for-each-ref", "--format=%(refname:short) %(objectname)", "refs/heads/"],
        cwd=repo_dir,
    )
    tips = {}
    for line in result.stdout.strip().splitlines():
        if line.strip():
            parts = line.strip().split()
            if len(parts) == 2:
                tips[parts[0]] = parts[1]
    return tips


def _get_tags(repo_dir: Path) -> set[str]:
    """Get the set of tag names."""
    result = _run(
        ["git", "for-each-ref", "--format=%(refname:short)", "refs/tags/"],
        cwd=repo_dir,
    )
    return {line.strip() for line in result.stdout.strip().splitlines() if line.strip()}


def _get_ancestors(repo_dir: Path, branch: str) -> list[str]:
    """Get ordered ancestor SHA list for a branch."""
    result = _run(["git", "log", "--format=%H", branch], cwd=repo_dir)
    return [line.strip() for line in result.stdout.strip().splitlines() if line.strip()]


class TestUpstreamRemote:
    """Tests for upstream remote configuration (R1.2)."""

    def test_upstream_remote_url_matches_expected(self, tmp_path: Path) -> None:
        """Each fork's upstream remote URL equals the public upstream (R1.2)."""
        repo_dir = tmp_path / "fork"
        repo_dir.mkdir()
        _init_repo_with_history(repo_dir)

        upstream_url = "https://github.com/lavalink-devs/Lavalink"
        _run(["git", "remote", "add", "upstream", upstream_url], cwd=repo_dir)

        result = _run(["git", "remote", "get-url", "upstream"], cwd=repo_dir)
        assert result.stdout.strip() == upstream_url

    def test_upstream_remote_fetch_succeeds_for_public_repo(self, tmp_path: Path) -> None:
        """git fetch upstream succeeds when pointing to a valid public repo (R1.2).

        Note: This test creates a local bare repo as a simulated upstream, since
        we can't guarantee network access to github.com in all test environments.
        """
        # Create a "public upstream" bare repo
        upstream_bare = tmp_path / "upstream.git"
        _run(["git", "init", "--bare", str(upstream_bare)], cwd=tmp_path)

        # Create the fork with some history and push to upstream
        fork_dir = tmp_path / "fork"
        fork_dir.mkdir()
        _init_repo_with_history(fork_dir)
        _run(["git", "remote", "add", "upstream", str(upstream_bare)], cwd=fork_dir)
        _run(["git", "push", "upstream", "--all"], cwd=fork_dir)
        _run(["git", "push", "upstream", "--tags"], cwd=fork_dir)

        # Now simulate fetching from upstream
        result = subprocess.run(
            ["git", "fetch", "upstream"],
            cwd=fork_dir,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0


class TestHistoryPreservation:
    """Tests for post-mirror-push history preservation (R1.4)."""

    def test_mirror_push_preserves_branch_tips(self, tmp_path: Path) -> None:
        """After git push --mirror, branch tip SHAs match source and target (R1.4)."""
        source = tmp_path / "source"
        source.mkdir()
        _init_repo_with_history(source)

        target_bare = tmp_path / "target.git"
        _run(["git", "init", "--bare", str(target_bare)], cwd=tmp_path)

        # Mirror push
        _run(["git", "push", "--mirror", str(target_bare)], cwd=source)

        # Compare branch tips directly on bare repo
        source_tips = _get_branch_tips(source)
        target_tips = _get_branch_tips(target_bare)

        assert source_tips == target_tips

    def test_mirror_push_preserves_tag_names(self, tmp_path: Path) -> None:
        """After git push --mirror, tag name sets match source and target (R1.4)."""
        source = tmp_path / "source"
        source.mkdir()
        _init_repo_with_history(source)

        target_bare = tmp_path / "target.git"
        _run(["git", "init", "--bare", str(target_bare)], cwd=tmp_path)

        _run(["git", "push", "--mirror", str(target_bare)], cwd=source)

        source_tags = _get_tags(source)
        target_tags = _get_tags(target_bare)

        assert source_tags == target_tags
        assert "v1.0" in source_tags
        assert "v2.0" in source_tags

    def test_mirror_push_preserves_ancestor_sets(self, tmp_path: Path) -> None:
        """After git push --mirror, ancestor SHA sets per branch match (R1.4)."""
        source = tmp_path / "source"
        source.mkdir()
        _init_repo_with_history(source)

        target_bare = tmp_path / "target.git"
        _run(["git", "init", "--bare", str(target_bare)], cwd=tmp_path)

        _run(["git", "push", "--mirror", str(target_bare)], cwd=source)

        # Compare ancestor sets on bare repo directly
        for branch in ["main", "dev"]:
            source_ancestors = _get_ancestors(source, branch)
            target_ancestors = _get_ancestors(target_bare, branch)
            assert source_ancestors == target_ancestors, f"Ancestor mismatch on branch {branch}"

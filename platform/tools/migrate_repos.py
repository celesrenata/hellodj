#!/usr/bin/env python3
"""Transactional CodeCommit migration tool (tasks R1.2-R1.6).

This is the executable that performs the one-time migration of the five
hellodj Source_Repos from public GitHub to private AWS CodeCommit repositories.
It drives :func:`hellodj_platform_logic.migration.migrate_repos` with a real
``attempt`` callback that performs the actual CodeCommit creation, upstream
remote configuration, mirror push, and history-preservation verification.

The five repos migrated (in fixed order) are:

1. ``hellodj`` — the application repo (branch ``main``, no upstream)
2. ``Lavalink`` — fork of lavalink-devs/Lavalink (branch ``dev``)
3. ``lavaplayer`` — fork of lavalink-devs/lavaplayer (branch ``main``)
4. ``LavaSrc`` — fork of topi314/LavaSrc (branch ``tidal-v2-api``)
5. ``youtube-source`` — fork of lavalink-devs/youtube-source (branch ``main``)

Transactional semantics (R1.5): repos are processed in order and migration
halts at the first failure. Already-migrated repos are left untouched and repos
after the failing one are never attempted.

Usage::

    python tools/migrate_repos.py                    # execute migration
    python tools/migrate_repos.py --dry-run          # preview without executing
    python tools/migrate_repos.py --region eu-west-1 # override region

Requirements: 1.2, 1.3, 1.4, 1.5, 1.6
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

PLATFORM_ROOT = Path(__file__).resolve().parent.parent
COMPONENTS_ROOT = PLATFORM_ROOT / "components"

# Make the shared pure-logic package importable without installation.
if str(COMPONENTS_ROOT) not in sys.path:
    sys.path.insert(0, str(COMPONENTS_ROOT))

from _migration_helpers import (  # noqa: E402
    _run,
    locate_local_repo,
    verify_history,
)
from hellodj_platform_logic.migration import migrate_repos  # noqa: E402
from hellodj_platform_logic.types import CodeCommitRepo, ForkMigration  # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_REGION = "us-east-1"

#: The five Source_Repos to migrate, in the spec-mandated fixed order.
REPOS: list[CodeCommitRepo] = [
    CodeCommitRepo(
        name="hellodj",
        build_branch="main",
        upstream_url=None,
    ),
    CodeCommitRepo(
        name="Lavalink",
        build_branch="dev",
        upstream_url="https://github.com/lavalink-devs/Lavalink",
    ),
    CodeCommitRepo(
        name="lavaplayer",
        build_branch="main",
        upstream_url="https://github.com/lavalink-devs/lavaplayer",
    ),
    CodeCommitRepo(
        name="LavaSrc",
        build_branch="tidal-v2-api",
        upstream_url="https://github.com/topi314/LavaSrc",
    ),
    CodeCommitRepo(
        name="youtube-source",
        build_branch="main",
        upstream_url="https://github.com/lavalink-devs/youtube-source",
    ),
]


def codecommit_url(region: str, repo_name: str) -> str:
    """Build the HTTPS CodeCommit clone URL for a repo."""
    return f"https://git-codecommit.{region}.amazonaws.com/v1/repos/{repo_name}"


# ---------------------------------------------------------------------------
# Attempt callback (the real side-effectful implementation)
# ---------------------------------------------------------------------------


def make_attempt_callback(
    region: str,
) -> callable[[CodeCommitRepo], tuple[bool, bool, bool]]:
    """Create the real attempt callback bound to the given AWS region.

    Returns a callback with signature
    ``(CodeCommitRepo) -> (created, upstream_remote_ok, history_preserved)``.
    """

    def attempt(repo: CodeCommitRepo) -> tuple[bool, bool, bool]:
        remote_url = codecommit_url(region, repo.name)
        print(f"\n  [{repo.name}] migrating to {remote_url} ...")

        # --- Step 1: Create/confirm CodeCommit repo (idempotent) ---
        created = False
        try:
            result = _run(
                [
                    "aws", "codecommit", "create-repository",
                    "--repository-name", repo.name,
                    "--region", region,
                ],
                check=False,
            )
            if result.returncode == 0:
                print(f"    created CodeCommit repo '{repo.name}'")
                created = True
            elif "RepositoryNameExistsException" in (result.stderr or ""):
                print(f"    CodeCommit repo '{repo.name}' already exists (ok)")
                created = True
            else:
                print(f"    FAILED to create repo: {result.stderr.strip()}")
                return (False, False, False)
        except Exception as exc:
            print(f"    FAILED to create repo (exception): {exc}")
            return (False, False, False)

        # --- Step 2: Locate local repo ---
        try:
            local_dir = locate_local_repo(repo.name)
        except FileNotFoundError as exc:
            print(f"    {exc}")
            return (created, False, False)

        # --- Step 3: Upstream remote (forks only) ---
        upstream_remote_ok = True
        if repo.upstream_url is not None:
            try:
                # Add upstream remote (idempotent: set-url if exists)
                existing = _run(
                    ["git", "remote", "get-url", "upstream"],
                    cwd=local_dir,
                    check=False,
                )
                if existing.returncode != 0:
                    _run(
                        ["git", "remote", "add", "upstream", repo.upstream_url],
                        cwd=local_dir,
                    )
                    print(f"    added upstream remote: {repo.upstream_url}")
                else:
                    _run(
                        ["git", "remote", "set-url", "upstream", repo.upstream_url],
                        cwd=local_dir,
                    )
                    print(f"    upstream remote confirmed: {repo.upstream_url}")

                # Verify with git fetch upstream
                fetch_result = _run(
                    ["git", "fetch", "upstream"],
                    cwd=local_dir,
                    check=False,
                )
                if fetch_result.returncode != 0:
                    print(f"    FAILED git fetch upstream: "
                          f"{(fetch_result.stderr or '').strip()}")
                    upstream_remote_ok = False
                else:
                    print("    git fetch upstream succeeded")
            except subprocess.CalledProcessError as exc:
                print(f"    FAILED upstream remote setup: {exc}")
                upstream_remote_ok = False
        else:
            # App repo (hellodj) has no upstream — satisfied by definition.
            print("    no upstream (app repo) — satisfied")

        if not upstream_remote_ok:
            return (created, False, False)

        # --- Step 4: Mirror push to CodeCommit ---
        try:
            push_result = _run(
                ["git", "push", "--mirror", remote_url],
                cwd=local_dir,
                check=False,
            )
            if push_result.returncode != 0:
                print(f"    FAILED git push --mirror: "
                      f"{(push_result.stderr or '').strip()}")
                return (created, upstream_remote_ok, False)
            print("    git push --mirror succeeded")
        except Exception as exc:
            print(f"    FAILED git push --mirror (exception): {exc}")
            return (created, upstream_remote_ok, False)

        # --- Step 5: Verify history preservation ---
        print("    verifying history preservation ...")
        history_preserved = verify_history(local_dir, remote_url)
        if history_preserved:
            print("    history preserved ✓")
        else:
            print("    history preservation FAILED")

        return (created, upstream_remote_ok, history_preserved)

    return attempt


# ---------------------------------------------------------------------------
# Dry-run mode
# ---------------------------------------------------------------------------


def _dry_run(region: str) -> None:
    """Print what would be done without executing."""
    print(f"DRY RUN — region: {region}")
    print(f"CodeCommit URL pattern: {codecommit_url(region, '<repo>')}")
    print()
    for repo in REPOS:
        remote_url = codecommit_url(region, repo.name)
        print(f"  [{repo.name}]")
        print(f"    branch: {repo.build_branch}")
        print(f"    upstream: {repo.upstream_url or '(none — app repo)'}")
        print(f"    target: {remote_url}")
        print("    actions:")
        print(f"      1. aws codecommit create-repository --repository-name {repo.name} "
              f"--region {region}")
        if repo.upstream_url:
            print(f"      2. git remote add/set-url upstream {repo.upstream_url}")
            print("      3. git fetch upstream")
        else:
            print("      2. (no upstream to configure)")
        print(f"      {'3' if not repo.upstream_url else '4'}. git push --mirror {remote_url}")
        print(f"      {'4' if not repo.upstream_url else '5'}. verify history "
              f"(tip SHAs, ancestor sets, branch/tag names)")
        print()
    print("No changes made (dry run).")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """Entry point for the CodeCommit migration tool.

    Args:
        argv: Command-line arguments (defaults to sys.argv[1:]).

    Returns:
        0 on full success, 1 on migration halt.
    """
    parser = argparse.ArgumentParser(
        description="Migrate hellodj Source_Repos to AWS CodeCommit (R1.2-R1.6).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be done without executing.",
    )
    parser.add_argument(
        "--region",
        default=DEFAULT_REGION,
        help=f"AWS region for CodeCommit (default: {DEFAULT_REGION}).",
    )
    args = parser.parse_args(argv)

    if args.dry_run:
        _dry_run(args.region)
        return 0

    print(f"CodeCommit migration — region: {args.region}")
    print(f"Migrating {len(REPOS)} repos in transactional order ...\n")

    attempt = make_attempt_callback(args.region)
    results: list[ForkMigration] = migrate_repos(REPOS, attempt)

    # --- Summary ---
    print("\n" + "=" * 60)
    print("MIGRATION SUMMARY")
    print("=" * 60)

    all_ok = True
    for fm in results:
        if fm.error:
            print(f"  FAILED  {fm.repo}: {fm.error}")
            all_ok = False
        else:
            print(f"  OK      {fm.repo}: created={fm.created}, "
                  f"upstream_ok={fm.upstream_remote_ok}")

    # Repos not attempted (after failure)
    attempted_names = {fm.repo for fm in results}
    for repo in REPOS:
        if repo.name not in attempted_names:
            print(f"  SKIPPED {repo.name}: not attempted (halted before)")

    print("=" * 60)

    if all_ok and len(results) == len(REPOS):
        print("All repos migrated successfully.")
        return 0
    else:
        failing = next((fm for fm in results if fm.error), None)
        if failing:
            print(f"Migration halted at repo '{failing.repo}'.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

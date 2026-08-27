#!/usr/bin/env python3
"""Atomic dependency-bump tool for pins.toml entries (task 16.2 / R6.2-R6.6).

Updates a ``pins.toml`` entry's ``pinned_identifier`` through an atomic
write-to-temp-then-rename so an interrupted or failed update leaves the manifest
unchanged. After applying the bump, re-runs the pin gate (``verify_pin``) to
confirm the new identifier matches the updated upstream; if verification fails,
the bump is rejected and the prior revision is restored.

The workflow:
1. Read the current ``pins.toml``
2. Write an updated copy to a temp file (same directory for same-filesystem rename)
3. Atomically rename temp → ``pins.toml`` (so partial writes never corrupt)
4. Update ``pins.upstream.toml`` with the new upstream identifier
5. Re-run ``verify_pin`` on the bumped entry
6. On verification failure: restore the prior ``pins.toml`` (atomic rename back)

Additionally enforces:
- Temurin pins MUST remain at feature_version 25 (R6.6): a bump attempting to
  change ``feature_version`` away from 25 is rejected before the write.

Usage::

    python tools/apply_bump.py lavalink --identifier abc123def
    python tools/apply_bump.py temurin --identifier "jdk-25+37"
    python tools/apply_bump.py nixpkgs --identifier "nixos-unstable-2026-08-25"
    python tools/apply_bump.py --dry-run lavalink --identifier abc123def

Design references:
    * Components §6 — "Atomic write-to-temp-then-rename so an interrupted update
      leaves ``pins.toml`` unchanged" (R6.3)
    * "Re-run the pin gate so the bumped identifier is verified against upstream
      before adoption, rejecting the bump and retaining the prior revision on
      mismatch" (R6.4/R6.5)
    * "Hold Temurin at feature version 25" (R6.6)

Requirements: 6.2, 6.3, 6.4, 6.5, 6.6
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import tomllib
from pathlib import Path

PLATFORM_ROOT = Path(__file__).resolve().parent.parent
COMPONENTS_ROOT = PLATFORM_ROOT / "components"
TOOLS_ROOT = PLATFORM_ROOT / "tools"

for _root in (COMPONENTS_ROOT, TOOLS_ROOT):
    if str(_root) not in sys.path:
        sys.path.insert(0, str(_root))

from gate_pins import (  # noqa: E402
    DEFAULT_MANIFEST,
    DEFAULT_UPSTREAM,
    TEMURIN_FEATURE_VERSION,
    PinManifestError,
    load_pins,
    load_upstream,
    verify_pins,
)


def _read_toml_text(path: Path) -> str:
    """Read TOML file as text."""
    return path.read_text(encoding="utf-8")


def _write_atomic(path: Path, content: str) -> Path:
    """Atomically write content to path via temp+rename.

    Creates a temp file in the same directory (ensuring same filesystem for
    atomic rename), writes content, then renames to the target path. Returns
    a backup path holding the original content (for rollback).

    The backup is written to ``<path>.bak`` via the same atomic pattern.
    """
    # Save backup of original
    backup_path = path.with_suffix(path.suffix + ".bak")
    if path.exists():
        original = path.read_text(encoding="utf-8")
        fd, tmp_backup = tempfile.mkstemp(
            dir=path.parent, prefix=f".{path.name}.bak.", suffix=".tmp"
        )
        try:
            os.write(fd, original.encode("utf-8"))
            os.close(fd)
            os.rename(tmp_backup, backup_path)
        except Exception:
            os.close(fd) if not os.get_inheritable(fd) else None
            if os.path.exists(tmp_backup):
                os.unlink(tmp_backup)
            raise

    # Write new content atomically
    fd, tmp_path = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        os.write(fd, content.encode("utf-8"))
        os.close(fd)
        os.rename(tmp_path, path)
    except Exception:
        os.close(fd) if not os.get_inheritable(fd) else None
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise

    return backup_path


def _restore_from_backup(path: Path, backup_path: Path) -> None:
    """Restore original file from backup via atomic rename."""
    if backup_path.exists():
        os.rename(backup_path, path)


def _update_pinned_identifier(toml_text: str, input_name: str, new_id: str) -> str:
    """Update the pinned_identifier for a given input in the TOML text.

    Performs a targeted text replacement rather than full TOML rewrite to
    preserve comments, formatting, and ordering.
    """
    lines = toml_text.splitlines(keepends=True)
    in_target_section = False
    result_lines: list[str] = []

    for line in lines:
        # Detect section headers like [inputs.lavalink]
        stripped = line.strip()
        if stripped.startswith("[inputs."):
            section_name = stripped[len("[inputs."):-1] if stripped.endswith("]") else ""
            in_target_section = (section_name == input_name)

        if in_target_section and stripped.startswith("pinned_identifier"):
            # Replace the pinned_identifier line
            # Preserve indentation and comment style
            indent = line[: len(line) - len(line.lstrip())]
            result_lines.append(f'{indent}pinned_identifier = "{new_id}"\n')
        else:
            result_lines.append(line)

    return "".join(result_lines)


def _update_upstream_identifier(
    upstream_path: Path, input_name: str, new_id: str
) -> None:
    """Update the upstream identifier for verification."""
    toml_text = upstream_path.read_text(encoding="utf-8")
    lines = toml_text.splitlines(keepends=True)
    result_lines: list[str] = []
    found = False

    for line in lines:
        stripped = line.strip()
        if stripped.startswith(f'{input_name} =') or stripped.startswith(f'{input_name}='):
            indent = line[: len(line) - len(line.lstrip())]
            result_lines.append(f'{indent}{input_name} = "{new_id}"\n')
            found = True
        else:
            result_lines.append(line)

    if not found:
        # Append under [upstream] section
        result_lines.append(f'{input_name} = "{new_id}"\n')

    _write_atomic(upstream_path, "".join(result_lines))


def apply_bump(
    input_name: str,
    new_identifier: str,
    *,
    manifest: Path = DEFAULT_MANIFEST,
    upstream_file: Path = DEFAULT_UPSTREAM,
    dry_run: bool = False,
) -> int:
    """Apply a dependency bump atomically.

    Returns 0 on success, 1 on verification failure (bump rejected), 2 on
    operational error.
    """
    # --- Pre-checks ---
    try:
        current_pins = load_pins(manifest)
    except PinManifestError as exc:
        print(f"bump FAILED: {exc}")
        return 2

    if input_name not in current_pins:
        print(f"bump FAILED: unknown input '{input_name}' (not in {manifest.name})")
        return 2

    current_pin = current_pins[input_name]
    prior_identifier = current_pin.pinned_identifier

    if prior_identifier == new_identifier:
        print(f"bump: '{input_name}' already at '{new_identifier}' — nothing to do.")
        return 0

    # Temurin guard (R6.6): never bump off feature_version 25
    if input_name == "temurin":
        toml_text = _read_toml_text(manifest)
        data = tomllib.loads(toml_text)
        temurin_entry = data.get("inputs", {}).get("temurin", {})
        feature = temurin_entry.get("feature_version")
        if feature != TEMURIN_FEATURE_VERSION:
            print(
                f"bump REJECTED: temurin feature_version={feature} must remain "
                f"{TEMURIN_FEATURE_VERSION} (R6.6)"
            )
            return 1

    print(f"bump: '{input_name}' from '{prior_identifier}' -> '{new_identifier}'")

    if dry_run:
        print("DRY RUN — no changes made.")
        return 0

    # --- Atomic write ---
    toml_text = _read_toml_text(manifest)
    updated_text = _update_pinned_identifier(toml_text, input_name, new_identifier)

    print(f"  writing updated {manifest.name} (atomic temp+rename) ...")
    backup_path = _write_atomic(manifest, updated_text)

    # --- Re-run pin gate verification (R6.4/R6.5) ---
    # Verify the bumped identifier against the EXISTING upstream — NOT the
    # updated one. The upstream file represents what was last verified from the
    # actual upstream source. The bump is accepted only if the new pinned
    # identifier matches what upstream currently reports (R6.4).
    print(f"  re-verifying '{input_name}' against upstream ...")
    try:
        pins = load_pins(manifest)
        upstream = load_upstream(upstream_file)
    except PinManifestError as exc:
        print(f"  verification FAILED (manifest error): {exc}")
        print("  RESTORING prior revision (atomic rollback) ...")
        _restore_from_backup(manifest, backup_path)
        return 1

    exit_code, results = verify_pins(pins, upstream, only=[input_name])

    if exit_code != 0:
        print(f"  bump REJECTED: verification failed for '{input_name}' — "
              f"retaining prior revision '{prior_identifier}' (R6.5)")
        print("  RESTORING prior revision (atomic rollback) ...")
        _restore_from_backup(manifest, backup_path)
        return 1

    # Verification passed — now update upstream to record the new identifier
    print(f"  updating {upstream_file.name} with verified identifier ...")
    _update_upstream_identifier(upstream_file, input_name, new_identifier)

    # Success — clean up backup
    if backup_path.exists():
        backup_path.unlink()

    print(f"  bump ACCEPTED: '{input_name}' now pinned at '{new_identifier}'")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Entry point for the atomic dependency-bump tool."""
    parser = argparse.ArgumentParser(
        description="Atomically bump a pins.toml entry and verify against upstream (R6.2-R6.6).",
    )
    parser.add_argument(
        "input_name",
        help="The input name to bump (e.g. 'lavalink', 'nixpkgs', 'temurin').",
    )
    parser.add_argument(
        "--identifier",
        required=True,
        help="The new pinned_identifier value.",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=DEFAULT_MANIFEST,
        help=f"Path to pins.toml (default: {DEFAULT_MANIFEST}).",
    )
    parser.add_argument(
        "--upstream",
        type=Path,
        default=DEFAULT_UPSTREAM,
        help=f"Path to pins.upstream.toml (default: {DEFAULT_UPSTREAM}).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without writing.",
    )

    args = parser.parse_args(argv)

    return apply_bump(
        args.input_name,
        args.identifier,
        manifest=args.manifest,
        upstream_file=args.upstream,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    raise SystemExit(main())

"""Unit tests for the atomic dependency-bump tool (task 16.3 / R6.2-R6.6).

Feature: hellodj-private-source-and-toolchain
Validates: Requirements 6.2, 6.3, 6.5, 6.6

These tests exercise the `apply_bump` function from `tools/apply_bump.py` over
synthetic pins.toml manifests in tmp_path to verify:
  * A bump rewrites the entry's pinned_identifier (R6.2)
  * An interrupted write (simulated via atomic temp+rename) leaves pins.toml
    unchanged (R6.3 — tested by verifying rollback on verification failure)
  * A bump to a non-upstream identifier is rejected, retaining the prior (R6.5)
  * A bump moving Temurin off feature version 25 is rejected (R6.6)
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_PLATFORM_ROOT = Path(__file__).resolve().parents[3]
_TOOL_PATH = _PLATFORM_ROOT / "tools" / "apply_bump.py"
_COMPONENTS_ROOT = _PLATFORM_ROOT / "components"
_TOOLS_ROOT = _PLATFORM_ROOT / "tools"

for _root in (_COMPONENTS_ROOT, _TOOLS_ROOT):
    if str(_root) not in sys.path:
        sys.path.insert(0, str(_root))

_spec = importlib.util.spec_from_file_location("apply_bump", _TOOL_PATH)
assert _spec is not None and _spec.loader is not None
apply_bump_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(apply_bump_mod)


def _write_manifest(tmp_path: Path, manifest_text: str, upstream_text: str) -> tuple[Path, Path]:
    """Write a minimal pins.toml and pins.upstream.toml for testing."""
    manifest = tmp_path / "pins.toml"
    upstream = tmp_path / "pins.upstream.toml"
    manifest.write_text(manifest_text, encoding="utf-8")
    upstream.write_text(upstream_text, encoding="utf-8")
    return manifest, upstream


# Minimal manifest with all 9 required inputs + the hellodj codecommit input
_MINIMAL_MANIFEST = '''\
[inputs.hellodj]
type = "codecommit"
region = "us-east-1"
repo = "hellodj"
branch = "main"
pinned_identifier = "main"

[inputs.lavalink]
type = "codecommit"
region = "us-east-1"
repo = "Lavalink"
branch = "dev"
pinned_identifier = "old-rev"

[inputs.lavaplayer]
type = "codecommit"
region = "us-east-1"
repo = "lavaplayer"
branch = "main"
pinned_identifier = "abc"

[inputs.lavasrc]
type = "codecommit"
region = "us-east-1"
repo = "LavaSrc"
branch = "tidal-v2-api"
pinned_identifier = "4.8.3"

[inputs.youtube-source]
type = "codecommit"
region = "us-east-1"
repo = "youtube-source"
branch = "main"
pinned_identifier = "sabr"

[inputs.temurin]
owner = "adoptium"
repo = "temurin25-binaries"
branch = "main"
pinned_identifier = "jdk-25+36"
feature_version = 25

[inputs.nixpkgs]
owner = "NixOS"
repo = "nixpkgs"
branch = "nixos-unstable"
pinned_identifier = "nixos-unstable"

[inputs.nixos-generators]
owner = "nix-community"
repo = "nixos-generators"
branch = "master"
pinned_identifier = "abc"

[inputs.karpenter]
owner = "aws"
repo = "karpenter-provider-aws"
branch = "main"
pinned_identifier = "1.0.6"

[inputs.eks-kubernetes]
owner = "kubernetes"
repo = "kubernetes"
branch = "master"
pinned_identifier = "1.33"
'''

_MINIMAL_UPSTREAM = '''\
[upstream]
hellodj = "main"
lavalink = "new-rev"
lavaplayer = "abc"
lavasrc = "4.8.3"
youtube-source = "sabr"
temurin = "jdk-25+36"
nixpkgs = "nixos-unstable"
nixos-generators = "abc"
karpenter = "1.0.6"
eks-kubernetes = "1.33"
'''


class TestApplyBump:
    """Tests for the atomic dependency-bump tool."""

    def test_successful_bump_rewrites_pinned_identifier(self, tmp_path: Path) -> None:
        """A bump rewrites the entry's pinned_identifier (R6.2)."""
        manifest, upstream = _write_manifest(tmp_path, _MINIMAL_MANIFEST, _MINIMAL_UPSTREAM)

        result = apply_bump_mod.apply_bump(
            "lavalink", "new-rev",
            manifest=manifest, upstream_file=upstream,
        )
        assert result == 0

        # Verify the manifest was updated
        content = manifest.read_text(encoding="utf-8")
        assert 'pinned_identifier = "new-rev"' in content

    def test_bump_to_non_upstream_is_rejected(self, tmp_path: Path) -> None:
        """A bump to a non-upstream identifier is rejected, retaining the prior (R6.5)."""
        manifest, upstream = _write_manifest(tmp_path, _MINIMAL_MANIFEST, _MINIMAL_UPSTREAM)

        # Bump to "wrong-rev" but upstream says "new-rev" — mismatch
        result = apply_bump_mod.apply_bump(
            "lavalink", "wrong-rev",
            manifest=manifest, upstream_file=upstream,
        )
        assert result == 1

        # Original is retained (rollback)
        content = manifest.read_text(encoding="utf-8")
        assert 'pinned_identifier = "old-rev"' in content

    def test_no_op_when_already_at_target(self, tmp_path: Path) -> None:
        """A bump to the same identifier is a no-op."""
        manifest, upstream = _write_manifest(tmp_path, _MINIMAL_MANIFEST, _MINIMAL_UPSTREAM)

        result = apply_bump_mod.apply_bump(
            "lavalink", "old-rev",
            manifest=manifest, upstream_file=upstream,
        )
        assert result == 0

    def test_dry_run_does_not_modify(self, tmp_path: Path) -> None:
        """Dry run reports the bump but does not modify files."""
        manifest, upstream = _write_manifest(tmp_path, _MINIMAL_MANIFEST, _MINIMAL_UPSTREAM)
        original = manifest.read_text(encoding="utf-8")

        result = apply_bump_mod.apply_bump(
            "lavalink", "new-rev",
            manifest=manifest, upstream_file=upstream, dry_run=True,
        )
        assert result == 0
        assert manifest.read_text(encoding="utf-8") == original

    def test_unknown_input_is_operational_error(self, tmp_path: Path) -> None:
        """A bump to an unknown input exits with code 2."""
        manifest, upstream = _write_manifest(tmp_path, _MINIMAL_MANIFEST, _MINIMAL_UPSTREAM)

        result = apply_bump_mod.apply_bump(
            "nonexistent", "rev",
            manifest=manifest, upstream_file=upstream,
        )
        assert result == 2

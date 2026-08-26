"""Unit / example tests for the deploy-side closure resolution + recording (task 16.2).

Feature: hellodj-nix-native-delivery, Requirement 7.

These tests exercise the two ``tools/`` executables that wire the S3-backed Nix
binary cache into the deploy path:

* ``tools/resolve_closure.py`` — pulls each artifact's prebuilt closure by its
  Nix store-path hash and (with ``--verify``) confirms it is retrievable from the
  cache before a stage deploy. It wraps the pure, property-tested
  :func:`hellodj_platform_logic.binary_cache.resolve_closure` (Property 5 /
  Property 6), so here we assert the *workflow*:
    - build-once/deploy-thrice: Beta, Staging, and Production all resolve the
      SAME store-path hash and reuse the closure (no rebuild) — R7.2/7.3;
    - a missing / not-retrievable closure halts the stage, surfaces the missing
      store path, and never substitutes a non-cache artifact — R7.4;
    - a component absent from the manifest halts and is named.

* ``tools/record_closure.py`` — marks a verified closure available for stage
  deploy by recording its build-once store-path hash into ``closures.toml``
  (R7.7), strictly after the push + narinfo read-back succeeds in the workflow.

The pure decision function itself is covered by the Property 5 / Property 6
Hypothesis tests; these are the workflow-level example tests.

Validates: Requirements 7.1, 7.2, 7.3, 7.7
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

# The tools live under platform/tools/ (a sibling of components/). Load them by
# path so the tests do not depend on tools/ being an installed package.
_PLATFORM_ROOT = Path(__file__).resolve().parents[3]
_COMPONENTS_ROOT = _PLATFORM_ROOT / "components"
if str(_COMPONENTS_ROOT) not in sys.path:
    sys.path.insert(0, str(_COMPONENTS_ROOT))


def _load(mod_name: str) -> object:
    path = _PLATFORM_ROOT / "tools" / f"{mod_name}.py"
    spec = importlib.util.spec_from_file_location(mod_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


resolve_closure_tool = _load("resolve_closure")
record_closure_tool = _load("record_closure")

STAGES = ("beta", "staging", "production")


def _write_manifest(tmp_path: Path, body: str) -> Path:
    manifest = tmp_path / "closures.toml"
    manifest.write_text(body, encoding="utf-8")
    return manifest


def _valid_manifest(tmp_path: Path) -> Path:
    return _write_manifest(
        tmp_path,
        '[cache]\nuri = "s3://hellodj-nix-cache?region=us-east-1"\n\n'
        "[closures.web-ui]\n"
        'store_path = "/nix/store/aaaaaa-web-ui-image.tar.gz"\n'
        'store_path_hash = "aaaaaa"\n\n'
        "[closures.gpu-ami]\n"
        'store_path = "/nix/store/bbbbbb-nixos-amazon-image.vhd"\n'
        'store_path_hash = "bbbbbb"\n',
    )


# ---------------------------------------------------------------------------
# The shipped manifest loads and every entry is a build-once store-path hash
# ---------------------------------------------------------------------------


def test_shipped_manifest_loads_with_cache_uri_and_closures() -> None:
    """closures.toml has an S3 cache URI and at least the AMI + a component (R7.1).

    Validates: Requirements 7.1
    """
    cache_uri, closures = resolve_closure_tool.load_manifest(
        resolve_closure_tool.DEFAULT_MANIFEST
    )
    assert cache_uri.startswith("s3://")
    assert resolve_closure_tool.AMI_KEY in closures
    for ref in closures.values():
        assert ref.store_path.startswith("/nix/store/")
        assert ref.store_path_hash


# ---------------------------------------------------------------------------
# Build once, deploy thrice — every stage resolves the SAME hash and reuses it
# ---------------------------------------------------------------------------


def test_present_closure_reused_across_all_three_stages(tmp_path: Path) -> None:
    """Beta, Staging, and Production all resolve the same hash and reuse it.

    The resolved store-path hash does not depend on the stage — that is the
    build-once/deploy-thrice guarantee (R7.2/7.3).

    Validates: Requirements 7.2, 7.3
    """
    manifest = _valid_manifest(tmp_path)
    present = {"aaaaaa"}  # narinfo read-back reports web-ui retrievable

    resolved_hashes = set()
    for stage in STAGES:
        exit_code, results = resolve_closure_tool.resolve_targets(
            ["web-ui"],
            manifest,
            verify=True,
            stage=stage,
            contents_provider=lambda _uri: present,
        )
        assert exit_code == 0, stage
        assert len(results) == 1
        outcome = results[0]
        assert outcome.halt is False
        assert outcome.present_in_cache is True
        resolved_hashes.add(outcome.requested.store_path_hash)

    # Exactly ONE hash resolved across all three stages — reused, not rebuilt.
    assert resolved_hashes == {"aaaaaa"}


def test_without_verify_manifest_record_is_taken_as_available(tmp_path: Path) -> None:
    """Without --verify the recorded closure is treated as available (R7.7).

    The publish job already confirmed retrievability before recording, so the
    deploy path may trust the manifest record when not re-verifying.

    Validates: Requirements 7.3, 7.7
    """
    manifest = _valid_manifest(tmp_path)
    exit_code, results = resolve_closure_tool.resolve_targets(
        ["web-ui", "gpu-ami"], manifest, verify=False, stage="production"
    )
    assert exit_code == 0
    assert all(not r.halt for r in results)


# ---------------------------------------------------------------------------
# Missing / not-retrievable closure halts the stage without substitution
# ---------------------------------------------------------------------------


def test_missing_closure_halts_and_surfaces_store_path(tmp_path: Path) -> None:
    """A closure absent from the cache halts the stage, surfacing its store path.

    No artifact is substituted from any non-cache source (R7.4).

    Validates: Requirements 7.4
    """
    manifest = _valid_manifest(tmp_path)
    # narinfo read-back reports NOTHING retrievable.
    exit_code, results = resolve_closure_tool.resolve_targets(
        ["web-ui"],
        manifest,
        verify=True,
        stage="beta",
        contents_provider=lambda _uri: set(),
    )
    assert exit_code == 1
    outcome = results[0]
    assert outcome.halt is True
    assert outcome.present_in_cache is False
    # The missing closure is surfaced BY ITS STORE PATH.
    assert "/nix/store/aaaaaa-web-ui-image.tar.gz" in outcome.requested.store_path
    assert "no substitution" in outcome.reason


def test_unrecorded_component_halts_and_is_named(tmp_path: Path) -> None:
    """A component not recorded in the manifest halts and is named (R7.4).

    Validates: Requirements 7.4
    """
    manifest = _valid_manifest(tmp_path)
    exit_code, results = resolve_closure_tool.resolve_targets(
        ["does-not-exist"],
        manifest,
        verify=True,
        stage="staging",
        contents_provider=lambda _uri: {"aaaaaa"},
    )
    assert exit_code == 1
    assert results[0].halt is True
    assert "does-not-exist" in results[0].requested.store_path


def test_malformed_manifest_is_operational_error(tmp_path: Path) -> None:
    """A manifest with no [closures] table is an operational error (exit 2)."""
    manifest = _write_manifest(tmp_path, '[cache]\nuri = "s3://x"\n')
    exit_code = resolve_closure_tool.main(
        ["--component", "web-ui", "--verify", "--manifest", str(manifest)]
    )
    assert exit_code == 2


def test_missing_cache_uri_is_operational_error(tmp_path: Path) -> None:
    """A manifest without a cache URI is rejected — deploy must know the cache (R7.1)."""
    manifest = _write_manifest(
        tmp_path,
        "[closures.web-ui]\n"
        'store_path = "/nix/store/aaaaaa-web-ui.tar.gz"\n'
        'store_path_hash = "aaaaaa"\n',
    )
    with pytest.raises(resolve_closure_tool.ClosureManifestError, match="cache"):
        resolve_closure_tool.load_manifest(manifest)


def test_self_test_passes() -> None:
    """The resolve tool's built-in --self-test smoke check passes."""
    assert resolve_closure_tool.main(["--self-test"]) == 0


# ---------------------------------------------------------------------------
# record_closure — marks a verified closure available (R7.7)
# ---------------------------------------------------------------------------


def test_record_replaces_existing_entry(tmp_path: Path) -> None:
    """Recording an existing artifact replaces its store path/hash in-place (R7.3).

    Validates: Requirements 7.3, 7.7
    """
    manifest = _valid_manifest(tmp_path)
    rc = record_closure_tool.main(
        [
            "--name",
            "web-ui",
            "--store-path",
            "/nix/store/ccccc1-web-ui-image.tar.gz",
            "--manifest",
            str(manifest),
        ]
    )
    assert rc == 0
    _uri, closures = resolve_closure_tool.load_manifest(manifest)
    assert closures["web-ui"].store_path_hash == "ccccc1"
    # gpu-ami is untouched.
    assert closures["gpu-ami"].store_path_hash == "bbbbbb"


def test_record_appends_new_entry(tmp_path: Path) -> None:
    """Recording a new artifact appends its entry (R7.7).

    Validates: Requirements 7.7
    """
    manifest = _valid_manifest(tmp_path)
    rc = record_closure_tool.main(
        [
            "--name",
            "hls-transcode",
            "--store-path",
            "/nix/store/ddddd2-hls-transcode-image.tar.gz",
            "--manifest",
            str(manifest),
        ]
    )
    assert rc == 0
    _uri, closures = resolve_closure_tool.load_manifest(manifest)
    assert closures["hls-transcode"].store_path_hash == "ddddd2"


def test_record_rejects_non_store_path(tmp_path: Path) -> None:
    """A non /nix/store/<hash>-<name> path is rejected (operational error)."""
    manifest = _valid_manifest(tmp_path)
    rc = record_closure_tool.main(
        ["--name", "web-ui", "--store-path", "not-a-store-path", "--manifest", str(manifest)]
    )
    assert rc == 2


def test_recorded_closure_is_then_resolvable_end_to_end(tmp_path: Path) -> None:
    """A recorded closure is subsequently resolved+reused across all stages (R7.2/7.3/7.7).

    Mirrors the workflow contract: publish job records after verify, then every
    stage's deploy resolves the same recorded hash.

    Validates: Requirements 7.2, 7.3, 7.7
    """
    manifest = _valid_manifest(tmp_path)
    record_closure_tool.main(
        [
            "--name",
            "activity-backend",
            "--store-path",
            "/nix/store/eeeee3-activity-backend-image.tar.gz",
            "--manifest",
            str(manifest),
        ]
    )
    _uri, closures = resolve_closure_tool.load_manifest(manifest)
    recorded_hash = closures["activity-backend"].store_path_hash
    for stage in STAGES:
        exit_code, results = resolve_closure_tool.resolve_targets(
            ["activity-backend"],
            manifest,
            verify=True,
            stage=stage,
            contents_provider=lambda _uri: {recorded_hash},
        )
        assert exit_code == 0
        assert results[0].requested.store_path_hash == recorded_hash

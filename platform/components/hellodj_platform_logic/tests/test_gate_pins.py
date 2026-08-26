"""Unit / example tests for the pin-time verification workflow (task 18.1).

Feature: hellodj-nix-native-delivery, Requirement 11.

These tests exercise the ``tools/gate_pins.py`` workflow that wires the pure,
property-tested :func:`hellodj_platform_logic.pinning.verify_pin` across every
enumerated flake input. The pure function itself is covered by the Property 13
Hypothesis test (``test_pinning_property.py``); here we assert the *workflow*:

* the shipped ``pins.toml`` enumerates every required input, each pinning via
  ``github:owner/repo/branch``, and verifies clean against the recorded
  ``pins.upstream.toml`` at pin time (R11.1/R11.2/R11.3);
* the Temurin input pins feature version 25 (R3.7 / R11.2);
* a mismatched pin is rejected, names the input, and the workflow does not
  mutate the manifest so the prior pinned revision is retained (R11.5);
* an unresolved upstream fails, names the input, prior revision retained
  (R11.6);
* a ``path:``-style / missing-field input is rejected as a non-github pin
  (R11.3);
* the workflow exits non-zero on any reject/fail so a bad pin is never adopted.

Validates: Requirements 11.1, 11.2, 11.3, 11.5, 11.6
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

# The workflow lives under platform/tools/ (a sibling of components/). Load it
# by path so the test does not depend on tools/ being an installed package,
# mirroring how the tool itself makes components/ importable.
_PLATFORM_ROOT = Path(__file__).resolve().parents[3]
_TOOL_PATH = _PLATFORM_ROOT / "tools" / "gate_pins.py"
_COMPONENTS_ROOT = _PLATFORM_ROOT / "components"
if str(_COMPONENTS_ROOT) not in sys.path:
    sys.path.insert(0, str(_COMPONENTS_ROOT))

_spec = importlib.util.spec_from_file_location("gate_pins", _TOOL_PATH)
assert _spec is not None and _spec.loader is not None
gate_pins = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate_pins)

from hellodj_platform_logic.types import FlakeInputPin  # noqa: E402


# ---------------------------------------------------------------------------
# The shipped manifest verifies clean at pin time
# ---------------------------------------------------------------------------


def test_shipped_manifest_pins_every_required_input() -> None:
    """pins.toml enumerates every input Requirement 11.1 calls out.

    Validates: Requirements 11.1
    """
    pins = gate_pins.load_pins(gate_pins.DEFAULT_MANIFEST)
    for required in gate_pins.REQUIRED_INPUTS:
        assert required in pins, f"pins.toml is missing required input {required!r}"
    # The four forks, Temurin, nixpkgs, nixos-generators, Karpenter, EKS k8s.
    assert len(gate_pins.REQUIRED_INPUTS) == 9


def test_shipped_manifest_inputs_are_all_github_form() -> None:
    """Every shipped input pins via github:owner/repo/branch (R11.3).

    Validates: Requirements 11.3
    """
    pins = gate_pins.load_pins(gate_pins.DEFAULT_MANIFEST)
    for name, pin in pins.items():
        assert pin.owner and pin.repo and pin.branch, name
        # No smuggled path:/scheme in any github coordinate.
        for field in (pin.owner, pin.repo, pin.branch):
            assert ":" not in field and not field.startswith("path"), (name, field)


def test_shipped_manifest_verifies_clean_against_recorded_upstream() -> None:
    """The shipped pins all match the recorded upstream — pin gate passes.

    Validates: Requirements 11.1, 11.2
    """
    exit_code = gate_pins.main([])
    assert exit_code == 0


def test_temurin_pins_feature_version_25() -> None:
    """The Temurin/JDK pin is Temurin 25 (LTS) — a non-25 feature version fails.

    Validates: Requirements 11.2
    """
    pins = gate_pins.load_pins(gate_pins.DEFAULT_MANIFEST)
    assert "temurin" in pins
    assert gate_pins.TEMURIN_FEATURE_VERSION == 25


# ---------------------------------------------------------------------------
# Reject / fail outcomes over synthetic pins
# ---------------------------------------------------------------------------


def _pin(name: str, identifier: str) -> FlakeInputPin:
    return FlakeInputPin(
        input_name=name,
        owner="hellodj",
        repo=name,
        branch="main",
        pinned_identifier=identifier,
    )


def test_matching_pin_accepted() -> None:
    """A pin equal to its resolved upstream is accepted (R11.1).

    Validates: Requirements 11.1
    """
    pins = {"lavalink": _pin("lavalink", "rev-a")}
    exit_code, results = gate_pins.verify_pins(pins, {"lavalink": "rev-a"})
    assert exit_code == 0
    assert results[0].accepted is True
    assert results[0].reason == ""


def test_mismatched_pin_rejected_names_input_and_retains_prior() -> None:
    """A mismatched pin is rejected, names the input, prior revision retained.

    The workflow returns a non-zero exit and never mutates the manifest, so the
    prior pinned revision stays in force (R11.5).

    Validates: Requirements 11.5
    """
    pins = {"lavasrc": _pin("lavasrc", "4.8.3")}
    exit_code, results = gate_pins.verify_pins(pins, {"lavasrc": "4.9.0"})
    assert exit_code == 1
    outcome = results[0]
    assert outcome.accepted is False
    assert outcome.input_name == "lavasrc"
    # The resolved (differing) upstream is carried through; the input is named.
    assert outcome.upstream_identifier == "4.9.0"
    assert "lavasrc" in outcome.reason
    assert "retained" in outcome.reason


def test_unresolved_upstream_fails_names_input_and_retains_prior() -> None:
    """An unresolved upstream fails, names the input, prior revision retained.

    Validates: Requirements 11.6
    """
    pins = {"temurin": _pin("temurin", "jdk-25+36")}
    # Absent from the upstream record -> unresolved (None) -> R11.6 path.
    exit_code, results = gate_pins.verify_pins(pins, {})
    assert exit_code == 1
    outcome = results[0]
    assert outcome.accepted is False
    assert outcome.input_name == "temurin"
    assert outcome.upstream_identifier is None
    assert "temurin" in outcome.reason
    assert "retained" in outcome.reason


def test_empty_upstream_string_is_treated_as_unresolved(tmp_path: Path) -> None:
    """An empty recorded upstream string models an unresolved upstream (R11.6).

    Validates: Requirements 11.6
    """
    upstream_file = tmp_path / "pins.upstream.toml"
    upstream_file.write_text('[upstream]\nlavalink = ""\n', encoding="utf-8")
    resolved = gate_pins.load_upstream(upstream_file)
    assert resolved["lavalink"] is None


# ---------------------------------------------------------------------------
# Manifest validation — path: inputs and malformed entries are rejected
# ---------------------------------------------------------------------------


def _write_manifest(tmp_path: Path, body: str) -> Path:
    manifest = tmp_path / "pins.toml"
    manifest.write_text(body, encoding="utf-8")
    return manifest


def test_path_style_input_is_rejected(tmp_path: Path) -> None:
    """A path:-style (non-github) input is rejected up front (R11.3).

    Validates: Requirements 11.3
    """
    manifest = _write_manifest(
        tmp_path,
        "[inputs.lavalink]\n"
        'owner = "path:/tmp/Lavalink"\n'
        'repo = "Lavalink"\n'
        'branch = "dev"\n'
        'pinned_identifier = "abc"\n',
    )
    with pytest.raises(gate_pins.PinManifestError, match="github"):
        gate_pins.load_pins(manifest)


def test_missing_field_input_is_rejected(tmp_path: Path) -> None:
    """An input missing a github coordinate is rejected (R11.3).

    Validates: Requirements 11.3
    """
    manifest = _write_manifest(
        tmp_path,
        "[inputs.lavalink]\n"
        'owner = "hellodj"\n'
        'repo = "Lavalink"\n'
        # branch omitted
        'pinned_identifier = "abc"\n',
    )
    with pytest.raises(gate_pins.PinManifestError, match="branch"):
        gate_pins.load_pins(manifest)


def test_temurin_non_25_feature_version_is_rejected(tmp_path: Path) -> None:
    """A Temurin pin that is not feature version 25 is rejected (R3.7 / R11.2).

    Validates: Requirements 11.2
    """
    manifest = _write_manifest(
        tmp_path,
        "[inputs.temurin]\n"
        'owner = "adoptium"\n'
        'repo = "temurin26-binaries"\n'
        'branch = "main"\n'
        'pinned_identifier = "jdk-26+1"\n'
        "feature_version = 26\n",
    )
    with pytest.raises(gate_pins.PinManifestError, match="Temurin"):
        gate_pins.load_pins(manifest)


def test_unknown_only_input_is_operational_error() -> None:
    """Requesting an unknown input name is an operational error (exit 2)."""
    pins = {"lavalink": _pin("lavalink", "rev-a")}
    exit_code, results = gate_pins.verify_pins(
        pins, {"lavalink": "rev-a"}, only=["does-not-exist"]
    )
    assert exit_code == 2
    assert results == []


def test_self_test_passes() -> None:
    """The workflow's built-in --self-test smoke check passes."""
    assert gate_pins.main(["--self-test", "--manifest", str(gate_pins.DEFAULT_MANIFEST)]) == 0

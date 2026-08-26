"""Static-scan unit / example tests for the migration compliance conditions.

These example-based tests scan the ACTUAL repository files on disk and assert
every static compliance condition the ``hellodj-nix-native-delivery`` design
enumerates under Testing Strategy -> "Unit / example tests" -> Static scans and
Flake-input form (task 18.2).

Conditions asserted in this file:

* Stage-naming reconciliation (R9.2): zero ``gamma``/``Gamma``/``GAMMA``
  occurrences across ``dns_naming.py``, ``promotion.py``, ``pipeline-stack.ts``
  and the Route 53 records (``edge-stack.ts``).
* No placeholder jars (R4.5): the Lavalink component flake declares no
  ``mkPlaceholderJar`` for the Lavalink / plugin jars.
* No authoritative Alpine base (R5.3): no authoritative
  ``FROM eclipse-temurin:21-jre-alpine`` Dockerfile in the Lavalink fork.
* Pin form (R11.3): every ``pins.toml`` input uses the
  ``github:owner/repo/branch`` form and there are zero ``path:`` inputs; the
  fork/component flakes declare no ``path:`` flake inputs.
* Temurin pin (R3.7 / R11.2): the Temurin pin's feature version equals 25.
* Fork-input form (R1.5 / R4.1): the Lavalink flake declares the three sibling
  forks as CodeCommit git+https inputs.
* buildLayeredImage (R5.1 / R4.2): every component flake builds its OCI image
  with ``pkgs.dockerTools.buildLayeredImage``.

Requirements: 1.5, 3.7, 4.1, 4.2, 4.5, 5.1, 5.3, 9.2, 11.2, 11.3
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Repository layout resolution.
# ---------------------------------------------------------------------------
_PLATFORM_ROOT = Path(__file__).resolve().parents[3]
_COMPONENTS_ROOT = _PLATFORM_ROOT / "components"
_INFRA_LIB = _PLATFORM_ROOT / "infra" / "lib"
_PINS_TOML = _PLATFORM_ROOT / "pins.toml"

# The account checkout root holding the four fork repos as siblings of `hellodj`.
_ACCOUNT_ROOT = _PLATFORM_ROOT.parent.parent
_FORK_REPOS = {
    "Lavalink": _ACCOUNT_ROOT / "Lavalink",
    "lavaplayer": _ACCOUNT_ROOT / "lavaplayer",
    "LavaSrc": _ACCOUNT_ROOT / "LavaSrc",
    "youtube-source": _ACCOUNT_ROOT / "youtube-source",
}

# Component flakes that MUST build their OCI image via buildLayeredImage (R5.1).
_COMPONENT_FLAKES = {
    "lavalink": _COMPONENTS_ROOT / "lavalink" / "flake.nix",
    "yt-cipher": _COMPONENTS_ROOT / "yt-cipher" / "flake.nix",
    "spotify-stream": _COMPONENTS_ROOT / "spotify-stream" / "flake.nix",
    "potoken-server": _COMPONENTS_ROOT / "potoken-server" / "flake.nix",
}

# The four files R9.2 names.
_GAMMA_SCAN_FILES = {
    "dns_naming.py": _COMPONENTS_ROOT / "hellodj_platform_logic" / "dns_naming.py",
    "promotion.py": _COMPONENTS_ROOT / "hellodj_platform_logic" / "promotion.py",
    "pipeline-stack.ts": _INFRA_LIB / "pipeline-stack.ts",
    "route53-records (edge-stack.ts)": _INFRA_LIB / "edge-stack.ts",
}

_GAMMA_RE = re.compile(r"\bgamma\b", re.IGNORECASE)


def _read(path: Path) -> str:
    """Read a repo file, failing the test clearly if it is missing."""
    assert path.exists(), f"expected file does not exist: {path}"
    return path.read_text(encoding="utf-8")


def _load_pins() -> dict:
    with _PINS_TOML.open("rb") as fh:
        return tomllib.load(fh)


# ---------------------------------------------------------------------------
# R9.2 -- zero gamma across dns_naming.py, promotion.py, pipeline-stack.ts,
#         and the Route 53 records.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("label", sorted(_GAMMA_SCAN_FILES))
def test_no_gamma_in_reconciled_stage_files(label: str) -> None:
    """No gamma/Gamma/GAMMA in the reconciled stage files (Requirement 9.2)."""
    path = _GAMMA_SCAN_FILES[label]
    text = _read(path)
    matches = _GAMMA_RE.findall(text)
    assert matches == [], (
        f"{label} ({path}) still contains {len(matches)} occurrence(s) of the "
        f"prior 'gamma' stage identifier: {matches!r}"
    )


# ---------------------------------------------------------------------------
# R4.5 -- no mkPlaceholderJar for the Lavalink / plugin jars.
# ---------------------------------------------------------------------------


def test_lavalink_component_flake_has_no_placeholder_jar() -> None:
    """The Lavalink component flake declares no mkPlaceholderJar (R4.5)."""
    text = _read(_COMPONENT_FLAKES["lavalink"])
    assert "mkPlaceholderJar =" not in text, (
        "Lavalink component flake defines mkPlaceholderJar; the placeholder jar "
        "derivations must be replaced by the real fork jars (R4.5)"
    )
    assert not re.search(r"mkPlaceholderJar\s*\{", text), (
        "Lavalink component flake INVOKES mkPlaceholderJar; the Lavalink/plugin "
        "jars must come from the real sibling Fork_Flakes (R4.5)"
    )


def test_lavalink_fork_flake_has_no_placeholder_jar() -> None:
    """The authoritative Lavalink fork flake uses no mkPlaceholderJar (R4.5)."""
    flake = _FORK_REPOS["Lavalink"] / "flake.nix"
    text = _read(flake)
    assert "mkPlaceholderJar =" not in text, (
        "Lavalink fork flake defines mkPlaceholderJar (R4.5)"
    )
    assert not re.search(r"mkPlaceholderJar\s*\{", text), (
        "Lavalink fork flake invokes mkPlaceholderJar (R4.5)"
    )


# ---------------------------------------------------------------------------
# R5.3 -- no authoritative FROM eclipse-temurin:21-jre-alpine.
# ---------------------------------------------------------------------------


def test_no_authoritative_alpine_temurin_dockerfile() -> None:
    """No authoritative Alpine Temurin Dockerfile in the Lavalink fork (R5.3)."""
    lavalink = _FORK_REPOS["Lavalink"]
    offenders: list[str] = []
    for path in lavalink.rglob("*"):
        if not path.is_file():
            continue
        if ".git" in path.parts:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if "FROM eclipse-temurin:21-jre-alpine" not in text:
            continue
        name = path.name.lower()
        rel = path.relative_to(lavalink).as_posix()
        is_reference = (
            path.suffix.lower() == ".md"
            or "legacy" in rel.lower()
            or "reference" in name
        )
        is_authoritative_dockerfile = name == "dockerfile" or name.startswith(
            "dockerfile."
        )
        if is_reference and path.suffix.lower() == ".md":
            is_authoritative_dockerfile = False
        if is_authoritative_dockerfile and not is_reference:
            offenders.append(rel)

    assert offenders == [], (
        "authoritative Alpine Temurin Dockerfile(s) still present in the "
        f"Lavalink fork (R5.3): {offenders!r}"
    )


# ---------------------------------------------------------------------------
# R11.3 -- every pins.toml input uses github:owner/repo/branch; zero path:.
# ---------------------------------------------------------------------------


def test_pins_have_no_path_inputs() -> None:
    """No pins.toml input uses a path: form (R11.3)."""
    text = _read(_PINS_TOML)
    for lineno, line in enumerate(text.splitlines(), start=1):
        code = line.split("#", 1)[0]
        assert "path:" not in code, (
            f"pins.toml line {lineno} declares a forbidden path: input: {line!r}"
        )


def test_every_pins_input_resolves_to_github_or_codecommit() -> None:
    """Every pins.toml input carries owner/repo/branch (github or codecommit form)."""
    pins = _load_pins()
    inputs = pins.get("inputs", {})
    assert inputs, "pins.toml declares no [inputs.*]"
    for name, spec in inputs.items():
        input_type = spec.get("type", "github")
        if input_type == "codecommit":
            for field in ("region", "repo", "branch"):
                assert field in spec and str(spec[field]).strip(), (
                    f"pins codecommit input {name!r} is missing a non-empty {field!r} (R3.4)"
                )
        else:
            for field in ("owner", "repo", "branch"):
                assert field in spec and str(spec[field]).strip(), (
                    f"pins github input {name!r} is missing a non-empty {field!r}, so it "
                    f"cannot form github:owner/repo/branch (R11.3)"
                )
            ref = f"github:{spec['owner']}/{spec['repo']}/{spec['branch']}"
            assert ref.startswith("github:") and ref.count("/") >= 2, (
                f"pins input {name!r} does not resolve to github:owner/repo/branch: "
                f"{ref!r} (R11.3)"
            )


def test_flakes_declare_no_path_inputs() -> None:
    """No fork/component flake declares a path: flake input (R11.3)."""
    flakes = [repo / "flake.nix" for repo in _FORK_REPOS.values()]
    flakes += list(_COMPONENT_FLAKES.values())
    url_path_re = re.compile(r'url\s*=\s*"path:')
    quoted_path_re = re.compile(r'"path:[^"]*"')
    for flake in flakes:
        text = _read(flake)
        for lineno, line in enumerate(text.splitlines(), start=1):
            code = line.split("#", 1)[0]
            assert not url_path_re.search(code), (
                f"{flake} line {lineno} declares a path: URL input (R11.3): "
                f"{line!r}"
            )
            assert not quoted_path_re.search(code), (
                f"{flake} line {lineno} declares a quoted path: input (R11.3): "
                f"{line!r}"
            )


# ---------------------------------------------------------------------------
# R3.7 / R11.2 -- Temurin pin feature version == 25.
# ---------------------------------------------------------------------------


def test_temurin_pin_feature_version_is_25() -> None:
    """The Temurin pin declares feature version 25 (R3.7 / R11.2)."""
    pins = _load_pins()
    temurin = pins.get("inputs", {}).get("temurin")
    assert temurin is not None, "pins.toml declares no [inputs.temurin]"
    assert temurin.get("feature_version") == 25, (
        "Temurin pin feature_version must equal 25 (the LTS target); got "
        f"{temurin.get('feature_version')!r} (R3.7 / R11.2)"
    )
    pinned = str(temurin.get("pinned_identifier", ""))
    assert "25" in pinned, (
        f"Temurin pinned_identifier {pinned!r} does not reference the 25 line "
        "(R3.7 / R11.2)"
    )


# ---------------------------------------------------------------------------
# R1.5 / R4.1 -- Lavalink flake declares the three sibling forks as
#               CodeCommit git+https inputs.
# ---------------------------------------------------------------------------


def test_lavalink_fork_flake_declares_siblings_as_codecommit() -> None:
    """Lavalink flake references the 3 sibling forks via CodeCommit git+https (R2.1/R4.1)."""
    text = _read(_FORK_REPOS["Lavalink"] / "flake.nix")
    for repo in ("lavaplayer", "LavaSrc", "youtube-source"):
        assert f"git-codecommit.us-east-1.amazonaws.com/v1/repos/{repo}" in text, (
            f"Lavalink fork flake does not declare {repo} as a CodeCommit "
            f"git+https input (R2.1 / R4.1)"
        )


def test_lavalink_component_flake_consumes_codecommit_fork() -> None:
    """The Lavalink component flake consumes the fork via CodeCommit git+https (R2.1)."""
    text = _read(_COMPONENT_FLAKES["lavalink"])
    assert "git-codecommit.us-east-1.amazonaws.com/v1/repos/Lavalink" in text, (
        "Lavalink component flake does not consume Lavalink via CodeCommit "
        "git+https (R2.1)"
    )


# ---------------------------------------------------------------------------
# R5.1 / R4.2 -- every component flake builds its OCI image with
#               pkgs.dockerTools.buildLayeredImage.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("component", sorted(_COMPONENT_FLAKES))
def test_component_flake_uses_build_layered_image(component: str) -> None:
    """Each component flake builds its image with buildLayeredImage (R5.1/R4.2)."""
    flake = _COMPONENT_FLAKES[component]
    text = _read(flake)
    if component == "lavalink":
        fork_text = _read(_FORK_REPOS["Lavalink"] / "flake.nix")
        assert "dockerTools.buildLayeredImage" in fork_text, (
            "Lavalink fork flake (authoritative image builder) does not use "
            "pkgs.dockerTools.buildLayeredImage (R5.1 / R4.2)"
        )
        return
    assert "dockerTools.buildLayeredImage" in text, (
        f"component flake {component} does not use "
        "pkgs.dockerTools.buildLayeredImage (R5.1 / R4.2)"
    )

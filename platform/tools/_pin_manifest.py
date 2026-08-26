"""Pin manifest I/O helpers for the pin-gate runner (gate_pins.py).

This private module holds the ``load_pins()`` and ``load_upstream()`` functions
extracted from the main runner, along with the supporting ``_load_toml()``
helper and ``PinManifestError`` exception. They handle all TOML parsing and
structural validation of ``pins.toml`` / ``pins.upstream.toml``.
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

PLATFORM_ROOT = Path(__file__).resolve().parent.parent
COMPONENTS_ROOT = PLATFORM_ROOT / "components"

if str(COMPONENTS_ROOT) not in sys.path:
    sys.path.insert(0, str(COMPONENTS_ROOT))

from hellodj_platform_logic.codecommit_input import (  # noqa: E402
    classify_input,
    missing_codecommit_fields,
    resolve_codecommit_input,
)
from hellodj_platform_logic.types import (  # noqa: E402
    FlakeInputPin,
    InputForm,
)

#: The exact set of inputs Requirement 11.1 enumerates. The manifest must
#: pin every one of them (a missing input is an operational error — the
#: workflow could not verify a mandated pin).
REQUIRED_INPUTS: tuple[str, ...] = (
    "lavalink",
    "lavaplayer",
    "lavasrc",
    "youtube-source",
    "temurin",
    "nixpkgs",
    "nixos-generators",
    "karpenter",
    "eks-kubernetes",
)

#: The Temurin/JDK migration target feature version (R3.7 / R11.2). The Temurin
#: pin MUST equal Temurin 25 (the LTS release), never another feature release.
TEMURIN_FEATURE_VERSION = 25


class PinManifestError(Exception):
    """Raised when the pin manifest is missing, malformed, or incomplete.

    This is an *operational* error (the workflow could not be evaluated),
    distinct from a legitimate reject/fail pin outcome. It makes the runner exit
    non-zero so a broken manifest never silently passes the pin gate.
    """


def _load_toml(path: Path) -> dict:
    """Parse a TOML file, raising :class:`PinManifestError` on any I/O/parse error."""
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except FileNotFoundError as exc:
        raise PinManifestError(f"{path}: file not found") from exc
    except (tomllib.TOMLDecodeError, OSError) as exc:
        raise PinManifestError(f"{path}: cannot parse: {exc}") from exc


def load_pins(manifest: Path) -> dict[str, FlakeInputPin]:
    """Load the pin manifest into ``input_name -> FlakeInputPin``.

    Supports both CodeCommit (``type = "codecommit"``) and legacy github entries.
    Each entry is classified via :func:`classify_input` (R3.2/R3.3/R3.4):

    * **PATH** entries are rejected (R3.3).
    * **INVALID** CodeCommit entries (missing region/repo/branch) are rejected
      naming the missing field(s) (R3.4).
    * **CODECOMMIT** entries must have a non-empty ``pinned_identifier``; they
      produce a :class:`FlakeInputPin` with ``owner`` set to the ``region``
      value (traceability field).
    * **GITHUB** entries must declare non-empty ``owner``, ``repo``, ``branch``,
      and ``pinned_identifier`` (R11.3). Defense-in-depth: individual github
      fields are also checked for ``":"`` / ``"path"`` smuggling.

    The Temurin input must additionally declare ``feature_version = 25``
    (R3.7 / R11.2). Every input enumerated by :data:`REQUIRED_INPUTS` must be
    present.

    Args:
        manifest: Path to ``pins.toml``.

    Returns:
        A mapping from input name to its :class:`FlakeInputPin`.

    Raises:
        PinManifestError: on a missing/empty ``[inputs]`` table, a malformed
            input entry, a ``path:``-style input (R3.3), a CodeCommit entry
            missing required fields (R3.4), a Temurin ``feature_version`` other
            than 25, or a missing required input.
    """
    data = _load_toml(manifest)
    raw_inputs = data.get("inputs")
    if not isinstance(raw_inputs, dict) or not raw_inputs:
        raise PinManifestError(
            f"{manifest}: missing or empty [inputs] table (need at least one "
            "input -> github:owner/repo/branch pin)"
        )

    pins: dict[str, FlakeInputPin] = {}
    for name, entry in raw_inputs.items():
        if not isinstance(entry, dict):
            raise PinManifestError(
                f"{manifest}: input '{name}' must be a table with owner/repo/"
                "branch/pinned_identifier"
            )

        form = classify_input(entry)

        # ── PATH: always rejected (R3.3) ──────────────────────────────────
        if form is InputForm.PATH:
            raise PinManifestError(
                f"{manifest}: input '{name}' is a path: reference — inputs "
                "must not use path: (R3.3)"
            )

        # ── INVALID: CodeCommit entry missing required fields (R3.4) ──────
        if form is InputForm.INVALID:
            missing_fields = missing_codecommit_fields(entry)
            raise PinManifestError(
                f"{manifest}: input '{name}' (type=codecommit) is missing "
                f"required field(s): {', '.join(missing_fields)} (R3.4)"
            )

        # ── CODECOMMIT: validated by classify_input, build pin (R3.2) ─────
        if form is InputForm.CODECOMMIT:
            pinned = entry.get("pinned_identifier")
            if not isinstance(pinned, str) or not pinned:
                raise PinManifestError(
                    f"{manifest}: input '{name}' (type=codecommit) has "
                    "missing/empty 'pinned_identifier'"
                )

            region = entry["region"]
            repo = entry["repo"]
            branch = entry["branch"]

            # Log the resolved URL for traceability.
            resolved_url = resolve_codecommit_input(region, repo, branch)
            print(f"  {name}: {resolved_url}")

            pins[name] = FlakeInputPin(
                input_name=name,
                owner=region,
                repo=repo,
                branch=branch,
                pinned_identifier=pinned,
            )

        # ── GITHUB: legacy github:owner/repo/branch form (R11.3) ─────────
        elif form is InputForm.GITHUB:
            owner = entry.get("owner")
            repo = entry.get("repo")
            branch = entry.get("branch")
            pinned = entry.get("pinned_identifier")

            # Every field must be a non-empty string (R11.3).
            for field, value in (
                ("owner", owner),
                ("repo", repo),
                ("branch", branch),
                ("pinned_identifier", pinned),
            ):
                if not isinstance(value, str) or not value:
                    raise PinManifestError(
                        f"{manifest}: input '{name}' has missing/empty "
                        f"'{field}' — every github input must pin via "
                        "github:owner/repo/branch (R11.3), never a path: input"
                    )

            # Defense-in-depth: guard against a smuggled scheme in any field
            # (e.g. owner = "path:/…"); the github: form takes bare values.
            for field, value in (("owner", owner), ("repo", repo), ("branch", branch)):
                if ":" in value or value.startswith("path"):
                    raise PinManifestError(
                        f"{manifest}: input '{name}' field '{field}'="
                        f"{value!r} looks like a non-github (path:/url) "
                        "reference — inputs must be "
                        "github:owner/repo/branch (R11.3)"
                    )

            pins[name] = FlakeInputPin(
                input_name=name,
                owner=owner,
                repo=repo,
                branch=branch,
                pinned_identifier=pinned,
            )

        # ── Temurin feature_version assertion (R3.7 / R11.2) ─────────────
        if name == "temurin":
            feature = entry.get("feature_version")
            if feature != TEMURIN_FEATURE_VERSION:
                raise PinManifestError(
                    f"{manifest}: temurin pin feature_version={feature!r} must "
                    f"be {TEMURIN_FEATURE_VERSION} — the migration target is "
                    f"Temurin {TEMURIN_FEATURE_VERSION} (LTS) and no other "
                    "feature release (R3.7 / R11.2)"
                )

    missing = [i for i in REQUIRED_INPUTS if i not in pins]
    if missing:
        raise PinManifestError(
            f"{manifest}: missing required input pin(s): {', '.join(missing)} — "
            "Requirement 11.1 mandates pinning every enumerated upstream/base "
            "version"
        )

    return pins


def load_upstream(upstream_file: Path) -> dict[str, str | None]:
    """Load the recorded resolved-at-pin-time upstream identifiers.

    Reads the ``[upstream]`` table of ``pins.upstream.toml``. A value present
    and non-empty is the upstream identifier resolved at pin time; an empty
    string is normalized to ``None`` (an explicitly-unresolved upstream). An
    input simply absent from the table is treated as ``None`` by the caller
    (unresolved upstream, R11.6).

    Args:
        upstream_file: Path to ``pins.upstream.toml``.

    Returns:
        A mapping from input name to its resolved upstream identifier (or
        ``None`` when recorded as unresolved).

    Raises:
        PinManifestError: on a malformed ``[upstream]`` table.
    """
    data = _load_toml(upstream_file)
    raw = data.get("upstream", {})
    if not isinstance(raw, dict):
        raise PinManifestError(
            f"{upstream_file}: [upstream] must be a table of input -> resolved "
            "upstream identifier"
        )

    resolved: dict[str, str | None] = {}
    for name, value in raw.items():
        if value is None or value == "":
            resolved[name] = None
        elif isinstance(value, str):
            resolved[name] = value
        else:
            raise PinManifestError(
                f"{upstream_file}: upstream identifier for '{name}' must be a "
                f"string (or empty for unresolved), got {value!r}"
            )
    return resolved

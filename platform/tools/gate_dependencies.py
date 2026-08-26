#!/usr/bin/env python3
"""Build-stage per-component dependency-compatibility gate runner (task 18.4).

This is the executable each Component's isolated build path invokes in the
Beta -> Gamma -> Prod deployment pipeline to enforce Requirement 4: *default
every Component to Graviton_Architecture (ARM64), and only drop x86-64 for a
Component once every runtime dependency of that Component is verified available
and functional on ARM64.* It is the thin CI wrapper around the pure,
property-tested decision function
``hellodj_platform_logic.dependency_gate.gate`` (Property 5), so the pipeline
and the shared decision logic reason over one source of truth.

What it does
------------

For one Component (named on the command line, e.g. ``python
tools/gate_dependencies.py voice-pipeline`` or ``--component voice-pipeline``)
it loads that Component's dependency -> ARM64-compatibility map from the
Component's ``arch-deps.toml`` manifest, runs it through
:func:`~hellodj_platform_logic.dependency_gate.gate`, and prints the resulting
:class:`~hellodj_platform_logic.types.GateDecision`:

  * **ARM64-only** when every runtime dependency is ARM64-compatible (R4.1,
    R4.2) — the default the platform wants for every Component.
  * **x86-64 fallback** when at least one dependency is not ARM64-compatible
    (R4.3), printing the specific incompatible dependencies so the x86
    requirement is documented (R4.4).

The gate is *informational / documenting*: choosing x86-64 is a legitimate,
documented outcome, not a build failure, so a clean run exits ``0`` for either
architecture (the gate decides arch and records the reason; it does not fail a
build merely for choosing x86). It exits non-zero only on an operational error
— a missing or malformed manifest, or an unknown Component — because those mean
the gate could not actually be evaluated and the build must not silently pass.

The manifest
------------

Each Component carries an ``arch-deps.toml`` next to its package describing the
ARM64 compatibility of its runtime dependencies. A component with no manifest
is treated as covered by the platform-wide default manifest
(:data:`DEFAULT_ARCH_DEPS`), which the design's Decision D4 establishes as
all-ARM64 (no hard ARM64 blocker was found in the stack once STT/intent/TTS
moved to Bedrock). The manifest is the documented override mechanism: flip a
dependency to ``false`` (and add a note) to force that Component onto x86-64
and have the gate document exactly which dependency required it.

Manifest format (TOML)::

    # arch-deps.toml
    [dependencies]
    wakeword_onnx_runtime = true
    stt_engine = true
    audio_libraries = true
    # ... a dependency that is NOT available on ARM64 -> forces x86-64:
    some_x86_only_lib = false

    [notes]
    some_x86_only_lib = "vendor ships x86-64 wheels only; see ticket ABC-123"

Usage::

    python tools/gate_dependencies.py <component>
    python tools/gate_dependencies.py --component <component>
    python tools/gate_dependencies.py --all
    python tools/gate_dependencies.py --self-test

Design references:
    * Decision D4 / Requirement 4: CPU architecture (Graviton default with
      dependency verification)
    * Correctness Property 5: Graviton/x86 dependency-gate decision

Requirements: 4.1, 4.2, 4.3, 4.4, 4.5
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

PLATFORM_ROOT = Path(__file__).resolve().parent.parent
COMPONENTS_ROOT = PLATFORM_ROOT / "components"

# Make the shared pure-logic package importable without installation, mirroring
# the layout used by the other platform tools (the package lives under
# ``components/hellodj_platform_logic``).
if str(COMPONENTS_ROOT) not in sys.path:
    sys.path.insert(0, str(COMPONENTS_ROOT))

from hellodj_platform_logic.dependency_gate import (  # noqa: E402
    REQUIRED_DEPENDENCY_CLASSES,
    gate,
)
from hellodj_platform_logic.types import CpuArch, GateDecision  # noqa: E402

#: The manifest file name a Component carries to describe its dependencies.
MANIFEST_NAME = "arch-deps.toml"

#: The platform-wide default dependency map used when a Component ships no
#: ``arch-deps.toml``. Per the design's Decision D4 no hard ARM64 blocker was
#: found in the stack (STT/intent/TTS moved to Bedrock; the wake-word
#: ``onnxruntime`` is the only build-from-source item and builds on ARM64), so
#: the default covers all six :data:`REQUIRED_DEPENDENCY_CLASSES` (R4.5) as
#: ARM64-compatible. A Component overrides this default by shipping its own
#: manifest.
DEFAULT_ARCH_DEPS: dict[str, bool] = dict.fromkeys(REQUIRED_DEPENDENCY_CLASSES, True)


class ManifestError(Exception):
    """Raised when a Component's dependency manifest is missing or malformed.

    This is an *operational* error (the gate could not be evaluated), distinct
    from a legitimate x86-64 decision. It makes the runner exit non-zero so a
    broken manifest never silently passes the build.
    """


def component_dir(component: str) -> Path:
    """Return the directory for ``component`` under ``components/``.

    Raises:
        ManifestError: if the named Component has no directory (an unknown
            Component means the gate cannot be evaluated).
    """
    candidate = (COMPONENTS_ROOT / component).resolve()
    if not candidate.is_dir():
        raise ManifestError(f"unknown component '{component}': no {candidate} directory")
    return candidate


def load_arch_deps(component: str) -> tuple[dict[str, bool], dict[str, str], Path | None]:
    """Load one Component's dependency map, notes, and manifest path.

    Reads ``<component>/arch-deps.toml`` when present, returning its
    ``[dependencies]`` table (dependency name -> ARM64 compatibility bool) and
    its optional ``[notes]`` table (dependency name -> human note documenting
    the x86 requirement, R4.4). When the Component ships no manifest, the
    platform-wide :data:`DEFAULT_ARCH_DEPS` is returned with a ``None`` path so
    the caller can report that the default was applied.

    Args:
        component: The Component name (a directory under ``components/``).

    Returns:
        A ``(deps, notes, manifest_path)`` tuple. ``manifest_path`` is ``None``
        when the default map was used.

    Raises:
        ManifestError: if the manifest exists but is not valid TOML, its
            ``[dependencies]`` table is missing/empty or malformed, or a
            compatibility value is not a boolean.
    """
    directory = component_dir(component)
    manifest = directory / MANIFEST_NAME
    if not manifest.is_file():
        return dict(DEFAULT_ARCH_DEPS), {}, None

    try:
        with manifest.open("rb") as handle:
            data = tomllib.load(handle)
    except (tomllib.TOMLDecodeError, OSError) as exc:
        raise ManifestError(f"{manifest}: cannot parse manifest: {exc}") from exc

    raw_deps = data.get("dependencies")
    if not isinstance(raw_deps, dict) or not raw_deps:
        raise ManifestError(
            f"{manifest}: missing or empty [dependencies] table (need at least one "
            "dependency -> ARM64-compatibility mapping)"
        )

    deps: dict[str, bool] = {}
    for name, value in raw_deps.items():
        if not isinstance(value, bool):
            raise ManifestError(
                f"{manifest}: dependency '{name}' must map to a boolean "
                f"(true = ARM64-compatible), got {value!r}"
            )
        deps[name] = value

    raw_notes = data.get("notes", {})
    if not isinstance(raw_notes, dict):
        raise ManifestError(f"{manifest}: [notes] must be a table of dependency -> note")
    notes = {str(k): str(v) for k, v in raw_notes.items()}

    return deps, notes, manifest


def _format_decision(component: str, decision: GateDecision, notes: dict[str, str]) -> str:
    """Render a human-readable summary of the gate decision for a Component.

    For an ARM64-only decision this documents that the Component keeps the
    Graviton default. For an x86-64 decision it lists the incompatible
    dependencies that forced the fallback and any documented notes (R4.4).
    """
    lines: list[str] = []
    if decision.arch is CpuArch.ARM64:
        lines.append(
            f"  {component}: ARM64-only (Graviton default) — "
            "all dependencies ARM64-compatible"
        )
        return "\n".join(lines)

    lines.append(
        f"  {component}: x86-64 fallback — {len(decision.incompatible_dependencies)} "
        "dependency(ies) not ARM64-compatible:"
    )
    for dep in decision.incompatible_dependencies:
        note = notes.get(dep)
        if note:
            lines.append(f"      - {dep} (requires x86-64): {note}")
        else:
            lines.append(f"      - {dep} (requires x86-64)")
    return "\n".join(lines)


def gate_component(component: str) -> int:
    """Evaluate the dependency gate for one Component and print the decision.

    Returns a process exit code: ``0`` for a successfully-evaluated decision
    (ARM64-only *or* a documented x86-64 fallback — the gate does not fail the
    build merely for choosing x86), and ``1`` on an operational error (missing
    or malformed manifest, or an unknown Component).
    """
    try:
        deps, notes, manifest = load_arch_deps(component)
    except ManifestError as exc:
        print(f"dependency gate FAILED for '{component}': {exc}")
        return 1

    decision = gate(deps)
    if manifest is not None:
        source = str(manifest.relative_to(PLATFORM_ROOT))
    else:
        source = f"platform default ({len(deps)} classes)"
    print(f"dependency gate for component '{component}' (source: {source}):")
    print(_format_decision(component, decision, notes))
    return 0


def _run_self_test() -> int:
    """Smoke-check that the gate reports ARM64 for all-true and x86 for a false.

    Returns a process exit code (0 on success, 1 on any unexpected outcome).
    """
    ok = True

    all_arm64 = gate(dict.fromkeys(REQUIRED_DEPENDENCY_CLASSES, True))
    if all_arm64.arch is not CpuArch.ARM64 or all_arm64.incompatible_dependencies:
        print("self-test FAILED: all-ARM64 map did not yield ARM64-only")
        ok = False

    mixed = gate({"lib_a": True, "x86_only": False})
    if mixed.arch is not CpuArch.X86_64 or mixed.incompatible_dependencies != ("x86_only",):
        print("self-test FAILED: a false dependency did not force x86-64")
        ok = False

    if ok:
        print("self-test passed: all-ARM64 -> ARM64-only, any incompatible -> x86-64.")
        return 0
    return 1


def discover_components() -> list[str]:
    """Return every Component directory name under ``components/``.

    Skips the shared pure-logic package (which ships no image and has no
    dependency manifest) and any non-directory entries.
    """
    if not COMPONENTS_ROOT.is_dir():
        return []
    names: list[str] = []
    for child in sorted(COMPONENTS_ROOT.iterdir()):
        if not child.is_dir():
            continue
        if child.name == "hellodj_platform_logic":
            continue
        if child.name.startswith("."):
            continue
        names.append(child.name)
    return names


def _usage() -> str:
    """Return the CLI usage string."""
    return (
        "usage: gate_dependencies.py <component> | --component <name> | --all | "
        "--self-test"
    )


def main(argv: list[str]) -> int:
    """Entry point: gate one Component (or all), returning a process exit code."""
    args = list(argv)

    if "--self-test" in args:
        args = [a for a in args if a != "--self-test"]
        rc = _run_self_test()
        if rc != 0 or not args:
            return rc

    if "--all" in args:
        components = discover_components()
        if not components:
            print("dependency gate: no components to scan.")
            return 0
        exit_code = 0
        for component in components:
            exit_code |= gate_component(component)
        return exit_code

    # Accept both `--component <name>` and a bare positional `<name>`.
    component: str | None = None
    if "--component" in args:
        idx = args.index("--component")
        if idx + 1 >= len(args):
            print(_usage())
            return 2
        component = args[idx + 1]
    else:
        positionals = [a for a in args if not a.startswith("-")]
        if len(positionals) == 1:
            component = positionals[0]

    if component is None:
        print(_usage())
        return 2

    return gate_component(component)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

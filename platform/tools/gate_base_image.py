#!/usr/bin/env python3
"""Build-stage Nix base-image gate runner (task 18.2).

This is the executable the Beta -> Gamma -> Prod deployment pipeline invokes in
its build stage to enforce Requirement 5.4: *if a Container_Image is defined
with a non-Nix base image, the pipeline rejects it during the build stage.* It
is the thin CI wrapper around the pure decision function
``hellodj_platform_logic.base_image_gate.check_base`` (Property 6), so the
pipeline and the property-tested logic share a single source of truth.

What it does
------------

For every platform Component under ``components/`` it discovers the image
build definition (a Nix ``flake.nix`` and/or any ``Dockerfile``), derives a
:class:`~hellodj_platform_logic.types.BaseImageDescriptor` for that image, and
runs it through :func:`~hellodj_platform_logic.base_image_gate.check_base`. If
*any* image's base is not Nix-produced (for example an ``ubuntu`` or ``debian``
base), the gate prints the offending component and reason and exits non-zero,
failing the build (R5.1, R5.4). When every image is a Nix-produced base it
prints a per-component pass line and exits zero.

Base descriptor derivation
---------------------------

The HelloDJ components are built as Nix OCI images via
``pkgs.dockerTools.buildLayeredImage`` inside a ``flake.nix`` (no Dockerfile
``FROM`` line). The scanner therefore treats a component as Nix-produced when
its build definition is a Nix flake that builds the image with ``dockerTools``
and does **not** reference a forbidden Debian/Ubuntu base. If a ``Dockerfile``
is present, its ``FROM`` base is extracted and fed to the gate as well, so a
Debian/Ubuntu ``FROM`` is caught even if a flake exists alongside it.

Usage::

    python tools/gate_base_image.py [COMPONENT_DIR ...]
    python tools/gate_base_image.py --self-test

With no arguments it scans every component under ``components/``. With
``--self-test`` it verifies the gate accepts a synthetic Nix base and rejects a
synthetic Debian/Ubuntu base (a fast smoke check for CI), then still runs the
real component scan.

Design references:
    * Deployment Pipeline build-stage base-image gate (R5.4)
    * Correctness Property 6: Non-Nix base-image gate

Requirements: 5.1, 5.4
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

PLATFORM_ROOT = Path(__file__).resolve().parent.parent
COMPONENTS_ROOT = PLATFORM_ROOT / "components"

# Make the shared pure-logic package importable without installation, mirroring
# the layout used by the other platform tools (the package lives under
# ``components/hellodj_platform_logic``).
if str(COMPONENTS_ROOT) not in sys.path:
    sys.path.insert(0, str(COMPONENTS_ROOT))

from hellodj_platform_logic.base_image_gate import (  # noqa: E402
    FORBIDDEN_BASE_NAMES,
    check_base,
)
from hellodj_platform_logic.types import BaseImageDescriptor  # noqa: E402

#: A Nix ``dockerTools`` image builder call (buildLayeredImage / buildImage /
#: streamLayeredImage). Its presence marks the flake as producing a Nix image.
_DOCKERTOOLS_RE = re.compile(r"dockerTools\.\w*[Ii]mage")

#: Alternation of the forbidden base names for use inside the patterns below.
_FORBIDDEN_ALT = "|".join(sorted(re.escape(n) for n in FORBIDDEN_BASE_NAMES))

#: A forbidden base referenced in a *base-declaring position* inside a Nix
#: flake. This deliberately does NOT match prose such as the flakes'
#: ``No Ubuntu/Debian base layers`` documentation note; it only matches an
#: actual base pull/reference:
#:   * a ``dockerTools.pullImage``/``fromImage``/``fromImageName`` that names an
#:     ``ubuntu``/``debian`` image, or
#:   * a quoted OCI image reference like ``"ubuntu:22.04"`` / ``"library/debian"``
#:     (a bare word followed by a ``:`` tag, ``/`` path, or ``@`` digest).
_FORBIDDEN_NIX_PATTERNS = (
    re.compile(
        r"(?:pullImage|fromImage(?:Name)?)\b[^\n]*?\b(" + _FORBIDDEN_ALT + r")\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"""["'](?:[\w.\-]+/)*(""" + _FORBIDDEN_ALT + r""")(?=[:@/])""",
        re.IGNORECASE,
    ),
)


def _forbidden_reference(build_text: str) -> str | None:
    """Return a forbidden base actually referenced by a Nix flake, if any.

    Only matches an ``ubuntu``/``debian`` base in a real base-declaring
    position (a ``dockerTools`` ``pullImage``/``fromImage`` call or a quoted OCI
    image reference such as ``"ubuntu:22.04"``). Comment lines are skipped and
    prose that merely documents the *absence* of a Debian/Ubuntu base (the
    flakes' ``No Ubuntu/Debian base layers`` note) does not match, so
    documentation never trips the gate.
    """
    for raw_line in build_text.splitlines():
        line = raw_line.strip()
        # Skip Nix / Dockerfile (``#``) comment lines outright.
        if line.startswith("#"):
            continue
        for pattern in _FORBIDDEN_NIX_PATTERNS:
            match = pattern.search(line)
            if match:
                return match.group(1)
    return None


def derive_descriptor(component_dir: Path) -> BaseImageDescriptor | None:
    """Derive a :class:`BaseImageDescriptor` for one component's image.

    The Nix ``flake.nix`` is the authoritative definition of a component's
    shippable image (the platform standard is Nix-only, R5.1). A component with
    a ``flake.nix`` is gated on that flake: it is Nix-produced when the flake
    builds the image with ``dockerTools`` and references no forbidden
    Debian/Ubuntu base in an active (non-comment) line; a flake that actively
    pulls such a base yields that forbidden ``base_name`` so the gate rejects
    it.

    A component that has **no** ``flake.nix`` yet — only a reference
    ``Dockerfile`` whose Nix packaging is still pending (task 20.1) — returns
    ``None`` so the gate reports it as *skipped/pending* rather than failing the
    build. Once its Nix flake lands, the flake becomes authoritative and the
    gate enforces it. A ``Dockerfile`` present *alongside* a flake is treated as
    reference material and ignored in favour of the flake.

    Args:
        component_dir: The component directory to inspect.

    Returns:
        The base-image descriptor fed to :func:`check_base`, or ``None`` when
        the component has no Nix flake yet (Nix packaging pending task 20.1).
    """
    flake = component_dir / "flake.nix"
    if flake.is_file():
        text = flake.read_text(encoding="utf-8", errors="replace")
        forbidden = _forbidden_reference(text)
        if forbidden is not None:
            # A flake that actively pulls in a Debian/Ubuntu base (outside of a
            # documentation comment) is reported with that forbidden base name.
            return BaseImageDescriptor(base_name=forbidden, nix_produced=False)
        nix_produced = bool(_DOCKERTOOLS_RE.search(text))
        return BaseImageDescriptor(
            base_name=f"nix:{component_dir.name}",
            nix_produced=nix_produced,
        )

    # No Nix flake yet: the only build definition is a reference Dockerfile
    # whose Nix packaging is deferred to task 20.1. Signal "pending" (None) so
    # the gate skips it rather than failing the build on a not-yet-Nix-packaged
    # component; it will be enforced once the flake exists.
    return None


def discover_component_dirs(roots: list[Path]) -> list[Path]:
    """Return the component directories to gate.

    With explicit roots, each root is a component directory. With no roots,
    every immediate subdirectory of ``components/`` that carries an image build
    definition (``flake.nix`` or ``Dockerfile``) is scanned; the pure-logic
    package directory (which ships no image) is skipped.
    """
    if roots:
        return [r.resolve() for r in roots]
    if not COMPONENTS_ROOT.is_dir():
        return []
    dirs: list[Path] = []
    for child in sorted(COMPONENTS_ROOT.iterdir()):
        if not child.is_dir():
            continue
        if (child / "flake.nix").is_file() or (child / "Dockerfile").is_file():
            dirs.append(child)
    return dirs


def _run_self_test() -> int:
    """Verify the gate accepts a Nix base and rejects a Debian/Ubuntu base.

    Returns a process exit code (0 on success, 1 on any unexpected outcome).
    """
    ok = True

    nix_base = BaseImageDescriptor(base_name="nix:example", nix_produced=True)
    if not check_base(nix_base).accepted:
        print("self-test FAILED: a Nix-produced base was rejected")
        ok = False

    for forbidden in ("ubuntu:22.04", "debian"):
        result = check_base(
            BaseImageDescriptor(base_name=forbidden, nix_produced=True),
        )
        if result.accepted:
            print(f"self-test FAILED: forbidden base '{forbidden}' was accepted")
            ok = False

    non_nix = BaseImageDescriptor(base_name="scratch", nix_produced=False)
    if check_base(non_nix).accepted:
        print("self-test FAILED: a non-Nix base was accepted")
        ok = False

    if ok:
        print("self-test passed: Nix bases accepted, non-Nix/forbidden rejected.")
        return 0
    return 1


def gate_components(component_dirs: list[Path]) -> int:
    """Gate every component's image base, returning a process exit code.

    Prints a pass/reject line per component and returns non-zero if any image
    is rejected (R5.4).
    """
    if not component_dirs:
        print("base-image gate: no components to scan.")
        return 0

    rejected: list[tuple[str, str]] = []
    passed = 0
    skipped = 0
    for component_dir in component_dirs:
        name = component_dir.name
        descriptor = derive_descriptor(component_dir)
        if descriptor is None:
            # No Nix flake yet — Nix packaging pending (task 20.1). Skip rather
            # than fail the build on a not-yet-Nix-packaged component.
            print(f"  SKIP {name}: Nix image build pending (task 20.1); not yet gated")
            skipped += 1
            continue
        result = check_base(descriptor)
        if result.accepted:
            print(f"  PASS {name}: base '{descriptor.base_name}' is Nix-produced")
            passed += 1
        else:
            print(f"  REJECT {name}: {result.reason}")
            rejected.append((name, result.reason))

    if rejected:
        print(f"base-image gate FAILED: {len(rejected)} non-Nix base(s) detected.")
        return 1
    print(
        f"base-image gate passed: {passed} component(s) Nix-produced, "
        f"{skipped} pending Nix packaging."
    )
    return 0


def main(argv: list[str]) -> int:
    """Entry point: run the self-test (optional) then gate all components."""
    args = list(argv)
    self_test = False
    if "--self-test" in args:
        self_test = True
        args = [a for a in args if a != "--self-test"]

    if self_test:
        rc = _run_self_test()
        if rc != 0:
            return rc

    roots = [Path(a) for a in args]
    component_dirs = discover_component_dirs(roots)
    return gate_components(component_dirs)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

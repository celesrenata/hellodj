#!/usr/bin/env python3
"""Pin-time verification workflow across all flake inputs (task 18.1).

This is the executable the pinning / ``nix flake update`` workflow invokes to
enforce Requirement 11: *every pinned upstream and base version is the latest
verified against upstream at pin time.* It is the thin wrapper around the pure,
property-tested decision function
``hellodj_platform_logic.pinning.verify_pin`` (Property 13), so the workflow
and the shared decision logic reason over one source of truth.

What it does
------------

It reads the declarative pin manifest ``pins.toml`` (next to ``components/`` at
the platform root), which enumerates EVERY input Requirement 11.1 calls out:

    Lavalink, lavaplayer, LavaSrc, youtube-source,
    Temurin/JDK (== 25 LTS), nixpkgs, nixos-generators, Karpenter,
    and the EKS Kubernetes version.

For each input it:

1. asserts the input pins upstream via ``github:owner/repo/branch`` (R11.3) —
   a ``path:`` input, or a missing owner/repo/branch, is rejected up front
   (the NixOS steering forbids ``path:`` inputs);
2. builds a :class:`~hellodj_platform_logic.types.FlakeInputPin` from the
   manifest (its ``pinned_identifier`` is the revision/tag/version captured in
   ``flake.lock`` — or, for the CDK-managed Karpenter/EKS inputs, the version
   literal in the infra code — at pin time);
3. resolves that input's upstream identifier (see *Upstream resolution* below)
   and runs the pin through :func:`verify_pin`, which

   * **accepts** iff the pinned identifier equals the resolved upstream
     identifier (R11.1);
   * **rejects** a mismatched pin, naming the input, so the caller retains the
     prior pinned revision (R11.5);
   * **fails** an unresolved upstream, naming the input, so the caller retains
     the prior pinned revision (R11.6).

The Temurin input additionally must pin Temurin **25** — its manifest
``feature_version`` is asserted ``== 25`` (R3.7 / R11.2) before its pin is even
verified against upstream, and the recorded upstream LTS identifier must equal
the pinned one (R11.2).

Prior-revision retention (R11.5/R11.6)
--------------------------------------

``verify_pin`` holds no state; it only reports the outcome. The *workflow*
implements the "retain the prior pinned revision" half of R11.5/R11.6: when an
input's pin is rejected or fails, this tool does **not** rewrite that input's
pin — ``pins.toml`` (and the underlying ``flake.lock`` the maintainer would
have refreshed) is left exactly as it was, so the prior pinned revision stays
in force. The tool never mutates the manifest; it verifies and reports, and a
non-zero exit blocks the pin refresh so no mismatched/unresolved pin is adopted.

Upstream resolution
-------------------

``verify_pin`` makes no live network/git calls (so the correctness property can
exercise it directly); the workflow resolves the upstream identifier and injects
it. Resolution reads the recorded ``pins.upstream.toml`` (the
resolved-at-pin-time identifiers observed from each input's upstream source). An
input absent from that record — or mapped to the empty string — models an
**unresolved upstream** (``None``), driving the R11.6 failure path. Refreshing a
pin means running ``nix flake update <input>`` (R11.4), re-resolving upstream,
and updating both files in lockstep.

Usage::

    python tools/gate_pins.py                     # verify all inputs in pins.toml
    python tools/gate_pins.py lavalink temurin    # verify only the named inputs
    python tools/gate_pins.py --manifest P --upstream U
    python tools/gate_pins.py --self-test

With ``--self-test`` it verifies (over synthetic inputs) that the workflow
accepts a matching pin, rejects a mismatched pin naming the input, and fails an
unresolved-upstream pin naming the input — a fast smoke check for CI — then
still verifies the real manifest.

Design references:
    * Components — Upstream version pinning (§11): pin-time verification wired
      across every ``github:owner/repo/branch`` input; mismatch -> reject +
      retain prior; unresolved -> fail + retain prior.
    * Correctness Property 13: pin verification accept/reject/retain.

Requirements: 11.1, 11.2, 11.3, 11.4, 11.5, 11.6
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

from hellodj_platform_logic.pinning import verify_pin  # noqa: E402
from hellodj_platform_logic.types import (  # noqa: E402
    FlakeInputPin,
    PinVerification,
)

#: The declarative pin manifest at the platform root (enumerates every input).
DEFAULT_MANIFEST = PLATFORM_ROOT / "pins.toml"

#: The recorded resolved-at-pin-time upstream identifiers.
DEFAULT_UPSTREAM = PLATFORM_ROOT / "pins.upstream.toml"

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

    Every input must declare a non-empty ``owner``, ``repo``, ``branch`` (so it
    resolves to ``github:owner/repo/branch`` — R11.3) and a non-empty
    ``pinned_identifier``. The Temurin input must additionally declare
    ``feature_version = 25`` (R3.7 / R11.2). Every input enumerated by
    :data:`REQUIRED_INPUTS` must be present.

    Args:
        manifest: Path to ``pins.toml``.

    Returns:
        A mapping from input name to its :class:`FlakeInputPin`.

    Raises:
        PinManifestError: on a missing/empty ``[inputs]`` table, a malformed
            input entry, a ``path:``-style (non-github) input, a Temurin
            ``feature_version`` other than 25, or a missing required input.
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

        owner = entry.get("owner")
        repo = entry.get("repo")
        branch = entry.get("branch")
        pinned = entry.get("pinned_identifier")

        # Every field must be a non-empty string. This is what makes the input a
        # `github:owner/repo/branch` pin (R11.3); a `path:` input (or one with a
        # `url = "path:..."` / missing owner) cannot satisfy these and is
        # rejected here — the NixOS steering forbids `path:` inputs.
        for field, value in (
            ("owner", owner),
            ("repo", repo),
            ("branch", branch),
            ("pinned_identifier", pinned),
        ):
            if not isinstance(value, str) or not value:
                raise PinManifestError(
                    f"{manifest}: input '{name}' has missing/empty '{field}' — "
                    "every input must pin via github:owner/repo/branch (R11.3), "
                    "never a path: input"
                )

        # Guard against a smuggled scheme in any field (e.g. someone writing
        # owner = "path:/…"): the github: form takes bare owner/repo/branch.
        for field, value in (("owner", owner), ("repo", repo), ("branch", branch)):
            if ":" in value or value.startswith("path"):
                raise PinManifestError(
                    f"{manifest}: input '{name}' field '{field}'={value!r} looks "
                    "like a non-github (path:/url) reference — inputs must be "
                    "github:owner/repo/branch (R11.3)"
                )

        if name == "temurin":
            feature = entry.get("feature_version")
            if feature != TEMURIN_FEATURE_VERSION:
                raise PinManifestError(
                    f"{manifest}: temurin pin feature_version={feature!r} must be "
                    f"{TEMURIN_FEATURE_VERSION} — the migration target is Temurin "
                    f"{TEMURIN_FEATURE_VERSION} (LTS) and no other feature release "
                    "(R3.7 / R11.2)"
                )

        pins[name] = FlakeInputPin(
            input_name=name,
            owner=owner,
            repo=repo,
            branch=branch,
            pinned_identifier=pinned,
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


def _format_outcome(result: PinVerification) -> str:
    """Render a human-readable pass/reject/fail line for one input."""
    if result.accepted:
        return (
            f"  PASS {result.input_name}: pinned identifier matches upstream "
            f"{result.upstream_identifier!r}"
        )
    if result.upstream_identifier is None:
        # Unresolved upstream (R11.6).
        return f"  FAIL {result.input_name}: {result.reason}"
    # Mismatch (R11.5).
    return f"  REJECT {result.input_name}: {result.reason}"


def verify_pins(
    pins: dict[str, FlakeInputPin],
    upstream: dict[str, str | None],
    only: list[str] | None = None,
) -> tuple[int, list[PinVerification]]:
    """Verify each pin against its resolved upstream identifier.

    For every input (optionally filtered to ``only``), the upstream identifier
    is looked up in ``upstream`` (absent -> ``None`` -> unresolved, R11.6) and
    the pin is run through :func:`verify_pin`. A rejected (R11.5) or failed
    (R11.6) outcome does not mutate any pin — the prior pinned revision is
    retained by leaving ``pins.toml``/``flake.lock`` untouched — and drives a
    non-zero exit so the pin refresh is blocked.

    Args:
        pins: The loaded input pins.
        upstream: The resolved-at-pin-time upstream identifiers.
        only: If given, verify only these input names (each must exist).

    Returns:
        A ``(exit_code, results)`` tuple: ``exit_code`` is ``0`` when every
        verified pin is accepted, ``1`` when any pin is rejected/failed, and
        ``2`` on an operational error (an unknown ``only`` name). ``results`` is
        the per-input :class:`PinVerification` list.
    """
    if only:
        unknown = [name for name in only if name not in pins]
        if unknown:
            print(
                f"pin gate FAILED: unknown input(s) requested: {', '.join(unknown)}"
            )
            return 2, []
        names = [name for name in pins if name in only]
    else:
        names = list(pins)

    results: list[PinVerification] = []
    rejected = 0
    failed = 0
    for name in names:
        pin = pins[name]
        # Absent from the record => unresolved upstream (None) => R11.6 path.
        resolved = upstream.get(name)
        result = verify_pin(pin, resolved)
        results.append(result)
        print(_format_outcome(result))
        if not result.accepted:
            if result.upstream_identifier is None:
                failed += 1
            else:
                rejected += 1

    if rejected or failed:
        print(
            f"pin gate FAILED: {rejected} mismatched (rejected), {failed} "
            "unresolved (failed) — prior pinned revision(s) retained; refresh "
            "with `nix flake update <input>` and re-verify"
        )
        return 1, results

    print(f"pin gate passed: {len(results)} input pin(s) match upstream at pin time.")
    return 0, results


def _run_self_test() -> int:
    """Verify the workflow accepts a match, rejects a mismatch, fails unresolved.

    Exercises the three R11 outcomes over synthetic inputs (no manifest needed),
    asserting the affected input is named on both failure paths. Returns a
    process exit code (0 on success, 1 on any unexpected outcome).
    """
    ok = True

    def pin(identifier: str) -> FlakeInputPin:
        return FlakeInputPin(
            input_name="example",
            owner="hellodj",
            repo="example",
            branch="main",
            pinned_identifier=identifier,
        )

    # Accept iff equal (R11.1).
    accept = verify_pin(pin("rev-a"), "rev-a")
    if not accept.accepted or accept.reason != "":
        print("self-test FAILED: matching pin was not accepted")
        ok = False

    # Reject on mismatch, name the input (R11.5).
    reject = verify_pin(pin("rev-a"), "rev-b")
    if reject.accepted or "example" not in reject.reason:
        print("self-test FAILED: mismatched pin was not rejected / not named")
        ok = False

    # Fail on unresolved upstream, name the input (R11.6).
    fail = verify_pin(pin("rev-a"), None)
    if fail.accepted or fail.upstream_identifier is not None or "example" not in fail.reason:
        print("self-test FAILED: unresolved upstream did not fail / not named")
        ok = False

    if ok:
        print(
            "self-test passed: match accepted, mismatch rejected (named), "
            "unresolved failed (named)."
        )
        return 0
    return 1


def _usage() -> str:
    """Return the CLI usage string."""
    return (
        "usage: gate_pins.py [INPUT ...] [--manifest PATH] [--upstream PATH] "
        "[--self-test]"
    )


def _extract_option(args: list[str], flag: str) -> tuple[list[str], str | None]:
    """Pull ``--flag VALUE`` out of ``args``, returning the remainder and value."""
    if flag not in args:
        return args, None
    idx = args.index(flag)
    if idx + 1 >= len(args):
        raise PinManifestError(f"{flag} requires a path argument")
    value = args[idx + 1]
    return args[:idx] + args[idx + 2 :], value


def main(argv: list[str]) -> int:
    """Entry point: verify the pin manifest, returning a process exit code."""
    args = list(argv)

    self_test = "--self-test" in args
    if self_test:
        args = [a for a in args if a != "--self-test"]

    try:
        args, manifest_opt = _extract_option(args, "--manifest")
        args, upstream_opt = _extract_option(args, "--upstream")
    except PinManifestError as exc:
        print(f"pin gate FAILED: {exc}")
        return 2

    if self_test:
        rc = _run_self_test()
        if rc != 0:
            return rc

    if any(a.startswith("-") for a in args):
        print(_usage())
        return 2

    manifest = Path(manifest_opt) if manifest_opt else DEFAULT_MANIFEST
    upstream_file = Path(upstream_opt) if upstream_opt else DEFAULT_UPSTREAM
    only = list(args) if args else None

    try:
        pins = load_pins(manifest)
        upstream = load_upstream(upstream_file)
    except PinManifestError as exc:
        print(f"pin gate FAILED: {exc}")
        return 2

    print(
        f"pin gate: verifying {len(only) if only else len(pins)} input(s) "
        f"from {manifest.name} against {upstream_file.name} at pin time (R11)"
    )
    exit_code, _results = verify_pins(pins, upstream, only)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

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

For each input it verifies that the pinned identifier matches the resolved
upstream identifier. See :mod:`_pin_manifest` for I/O details.

Design references:
    * Components — Upstream version pinning (§11): pin-time verification wired
      across every ``github:owner/repo/branch`` input; mismatch -> reject +
      retain prior; unresolved -> fail + retain prior.
    * Correctness Property 13: pin verification accept/reject/retain.

Requirements: 11.1, 11.2, 11.3, 11.4, 11.5, 11.6
"""

from __future__ import annotations

import sys
from pathlib import Path

PLATFORM_ROOT = Path(__file__).resolve().parent.parent
COMPONENTS_ROOT = PLATFORM_ROOT / "components"

# Make the shared pure-logic package importable without installation.
if str(COMPONENTS_ROOT) not in sys.path:
    sys.path.insert(0, str(COMPONENTS_ROOT))

from _pin_manifest import (  # noqa: E402
    PinManifestError,
    load_pins,
    load_upstream,
)
from hellodj_platform_logic.pinning import verify_pin  # noqa: E402
from hellodj_platform_logic.types import (  # noqa: E402
    FlakeInputPin,
    PinVerification,
)

#: The declarative pin manifest at the platform root (enumerates every input).
DEFAULT_MANIFEST = PLATFORM_ROOT / "pins.toml"

#: The recorded resolved-at-pin-time upstream identifiers.
DEFAULT_UPSTREAM = PLATFORM_ROOT / "pins.upstream.toml"


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

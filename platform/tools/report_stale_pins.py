#!/usr/bin/env python3
"""Stale-pin report over ``pins.toml`` vs current upstream (task 16.1 / R6.1).

This is the executable the dependency-bump workflow invokes to answer the
question Requirement 6.1 poses: *which pinned dependencies are out of date?* It
is the thin wrapper around the pure, property-tested decision function
:func:`hellodj_platform_logic.stale_pins.stale_pins` (Property 6), so the
tooling and the shared decision logic reason over one source of truth.

What it does
------------

It loads the declarative pin manifest ``pins.toml`` (at the platform root — the
same manifest the pin gate reads) and the recorded resolved-at-pin-time upstream
identifiers ``pins.upstream.toml`` (via the *same* ``pins.upstream.toml``
resolution the pin gate uses — :func:`gate_pins.load_upstream`), then calls
:func:`stale_pins` to produce the report. It prints one line per stale entry —
every entry whose pinned identifier does **not** equal the current upstream
identifier resolved for it — listing that entry's pinned identifier and its
current upstream identifier.

"Stale" is defined by *exactly* the same ``pinned != upstream`` comparison
:func:`hellodj_platform_logic.pinning.verify_pin` performs — a stale entry is
one ``verify_pin`` would *reject*. An entry whose upstream cannot be resolved
(absent from / empty in ``pins.upstream.toml``) is a *resolution failure*, not a
stale pin, and is therefore **excluded** from the report (surfaced separately by
the pin gate as a fail, R11.6) — this is the ``stale_pins`` contract, verified
by Property 6.

Manifest loading (CodeCommit + github inputs)
--------------------------------------------

The HelloDJ source of truth has moved off public GitHub into private Amazon
CodeCommit (R1/R2/R3), so ``pins.toml`` now mixes two input forms: the five
migrated repos declare ``type = "codecommit"`` (region/repo/branch) while the
toolchain/cluster inputs remain legacy ``github:owner/repo/branch`` entries.
This wrapper classifies each entry with the pure
:func:`hellodj_platform_logic.codecommit_input.classify_input` (the same
decision the pin gate uses) and builds a
:class:`~hellodj_platform_logic.types.FlakeInputPin` for github and CodeCommit
entries alike — a CodeCommit entry additionally resolves to its ``git+https``
flake-input string via
:func:`~hellodj_platform_logic.codecommit_input.resolve_codecommit_input`
(logged for traceability). A ``path:`` entry (rejected everywhere by the NixOS
steering) and a CodeCommit entry missing region/repo/branch are operational
errors here: the report cannot be produced over a malformed manifest, so the
tool exits non-zero naming the offending entry — it never silently drops an
input from the staleness comparison.

Only ``input_name`` and ``pinned_identifier`` participate in the staleness
comparison (``stale_pins`` defers to ``verify_pin``, which compares only the
pinned vs upstream identifier), but every :class:`FlakeInputPin` field is
populated non-empty so the constructed pins are well-formed.

Usage::

    python tools/report_stale_pins.py                    # report over all inputs
    python tools/report_stale_pins.py lavalink temurin   # report over named inputs
    python tools/report_stale_pins.py --manifest P --upstream U
    python tools/report_stale_pins.py --self-test

With ``--self-test`` it verifies (over synthetic inputs) that the report lists
exactly the entries whose pinned identifier differs from a resolved upstream and
excludes an unresolved-upstream entry — a fast smoke check for CI — then still
reports over the real manifest.

Design references:
    * Components §6 — "Bump outdated dependencies through the existing pin
      workflow": the ``stale_pins`` mechanism + ``tools/report_stale_pins.py``
      wrapper enumerating every entry whose pinned identifier != current
      upstream identifier, listing both identifiers; reuses the pin gate's
      ``pins.upstream.toml`` resolution; unresolved excluded.
    * Correctness Property 6: the stale-pin report lists exactly the pins whose
      pinned identifier differs from upstream.
    * Data Models — ``StalePin``.

Requirements: 6.1
"""

from __future__ import annotations

import sys
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

PLATFORM_ROOT = Path(__file__).resolve().parent.parent
COMPONENTS_ROOT = PLATFORM_ROOT / "components"
TOOLS_ROOT = PLATFORM_ROOT / "tools"

# Make the shared pure-logic package and the sibling tools importable without
# installation, mirroring the layout used by the other platform tools (the
# package lives under ``components/hellodj_platform_logic``; the sibling gate
# tool lives under ``tools/``).
for _root in (COMPONENTS_ROOT, TOOLS_ROOT):
    if str(_root) not in sys.path:
        sys.path.insert(0, str(_root))

# Reuse the pin gate's upstream resolution verbatim so "stale" is defined by
# exactly the resolution the gate uses (R6.1). ``load_upstream`` reads
# ``pins.upstream.toml``'s ``[upstream]`` table into ``name -> identifier|None``.
from gate_pins import (  # noqa: E402
    DEFAULT_MANIFEST,
    DEFAULT_UPSTREAM,
    PinManifestError,
    load_upstream,
)
from hellodj_platform_logic.codecommit_input import (  # noqa: E402
    classify_input,
    missing_codecommit_fields,
    resolve_codecommit_input,
)
from hellodj_platform_logic.stale_pins import stale_pins  # noqa: E402
from hellodj_platform_logic.types import (  # noqa: E402
    FlakeInputPin,
    InputForm,
    StalePin,
)


def _load_toml(path: Path) -> dict:
    """Parse a TOML file, raising :class:`PinManifestError` on any I/O/parse error."""
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except FileNotFoundError as exc:
        raise PinManifestError(f"{path}: file not found") from exc
    except (tomllib.TOMLDecodeError, OSError) as exc:
        raise PinManifestError(f"{path}: cannot parse: {exc}") from exc


def _pin_from_entry(name: str, entry: Mapping[str, Any], manifest: Path) -> FlakeInputPin:
    """Build a :class:`FlakeInputPin` from one classified ``pins.toml`` entry.

    Classifies the entry with the pure :func:`classify_input` — the same
    decision the pin gate makes — and constructs a well-formed pin for github
    and CodeCommit entries alike. A ``path:`` entry (R3.3) or a CodeCommit entry
    missing region/repo/branch (R3.4) is an operational error: the report cannot
    be produced over a malformed manifest, so this raises :class:`PinManifestError`
    naming the offending entry rather than silently dropping it from the
    comparison.

    Only ``input_name`` and ``pinned_identifier`` are used by the staleness
    comparison, but every field is populated non-empty so the pin is valid.
    """
    if not isinstance(entry, Mapping):
        raise PinManifestError(
            f"{manifest}: input '{name}' must be a table with a pinned_identifier"
        )

    pinned = entry.get("pinned_identifier")
    if not isinstance(pinned, str) or not pinned:
        raise PinManifestError(
            f"{manifest}: input '{name}' has missing/empty 'pinned_identifier' — "
            "every input must record the revision/tag/version pinned at pin time"
        )

    form = classify_input(entry)

    if form is InputForm.PATH:
        raise PinManifestError(
            f"{manifest}: input '{name}' is a path:/path-style reference — inputs "
            "must be github:owner/repo/branch or a codecommit input, never a "
            "path: input (R3.3)"
        )

    if form is InputForm.INVALID:
        missing = ", ".join(missing_codecommit_fields(entry))
        raise PinManifestError(
            f"{manifest}: codecommit input '{name}' is missing required "
            f"field(s): {missing} (R3.4)"
        )

    if form is InputForm.CODECOMMIT:
        region = entry["region"]
        repo = entry["repo"]
        branch = entry["branch"]
        # Log the resolved git+https flake-input string for traceability; the
        # staleness comparison itself only uses input_name + pinned_identifier.
        resolved = resolve_codecommit_input(region, repo, branch)
        print(f"  input {name}: codecommit -> {resolved}")
        return FlakeInputPin(
            input_name=name,
            owner=region,
            repo=repo,
            branch=branch,
            pinned_identifier=pinned,
        )

    # GITHUB: a well-formed legacy github:owner/repo/branch entry.
    owner = entry.get("owner")
    repo = entry.get("repo")
    branch = entry.get("branch")
    for field, value in (("owner", owner), ("repo", repo), ("branch", branch)):
        if not isinstance(value, str) or not value:
            raise PinManifestError(
                f"{manifest}: github input '{name}' has missing/empty '{field}' — "
                "a legacy input must pin via github:owner/repo/branch"
            )
    return FlakeInputPin(
        input_name=name,
        owner=owner,
        repo=repo,
        branch=branch,
        pinned_identifier=pinned,
    )


def load_pins(manifest: Path) -> dict[str, FlakeInputPin]:
    """Load the pin manifest into ``input_name -> FlakeInputPin`` (both forms).

    Reads ``pins.toml``'s ``[inputs]`` table and builds a
    :class:`FlakeInputPin` per entry, accepting both the migrated
    ``type = "codecommit"`` inputs and the legacy ``github:owner/repo/branch``
    inputs (via :func:`classify_input`). This is the loader the stale-pin report
    uses; the pin gate performs the full R11 enumeration/Temurin assertions in
    its own loader — here we only need the ``pinned_identifier`` per input to
    compare against upstream, so the report can be produced over any well-formed
    manifest.

    Args:
        manifest: Path to ``pins.toml``.

    Returns:
        A mapping from input name to its :class:`FlakeInputPin`.

    Raises:
        PinManifestError: on a missing/empty ``[inputs]`` table, a ``path:``
            entry, or a CodeCommit entry missing a required field.
    """
    data = _load_toml(manifest)
    raw_inputs = data.get("inputs")
    if not isinstance(raw_inputs, dict) or not raw_inputs:
        raise PinManifestError(
            f"{manifest}: missing or empty [inputs] table (need at least one "
            "input -> pinned_identifier entry)"
        )

    pins: dict[str, FlakeInputPin] = {}
    for name, entry in raw_inputs.items():
        pins[name] = _pin_from_entry(name, entry, manifest)
    return pins


def _format_stale(entry: StalePin) -> str:
    """Render a human-readable stale-pin line listing both identifiers (R6.1)."""
    return (
        f"  STALE {entry.input_name}: pinned {entry.pinned_identifier!r} != "
        f"current upstream {entry.upstream_identifier!r}"
    )


def report_stale(
    pins: dict[str, FlakeInputPin],
    upstream: dict[str, str | None],
    only: list[str] | None = None,
) -> tuple[int, list[StalePin]]:
    """Produce and print the stale-pin report (R6.1).

    Delegates the per-entry staleness decision to the pure
    :func:`hellodj_platform_logic.stale_pins.stale_pins`, so the tool and
    Property 6 share one decision. Every reported entry lists its pinned
    identifier and the current upstream identifier it differs from; an
    unresolved-upstream entry is excluded (resolution failure, surfaced by the
    pin gate).

    Args:
        pins: The loaded input pins.
        upstream: The resolved-at-pin-time upstream identifiers (from the pin
            gate's ``pins.upstream.toml`` resolution).
        only: If given, report over only these input names (each must exist).

    Returns:
        A ``(exit_code, report)`` tuple: ``exit_code`` is ``0`` when no verified
        input is stale, ``1`` when one or more inputs are stale, and ``2`` on an
        operational error (an unknown ``only`` name). ``report`` is the
        :class:`StalePin` list.
    """
    if only:
        unknown = [name for name in only if name not in pins]
        if unknown:
            print(
                "stale-pin report FAILED: unknown input(s) requested: "
                f"{', '.join(unknown)}"
            )
            return 2, []
        selected = {name: pins[name] for name in pins if name in only}
    else:
        selected = pins

    report = stale_pins(selected, upstream)

    for entry in report:
        print(_format_stale(entry))

    if report:
        print(
            f"stale-pin report: {len(report)} of {len(selected)} pinned input(s) "
            "are behind upstream — bump with `nix flake update <input>` and "
            "re-verify via the pin gate before adoption (R6.2/R6.4)"
        )
        return 1, report

    print(
        f"stale-pin report: all {len(selected)} verified input pin(s) match "
        "current upstream — nothing to bump."
    )
    return 0, report


def _run_self_test() -> int:
    """Verify the report lists exactly the differing pins and excludes unresolved.

    Exercises Property 6's contract over synthetic inputs (no manifest needed):
    a pin equal to upstream is omitted; a pin differing from a resolved upstream
    is reported (listing both identifiers); an unresolved-upstream pin is
    excluded. Returns a process exit code (0 on success).
    """
    ok = True

    def pin(name: str, identifier: str) -> FlakeInputPin:
        return FlakeInputPin(
            input_name=name,
            owner="hellodj",
            repo=name,
            branch="main",
            pinned_identifier=identifier,
        )

    pins = {
        "matches": pin("matches", "rev-a"),
        "differs": pin("differs", "rev-a"),
        "unresolved": pin("unresolved", "rev-a"),
    }
    upstream: dict[str, str | None] = {
        "matches": "rev-a",
        "differs": "rev-b",
        "unresolved": None,
    }

    report = stale_pins(pins, upstream)
    names = {entry.input_name for entry in report}

    if names != {"differs"}:
        print(
            "self-test FAILED: report must list exactly the differing pin, "
            f"got {sorted(names)}"
        )
        ok = False

    for entry in report:
        if entry.input_name == "differs" and (
            entry.pinned_identifier != "rev-a"
            or entry.upstream_identifier != "rev-b"
        ):
            print("self-test FAILED: stale entry did not list both identifiers")
            ok = False

    if ok:
        print(
            "self-test passed: report lists exactly the differing pin (both "
            "identifiers), excludes matching and unresolved."
        )
        return 0
    return 1


def _usage() -> str:
    """Return the CLI usage string."""
    return (
        "usage: report_stale_pins.py [INPUT ...] [--manifest PATH] "
        "[--upstream PATH] [--self-test]"
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
    """Entry point: report stale pins over the manifest, returning an exit code."""
    args = list(argv)

    self_test = "--self-test" in args
    if self_test:
        args = [a for a in args if a != "--self-test"]

    try:
        args, manifest_opt = _extract_option(args, "--manifest")
        args, upstream_opt = _extract_option(args, "--upstream")
    except PinManifestError as exc:
        print(f"stale-pin report FAILED: {exc}")
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
        print(f"stale-pin report FAILED: {exc}")
        return 2

    print(
        f"stale-pin report: comparing {len(only) if only else len(pins)} pinned "
        f"input(s) from {manifest.name} against current upstream in "
        f"{upstream_file.name} (R6.1)"
    )
    exit_code, _report = report_stale(pins, upstream, only)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

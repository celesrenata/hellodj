#!/usr/bin/env python3
"""Record a verified closure as available for stage deploy (task 16.2).

This is the executable the ``publish-cache`` job of the GitHub Actions Nix build
workflow (``.github/workflows/nix-build.yml``) invokes **after** it has signed a
closure, pushed it to the S3-backed ``Nix_Binary_Cache``, and confirmed it is
retrievable via a ``narinfo`` read-back. Recording the closure's Nix
**store-path hash** into ``closures.toml`` is what "marks the artifact available
for stage deployment" (R7.7): only closures recorded here are pulled by the
deploy path (:mod:`tools.resolve_closure`).

Build once, deploy thrice (R7.2/7.3)
------------------------------------

The recorded ``store_path``/``store_path_hash`` is the ONE build-once identity
for the artifact. Beta, Staging, and Production all resolve this same hash, so an
identical closure is reused across all three stages and never rebuilt. This tool
writes exactly one artifact's entry per call, in-place, so the manifest stays a
faithful, append/replace record of what has been verified-and-published.

Ordering contract (R7.7)
------------------------

The workflow calls this **strictly after** the push + read-back succeeds. If the
read-back fails the workflow exits before reaching this tool, so an unverified
closure is never recorded (never "marked available"). This tool performs no push
or verification itself — it only records the already-verified result.

Usage::

    python tools/record_closure.py --name web-ui \
        --store-path /nix/store/<hash>-web-ui-image.tar.gz
    python tools/record_closure.py --name gpu-ami \
        --store-path /nix/store/<hash>-nixos-amazon-image.vhd

Design references:
    * Components §7 — push + verify retrievable before available (R7.7);
      build-once/deploy-thrice by store-path-hash identity (R7.2/7.3).

Requirements: 7.2, 7.3, 7.7
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

PLATFORM_ROOT = Path(__file__).resolve().parent.parent

#: The declarative build-once closure manifest at the platform root.
DEFAULT_MANIFEST = PLATFORM_ROOT / "closures.toml"


class RecordError(Exception):
    """Raised on a malformed argument or manifest the tool cannot record into."""


def store_path_hash(store_path: str) -> str:
    """Extract the ``<hash>`` segment from a ``/nix/store/<hash>-<name>`` path.

    The store-path hash is the leading path component after ``/nix/store/`` up to
    the first ``-``; it is the build-once identity key (R7.2/7.3).

    Args:
        store_path: A full Nix store path.

    Returns:
        The store-path hash segment.

    Raises:
        RecordError: if ``store_path`` is not a ``/nix/store/<hash>-<name>`` path.
    """
    m = re.fullmatch(r"/nix/store/([a-z0-9]+)-(.+)", store_path)
    if not m:
        raise RecordError(
            f"{store_path!r} is not a /nix/store/<hash>-<name> path — cannot "
            "extract the build-once store-path hash"
        )
    return m.group(1)


def _render_entry(name: str, store_path: str, hash_: str) -> str:
    """Render a ``[closures.<name>]`` TOML block for the given closure."""
    return (
        f"[closures.{name}]\n"
        f'store_path = "{store_path}"\n'
        f'store_path_hash = "{hash_}"\n'
    )


def record(manifest: Path, name: str, store_path: str) -> str:
    """Record (insert or replace) one artifact's verified closure in the manifest.

    If a ``[closures.<name>]`` block already exists it is replaced in-place;
    otherwise the new block is appended. Only the ``store_path`` and
    ``store_path_hash`` keys are written for the entry (the schema the deploy
    tool reads).

    Args:
        manifest: Path to ``closures.toml``.
        name: The artifact/component name.
        store_path: The verified ``/nix/store/<hash>-<name>`` path.

    Returns:
        The extracted store-path hash that was recorded.

    Raises:
        RecordError: on a malformed store path or an unreadable manifest.
    """
    hash_ = store_path_hash(store_path)
    try:
        text = manifest.read_text(encoding="utf-8")
    except OSError as exc:
        raise RecordError(f"{manifest}: cannot read: {exc}") from exc

    block = _render_entry(name, store_path, hash_)

    # Match an existing [closures.<name>] block (header + its key lines up to the
    # next table header or EOF) and replace it; otherwise append.
    header = re.escape(f"[closures.{name}]")
    pattern = re.compile(
        rf"^\[closures\.{re.escape(name)}\][^\[]*",
        re.MULTILINE,
    )
    if re.search(rf"^{header}\s*$", text, re.MULTILINE):
        new_text = pattern.sub(block, text, count=1)
    else:
        sep = "" if text.endswith("\n") else "\n"
        new_text = f"{text}{sep}\n{block}"

    try:
        manifest.write_text(new_text, encoding="utf-8")
    except OSError as exc:
        raise RecordError(f"{manifest}: cannot write: {exc}") from exc

    return hash_


def _extract_option(args: list[str], flag: str) -> tuple[list[str], str | None]:
    """Pull ``--flag VALUE`` out of ``args``, returning the remainder and value."""
    if flag not in args:
        return args, None
    idx = args.index(flag)
    if idx + 1 >= len(args):
        raise RecordError(f"{flag} requires an argument")
    value = args[idx + 1]
    return args[:idx] + args[idx + 2 :], value


def _usage() -> str:
    """Return the CLI usage string."""
    return (
        "usage: record_closure.py --name NAME --store-path /nix/store/<hash>-<name> "
        "[--manifest PATH]"
    )


def main(argv: list[str]) -> int:
    """Entry point: record one verified closure, returning a process exit code."""
    args = list(argv)
    try:
        args, manifest_opt = _extract_option(args, "--manifest")
        args, name = _extract_option(args, "--name")
        args, store_path = _extract_option(args, "--store-path")
    except RecordError as exc:
        print(f"record-closure FAILED: {exc}")
        return 2

    if args or not name or not store_path:
        print(_usage())
        return 2

    manifest = Path(manifest_opt) if manifest_opt else DEFAULT_MANIFEST
    try:
        hash_ = record(manifest, name, store_path)
    except RecordError as exc:
        print(f"record-closure FAILED: {exc}")
        return 2

    print(
        f"record-closure: marked '{name}' available for stage deploy — "
        f"{store_path} (build-once hash {hash_}) recorded in {manifest.name} (R7.7)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

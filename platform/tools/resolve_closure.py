#!/usr/bin/env python3
"""Deploy-side closure resolution — build once, deploy thrice (task 16.2).

This is the executable the CDK deploy pipeline invokes (see
``getComponentBuildCommands`` / ``getBuildCommands`` in
``platform/infra/lib/pipeline-stack.ts``) to **pull a prebuilt artifact's
closure by its Nix store-path hash** from the S3-backed ``Nix_Binary_Cache`` and
confirm it is retrievable **before** the artifact is used for a stage deploy. It
does **no** build compute — the GitHub Actions (Nix) ``Build_Trigger`` already
compiled and published every closure — so no build compute is billed here
(R6.3/6.4). It is the thin wrapper around the pure, property-tested decision
function :func:`hellodj_platform_logic.binary_cache.resolve_closure`
(Property 5 / Property 6), so the deploy wiring and the shared decision logic
reason over one source of truth.

Build once, deploy thrice (R7.2/7.3)
------------------------------------

The closure manifest ``closures.toml`` (at the platform root) records, for every
component and the GPU AMI, the ONE ``store_path``/``store_path_hash`` the build
published. That hash is the **build-once identity**: Beta, Staging, and
Production **all** resolve this same hash, so an identical closure is pulled and
**reused** across all three stages and is **never rebuilt** for any stage. This
tool takes ``--stage`` only for logging/traceability — the resolved hash does
**not** depend on the stage, which is exactly what "deploy thrice" means.

What ``--verify`` does (R7.7 read-back)
---------------------------------------

With ``--verify`` the tool performs the cache retrievability read-back: it looks
up the artifact's store-path hash in the set of hashes currently present in the
cache (the ``narinfo`` read-back), and only a **present** hash is resolved for
reuse. The set of present hashes is obtained from a cache-contents provider:

* In production the provider shells out to ``nix path-info --store <cache-uri>``
  / a ``narinfo`` HEAD against the S3 cache (the same read-back the publish job
  used to confirm retrievability). The concrete cache URI comes from the
  ``[cache].uri`` in the manifest / the ``NIX_CACHE_S3_URI`` deploy env.
* In tests (and ``--self-test``) the provider is injected directly, so the pure
  reuse/halt decision can be exercised without any network/Nix dependency.

Missing-closure halt (R7.4)
---------------------------

If the required closure's hash is **absent** from the cache contents (or the
artifact is not recorded in the manifest at all), the deploy **halts** for that
stage, the tool surfaces the missing closure **by its store path**, exits
non-zero, and **never substitutes** an artifact from any non-cache source —
exactly the ``resolve_closure`` halt branch.

Usage::

    python tools/resolve_closure.py --component web-ui --verify
    python tools/resolve_closure.py --component web-ui --verify --stage staging
    python tools/resolve_closure.py --ami --verify
    python tools/resolve_closure.py --all --verify
    python tools/resolve_closure.py --self-test

Design references:
    * Components §7 — Nix binary cache backend: build-once/deploy-thrice by
      store-path-hash identity (R7.2/7.3), push + verify retrievable before
      available (R7.7), missing-closure halt (R7.4).
    * Correctness Property 5: build-once identity — every stage resolves the
      same store-path-hash and reuses it.
    * Correctness Property 6: a missing required closure halts the stage without
      substitution.

Requirements: 7.1, 7.2, 7.3, 7.7
"""

from __future__ import annotations

import subprocess  # noqa: S404 - used only for the read-back, never with shell=True
import sys
import tomllib
from collections.abc import Callable, Iterable
from pathlib import Path

PLATFORM_ROOT = Path(__file__).resolve().parent.parent
COMPONENTS_ROOT = PLATFORM_ROOT / "components"

# Make the shared pure-logic package importable without installation, mirroring
# the layout used by the other platform tools.
if str(COMPONENTS_ROOT) not in sys.path:
    sys.path.insert(0, str(COMPONENTS_ROOT))

from hellodj_platform_logic.binary_cache import resolve_closure  # noqa: E402
from hellodj_platform_logic.types import (  # noqa: E402
    ClosureRef,
    ClosureResolution,
)

#: The declarative build-once closure manifest at the platform root.
DEFAULT_MANIFEST = PLATFORM_ROOT / "closures.toml"

#: The manifest key for the single shared GPU AMI closure (R5.2 / 8.4).
AMI_KEY = "gpu-ami"

#: A provider that returns the set of store-path hashes currently retrievable
#: from the binary cache (the ``narinfo`` read-back set). Injected in tests.
CacheContentsProvider = Callable[[str], set[str]]


class ClosureManifestError(Exception):
    """Raised when the closure manifest is missing or malformed.

    This is an *operational* error (the deploy step could not be evaluated),
    distinct from a legitimate missing-closure halt (R7.4). It makes the runner
    exit non-zero so a broken manifest never silently passes.
    """


def _load_toml(path: Path) -> dict:
    """Parse a TOML file, raising :class:`ClosureManifestError` on any error."""
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except FileNotFoundError as exc:
        raise ClosureManifestError(f"{path}: file not found") from exc
    except (tomllib.TOMLDecodeError, OSError) as exc:
        raise ClosureManifestError(f"{path}: cannot parse: {exc}") from exc


def load_manifest(manifest: Path) -> tuple[str, dict[str, ClosureRef]]:
    """Load the closure manifest into ``(cache_uri, {name -> ClosureRef})``.

    Every ``[closures.<name>]`` entry must declare a non-empty ``store_path`` and
    ``store_path_hash`` (the build-once identity key). The ``[cache].uri`` is the
    S3-backed cache the read-back consults.

    Args:
        manifest: Path to ``closures.toml``.

    Returns:
        A ``(cache_uri, closures)`` tuple.

    Raises:
        ClosureManifestError: on a missing/empty ``[closures]`` table or a
            malformed entry.
    """
    data = _load_toml(manifest)

    cache = data.get("cache", {})
    if not isinstance(cache, dict):
        raise ClosureManifestError(f"{manifest}: [cache] must be a table")
    cache_uri = cache.get("uri", "")
    if not isinstance(cache_uri, str) or not cache_uri:
        raise ClosureManifestError(
            f"{manifest}: [cache].uri missing/empty — the deploy path must know "
            "which S3-backed Nix binary cache to read closures from (R7.1)"
        )

    raw = data.get("closures")
    if not isinstance(raw, dict) or not raw:
        raise ClosureManifestError(
            f"{manifest}: missing or empty [closures] table (need at least one "
            "artifact -> store_path/store_path_hash entry)"
        )

    closures: dict[str, ClosureRef] = {}
    for name, entry in raw.items():
        if not isinstance(entry, dict):
            raise ClosureManifestError(
                f"{manifest}: closure '{name}' must be a table with "
                "store_path/store_path_hash"
            )
        store_path = entry.get("store_path")
        store_path_hash = entry.get("store_path_hash")
        for field, value in (
            ("store_path", store_path),
            ("store_path_hash", store_path_hash),
        ):
            if not isinstance(value, str) or not value:
                raise ClosureManifestError(
                    f"{manifest}: closure '{name}' has missing/empty '{field}' — "
                    "every artifact must record its build-once store-path hash "
                    "(R7.2/7.3)"
                )
        closures[name] = ClosureRef(
            store_path=store_path,
            store_path_hash=store_path_hash,
        )

    return cache_uri, closures


def _probe_hash(cache_uri: str, ref: ClosureRef) -> bool:
    """Probe whether one closure is retrievable from the cache (narinfo read-back).

    Runs ``nix path-info --store <cache_uri> <store_path>``; a zero exit means
    the ``narinfo`` is present and the closure is retrievable. Any non-zero exit
    or missing ``nix`` binary is treated as not-retrievable (the stage will halt
    per R7.4 — never substituting a non-cache source).
    """
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [
                "nix",
                "path-info",
                "--store",
                cache_uri,
                ref.store_path,
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,  # the R7.6 cache-response budget
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


def resolve_one(
    name: str,
    closures: dict[str, ClosureRef],
    cache_contents: set[str],
) -> ClosureResolution:
    """Resolve a single artifact's closure by its build-once store-path hash.

    Delegates the reuse-vs-halt decision to the pure
    :func:`hellodj_platform_logic.binary_cache.resolve_closure` so the deploy
    tool and the property test share one decision. A name absent from the
    manifest is a missing closure and halts (R7.4) — surfaced by a synthetic
    store path naming the unknown artifact.

    Args:
        name: The artifact/component name (or :data:`AMI_KEY`).
        closures: The loaded manifest closures.
        cache_contents: The set of store-path hashes present/retrievable in the
            cache (the ``narinfo`` read-back set).

    Returns:
        The :class:`ClosureResolution` for the artifact.
    """
    ref = closures.get(name)
    if ref is None:
        # Not recorded as available -> treat as a missing closure that halts,
        # surfacing the artifact by name (no non-cache substitution, R7.4).
        missing = ClosureRef(
            store_path=f"/nix/store/<unrecorded>-{name}",
            store_path_hash=f"<unrecorded:{name}>",
        )
        return resolve_closure(missing, cache_contents)
    return resolve_closure(ref, cache_contents)


def _format(name: str, result: ClosureResolution) -> str:
    """Render a human-readable resolve/verify line for one artifact."""
    if result.halt:
        return (
            f"  HALT {name}: required closure "
            f"{result.requested.store_path!r} not retrievable from cache — "
            f"{result.reason}"
        )
    return (
        f"  REUSE {name}: closure {result.requested.store_path_hash} present; "
        f"reusing {result.requested.store_path!r} (no rebuild — build-once)"
    )


def resolve_targets(
    names: Iterable[str],
    manifest: Path,
    *,
    verify: bool,
    stage: str | None,
    contents_provider: CacheContentsProvider | None = None,
) -> tuple[int, list[ClosureResolution]]:
    """Resolve each named artifact's closure by store-path hash, optionally verifying.

    Every named artifact resolves the SAME build-once store-path hash regardless
    of ``stage`` (build once, deploy thrice — R7.2/7.3). With ``verify`` the tool
    consults the cache-contents provider (the ``narinfo`` read-back) so only a
    retrievable closure is reused; without ``verify`` the manifest presence is
    taken as the recorded availability (the publish job already verified it).

    Args:
        names: The artifact/component names to resolve.
        manifest: Path to ``closures.toml``.
        verify: Whether to perform the cache retrievability read-back (R7.7).
        stage: The deploy stage (logging/traceability only; does not change the
            resolved hash — that is the whole point of build-once/deploy-thrice).
        contents_provider: Test/override provider returning the present-hash set
            for a cache URI. When ``None`` the production Nix read-back is used.

    Returns:
        A ``(exit_code, results)`` tuple: ``0`` when every closure is present and
        reused, ``1`` when any closure halts (missing/not retrievable), ``2`` on
        an operational manifest error.
    """
    cache_uri, closures = load_manifest(manifest)

    targets = list(names)
    stage_note = f" for stage {stage}" if stage else ""
    print(
        f"resolve-closure{stage_note}: resolving {len(targets)} artifact(s) by "
        f"store-path hash from {cache_uri} (build-once/deploy-thrice — R7.2/7.3)"
    )

    # Build the present-hash set (the narinfo read-back). Without --verify we
    # trust the manifest record (the publish job confirmed retrievability at
    # push time, R7.7) and take every recorded closure as present.
    if verify:
        if contents_provider is not None:
            cache_contents = contents_provider(cache_uri)
        else:
            cache_contents = {
                closures[name].store_path_hash
                for name in targets
                if name in closures and _probe_hash(cache_uri, closures[name])
            }
    else:
        cache_contents = {ref.store_path_hash for ref in closures.values()}

    results: list[ClosureResolution] = []
    halted = 0
    for name in targets:
        result = resolve_one(name, closures, cache_contents)
        results.append(result)
        print(_format(name, result))
        if result.halt:
            halted += 1

    if halted:
        print(
            f"resolve-closure FAILED{stage_note}: {halted} required closure(s) "
            "not retrievable from the Nix binary cache — stage halted, no "
            "non-cache substitution (R7.4)"
        )
        return 1, results

    print(
        f"resolve-closure passed{stage_note}: {len(results)} closure(s) present "
        "and reused across all stages (no rebuild — R7.2/7.3)."
    )
    return 0, results


def _run_self_test() -> int:
    """Verify reuse-when-present and halt-when-absent over synthetic closures.

    Exercises the two R7 deploy outcomes without any Nix/network dependency:
    a present hash is reused (no rebuild) and an absent hash halts the stage,
    surfacing the missing store path and never substituting. Returns a process
    exit code (0 on success).
    """
    ok = True
    ref = ClosureRef(store_path="/nix/store/abc-web-ui.tar.gz", store_path_hash="abc")
    closures = {"web-ui": ref}

    present = resolve_one("web-ui", closures, {"abc"})
    if present.halt or not present.present_in_cache:
        print("self-test FAILED: present closure was not reused")
        ok = False

    absent = resolve_one("web-ui", closures, set())
    if not absent.halt or absent.present_in_cache:
        print("self-test FAILED: absent closure did not halt the stage")
        ok = False
    if "web-ui" not in absent.requested.store_path:
        print("self-test FAILED: halt did not surface the missing store path")
        ok = False

    unrecorded = resolve_one("does-not-exist", closures, {"abc"})
    if not unrecorded.halt or "does-not-exist" not in unrecorded.requested.store_path:
        print("self-test FAILED: unrecorded artifact did not halt / not named")
        ok = False

    if ok:
        print("self-test passed: present reused, absent halted (store path named).")
        return 0
    return 1


def _usage() -> str:
    """Return the CLI usage string."""
    return (
        "usage: resolve_closure.py (--component NAME | --ami | --all) "
        "[--verify] [--stage STAGE] [--manifest PATH] [--self-test]"
    )


def _extract_option(args: list[str], flag: str) -> tuple[list[str], str | None]:
    """Pull ``--flag VALUE`` out of ``args``, returning the remainder and value."""
    if flag not in args:
        return args, None
    idx = args.index(flag)
    if idx + 1 >= len(args):
        raise ClosureManifestError(f"{flag} requires an argument")
    value = args[idx + 1]
    return args[:idx] + args[idx + 2 :], value


def main(argv: list[str]) -> int:
    """Entry point: resolve the requested closure(s), returning a process exit code."""
    args = list(argv)

    self_test = "--self-test" in args
    args = [a for a in args if a != "--self-test"]
    verify = "--verify" in args
    args = [a for a in args if a != "--verify"]
    want_ami = "--ami" in args
    args = [a for a in args if a != "--ami"]
    want_all = "--all" in args
    args = [a for a in args if a != "--all"]

    try:
        args, manifest_opt = _extract_option(args, "--manifest")
        args, component_opt = _extract_option(args, "--component")
        args, stage_opt = _extract_option(args, "--stage")
    except ClosureManifestError as exc:
        print(f"resolve-closure FAILED: {exc}")
        return 2

    if self_test:
        rc = _run_self_test()
        if rc != 0:
            return rc

    if any(a.startswith("-") for a in args) or args:
        print(_usage())
        return 2

    manifest = Path(manifest_opt) if manifest_opt else DEFAULT_MANIFEST

    try:
        _cache_uri, closures = load_manifest(manifest)
    except ClosureManifestError as exc:
        print(f"resolve-closure FAILED: {exc}")
        return 2

    if want_all:
        names: list[str] = list(closures)
    elif want_ami:
        names = [AMI_KEY]
    elif component_opt:
        names = [component_opt]
    elif self_test:
        # --self-test alone (no target) is a valid smoke-only invocation.
        return 0
    else:
        print(_usage())
        return 2

    try:
        exit_code, _results = resolve_targets(
            names, manifest, verify=verify, stage=stage_opt
        )
    except ClosureManifestError as exc:
        print(f"resolve-closure FAILED: {exc}")
        return 2
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

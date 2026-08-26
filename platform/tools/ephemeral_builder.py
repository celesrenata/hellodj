#!/usr/bin/env python3
"""Fallback ephemeral-builder safety + cache-unreachable rebuild wiring (task 16.3).

This is the executable the GitHub Actions Nix build workflow
(``.github/workflows/nix-build.yml``) invokes when it must fall back to an
*on-demand ephemeral builder* — the rejected-as-primary ``Build_Trigger`` kept
ONLY as the fallback for large aarch64 builds that exceed the GitHub-hosted
runner limits (design §6). The primary trigger is GitHub Actions with Nix, which
bills only while a job runs; the ephemeral builder therefore carries the
strongest cost guarantees in the spec (R6) and this tool enforces them so the
fallback can never bill silently while idle.

It does **no** provisioning or teardown side effects itself. It is the thin
wrapper around two pure, property-tested decision functions so the build wiring
and the shared decision logic reason over one source of truth:

* :func:`hellodj_platform_logic.ephemeral_build.ephemeral_teardown`
  (Property 4 / R6.6-R6.9) — given an ephemeral builder's lifecycle facts,
  whether its stop was confirmed, and the teardown timestamp, decide the
  teardown record: honour the <=300 s teardown deadline and the <=10800 s (3 h)
  hard maximum-lifetime forced-termination cap, emit an alert naming the resource
  exactly when the stop is NOT confirmed (R6.8), and retain the resource
  identifier + teardown timestamp when the stop IS confirmed (R6.9).

* :func:`hellodj_platform_logic.binary_cache.cache_fetch_policy`
  (Property 7 / R7.6) — given whether the cache responded within its 30 s budget
  and how many consecutive retries were attempted, decide whether a local
  rebuild is permitted (and recorded) because the cache was unreachable.

Explicit rebuild (R7.5)
-----------------------

The workflow exposes a ``workflow_dispatch`` input ``explicit_rebuild`` (already
wired in ``nix-build.yml``). When set, the build path is PERMITTED to rebuild the
artifact and re-push the resulting closure to the ``Nix_Binary_Cache`` even when
an identical closure is already present. This tool's :func:`rebuild_decision`
folds the explicit-rebuild request together with the cache-unreachability policy
into a single "should we rebuild locally + re-push?" decision the workflow acts
on: a rebuild is permitted when EITHER an explicit rebuild was requested (R7.5)
OR the cache was unreachable (R7.6).

Usage::

    # Decide + record the fallback ephemeral builder's teardown (R6.6-6.9).
    python tools/ephemeral_builder.py teardown \
        --resource-id i-0abc123 --stopped-confirmed true \
        --timestamp 2026-01-02T03:04:05Z

    # Unconfirmed stop -> non-zero exit + an alert naming the resource (R6.8).
    python tools/ephemeral_builder.py teardown \
        --resource-id i-0abc123 --stopped-confirmed false \
        --timestamp 2026-01-02T03:04:05Z

    # Decide whether to rebuild locally + re-push (R7.5 explicit / R7.6 cache).
    python tools/ephemeral_builder.py rebuild-decision \
        --cache-responded false --retries 3
    python tools/ephemeral_builder.py rebuild-decision --explicit-rebuild true

    python tools/ephemeral_builder.py --self-test

Design references:
    * Components §6 — "Where builds run without a paid server (R6)":
      ephemeral-compute safety (torn down within 300 s; hard max lifetime
      10800 s; alert on unconfirmed stop; record id + timestamp), the on-demand
      ephemeral builder "kept as the fallback for large aarch64 builds".
    * Components §7 — explicit rebuild (R7.5) and cache-unreachable fallback
      (R7.6).
    * Correctness Property 4 (ephemeral teardown) and Property 7 (cache
      unreachability permits a recorded local rebuild).

Requirements: 6.6, 6.7, 6.8, 6.9, 7.5, 7.6
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

PLATFORM_ROOT = Path(__file__).resolve().parent.parent
COMPONENTS_ROOT = PLATFORM_ROOT / "components"

# Make the shared pure-logic package importable without installation, mirroring
# the layout used by the other platform tools (resolve_closure.py etc.).
if str(COMPONENTS_ROOT) not in sys.path:
    sys.path.insert(0, str(COMPONENTS_ROOT))

from hellodj_platform_logic.binary_cache import (  # noqa: E402
    CACHE_RETRY_LIMIT,
    cache_fetch_policy,
)
from hellodj_platform_logic.ephemeral_build import (  # noqa: E402
    MAX_LIFETIME_CAP_SECONDS,
    TEARDOWN_DEADLINE_CAP_SECONDS,
    ephemeral_teardown,
)
from hellodj_platform_logic.types import (  # noqa: E402
    CacheFetchOutcome,
    EphemeralCompute,
    TeardownResult,
)


class EphemeralBuilderError(Exception):
    """Raised on a malformed argument the tool cannot act on (operational error)."""


@dataclass(frozen=True)
class RebuildDecision:
    """Whether the build path should rebuild locally + re-push, and why.

    Folds the explicit-rebuild request (R7.5) together with the cache-fetch
    policy outcome (R7.6): a rebuild is permitted when EITHER an explicit rebuild
    was requested OR the cache was unreachable.
    """

    rebuild: bool
    explicit: bool                 # explicit rebuild requested (R7.5)
    cache_outcome: CacheFetchOutcome
    reason: str = ""


# ---------------------------------------------------------------------------
# Decisions (thin wrappers over the pure, property-tested functions)
# ---------------------------------------------------------------------------


def teardown_decision(
    resource_id: str,
    stopped_confirmed: bool,
    timestamp: str,
    *,
    teardown_deadline_seconds: float = TEARDOWN_DEADLINE_CAP_SECONDS,
    max_lifetime_seconds: float = MAX_LIFETIME_CAP_SECONDS,
) -> TeardownResult:
    """Decide + record the fallback ephemeral builder's teardown (R6.6-R6.9).

    Constructs the :class:`EphemeralCompute` lifecycle facts (bounded by the
    <=300 s teardown deadline and <=10800 s hard max-lifetime cap) and delegates
    to the pure :func:`ephemeral_teardown` so the CLI and the Property 4 test
    share one decision.

    Args:
        resource_id: The ephemeral builder's cloud resource id (e.g. an EC2
            instance id) so an alert / record can name it.
        stopped_confirmed: Whether teardown confirmed the compute has stopped.
        timestamp: The teardown timestamp to record (retained on both the
            confirmed and the alert paths).
        teardown_deadline_seconds: The teardown-deadline bound to assert against
            the 300 s cap (R6.6). Defaults to the cap.
        max_lifetime_seconds: The hard max-lifetime bound to assert against the
            10800 s cap (R6.7). Defaults to the cap.

    Returns:
        The :class:`TeardownResult` (resource id, confirmed flag, timestamp, and
        ``alert_emitted`` set exactly when the stop was not confirmed).

    Raises:
        ValueError: if a bound exceeds its design cap (R6.6/R6.7) — surfaced by
            the pure function.
    """
    compute = EphemeralCompute(
        resource_id=resource_id,
        teardown_deadline_seconds=teardown_deadline_seconds,
        max_lifetime_seconds=max_lifetime_seconds,
    )
    return ephemeral_teardown(compute, stopped_confirmed, timestamp)


def rebuild_decision(
    *,
    explicit_rebuild: bool,
    cache_responded: bool,
    retries: int,
) -> RebuildDecision:
    """Decide whether to rebuild locally + re-push (R7.5 explicit / R7.6 cache).

    A local rebuild is permitted when EITHER:

    * an explicit rebuild was requested (R7.5) — the build path is permitted to
      rebuild the artifact and re-push the resulting closure to the cache even
      when an identical closure is already present; OR
    * the cache was unreachable per :func:`cache_fetch_policy` (R7.6) — it did
      not respond within the 30 s budget or the 3 consecutive retries were
      exhausted, in which case the rebuild is recorded as caused by cache
      unreachability.

    Args:
        explicit_rebuild: Whether an explicit rebuild + re-push was requested
            (the ``explicit_rebuild`` ``workflow_dispatch`` input).
        cache_responded: Whether the cache responded within its 30 s budget.
        retries: The number of consecutive retry attempts made against the
            cache (reaching :data:`CACHE_RETRY_LIMIT` exhausts them).

    Returns:
        A :class:`RebuildDecision` recording the fold of the explicit-rebuild
        request and the cache-fetch outcome.
    """
    outcome = cache_fetch_policy(cache_responded, retries)
    rebuild = explicit_rebuild or outcome.rebuilt_locally

    if explicit_rebuild and outcome.rebuilt_locally:
        reason = (
            "explicit rebuild requested (R7.5) and cache unreachable (R7.6); "
            f"rebuild + re-push permitted — {outcome.reason}"
        )
    elif explicit_rebuild:
        reason = (
            "explicit rebuild requested; rebuild + re-push permitted even though "
            "an identical closure may be present (R7.5)"
        )
    elif outcome.rebuilt_locally:
        reason = f"cache unreachable; local rebuild permitted (R7.6) — {outcome.reason}"
    else:
        reason = (
            "no explicit rebuild and cache reachable within budget; "
            "no rebuild forced (reuse cached closure — R7.2/7.3)"
        )

    return RebuildDecision(
        rebuild=rebuild,
        explicit=explicit_rebuild,
        cache_outcome=outcome,
        reason=reason,
    )


# ---------------------------------------------------------------------------
# CLI helpers
# ---------------------------------------------------------------------------


def _parse_bool(value: str, flag: str) -> bool:
    """Parse a CLI boolean (``true``/``false``/``1``/``0``/``yes``/``no``)."""
    lowered = value.strip().lower()
    if lowered in ("true", "1", "yes", "y"):
        return True
    if lowered in ("false", "0", "no", "n"):
        return False
    raise EphemeralBuilderError(f"{flag} expects a boolean, got {value!r}")


def _extract_option(args: list[str], flag: str) -> tuple[list[str], str | None]:
    """Pull ``--flag VALUE`` out of ``args``, returning the remainder and value."""
    if flag not in args:
        return args, None
    idx = args.index(flag)
    if idx + 1 >= len(args):
        raise EphemeralBuilderError(f"{flag} requires an argument")
    value = args[idx + 1]
    return args[:idx] + args[idx + 2 :], value


def _usage() -> str:
    """Return the CLI usage string."""
    return (
        "usage:\n"
        "  ephemeral_builder.py teardown --resource-id ID "
        "--stopped-confirmed BOOL --timestamp TS "
        "[--teardown-deadline-seconds S] [--max-lifetime-seconds S]\n"
        "  ephemeral_builder.py rebuild-decision "
        "[--explicit-rebuild BOOL] [--cache-responded BOOL] [--retries N]\n"
        "  ephemeral_builder.py --self-test"
    )


def _cmd_teardown(args: list[str]) -> int:
    """Handle the ``teardown`` subcommand."""
    args, resource_id = _extract_option(args, "--resource-id")
    args, stopped_opt = _extract_option(args, "--stopped-confirmed")
    args, timestamp = _extract_option(args, "--timestamp")
    args, deadline_opt = _extract_option(args, "--teardown-deadline-seconds")
    args, lifetime_opt = _extract_option(args, "--max-lifetime-seconds")

    if args or not resource_id or stopped_opt is None or not timestamp:
        print(_usage())
        return 2

    stopped_confirmed = _parse_bool(stopped_opt, "--stopped-confirmed")
    deadline = (
        float(deadline_opt) if deadline_opt is not None else TEARDOWN_DEADLINE_CAP_SECONDS
    )
    lifetime = (
        float(lifetime_opt) if lifetime_opt is not None else MAX_LIFETIME_CAP_SECONDS
    )

    try:
        result = teardown_decision(
            resource_id,
            stopped_confirmed,
            timestamp,
            teardown_deadline_seconds=deadline,
            max_lifetime_seconds=lifetime,
        )
    except ValueError as exc:
        # A bound exceeding the design cap (R6.6/R6.7) is an operational error.
        print(f"ephemeral-builder teardown FAILED: {exc}")
        return 2

    if result.alert_emitted:
        # Stop NOT confirmed (R6.8): surface an alert naming the still-running
        # compute so the ongoing cost is visible for manual intervention, and
        # exit non-zero so the workflow step FAILS (does not silently pass).
        print(
            f"::error::ALERT ephemeral build compute {result.resource_id} stop "
            f"NOT confirmed at {result.teardown_timestamp} — it may still be "
            "running and billing; manual intervention required (R6.8)"
        )
        return 1

    # Stop confirmed (R6.9): record the resource id + teardown timestamp.
    print(
        f"ephemeral-builder teardown: {result.resource_id} confirmed stopped at "
        f"{result.teardown_timestamp} — no build compute remains running; "
        f"torn down within {TEARDOWN_DEADLINE_CAP_SECONDS:.0f}s deadline / "
        f"{MAX_LIFETIME_CAP_SECONDS:.0f}s hard cap (R6.6/6.7/6.9)"
    )
    return 0


def _cmd_rebuild_decision(args: list[str]) -> int:
    """Handle the ``rebuild-decision`` subcommand."""
    args, explicit_opt = _extract_option(args, "--explicit-rebuild")
    args, responded_opt = _extract_option(args, "--cache-responded")
    args, retries_opt = _extract_option(args, "--retries")

    if args:
        print(_usage())
        return 2

    explicit = _parse_bool(explicit_opt, "--explicit-rebuild") if explicit_opt else False
    # Default: cache responded within budget with no retries exhausted, so the
    # decision reduces to the explicit-rebuild request alone.
    responded = _parse_bool(responded_opt, "--cache-responded") if responded_opt else True
    retries = int(retries_opt) if retries_opt is not None else 0

    decision = rebuild_decision(
        explicit_rebuild=explicit,
        cache_responded=responded,
        retries=retries,
    )

    verb = "REBUILD" if decision.rebuild else "REUSE"
    print(f"ephemeral-builder rebuild-decision: {verb} — {decision.reason}")
    # Emit the machine-readable decision for the workflow to act on (drives the
    # `--rebuild` / re-push flags in the publish job).
    print(f"rebuild={'true' if decision.rebuild else 'false'}")
    return 0


def _run_self_test() -> int:
    """Verify the teardown + rebuild decisions without any cloud dependency.

    Exercises: confirmed stop records id+timestamp with no alert (R6.9);
    unconfirmed stop emits an alert naming the resource (R6.8); cache-unreachable
    (timeout or exhausted retries) permits a recorded rebuild (R7.6); explicit
    rebuild permits a rebuild even with a healthy cache (R7.5); and a healthy
    cache with no explicit request forces no rebuild.
    """
    ok = True

    confirmed = teardown_decision("i-confirmed", True, "2026-01-01T00:00:00Z")
    if confirmed.alert_emitted or not confirmed.confirmed_stopped:
        print("self-test FAILED: confirmed stop should not alert")
        ok = False
    if confirmed.teardown_timestamp != "2026-01-01T00:00:00Z":
        print("self-test FAILED: confirmed stop must retain the timestamp (R6.9)")
        ok = False

    unconfirmed = teardown_decision("i-runaway", False, "2026-01-01T00:00:00Z")
    if not unconfirmed.alert_emitted:
        print("self-test FAILED: unconfirmed stop must emit an alert (R6.8)")
        ok = False
    if unconfirmed.resource_id != "i-runaway":
        print("self-test FAILED: alert must name the resource (R6.8)")
        ok = False

    # A bound over the cap is rejected (R6.6/R6.7).
    try:
        teardown_decision(
            "i-bad", True, "t", teardown_deadline_seconds=TEARDOWN_DEADLINE_CAP_SECONDS + 1
        )
    except ValueError:
        pass
    else:
        print("self-test FAILED: over-cap teardown deadline must be rejected (R6.6)")
        ok = False

    timeout = rebuild_decision(explicit_rebuild=False, cache_responded=False, retries=0)
    if not (timeout.rebuild and timeout.cache_outcome.rebuilt_locally):
        print("self-test FAILED: cache timeout must permit a recorded rebuild (R7.6)")
        ok = False

    exhausted = rebuild_decision(
        explicit_rebuild=False, cache_responded=True, retries=CACHE_RETRY_LIMIT
    )
    if not exhausted.rebuild:
        print("self-test FAILED: exhausted retries must permit a rebuild (R7.6)")
        ok = False

    explicit = rebuild_decision(explicit_rebuild=True, cache_responded=True, retries=0)
    if not explicit.rebuild:
        print("self-test FAILED: explicit rebuild must permit a rebuild (R7.5)")
        ok = False

    healthy = rebuild_decision(explicit_rebuild=False, cache_responded=True, retries=0)
    if healthy.rebuild:
        print("self-test FAILED: healthy cache + no explicit must not rebuild (R7.2/7.3)")
        ok = False

    if ok:
        print(
            "self-test passed: teardown alert-iff-unconfirmed + id/timestamp "
            "retained; rebuild permitted on explicit/cache-unreachable only."
        )
        return 0
    return 1


def main(argv: list[str]) -> int:
    """Entry point: dispatch the subcommand, returning a process exit code."""
    args = list(argv)

    self_test = "--self-test" in args
    args = [a for a in args if a != "--self-test"]
    if self_test:
        rc = _run_self_test()
        if rc != 0:
            return rc
        if not args:
            return 0

    if not args:
        print(_usage())
        return 2

    command, rest = args[0], args[1:]
    try:
        if command == "teardown":
            return _cmd_teardown(rest)
        if command == "rebuild-decision":
            return _cmd_rebuild_decision(rest)
    except EphemeralBuilderError as exc:
        print(f"ephemeral-builder FAILED: {exc}")
        return 2

    print(_usage())
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

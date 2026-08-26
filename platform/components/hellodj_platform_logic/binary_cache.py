"""Binary-cache decision logic for the build-once/deploy-thrice pipeline.

This module holds the pure decision functions that govern how the Nix binary
cache is consulted, so an artifact is built once and every deployment stage
(Beta, Staging, Production) reuses the *same* closure identified by its Nix
store path hash (R7). It is free of any Nix, S3 or network dependency: the
cache contents and reachability facts are supplied as plain inputs, so both the
pipeline wiring and the property tests import a single source of truth and can
exercise the decisions directly.

Implemented here:

* :func:`resolve_closure` -- Property 5 / Property 6 / R7.2, R7.3, R7.4. Given a
  required closure and the set of store-path hashes currently present in the
  cache, decide whether the closure is reused from the cache (present -- no
  rebuild) or the stage halts without substitution (absent). The match is made
  purely on the closure's store-path hash, which is the build-once identity key
  shared across all three stages, so an identical closure is never rebuilt for
  any stage.

* :func:`cache_fetch_policy` -- Property 7 / R7.6. Given whether the cache
  responded within its budget and how many consecutive retries have been
  attempted, decide whether a local rebuild is permitted (and recorded) because
  the cache was unreachable. A local rebuild is permitted *only* when the cache
  did not respond within the 30 s budget or the 3 consecutive retry attempts
  were exhausted; a healthy cache never forces a rebuild.

Design references:
    * Architecture -- "Store-path-hash identity is the build-once/deploy-thrice
      mechanism" (R7.2/7.3) and "A missing closure halts a stage" (R7.4).
    * Components section 7 -- Nix binary cache backend: missing-closure halt
      (R7.4) and cache-unreachable fallback (R7.6).
    * Correctness Property 5: Build-once identity -- every stage resolves the
      same store-path-hash and reuses it.
    * Correctness Property 6: A missing required closure halts the stage without
      substitution.
    * Correctness Property 7: Cache unreachability permits a recorded local
      rebuild.

Requirements: 7.2, 7.3, 7.4, 7.6
"""

from __future__ import annotations

from hellodj_platform_logic.types import (
    CacheFetchOutcome,
    ClosureRef,
    ClosureResolution,
)

__all__ = [
    "CACHE_RETRY_LIMIT",
    "cache_fetch_policy",
    "resolve_closure",
]

#: The number of consecutive retry attempts that, once exhausted, permits a
#: cache-unreachable local rebuild (R7.6). When ``retries`` reaches this value a
#: rebuild is permitted even if the last attempt is reported as responded.
CACHE_RETRY_LIMIT = 3


def resolve_closure(
    ref: ClosureRef,
    cache_contents: set[str],
) -> ClosureResolution:
    """Resolve a required closure from the binary cache by store-path hash.

    Implements Property 5 / Property 6 / R7.2, R7.3, R7.4. The decision keys
    purely on ``ref.store_path_hash`` -- the build-once identity that every
    stage shares:

    * **Present (reuse, no rebuild).** If the closure's store-path hash is in
      ``cache_contents``, the closure is reused from the cache and the stage
      proceeds; ``present_in_cache`` is ``True`` and ``halt`` is ``False``.
      Because all three stages resolve the same hash, an identical closure is
      never rebuilt for any stage (R7.2/7.3).
    * **Absent (halt, no substitution).** If the store-path hash is not present,
      the stage halts and the missing closure is surfaced *by its store path*;
      ``halt`` is ``True`` and no artifact is substituted from any non-cache
      source (R7.4).

    Args:
        ref: The required closure, identified by its full store path and the
            store-path hash segment used as the cache-lookup key.
        cache_contents: The set of store-path hashes currently present in the
            binary cache. Membership is tested against ``ref.store_path_hash``.

    Returns:
        A :class:`~hellodj_platform_logic.types.ClosureResolution` recording the
        requested closure, whether it was present, whether the stage halts, and
        (on halt) a reason naming the missing store path.

    Requirements: 7.2, 7.3, 7.4
    """
    present = ref.store_path_hash in cache_contents

    if present:
        return ClosureResolution(
            requested=ref,
            present_in_cache=True,
            halt=False,
            reason=(
                f"closure {ref.store_path_hash!r} present in cache; "
                f"reusing {ref.store_path!r} (no rebuild)"
            ),
        )

    return ClosureResolution(
        requested=ref,
        present_in_cache=False,
        halt=True,
        reason=(
            f"required closure {ref.store_path!r} absent from cache; "
            "stage halted, no substitution from any non-cache source"
        ),
    )


def cache_fetch_policy(responded: bool, retries: int) -> CacheFetchOutcome:
    """Decide whether cache unreachability permits a recorded local rebuild.

    Implements Property 7 / R7.6. A local rebuild is permitted -- and recorded
    as caused by cache unreachability -- *only* when the cache did not respond
    within its 30 s budget or the :data:`CACHE_RETRY_LIMIT` consecutive retry
    attempts were exhausted. When the cache responded within budget and the
    retries were not exhausted, no rebuild is forced.

    Args:
        responded: Whether the cache responded within the 30 s budget.
        retries: The number of consecutive retry attempts made against the
            cache. Reaching :data:`CACHE_RETRY_LIMIT` exhausts the retries and
            permits a rebuild.

    Returns:
        A :class:`~hellodj_platform_logic.types.CacheFetchOutcome` recording
        whether the cache responded within budget, whether the retries were
        exhausted, whether a local rebuild is permitted, and a reason describing
        the outcome.

    Requirements: 7.6
    """
    retries_exhausted = retries >= CACHE_RETRY_LIMIT
    rebuilt_locally = (not responded) or retries_exhausted

    if rebuilt_locally:
        reason = (
            "cache unreachable ("
            + (
                "did not respond within budget"
                if not responded
                else f"{CACHE_RETRY_LIMIT} consecutive retries exhausted"
            )
            + "); local rebuild permitted and recorded"
        )
    else:
        reason = "cache reachable within budget; no local rebuild forced"

    return CacheFetchOutcome(
        responded_within_timeout=responded,
        retries_exhausted=retries_exhausted,
        rebuilt_locally=rebuilt_locally,
        reason=reason,
    )

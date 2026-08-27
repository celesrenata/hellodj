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

* :func:`tiered_cache_lookup` -- Property 3 / Property 4 / R4.2, R4.3, R4.4,
  R4.5, R4.9 (``hellodj-private-source-and-toolchain``). The local analogue of
  :func:`resolve_closure`: given whether a required closure is present in the
  builder's local Nix cache tier, whether that local copy passes store-path
  integrity verification, and whether the closure is present in the S3 binary
  cache, decide whether it is reused locally (``LOCAL_HIT`` -- no rebuild, no S3
  fetch), fetched from S3 and repopulated locally (``S3_HIT``), or built and
  pushed to both tiers (``BUILD``). A local copy that fails integrity
  verification is treated as absent (integrity fallthrough), so it never yields
  a ``LOCAL_HIT``. The local tier sits *in front of* the S3 cache and never
  replaces it as the shared build-once source.

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
    CacheTier,
    CacheTierResolution,
    ClosureRef,
    ClosureResolution,
)

__all__ = [
    "CACHE_RETRY_LIMIT",
    "cache_fetch_policy",
    "resolve_closure",
    "tiered_cache_lookup",
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


def tiered_cache_lookup(
    local_present: bool,
    local_integrity_ok: bool,
    s3_present: bool,
) -> CacheTierResolution:
    """Resolve a closure through the local-in-front-of-S3 cache tiers.

    Implements Property 3 / Property 4 / R4.2, R4.3, R4.4, R4.5, R4.9. This is
    the local analogue of :func:`resolve_closure`: it composes the per-builder
    ``Local_Nix_Cache`` tier *in front of* the existing S3 binary cache and
    decides, purely from the three presence/integrity facts, which tier serves
    the closure:

    * **LOCAL_HIT** -- ``local_present`` and ``local_integrity_ok``. The local
      closure is reused; it is neither rebuilt nor fetched from S3
      (``populated_local`` and ``pushed_s3`` are both ``False``) (R4.2/R4.4).
    * **S3_HIT** -- not usable locally (either absent, or present but failing
      integrity) yet present in S3. The closure is fetched from S3 and the local
      tier is repopulated (``populated_local`` is ``True``); S3 is not written
      (R4.3/R4.5).
    * **BUILD** -- usable at neither tier. The closure is built, the local tier
      is populated, and the closure is pushed to S3 consistent with the existing
      binary-cache publish path (``populated_local`` and ``pushed_s3`` are both
      ``True``) (R4.5 + R4.9).

    The **integrity fallthrough** (R4.5) is expressed by only trusting the local
    tier when ``local_present and local_integrity_ok``: a corrupt local closure
    (present but failing integrity) is treated exactly as absent, so it never
    yields a LOCAL_HIT and always falls through to the S3 tier (or a build).

    Args:
        local_present: Whether the closure's ``Store_Path_Hash`` is present in
            the builder's local Nix cache tier.
        local_integrity_ok: Whether the locally-present closure passes Nix
            store-path integrity verification. Only consulted when
            ``local_present`` is ``True``.
        s3_present: Whether the closure's ``Store_Path_Hash`` is present in the
            S3 binary cache.

    Returns:
        A :class:`~hellodj_platform_logic.types.CacheTierResolution` recording
        the resolving tier, whether the local tier was (re)populated, whether the
        closure was pushed to S3, and a human-readable reason.

    Requirements: 4.2, 4.3, 4.4, 4.5, 4.9
    """
    # A local closure is only usable when it is present AND passes integrity
    # verification. A present-but-corrupt closure is treated as absent, which is
    # the integrity fallthrough (R4.5).
    local_usable = local_present and local_integrity_ok

    if local_usable:
        return CacheTierResolution(
            tier=CacheTier.LOCAL_HIT,
            populated_local=False,
            pushed_s3=False,
            reason=(
                "closure present and integrity-valid in the local Nix cache; "
                "reused locally (no rebuild, no S3 fetch)"
            ),
        )

    # Not usable locally: either absent, or present but failed integrity. In the
    # corrupt-local case, note the fallthrough in the reason (R4.5).
    fell_through = local_present and not local_integrity_ok

    if s3_present:
        reason = (
            "local closure failed integrity verification (treated as absent); "
            "fetched from S3 and repopulated the local tier"
            if fell_through
            else "closure absent from the local tier but present in S3; "
            "fetched from S3 and populated the local tier"
        )
        return CacheTierResolution(
            tier=CacheTier.S3_HIT,
            populated_local=True,
            pushed_s3=False,
            reason=reason,
        )

    reason = (
        "local closure failed integrity verification (treated as absent) and "
        "closure absent from S3; built, populated the local tier, and pushed to S3"
        if fell_through
        else "closure absent from both the local tier and S3; built, populated "
        "the local tier, and pushed to S3"
    )
    return CacheTierResolution(
        tier=CacheTier.BUILD,
        populated_local=True,
        pushed_s3=True,
        reason=reason,
    )

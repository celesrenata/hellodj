"""Ephemeral build-compute teardown decision for the no-paid-build-server path.

This module holds the pure decision function that governs the lifecycle of the
*fallback* ephemeral build compute (the on-demand builder used only for large
aarch64 builds that exceed the hosted-runner limits). The primary
``Build_Trigger`` is GitHub Actions with Nix, which is billed only while a job
runs; the ephemeral builder is the safety fallback and therefore carries the
strongest cost guarantees in the design (R6): it must always be torn down within
a bounded time and never bill silently while idle.

It is imported by both the CDK infrastructure layer (the ephemeral-builder
fallback wiring) and any runtime build tooling so they share a single source of
truth, and it makes no live AWS calls so the correctness property can exercise
it directly.

Implemented here:

* :func:`ephemeral_teardown` -- Property 4 / R6.6, R6.7, R6.8, R6.9. Given an
  ephemeral builder's lifecycle facts, whether its stop was confirmed, and the
  teardown timestamp, decide the teardown record: honour the <=300 s teardown
  deadline and the <=10800 s (3 h) hard maximum-lifetime cap, emit an alert
  naming the resource exactly when the stop is *not* confirmed (R6.8), and
  retain the resource identifier and teardown timestamp when the stop *is*
  confirmed (R6.9). The confirmation flag and timestamp are injected so the pure
  decision can be exercised directly by the correctness property -- this
  function performs no teardown side effects itself.

Design references:
    * Components -- "Where builds run without a paid server (R6)": the
      ephemeral-compute safety rules (torn down within 300 s of completion; hard
      max lifetime 10800 s; alert on unconfirmed stop; record id + timestamp on
      confirmation), "Modeled by the pure ``ephemeral_teardown`` decision
      function".
    * Data Models -- ``EphemeralCompute`` / ``TeardownResult``.
    * Correctness Property 4: Ephemeral build compute is always torn down within
      bounded time.

Requirements: 6.6, 6.7, 6.8, 6.9
"""

from __future__ import annotations

from hellodj_platform_logic.types import EphemeralCompute, TeardownResult

__all__ = [
    "TEARDOWN_DEADLINE_CAP_SECONDS",
    "MAX_LIFETIME_CAP_SECONDS",
    "ephemeral_teardown",
]

#: The upper bound on the teardown deadline after build completion (R6.6): the
#: ephemeral build compute is torn down within 300 seconds of completion so it
#: does not continue to incur charges.
TEARDOWN_DEADLINE_CAP_SECONDS: float = 300.0

#: The hard upper bound on ephemeral-builder lifetime (R6.7): a forced
#: termination is scheduled at a maximum lifetime not exceeding 10800 seconds
#: (3 hours), after which the compute is forcibly terminated even if teardown
#: fails or the build process crashes.
MAX_LIFETIME_CAP_SECONDS: float = 10800.0


def ephemeral_teardown(
    compute: EphemeralCompute,
    stopped_confirmed: bool,
    ts: str,
) -> TeardownResult:
    """Decide the teardown record for one ephemeral build compute.

    Implements Property 4 / R6.6-R6.9. The decision is pure: it does not perform
    the teardown, it records the outcome of one. It holds regardless of the
    build completion outcome (success or failure) and regardless of the teardown
    scenario (clean stop, teardown failure, or a crashed build process) -- those
    are captured by ``stopped_confirmed``.

    The two lifecycle bounds carried by ``compute`` are asserted against the
    design caps so a misconfigured builder cannot silently exceed them:

    * **Teardown deadline (R6.6).** ``compute.teardown_deadline_seconds`` must be
      at most :data:`TEARDOWN_DEADLINE_CAP_SECONDS` (300 s) -- the compute is
      torn down within 300 seconds of build completion.
    * **Maximum lifetime (R6.7).** ``compute.max_lifetime_seconds`` must be at
      most :data:`MAX_LIFETIME_CAP_SECONDS` (10800 s) -- a forced termination is
      scheduled at a lifetime not exceeding 3 hours even if teardown fails or the
      build crashes.

    The recorded outcome then depends only on ``stopped_confirmed``:

    * **Stop confirmed (R6.9).** The result retains the resource identifier and
      the teardown timestamp confirming no build compute remains running, and no
      alert is emitted.
    * **Stop not confirmed (R6.8).** An alert is emitted (``alert_emitted`` is
      ``True``) so the still-running compute -- and its ongoing cost -- is
      surfaced for manual intervention. The resource identifier and timestamp
      are still recorded so the alert names the resource.

    Consequently ``alert_emitted`` is ``True`` if and only if ``stopped_confirmed``
    is ``False`` (R6.8), and ``confirmed_stopped`` mirrors ``stopped_confirmed``.

    Args:
        compute: The ephemeral builder's lifecycle facts -- its resource
            identifier and its teardown-deadline / maximum-lifetime bounds. The
            bounds must be within the design caps (see above).
        stopped_confirmed: Whether the teardown confirmed the compute has
            stopped. ``True`` for a confirmed clean stop; ``False`` when teardown
            failed, the build crashed, or forced termination did not confirm the
            stop.
        ts: The teardown timestamp to record. Retained on the result in both the
            confirmed and unconfirmed cases (on confirmation per R6.9; on an
            alert so the alert can name the resource and time).

    Returns:
        A :class:`~hellodj_platform_logic.types.TeardownResult` carrying the
        resource identifier from ``compute``, ``confirmed_stopped`` equal to
        ``stopped_confirmed``, the recorded ``teardown_timestamp``, and
        ``alert_emitted`` set exactly when the stop was not confirmed.

    Raises:
        ValueError: If ``compute.teardown_deadline_seconds`` exceeds
            :data:`TEARDOWN_DEADLINE_CAP_SECONDS` or
            ``compute.max_lifetime_seconds`` exceeds
            :data:`MAX_LIFETIME_CAP_SECONDS` -- a misconfigured builder that
            would violate the R6.6/R6.7 bounds.

    Requirements: 6.6, 6.7, 6.8, 6.9
    """
    if compute.teardown_deadline_seconds > TEARDOWN_DEADLINE_CAP_SECONDS:
        raise ValueError(
            f"ephemeral builder {compute.resource_id!r} teardown deadline "
            f"{compute.teardown_deadline_seconds}s exceeds the "
            f"{TEARDOWN_DEADLINE_CAP_SECONDS}s cap (R6.6)"
        )
    if compute.max_lifetime_seconds > MAX_LIFETIME_CAP_SECONDS:
        raise ValueError(
            f"ephemeral builder {compute.resource_id!r} maximum lifetime "
            f"{compute.max_lifetime_seconds}s exceeds the "
            f"{MAX_LIFETIME_CAP_SECONDS}s cap (R6.7)"
        )

    # An alert is emitted exactly when the stop is not confirmed (R6.8); the
    # resource id and teardown timestamp are retained regardless so a confirmed
    # stop is fully recorded (R6.9) and an alert can name the resource.
    return TeardownResult(
        resource_id=compute.resource_id,
        confirmed_stopped=stopped_confirmed,
        teardown_timestamp=ts,
        alert_emitted=not stopped_confirmed,
    )

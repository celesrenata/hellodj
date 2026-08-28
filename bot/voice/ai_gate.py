"""HelloDJ — AI integration entitlement gate + immediate cost metering.

This module is the enforcement point for the *AI integration* entitlement
(Requirement 9) and the *immediate* half of AI cost metering (Requirements 9.4,
10.1, 10.5). It sits in front of the two AI-backed request paths in the voice
pipeline:

* ``voice/llm_intent.py``  — LLM intent extraction (Ollama), and
* ``voice/query_handler.py`` — general LLM+tool queries.

Enforcement contract
---------------------

Every AI-backed request MUST pass through :func:`gate_ai_request` before the
request reaches the AI service. The gate resolves the acting Discord user's
effective entitlements (explicit record merged over the secure restrictive
defaults, cached with a bounded TTL, fail-safe to defaults) via the process-wide
:class:`playback.user_entitlements.UserEntitlementResolver` and returns an
:class:`AiGateDecision`:

* **ai_integration disabled** → ``permitted=False`` and *no cost is incurred*
  (R9.2). The default entitlement set has ``ai_integration=False`` (secure by
  default), so an unresolved / unlinked / datastore-unavailable user is declined
  without cost too (R14.3).
* **resolver unavailable** (``None``) → ``permitted=False`` (restrictive default;
  a governed capability is never granted without a resolver).
* **ai_integration enabled** → cost is metered **immediately, at permit time**
  via ``record_ai_cost`` — NOT deferred to AI completion (R9.4, R10.1) — and the
  gate returns ``permitted=True`` with an optional over-cap ``warning`` (R10.5).

Fail-closed (R9.3)
------------------

The AI request paths call the gate and proceed to the AI service **only** when
``decision.permitted`` is true. Any request that is not explicitly permitted is
blocked entirely (it returns the decline text and never calls the model) rather
than being allowed to proceed — a non-declined-but-should-have-been request is
treated as an error and blocked (R9.3).

Over-cap warns, never blocks (R10.5)
------------------------------------

When a per-user AI spend cap is configured and the user's accumulated tally is at
or over the cap, the gate still permits the request (``permitted=True``) but
attaches a ``warning``. The cap surfaces a warning; it does not hard-block.

Requirements: 9.2, 9.3, 9.4, 10.1, 10.5
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol

log = logging.getLogger(__name__)

__all__ = [
    "AI_REQUEST_BEDROCK_COST_ESTIMATE",
    "AiGateDecision",
    "EntitlementResolverLike",
    "gate_ai_request",
    "over_cap",
]

#: Nominal per-request Bedrock cost (USD) metered at permit time. Requirement
#: 9.4 mandates metering *immediately when the request is permitted*, before the
#: AI service completes, so the exact token counts are not yet known here — a
#: nominal per-request estimate is recorded up front. The markup (data, not code
#: — ``CONFIG#AIPRICING.markup``, R10.3) is applied inside ``record_ai_cost``;
#: the effective cost added to the tally is ``estimate * (1 + markup)``.
AI_REQUEST_BEDROCK_COST_ESTIMATE: float = 0.001

#: User-facing decline text when AI integration is not permitted (R9.2). Kept
#: voice-friendly (spoken via TTS).
DECLINE_MESSAGE = "AI features are not enabled for you."


def over_cap(accumulated: float, cap: float | None) -> bool:
    """Return ``True`` when a cap is set and the tally is at or over it (R10.5).

    Mirrors the web-ui ``entitlements_core.over_cap`` semantics: equality counts
    as over cap, and an unset (``None``) cap is never over cap. Being over cap
    surfaces a warning but SHALL NOT by itself hard-block an AI request.
    """
    if cap is None:
        return False
    try:
        return float(accumulated) >= float(cap)
    except (TypeError, ValueError):
        return False


@dataclass(frozen=True)
class AiGateDecision:
    """The outcome of gating one AI-backed request.

    Attributes
    ----------
    permitted:
        ``True`` iff the acting user's effective ``ai_integration`` flag is
        enabled and a resolver was available. The AI request paths proceed to the
        model ONLY when this is true; otherwise the request is blocked (R9.3).
    reason:
        A short, voice-friendly decline message when ``permitted`` is false;
        empty when permitted.
    warning:
        An optional over-cap warning surfaced alongside a *permitted* request
        (R10.5); ``None`` when under cap or no cap is configured.
    """

    permitted: bool
    reason: str = ""
    warning: str | None = None


class EntitlementResolverLike(Protocol):
    """The subset of ``UserEntitlementResolver`` the gate depends on.

    Declared as a Protocol so the gate is exercised with an in-memory fake in
    tests without constructing the real ``CoreTable``-backed resolver.
    """

    def effective_for_discord(self, discord_id: str | int) -> dict[str, Any]:
        """Return the acting Discord user's effective entitlements."""
        ...

    def sub_for_discord(self, discord_id: str | int) -> str | None:
        """Resolve the acting Discord id to its Cognito sub, or ``None``."""
        ...

    def ai_tally_for_sub(self, sub: str) -> float:
        """Return the sub's accumulated AI cost tally."""
        ...

    def record_ai_cost(self, sub: str, bedrock_cost: float) -> None:
        """Meter a bedrock cost against the sub's AI tally."""
        ...


def gate_ai_request(
    resolver: EntitlementResolverLike | None,
    discord_id: str | int,
    *,
    bedrock_cost: float = AI_REQUEST_BEDROCK_COST_ESTIMATE,
) -> AiGateDecision:
    """Gate one AI-backed request and meter its cost immediately when permitted.

    Parameters
    ----------
    resolver:
        The process-wide entitlement resolver (``bot.get_user_entitlements()``),
        or ``None`` when unavailable. A ``None`` resolver declines (restrictive
        default — never grant a governed capability without a resolver).
    discord_id:
        The acting Discord user id.
    bedrock_cost:
        The nominal per-request Bedrock cost to meter at permit time. Defaults to
        :data:`AI_REQUEST_BEDROCK_COST_ESTIMATE`.

    Returns
    -------
    AiGateDecision
        ``permitted=False`` (with a decline ``reason`` and no cost) when AI
        integration is disabled or unresolved (R9.2); otherwise
        ``permitted=True`` after metering the cost immediately (R9.4/R10.1), with
        an optional over-cap ``warning`` (R10.5).
    """
    # No resolver → restrictive default: decline without cost (R14.3, R9.2).
    if resolver is None:
        return AiGateDecision(permitted=False, reason=DECLINE_MESSAGE)

    # Resolve effective entitlements (fail-safe to restrictive defaults inside
    # the resolver on any datastore/lookup failure).
    effective = resolver.effective_for_discord(discord_id)
    if not effective.get("ai_integration", False):
        # Disabled (or defaulted-off) → decline WITHOUT incurring cost (R9.2).
        return AiGateDecision(permitted=False, reason=DECLINE_MESSAGE)

    # Permitted. Meter the cost IMMEDIATELY at permit time (R9.4/R10.1) — not
    # deferred to AI completion. Metering is keyed by the platform account
    # (Cognito sub); an unlinked id simply skips metering (no sub → no tally).
    sub = resolver.sub_for_discord(discord_id)
    warning: str | None = None
    if sub:
        resolver.record_ai_cost(sub, bedrock_cost)
        # Surface an over-cap warning WITHOUT hard-blocking (R10.5). Read the
        # tally AFTER metering so the just-charged request counts toward the cap.
        cap = effective.get("ai_spend_cap")
        if over_cap(resolver.ai_tally_for_sub(sub), cap):
            warning = (
                "Heads up: you are at or over your AI spend cap. "
                "Your request will still be processed."
            )

    return AiGateDecision(permitted=True, warning=warning)

"""Shared constants (design-phase settings / Testing Strategy thresholds)."""

from __future__ import annotations

__all__ = [
    "HELLODJ_ZONE",
    "INTERACTIVE_LATENCY_BUDGET_SECONDS",
    "DEFAULT_DRAIN_TIMEOUT_SECONDS",
    "DEFAULT_SCALE_OUT_THRESHOLD",
    "DEFAULT_SCALE_IN_THRESHOLD",
]

#: The DNS zone that every derived environment name is a subdomain of (R12).
HELLODJ_ZONE = "hellodj.bot"

#: Interactive latency budget in seconds (R3.13). GPU strategy selection must
#: never return a strategy whose warm-start latency exceeds this budget.
INTERACTIVE_LATENCY_BUDGET_SECONDS = 5.0

#: Default connection-draining timeout in seconds for app/transcode workloads
#: (R17.3); tunable per component.
DEFAULT_DRAIN_TIMEOUT_SECONDS = 120.0

#: Default autoscaling thresholds as utilization fractions (R16): scale out when
#: any signal exceeds ``SCALE_OUT``; scale in only when all signals are below
#: ``SCALE_IN``. The gap between them provides hysteresis.
DEFAULT_SCALE_OUT_THRESHOLD = 0.70
DEFAULT_SCALE_IN_THRESHOLD = 0.40

"""Cross-stage endpoint routing for the single shared GPU host.

This module implements the pure decision function behind cross-stage routing
isolation (Requirement 8). Beta, Staging and Production are consolidated onto a
single shared cluster/host and isolated only by their distinct
:class:`~hellodj_platform_logic.types.StageEndpoint` (namespace / port /
hostname ``<stage>.<region>.hellodj.bot``). It performs no AWS or Kubernetes
calls so both the CDK Ingress wiring (hostname -> namespace Service) and the
property tests can import a single source of truth.

The function answers one question: *given the hostname a request targets and the
set of stage endpoints, which stage — if any — does the request reach?* It
encodes the design invariant:

* **Cross-stage routing isolation (R8.7).** A request targeting one
  ``Stage_Endpoint``'s hostname routes only to that stage's workload, never to
  another stage's; a hostname matching no endpoint routes nowhere.

Design references:
    * "Cross-stage routing isolation (R8.7)" in Components and Interfaces §8,
      modeled by the pure ``route_endpoint`` function.
    * Correctness Property 9: A request routes only to the stage whose endpoint
      it targets.

Requirements: 8.7
"""

from __future__ import annotations

from collections.abc import Iterable

from hellodj_platform_logic.types import StageEndpoint

__all__ = ["route_endpoint"]


def route_endpoint(
    hostname: str,
    endpoints: Iterable[StageEndpoint],
) -> StageEndpoint | None:
    """Route a request hostname to exactly the matching stage endpoint.

    Implements Property 9 / R8.7. Returns exactly the
    :class:`~hellodj_platform_logic.types.StageEndpoint` whose ``hostname``
    equals ``hostname`` (an exact string match), and never a different stage's
    endpoint. A ``hostname`` matching no endpoint returns ``None`` (routes
    nowhere), preserving cross-stage isolation on the shared host.

    The endpoints are expected to have distinct hostnames (each stage owns
    ``<stage>.<region>.hellodj.bot``); the first endpoint whose hostname matches
    is returned, so distinct-hostname sets yield a unique route.

    Args:
        hostname: The request hostname to route, matched exactly against each
            endpoint's ``hostname``.
        endpoints: The stage endpoints to route among (typically the three
            Beta / Staging / Production endpoints with distinct hostnames). The
            iterable is consumed at most once.

    Returns:
        The :class:`~hellodj_platform_logic.types.StageEndpoint` whose
        ``hostname`` equals ``hostname``, or ``None`` when no endpoint matches.

    Requirements: 8.7
    """
    for endpoint in endpoints:
        if endpoint.hostname == hostname:
            return endpoint
    return None

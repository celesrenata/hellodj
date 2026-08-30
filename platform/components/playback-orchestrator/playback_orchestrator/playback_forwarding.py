"""Cross-replica play forwarding for the sharded orchestrator (R4).

When the orchestrator runs as a sharded StatefulSet (distributed-bot-sharding),
a guild is served by exactly ONE replica — its owner `shard(guild_id, N) ==
ordinal`. A ``POST /v1/playback`` may land on any replica (the ClusterIP Service
load-balances), so a request for a guild this replica does NOT own must be
FORWARDED to the owning replica, which holds that guild's connected bot(s) and
session state.

This module is the forwarding decision + relay, kept out of
:mod:`playback_api` so that pure request→response mapping stays HTTP-free and
this (HTTP-relay) concern is separately testable with an injected transport.

Contract (R4):

* The receiving replica computes ``owner = shard(guild_id, N)``.
* If ``owner == ordinal`` OR the request already carries the forward-once hop
  guard header, the request is handled LOCALLY (the caller invokes the normal
  ``handle_playback``).
* Otherwise the request is forwarded exactly once to
  ``playback-orchestrator-<owner>.<headless-svc>:<port>/v1/playback`` with the
  hop-guard header set, and the owner's response body is relayed verbatim.
* On any transport error the receiver returns a truthful "temporarily
  unavailable" body — NEVER a false success, and NEVER a local connect of the
  remote-owned app (R3.1/R4.3).

At ``replica_count == 1`` (single shard / degraded topology) every guild is
owned locally, so :func:`forward_decision` always says "handle locally" and this
module is inert — identical to today's single-replica behavior (R7.1).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Protocol
from urllib import error as urlerror
from urllib import request as urlrequest

from .sharding import shard

_LOG = logging.getLogger("playback_orchestrator.playback_forwarding")

__all__ = [
    "FORWARDED_HEADER",
    "ForwardHttp",
    "PlaybackForwarder",
    "forward_decision",
    "owner_pod_url",
]

#: Hop-guard header: present (== "1") means the request has already been
#: forwarded once, so the receiving replica MUST handle it locally rather than
#: forward again — guarantees at-most-one hop and prevents relay loops (R4.4).
FORWARDED_HEADER = "X-HelloDJ-Forwarded"


def forward_decision(
    guild_id: str,
    ordinal: int,
    replica_count: int,
    *,
    already_forwarded: bool,
) -> int | None:
    """Return the owner ordinal to forward to, or ``None`` to handle locally.

    Handle locally (``None``) when: this replica owns the guild
    (``shard(guild_id, N) == ordinal``), the topology is single-shard
    (``replica_count <= 1``), OR the request was already forwarded once
    (hop guard, R4.4). Otherwise return the owning ordinal to forward to.
    """
    if replica_count <= 1 or already_forwarded:
        return None
    owner = shard(guild_id, replica_count)
    return None if owner == ordinal else owner


def owner_pod_url(
    owner_ordinal: int,
    *,
    service_name: str,
    namespace: str,
    port: int,
    route: str = "/v1/playback",
) -> str:
    """Build the stable per-pod URL for the owner replica (headless-svc DNS).

    A StatefulSet + headless Service gives each pod a stable DNS name
    ``<statefulset>-<ordinal>.<service>.<namespace>.svc.cluster.local``. We reach
    the owner replica directly (not via the load-balanced ClusterIP) so the
    forwarded request lands on the specific pod that owns the guild (R4.1).
    """
    host = (
        f"playback-orchestrator-{owner_ordinal}."
        f"{service_name}.{namespace}.svc.cluster.local"
    )
    return f"http://{host}:{port}{route}"


class ForwardHttp(Protocol):
    """Minimal HTTP POST seam so forwarding is unit-testable without a network."""

    def post_json(
        self, url: str, body: dict[str, Any], headers: dict[str, str]
    ) -> dict[str, Any]:
        """POST ``body`` as JSON to ``url``; return the decoded JSON response."""
        ...


@dataclass
class UrllibForwardHttp:
    """Default :class:`ForwardHttp` using stdlib urllib (no extra deps).

    Bounded by ``timeout`` seconds; raises on transport/HTTP error so the
    forwarder maps it to the truthful "unavailable" body (R4.3).
    """

    timeout: float = 5.0

    def post_json(
        self, url: str, body: dict[str, Any], headers: dict[str, str]
    ) -> dict[str, Any]:
        data = json.dumps(body).encode("utf-8")
        req = urlrequest.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        for key, value in headers.items():
            req.add_header(key, value)
        with urlrequest.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310 - in-cluster http to a fixed pod DNS
            raw = resp.read()
        parsed = json.loads(raw or b"{}")
        return parsed if isinstance(parsed, dict) else {}


class PlaybackForwarder:
    """Forwards a playback request to a guild's owning replica (R4).

    Dependency-injected topology + HTTP transport so the forward/relay path is
    unit-testable with a fake transport. When single-shard, :meth:`maybe_forward`
    is always a "handle locally" no-op.
    """

    def __init__(
        self,
        *,
        ordinal: int,
        replica_count: int,
        service_name: str,
        namespace: str,
        port: int,
        http: ForwardHttp | None = None,
    ) -> None:
        self._ordinal = ordinal
        self._replica_count = replica_count if replica_count > 1 else 1
        self._service_name = service_name
        self._namespace = namespace
        self._port = port
        self._http = http or UrllibForwardHttp()

    def maybe_forward(
        self, body: dict[str, Any], *, already_forwarded: bool
    ) -> dict[str, Any] | None:
        """Forward the request if a remote replica owns the guild, else ``None``.

        Returns the relayed owner response body when the request was forwarded
        (including the truthful "unavailable" body on transport error), or
        ``None`` to tell the caller to handle it LOCALLY (owner is self,
        single-shard, or already forwarded — R4.4).
        """
        guild_id = str(body.get("guildId", "") or "").strip()
        if not guild_id:
            # No guild → can't shard-route; handle locally (the API layer will
            # return a clean "invalid request" body).
            return None

        owner = forward_decision(
            guild_id,
            self._ordinal,
            self._replica_count,
            already_forwarded=already_forwarded,
        )
        if owner is None:
            return None  # handle locally

        url = owner_pod_url(
            owner,
            service_name=self._service_name,
            namespace=self._namespace,
            port=self._port,
        )
        try:
            return self._http.post_json(
                url, body, {FORWARDED_HEADER: "1", "Content-Type": "application/json"}
            )
        except (urlerror.URLError, OSError, ValueError, TimeoutError) as exc:
            _LOG.warning(
                "playback forward to ordinal %d failed (%s); returning unavailable",
                owner,
                exc,
            )
            return {
                "ok": False,
                "message": (
                    "Playback is temporarily unavailable "
                    "(owning node unreachable). Please try again."
                ),
                "data": {},
            }

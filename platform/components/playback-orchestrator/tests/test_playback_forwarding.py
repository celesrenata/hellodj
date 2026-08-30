"""Tests for cross-replica play forwarding (distributed-bot-sharding T4/R4).

Covers:

* ``forward_decision`` — local when owner/single-shard/already-forwarded; else
  the owning ordinal (Property 3 forward-once / R4.4).
* ``owner_pod_url`` — stable headless-Service per-pod DNS (R4.1).
* ``PlaybackForwarder.maybe_forward`` — handle-locally vs relay; forward-once
  hop guard; truthful "unavailable" on transport error, never a local connect
  (R4.2/R4.3/R4.4).

Uses a fake transport so no network is needed.

Tagged: Feature: distributed-bot-sharding, Property 3 (forward-once).
"""

from __future__ import annotations

from typing import Any

from playback_orchestrator.playback_forwarding import (
    FORWARDED_HEADER,
    PlaybackForwarder,
    forward_decision,
    owner_pod_url,
)
from playback_orchestrator.sharding import shard

# --- forward_decision ------------------------------------------------------


def test_forward_decision_single_shard_is_local() -> None:
    """replica_count <= 1 → always local (R7.1)."""
    assert forward_decision("123", 0, 1, already_forwarded=False) is None


def test_forward_decision_already_forwarded_is_local() -> None:
    """R4.4: an already-forwarded request is handled locally (no second hop)."""
    n = 4
    g = next(str(x) for x in range(1, 999) if shard(str(x), n) != 0)
    # Even though ordinal 0 does NOT own g, the hop guard forces local handling.
    assert forward_decision(g, 0, n, already_forwarded=True) is None


def test_forward_decision_local_when_owner() -> None:
    n = 4
    g = next(str(x) for x in range(1, 999) if shard(str(x), n) == 2)
    assert forward_decision(g, 2, n, already_forwarded=False) is None


def test_forward_decision_forwards_to_owner() -> None:
    n = 4
    g = next(str(x) for x in range(1, 999) if shard(str(x), n) == 3)
    # Received on ordinal 0, owned by 3 → forward to 3.
    assert forward_decision(g, 0, n, already_forwarded=False) == 3


# --- owner_pod_url ---------------------------------------------------------


def test_owner_pod_url_is_stable_pod_dns() -> None:
    url = owner_pod_url(
        2, service_name="playback-orchestrator", namespace="hellodj-beta", port=8080
    )
    assert url == (
        "http://playback-orchestrator-2.playback-orchestrator."
        "hellodj-beta.svc.cluster.local:8080/v1/playback"
    )


# --- PlaybackForwarder -----------------------------------------------------


class _FakeHttp:
    """Records forwarded requests; returns a canned owner response or raises."""

    def __init__(
        self,
        response: dict[str, Any] | None = None,
        raise_exc: Exception | None = None,
    ) -> None:
        self.response = response or {"ok": True, "message": "relayed", "data": {}}
        self.raise_exc = raise_exc
        self.calls: list[tuple[str, dict[str, Any], dict[str, str]]] = []

    def post_json(self, url: str, body: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
        self.calls.append((url, body, headers))
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.response


def _forwarder(http: _FakeHttp, *, ordinal: int, n: int) -> PlaybackForwarder:
    return PlaybackForwarder(
        ordinal=ordinal,
        replica_count=n,
        service_name="playback-orchestrator",
        namespace="hellodj-beta",
        port=8080,
        http=http,
    )


def test_maybe_forward_local_when_owner() -> None:
    n = 3
    g = next(str(x) for x in range(1, 999) if shard(str(x), n) == 1)
    http = _FakeHttp()
    fwd = _forwarder(http, ordinal=1, n=n)
    assert fwd.maybe_forward({"guildId": g}, already_forwarded=False) is None
    assert http.calls == []  # nothing forwarded


def test_maybe_forward_relays_to_owner() -> None:
    n = 3
    g = next(str(x) for x in range(1, 999) if shard(str(x), n) == 2)
    http = _FakeHttp(response={"ok": True, "message": "owned-here", "data": {"x": 1}})
    fwd = _forwarder(http, ordinal=0, n=n)  # 0 does not own g (owner 2)
    out = fwd.maybe_forward({"guildId": g, "action": "play"}, already_forwarded=False)
    assert out == {"ok": True, "message": "owned-here", "data": {"x": 1}}
    # Forwarded exactly once, to owner pod 2, with the hop-guard header set.
    assert len(http.calls) == 1
    url, body, headers = http.calls[0]
    assert "playback-orchestrator-2." in url
    assert headers[FORWARDED_HEADER] == "1"
    assert body["guildId"] == g


def test_maybe_forward_hop_guard_handles_locally() -> None:
    """R4.4: an already-forwarded request is never forwarded again."""
    n = 3
    g = next(str(x) for x in range(1, 999) if shard(str(x), n) == 2)
    http = _FakeHttp()
    fwd = _forwarder(http, ordinal=0, n=n)
    assert fwd.maybe_forward({"guildId": g}, already_forwarded=True) is None
    assert http.calls == []


def test_maybe_forward_transport_error_is_unavailable() -> None:
    """R4.3: transport error → truthful unavailable body, no local connect."""
    n = 3
    g = next(str(x) for x in range(1, 999) if shard(str(x), n) == 2)
    http = _FakeHttp(raise_exc=OSError("connection refused"))
    fwd = _forwarder(http, ordinal=0, n=n)
    out = fwd.maybe_forward({"guildId": g}, already_forwarded=False)
    assert out is not None
    assert out["ok"] is False
    assert "unavailable" in out["message"].lower()


def test_maybe_forward_no_guild_is_local() -> None:
    """A missing guildId can't be shard-routed → handle locally."""
    http = _FakeHttp()
    fwd = _forwarder(http, ordinal=0, n=3)
    assert fwd.maybe_forward({"action": "play"}, already_forwarded=False) is None
    assert http.calls == []


def test_maybe_forward_single_shard_is_local() -> None:
    """replica_count 1 → always local (forwarder inert)."""
    http = _FakeHttp()
    fwd = _forwarder(http, ordinal=0, n=1)
    assert fwd.maybe_forward({"guildId": "123"}, already_forwarded=False) is None
    assert http.calls == []

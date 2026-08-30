"""Tests for shard-aware served-guild discovery in instance_bootstrap.

Covers the distributed-bot-sharding task 2 additions:

* ``_served_guild_ids`` — filters claimed guilds to the ones THIS replica owns
  (``shard(gid, N) == ordinal``); at ``N == 1`` serves everything (R7.1); the
  union across ordinals equals the full claimed set and is pairwise disjoint
  (Property 1 / R2.2).
* ``_resolve_shard_topology`` — reads ``HOSTNAME`` + ``HELLODJ_ORCHESTRATOR_REPLICAS``
  and degrades to ``(0, 1)`` on bad input (R1.3).

Tagged: Feature: distributed-bot-sharding, Property 1 (guild partition),
Property 4 (identity at N=1).
"""

from __future__ import annotations

from playback_orchestrator import instance_bootstrap
from playback_orchestrator.instance_bootstrap import _resolve_shard_topology
from playback_orchestrator.sharding import shard

_served = instance_bootstrap._served_guild_ids


def test_served_single_replica_serves_all() -> None:
    """R7.1: replica_count == 1 serves every claimed guild, order preserved."""
    guilds = ["10", "20", "30"]
    assert _served(guilds, 0, 1) == guilds


def test_served_filters_to_owned_only() -> None:
    """A replica keeps only guilds whose shard equals its ordinal."""
    guilds = [str(g) for g in range(1, 40)]
    count = 4
    for ordinal in range(count):
        got = _served(guilds, ordinal, count)
        assert all(shard(g, count) == ordinal for g in got)
        # Nothing owned by another ordinal leaks in.
        assert not any(shard(g, count) != ordinal for g in got)


def test_served_partition_is_total_and_disjoint() -> None:
    """Property 1 (R2.2): union over ordinals == all claimed; pairwise disjoint."""
    guilds = [str(g) for g in range(1, 60)]
    count = 5
    buckets = [set(_served(guilds, o, count)) for o in range(count)]

    union: set[str] = set()
    for b in buckets:
        union |= b
    assert union == set(guilds)

    # Disjoint: no guild appears in two ordinals' served sets.
    total = sum(len(b) for b in buckets)
    assert total == len(set(guilds))


def test_served_preserves_input_order() -> None:
    """The served slice keeps the discovery order (already sorted)."""
    count = 3
    guilds = [str(g) for g in range(1, 30)]
    for ordinal in range(count):
        got = _served(guilds, ordinal, count)
        assert got == [g for g in guilds if shard(g, count) == ordinal]


def test_resolve_shard_topology_from_env(monkeypatch) -> None:
    monkeypatch.setenv("HOSTNAME", "playback-orchestrator-2")
    monkeypatch.setenv("HELLODJ_ORCHESTRATOR_REPLICAS", "3")
    assert _resolve_shard_topology() == (2, 3)


def test_resolve_shard_topology_degrades(monkeypatch) -> None:
    """R1.3: missing/bad replica env → single-shard (0, 1)."""
    monkeypatch.setenv("HOSTNAME", "playback-orchestrator-2")
    monkeypatch.delenv("HELLODJ_ORCHESTRATOR_REPLICAS", raising=False)
    assert _resolve_shard_topology() == (0, 1)

    monkeypatch.setenv("HELLODJ_ORCHESTRATOR_REPLICAS", "bogus")
    assert _resolve_shard_topology() == (0, 1)


def test_resolve_shard_topology_out_of_range_ordinal(monkeypatch) -> None:
    """An ordinal >= count degrades to (0, 1) rather than overlap a shard."""
    monkeypatch.setenv("HOSTNAME", "playback-orchestrator-9")
    monkeypatch.setenv("HELLODJ_ORCHESTRATOR_REPLICAS", "3")
    assert _resolve_shard_topology() == (0, 1)

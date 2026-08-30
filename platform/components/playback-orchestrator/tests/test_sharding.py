"""Unit + property tests for the pure shard math (distributed-bot-sharding).

Exercises :mod:`playback_orchestrator.sharding`:

* :func:`shard` — deterministic guild→ordinal mapping; the partition property
  (every guild owned by exactly one ordinal in range; the union over ordinals is
  total and pairwise-disjoint — Property 1 / R2.2); process-stable (no
  ``PYTHONHASHSEED`` dependence); the ``replica_count == 1`` identity (always 0
  — Property 4 / R7.1).
* :func:`parse_ordinal` — StatefulSet hostname suffix parsing + ``None`` on
  absence.
* :func:`resolve_topology` — ``(ordinal, replica_count)`` resolution and the
  R1.3 degrade-to-``(0, 1)`` on any bad input, always keeping
  ``0 <= ordinal < replica_count``.

Tagged: Feature: distributed-bot-sharding, Property 1 (guild partition),
Property 4 (identity at N=1).
"""

from __future__ import annotations

import hashlib

from hypothesis import given
from hypothesis import strategies as st

from playback_orchestrator.sharding import (
    parse_ordinal,
    resolve_topology,
    shard,
)

# A guild id is a Discord snowflake string; model it as a numeric string.
_guild_ids = st.integers(min_value=1, max_value=10**20).map(str)
_replica_counts = st.integers(min_value=1, max_value=16)


# --- shard: range + determinism -------------------------------------------


@given(_guild_ids, _replica_counts)
def test_shard_in_range(guild_id: str, count: int) -> None:
    """shard() always returns an ordinal within [0, count)."""
    assert 0 <= shard(guild_id, count) < count


@given(_guild_ids, _replica_counts)
def test_shard_deterministic(guild_id: str, count: int) -> None:
    """shard() is deterministic for the same inputs (process-stable)."""
    assert shard(guild_id, count) == shard(guild_id, count)


@given(_guild_ids)
def test_shard_single_replica_is_zero(guild_id: str) -> None:
    """Property 4 (R7.1): with one replica, every guild maps to ordinal 0."""
    assert shard(guild_id, 1) == 0


def test_shard_nonpositive_count_degrades_to_single() -> None:
    """A count < 1 is treated as a single shard (never raises / negative)."""
    assert shard("123", 0) == 0
    assert shard("123", -5) == 0


def test_shard_stable_against_hash_randomization() -> None:
    """shard() uses blake2b, NOT builtin hash(), so it is process-stable.

    Recompute the expected owner independently from blake2b and assert equality;
    this would break if the implementation ever switched to the salted builtin
    ``hash()``.
    """
    guild_id = "9876543210"
    count = 7
    digest = hashlib.blake2b(guild_id.encode("utf-8"), digest_size=8).digest()
    expected = int.from_bytes(digest, "big") % count
    assert shard(guild_id, count) == expected


# --- shard: partition property (Property 1 / R2.2) ------------------------


@given(st.lists(_guild_ids, min_size=1, max_size=50, unique=True), _replica_counts)
def test_shard_partitions_guilds(guild_ids: list[str], count: int) -> None:
    """Property 1 (R2.2): served-guild sets are disjoint and total.

    Assigning each guild to ``shard(g, count)`` produces per-ordinal buckets
    whose union is the full guild set and whose pairwise intersection is empty
    (a guild belongs to exactly one ordinal).
    """
    buckets: dict[int, set[str]] = {i: set() for i in range(count)}
    for g in guild_ids:
        buckets[shard(g, count)].add(g)

    # Total: union of all buckets == the input set.
    union: set[str] = set()
    for b in buckets.values():
        union |= b
    assert union == set(guild_ids)

    # Disjoint: every guild appears in exactly one bucket.
    total_assigned = sum(len(b) for b in buckets.values())
    assert total_assigned == len(set(guild_ids))


# --- parse_ordinal ---------------------------------------------------------


def test_parse_ordinal_statefulset_hostname() -> None:
    assert parse_ordinal("playback-orchestrator-0") == 0
    assert parse_ordinal("playback-orchestrator-2") == 2
    assert parse_ordinal("playback-orchestrator-13") == 13


def test_parse_ordinal_absent() -> None:
    """A non-StatefulSet hostname (or empty) has no ordinal."""
    assert parse_ordinal("") is None
    assert parse_ordinal("web-ui-66d74bd477-rnv45") is None  # Deployment pod
    assert parse_ordinal("plainhost") is None


# --- resolve_topology ------------------------------------------------------


def test_resolve_topology_happy() -> None:
    assert resolve_topology("playback-orchestrator-0", "3") == (0, 3)
    assert resolve_topology("playback-orchestrator-2", "3") == (2, 3)


def test_resolve_topology_single_replica() -> None:
    """replicas=1 → (0, 1) regardless of hostname (identity behavior)."""
    assert resolve_topology("playback-orchestrator-0", "1") == (0, 1)
    assert resolve_topology("anything", "1") == (0, 1)


def test_resolve_topology_degrades_on_bad_env() -> None:
    """R1.3: unparseable/absent/<1 replica count → (0, 1), never raises."""
    assert resolve_topology("playback-orchestrator-2", "") == (0, 1)
    assert resolve_topology("playback-orchestrator-2", "notanint") == (0, 1)
    assert resolve_topology("playback-orchestrator-2", "0") == (0, 1)
    assert resolve_topology("playback-orchestrator-2", "-4") == (0, 1)


def test_resolve_topology_degrades_on_unparseable_ordinal() -> None:
    """R1.3: a valid count but no parseable ordinal → (0, 1)."""
    assert resolve_topology("web-ui-66d74bd477-rnv45", "3") == (0, 1)
    assert resolve_topology("", "3") == (0, 1)


def test_resolve_topology_degrades_on_out_of_range_ordinal() -> None:
    """An ordinal >= count would overlap another shard's slice → degrade."""
    assert resolve_topology("playback-orchestrator-5", "3") == (0, 1)


@given(
    st.integers(min_value=0, max_value=15),
    st.integers(min_value=1, max_value=16),
)
def test_resolve_topology_invariant(ordinal: int, count: int) -> None:
    """Whatever the inputs, the result keeps 0 <= ordinal < replica_count."""
    hostname = f"playback-orchestrator-{ordinal}"
    got_ordinal, got_count = resolve_topology(hostname, str(count))
    assert got_count >= 1
    assert 0 <= got_ordinal < got_count

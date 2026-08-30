"""Pure shard math for the distributed (per-node) orchestrator runtime.

The AWS orchestrator runs as a sharded StatefulSet: each pod has a stable
ordinal (``playback-orchestrator-<ordinal>``) and owns a DISJOINT slice of the
work. This module holds the pure, dependency-free functions that decide who owns
what — no boto3, no discord, no k8s — so they are trivially unit/property
testable.

Two ownership questions, one function:

* **Guild ownership** — a guild ``G`` is served by the replica whose ordinal
  equals :func:`shard`\\ ``(G, N)``. This partitions guilds across replicas so a
  guild's secondary bots + session/queue writes live on exactly one replica
  (single-writer preserved).
* **App ownership** — a pool application shared across guilds that land on
  different replicas is connected by exactly ONE replica: the owner of the
  lexicographically-smallest claiming guild id (computed by the caller as
  ``shard(min(claiming_guild_ids), N)``). :func:`shard` is the single primitive
  behind both; the "min claiming guild" tiebreak lives in the credential source.

The hash MUST be stable across processes and pod restarts, so it does NOT use
Python's built-in ``hash()`` (salted per-process by ``PYTHONHASHSEED``). It uses
``hashlib.blake2b`` folded to an int, modulo the replica count.

Design: distributed-bot-sharding R1.2, R1.3, R1.4, R2.2.
"""

from __future__ import annotations

import hashlib
import re

__all__ = [
    "shard",
    "parse_ordinal",
    "resolve_topology",
]

#: A StatefulSet pod hostname is ``<statefulset-name>-<ordinal>``; the ordinal is
#: the trailing integer. We match a trailing ``-<digits>`` so any prefix works
#: (``playback-orchestrator-0`` → 0). Anchored at end of string.
_ORDINAL_RE = re.compile(r"-(\d+)$")


def shard(guild_id: str, replica_count: int) -> int:
    """Return the owning replica ordinal for a guild id.

    A deterministic, process-stable mapping ``guild_id -> [0, replica_count)``.
    Uses a ``blake2b`` digest of the guild id (independent of
    ``PYTHONHASHSEED``, unlike the builtin ``hash``) folded to an integer and
    reduced modulo ``replica_count``, so every replica computes the SAME owner
    for a given guild and the mapping never shifts across restarts (only across
    a deliberate rescale of ``replica_count``).

    Args:
        guild_id: The Discord guild id (as a string; the raw claim key value).
        replica_count: Total orchestrator replicas (``>= 1``). Values ``< 1`` are
            treated as ``1`` (single shard) so a misconfigured count can never
            raise or produce a negative/degenerate ordinal.

    Returns:
        The owning ordinal in ``[0, max(replica_count, 1))``. When
        ``replica_count <= 1`` this is always ``0`` (one shard owns everything —
        the R7.1 identity with today's single-replica behavior).
    """
    count = replica_count if replica_count > 1 else 1
    if count == 1:
        return 0
    digest = hashlib.blake2b(guild_id.encode("utf-8"), digest_size=8).digest()
    value = int.from_bytes(digest, "big")
    return value % count


def parse_ordinal(hostname: str) -> int | None:
    """Extract the StatefulSet ordinal from a pod hostname, or ``None``.

    A StatefulSet pod is named ``<name>-<ordinal>`` (e.g.
    ``playback-orchestrator-2`` → ``2``). Returns ``None`` when the hostname has
    no trailing ``-<digits>`` (e.g. a plain Deployment pod's random suffix, or an
    empty string), so the caller can degrade deterministically.

    Args:
        hostname: The pod hostname (typically ``$HOSTNAME`` / ``os.uname().nodename``).

    Returns:
        The trailing integer ordinal, or ``None`` if absent/unparseable.
    """
    if not hostname:
        return None
    match = _ORDINAL_RE.search(hostname.strip())
    if match is None:
        return None
    try:
        return int(match.group(1))
    except ValueError:  # pragma: no cover - regex guarantees digits
        return None


def resolve_topology(hostname: str, replicas_env: str) -> tuple[int, int]:
    """Resolve ``(ordinal, replica_count)`` from the pod hostname + env.

    The runtime's single entry point for its shard identity. Parses the ordinal
    from ``hostname`` (the StatefulSet suffix) and the replica count from
    ``replicas_env`` (``HELLODJ_ORCHESTRATOR_REPLICAS``). Enforces the invariant
    ``0 <= ordinal < replica_count`` and ``replica_count >= 1``.

    Degradation (R1.3): if the ordinal cannot be parsed, the replica count is
    absent/unparseable/``< 1``, OR the parsed ordinal is out of range for the
    parsed count, this returns ``(0, 1)`` — single-shard behavior identical to
    today's single-replica orchestrator (R7.1) — and NEVER raises. The caller
    logs the degradation.

    Args:
        hostname: Pod hostname to parse the ordinal from.
        replicas_env: Raw value of ``HELLODJ_ORCHESTRATOR_REPLICAS`` (may be
            empty/absent/non-numeric).

    Returns:
        ``(ordinal, replica_count)`` with ``replica_count >= 1`` and
        ``0 <= ordinal < replica_count``; ``(0, 1)`` on any degradation.
    """
    count = _parse_replica_count(replicas_env)
    if count is None or count < 1:
        return (0, 1)
    if count == 1:
        return (0, 1)

    ordinal = parse_ordinal(hostname)
    if ordinal is None or ordinal < 0 or ordinal >= count:
        # Can't place this pod in the partition safely — degrade to single shard
        # rather than claim an ordinal that overlaps another replica's slice.
        return (0, 1)
    return (ordinal, count)


def _parse_replica_count(replicas_env: str) -> int | None:
    """Parse the replica-count env into an int, or ``None`` if unusable."""
    raw = (replicas_env or "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None

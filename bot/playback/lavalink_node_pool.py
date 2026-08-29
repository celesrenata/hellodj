"""HelloDJ — Lavalink node pool for multi-tenant YouTube resolution.

The youtube-source plugin's ``POST /youtube`` replaces ALL credential fields on
a node (OAuth refresh token + poToken + visitorData together, never split), so
one Lavalink node can hold only ONE YouTube credential set live at a time. On a
single shared node, two guilds resolving YouTube tracks at the same instant
serialize on that node's :meth:`YouTubeCredentialInjector.swap_lock` — correct
per resolution, but not concurrent.

:class:`LavalinkNodePool` makes YouTube resolution concurrent up to a configured
number of nodes by mapping the owning user's Cognito ``sub`` to one node from a
fixed pool. A YouTube resolution for a guild resolves the guild's ``owner_sub``,
asks the pool which node that user is assigned, and acquires THAT node's
``swap_lock(node_key)`` around the single all-fields ``POST /youtube`` push and
the subsequent track resolution (task 4.2 wires this). With N nodes, up to N
distinct users resolve YouTube concurrently, each on its own node — true
per-user isolation up to the pool size (R4.2/R4.3). Beyond N concurrent distinct
users, requests share a node and serialize on its lock (still correct per
resolution).

Assignment policy (R4.3)
------------------------

* **Sticky** — once ``sub`` is assigned a node it keeps that node on every
  subsequent resolution, so a user's just-pushed credential is reused without a
  needless re-swap while that user stays active.
* **Least-loaded free-node pick** — a brand-new ``sub`` is placed on a node that
  currently has NO assigned user if one exists; otherwise on the node carrying
  the fewest assigned users, so load spreads evenly across the pool.
* **LRU reassignment** — when every node already carries users and a new ``sub``
  arrives, the pool reuses the node of the least-recently-used ``sub`` (evicting
  that stale assignment) rather than growing without bound. The evicted user is
  simply reassigned (its node re-swaps its credential) on its next resolution;
  nothing is closed, because the nodes themselves are a fixed, reused pool.

Config-driven size (default 1 == today)
---------------------------------------

The pool size comes from ``HELLODJ_LAVALINK_NODE_POOL`` (see
:func:`node_keys_from_env`). **The default is 1**, which makes the pool assign
every user to the single ``"default"`` node — byte-for-byte today's
serialized-correct behavior — so wiring the pool in is strictly additive and a
single-node deployment is unchanged.

This module is pure, dependency-light, and thread/async-safe (a single lock
guards the assignment maps), mirroring the shared ``SessionRegistry`` style so
the bot keeps it importable without ``boto3`` / ``hellodj_platform_logic``. It
holds NO credential material and never logs tokens: it maps an opaque ``sub`` to
an opaque node key and nothing else.

Requirements: 4.1, 4.2, 4.3
"""

from __future__ import annotations

import os
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Sequence

__all__ = [
    "DEFAULT_NODE_KEY",
    "DEFAULT_POOL_SIZE",
    "POOL_SIZE_ENV",
    "LavalinkNodePool",
    "node_keys_from_env",
    "pool_size_from_env",
]

#: Environment variable naming the Lavalink node pool. It accepts EITHER an
#: integer count (``"3"`` → three synthetic node keys ``node-0..node-2``) OR a
#: comma/whitespace-separated list of explicit node keys/URIs
#: (``"ll-a,ll-b,ll-c"``). Absent/blank → the single-node default.
POOL_SIZE_ENV = "HELLODJ_LAVALINK_NODE_POOL"

#: The single node key used when the pool size is 1. This is the SAME default
#: key ``YouTubeCredentialInjector.swap_lock`` uses today, so a size-1 pool is
#: indistinguishable from the current single-shared-node behavior.
DEFAULT_NODE_KEY = "default"

#: The default pool size when the env var is absent/blank/invalid: ONE node,
#: i.e. today's serialized-correct behavior (strictly additive).
DEFAULT_POOL_SIZE = 1


def pool_size_from_env(env: dict[str, str] | None = None) -> int:
    """Return the configured node-pool size (>= 1), defaulting to 1.

    Reads :data:`POOL_SIZE_ENV`. A bare integer is that many nodes; an explicit
    node-key list is as many nodes as it has entries. Anything absent, blank,
    non-positive, or unparseable falls back to :data:`DEFAULT_POOL_SIZE` (1), so
    a misconfiguration degrades to today's single-node behavior rather than
    breaking YouTube.
    """
    return len(node_keys_from_env(env))


def node_keys_from_env(env: dict[str, str] | None = None) -> list[str]:
    """Return the list of node keys for the configured pool (never empty).

    * absent / blank            → ``["default"]`` (size 1 == today)
    * a positive integer ``N``  → ``["node-0", ..., "node-(N-1)"]``
      (with ``N == 1`` collapsing to ``["default"]`` so the single-node key is
      unchanged)
    * an explicit list          → the de-duplicated, order-preserving keys
      (e.g. ``"ll-a, ll-b"`` → ``["ll-a", "ll-b"]``)

    The result is always a non-empty list, so callers never have to special-case
    an empty pool.
    """
    raw = (env if env is not None else os.environ).get(POOL_SIZE_ENV, "")
    raw = (raw or "").strip()
    if not raw:
        return [DEFAULT_NODE_KEY]

    # Pure integer → synthetic node keys.
    if _is_int(raw):
        count = int(raw)
        if count <= 1:
            return [DEFAULT_NODE_KEY]
        return [f"node-{i}" for i in range(count)]

    # Otherwise treat as a comma/whitespace-separated list of explicit keys.
    keys = _dedupe([tok for tok in _split_tokens(raw) if tok])
    if not keys:
        return [DEFAULT_NODE_KEY]
    return keys


def _is_int(text: str) -> bool:
    """Return whether ``text`` is a plain (optionally signed) integer literal."""
    body = text[1:] if text[:1] in "+-" else text
    return body.isdigit()


def _split_tokens(raw: str) -> list[str]:
    """Split an explicit node list on commas and surrounding whitespace."""
    return [tok.strip() for tok in raw.replace("\n", ",").split(",")]


def _dedupe(items: Sequence[str]) -> list[str]:
    """Return ``items`` with duplicates removed, preserving first-seen order."""
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


class LavalinkNodePool:
    """Map an owning user's ``sub`` to one node key from a fixed pool (R4.3).

    The pool owns a fixed, ordered list of node keys (never empty; size 1 ==
    today's ``"default"`` node). :meth:`assign` returns the node key an owner
    ``sub`` should use for its YouTube resolution; that key is passed to
    :meth:`YouTubeCredentialInjector.swap_lock` so the single all-fields
    ``POST /youtube`` push and the track resolution run under that node's lock.

    Assignment is **sticky** (a ``sub`` keeps its node), spreads new users onto
    the **least-loaded free node** first, and **reassigns the LRU** ``sub``'s
    node once every node is occupied — see the module docstring. The pool holds
    no credentials and closes nothing on reassignment (nodes are reused).

    Thread/async-safe: a single lock guards the assignment maps, so concurrent
    :meth:`assign` calls from the event loop or executor threads never corrupt
    the LRU ordering.
    """

    def __init__(
        self,
        node_keys: Sequence[str] | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Build a pool over ``node_keys`` (defaults to the env-configured set).

        Args:
            node_keys: The fixed node keys. Defaults to :func:`node_keys_from_env`
                (``["default"]`` when unset). De-duplicated, order-preserving; an
                empty sequence falls back to the single ``"default"`` node so the
                pool is never empty.
            clock: A monotonic clock (seconds) for LRU recency; injected for
                deterministic tests. Defaults to :func:`time.monotonic`.
        """
        keys = _dedupe([str(k) for k in node_keys]) if node_keys else []
        self._nodes: list[str] = keys or list(node_keys_from_env())
        self._clock = clock
        self._lock = threading.RLock()
        # sub -> node key (the sticky assignment).
        self._assigned: dict[str, str] = {}
        # sub -> last-used monotonic time, ordered least-recently-used first for
        # LRU reassignment.
        self._recency: OrderedDict[str, float] = OrderedDict()

    # ── introspection ──────────────────────────────────────────────────

    @property
    def size(self) -> int:
        """The number of nodes in the pool (>= 1)."""
        return len(self._nodes)

    @property
    def node_keys(self) -> list[str]:
        """A copy of the pool's node keys, in order."""
        return list(self._nodes)

    def assignments(self) -> dict[str, str]:
        """A snapshot mapping of currently-assigned ``sub`` → node key."""
        with self._lock:
            return dict(self._assigned)

    def node_of(self, sub: str) -> str | None:
        """Return the node currently assigned to ``sub`` without assigning one.

        Never mutates recency — a health probe must not count as a use.
        """
        with self._lock:
            return self._assigned.get(sub)

    # ── core operation ─────────────────────────────────────────────────

    def assign(self, owner_sub: str) -> str:
        """Return the node key ``owner_sub`` should use, assigning one on miss.

        Sticky: an already-assigned ``sub`` gets its existing node back (and is
        marked most-recently-used). A new ``sub`` is placed on the least-loaded
        free node if one exists, else it reuses the least-recently-used ``sub``'s
        node (LRU reassignment). Always returns a node key from the pool.

        Args:
            owner_sub: The owning user's Cognito ``sub``.

        Returns:
            The node key for ``owner_sub`` (feeds ``swap_lock(node_key)``).
        """
        with self._lock:
            existing = self._assigned.get(owner_sub)
            if existing is not None:
                self._touch_locked(owner_sub)
                return existing

            node = self._pick_free_node_locked()
            if node is None:
                node = self._reassign_lru_node_locked()
            self._assigned[owner_sub] = node
            self._touch_locked(owner_sub)
            return node

    def release(self, owner_sub: str) -> bool:
        """Drop ``owner_sub``'s assignment (e.g. on idle reclamation).

        A later :meth:`assign` for the same ``sub`` picks a node fresh. Returns
        ``True`` when an assignment was present and removed.
        """
        with self._lock:
            self._recency.pop(owner_sub, None)
            return self._assigned.pop(owner_sub, None) is not None

    # ── internal helpers (all called under self._lock) ─────────────────

    def _touch_locked(self, sub: str) -> None:
        """Mark ``sub`` most-recently-used (moves it to the end of the order)."""
        self._recency[sub] = self._clock()
        self._recency.move_to_end(sub, last=True)

    def _load_by_node_locked(self) -> dict[str, int]:
        """Return the count of assigned users per node key (zero-filled)."""
        load: dict[str, int] = {node: 0 for node in self._nodes}
        for node in self._assigned.values():
            if node in load:
                load[node] += 1
        return load

    def _pick_free_node_locked(self) -> str | None:
        """Return the least-loaded node, or ``None`` if every node has a user.

        Prefers a node with ZERO assigned users; when none is empty this returns
        ``None`` so the caller falls back to LRU reassignment (rather than
        piling a brand-new user onto an already-shared node while a stale
        assignment could be reused instead).
        """
        load = self._load_by_node_locked()
        # First-seen order among equally-loaded nodes keeps assignment stable.
        best_node: str | None = None
        best_load = -1
        for node in self._nodes:
            n = load[node]
            if n == 0:
                return node
            if best_node is None or n < best_load:
                best_node = node
                best_load = n
        # Every node carries at least one user → signal LRU reassignment.
        return None

    def _reassign_lru_node_locked(self) -> str:
        """Evict the LRU ``sub`` and return its freed node for reuse.

        Called only when every node already carries a user. The least-recently-
        used ``sub`` loses its assignment (it will pick a node fresh on its next
        resolution); its node is handed to the incoming ``sub``. Nodes are never
        closed — they are a fixed, reused pool.
        """
        # Least-recently-used sub is at the FRONT of the recency order.
        for stale_sub in list(self._recency.keys()):
            node = self._assigned.get(stale_sub)
            if node is not None:
                self._recency.pop(stale_sub, None)
                del self._assigned[stale_sub]
                return node
        # Defensive fallback (recency/assigned out of sync): reuse first node.
        return self._nodes[0]

"""Task 4.1 — ``LavalinkNodePool`` unit + property tests.

Covers the node pool that maps an owning user's Cognito ``sub`` to one Lavalink
node key from a fixed pool (``lavalink_node_pool.LavalinkNodePool``), behind the
existing ``YouTubeCredentialInjector.swap_lock(node_key)`` seam. No live AWS /
Lavalink — the pool is pure assignment logic.

What is asserted (Requirements 4.1, 4.3):

* **Config parsing / default size 1 (R4.1):** ``HELLODJ_LAVALINK_NODE_POOL``
  absent/blank/``"1"`` → the single ``"default"`` node (byte-for-byte today's
  serialized behavior); an integer ``N`` → ``N`` synthetic keys; an explicit
  list → those keys de-duplicated in order.
* **Sticky assignment (R4.3):** a ``sub`` keeps its node across repeated
  ``assign`` calls.
* **Least-loaded free-node pick (R4.3):** distinct subs fill empty nodes first,
  spreading one-per-node up to the pool size before doubling up.
* **LRU reassignment (R4.3):** once every node is occupied, a new ``sub`` reuses
  the least-recently-used ``sub``'s node.
* **Size-1 == default:** every sub maps to ``"default"`` (== today).
* **Property tests (hypothesis):** across random sub sequences the pool never
  returns a node outside its fixed set, stickiness holds within a stable working
  set, and with pool size N the first N distinct subs land on N DISTINCT nodes
  (the concurrency-up-to-N isolation guarantee, R4.2/R4.3).

Bare imports rely on ``bot/playback`` being on ``sys.path`` (see ``conftest``).

Validates: Requirements 4.1, 4.3
"""

from __future__ import annotations

import threading

from hypothesis import given, settings
from hypothesis import strategies as st

from lavalink_node_pool import (
    DEFAULT_NODE_KEY,
    DEFAULT_POOL_SIZE,
    LavalinkNodePool,
    node_keys_from_env,
    pool_size_from_env,
)


class _FakeClock:
    """A deterministic monotonic clock; ``tick()`` advances it by one second."""

    def __init__(self) -> None:
        self._t = 0.0

    def __call__(self) -> float:
        return self._t

    def tick(self, dt: float = 1.0) -> None:
        self._t += dt


# ── config parsing / default size ──────────────────────────────────────


class TestConfig:
    def test_absent_defaults_to_single_default_node(self):
        assert node_keys_from_env({}) == [DEFAULT_NODE_KEY]
        assert pool_size_from_env({}) == DEFAULT_POOL_SIZE == 1

    def test_blank_defaults_to_single_default_node(self):
        assert node_keys_from_env({"HELLODJ_LAVALINK_NODE_POOL": "   "}) == [
            DEFAULT_NODE_KEY
        ]

    def test_size_one_collapses_to_default_key(self):
        # "1" must be indistinguishable from today's single shared node.
        assert node_keys_from_env({"HELLODJ_LAVALINK_NODE_POOL": "1"}) == [
            DEFAULT_NODE_KEY
        ]

    def test_integer_count_makes_synthetic_keys(self):
        assert node_keys_from_env({"HELLODJ_LAVALINK_NODE_POOL": "3"}) == [
            "node-0",
            "node-1",
            "node-2",
        ]
        assert pool_size_from_env({"HELLODJ_LAVALINK_NODE_POOL": "3"}) == 3

    def test_explicit_list_is_deduped_in_order(self):
        keys = node_keys_from_env(
            {"HELLODJ_LAVALINK_NODE_POOL": "ll-a, ll-b ,ll-a, ll-c"}
        )
        assert keys == ["ll-a", "ll-b", "ll-c"]

    def test_zero_or_negative_falls_back_to_default(self):
        assert node_keys_from_env({"HELLODJ_LAVALINK_NODE_POOL": "0"}) == [
            DEFAULT_NODE_KEY
        ]
        assert node_keys_from_env({"HELLODJ_LAVALINK_NODE_POOL": "-4"}) == [
            DEFAULT_NODE_KEY
        ]


# ── assignment policy ──────────────────────────────────────────────────


class TestAssignment:
    def test_size_one_maps_every_sub_to_default(self):
        pool = LavalinkNodePool(["default"])
        assert pool.size == 1
        for sub in ("a", "b", "c"):
            assert pool.assign(sub) == "default"

    def test_sticky_assignment(self):
        pool = LavalinkNodePool(["n0", "n1", "n2"])
        first = pool.assign("alice")
        # Repeated assigns return the SAME node.
        assert pool.assign("alice") == first
        assert pool.assign("alice") == first
        assert pool.node_of("alice") == first

    def test_least_loaded_fills_empty_nodes_first(self):
        pool = LavalinkNodePool(["n0", "n1", "n2"])
        nodes = {pool.assign(sub) for sub in ("a", "b", "c")}
        # Three distinct subs onto a 3-node pool → one per node, all distinct.
        assert nodes == {"n0", "n1", "n2"}

    def test_fourth_sub_reuses_lru_node(self):
        clock = _FakeClock()
        pool = LavalinkNodePool(["n0", "n1", "n2"], clock=clock)
        # Assign a, b, c one-per-node, each at a distinct time.
        na = pool.assign("a")
        clock.tick()
        pool.assign("b")
        clock.tick()
        pool.assign("c")
        clock.tick()
        # "a" is now the least-recently-used → the 4th sub reuses a's node.
        d_node = pool.assign("d")
        assert d_node == na
        # a's assignment was evicted; it re-picks on next assign.
        assert pool.node_of("a") is None

    def test_touch_updates_recency_so_lru_victim_changes(self):
        clock = _FakeClock()
        pool = LavalinkNodePool(["n0", "n1"], clock=clock)
        a_node = pool.assign("a")
        clock.tick()
        b_node = pool.assign("b")
        clock.tick()
        # Re-touch "a" so "b" becomes the LRU victim instead.
        pool.assign("a")
        clock.tick()
        c_node = pool.assign("c")
        # c reuses b's freed node (b was LRU after a's re-touch), not a's.
        assert c_node == b_node
        assert c_node != a_node
        # b was evicted; a kept its node.
        assert pool.node_of("b") is None
        assert pool.node_of("a") == a_node

    def test_release_drops_assignment(self):
        pool = LavalinkNodePool(["n0", "n1"])
        pool.assign("a")
        assert pool.release("a") is True
        assert pool.node_of("a") is None
        assert pool.release("a") is False
        # Re-assign works cleanly afterwards.
        assert pool.assign("a") in {"n0", "n1"}

    def test_empty_node_keys_falls_back_to_default(self):
        pool = LavalinkNodePool([])
        assert pool.node_keys == [DEFAULT_NODE_KEY]
        assert pool.assign("a") == DEFAULT_NODE_KEY


# ── property-based tests ────────────────────────────────────────────────

_SUB = st.text(
    alphabet=st.characters(min_codepoint=97, max_codepoint=122), min_size=1, max_size=4
)


class TestNodePoolProperties:
    @settings(max_examples=200, deadline=None)
    @given(size=st.integers(min_value=1, max_value=6), subs=st.lists(_SUB, max_size=40))
    def test_assign_only_ever_returns_a_pool_node(self, size, subs):
        """Every assignment is a node key from the fixed pool — never invented."""
        keys = [f"n{i}" for i in range(size)]
        pool = LavalinkNodePool(keys)
        valid = set(pool.node_keys)
        for sub in subs:
            assert pool.assign(sub) in valid

    @settings(max_examples=200, deadline=None)
    @given(size=st.integers(min_value=2, max_value=6), subs=st.lists(_SUB, min_size=1, max_size=20))
    def test_sticky_within_working_set(self, size, subs):
        """A sub re-assigned immediately returns the same node (stickiness).

        Immediately re-assigning the just-assigned sub can never trigger LRU
        eviction of itself, so the node is stable.
        """
        keys = [f"n{i}" for i in range(size)]
        pool = LavalinkNodePool(keys)
        for sub in subs:
            first = pool.assign(sub)
            assert pool.assign(sub) == first

    @settings(max_examples=200, deadline=None)
    @given(size=st.integers(min_value=1, max_value=6))
    def test_first_n_distinct_subs_land_on_n_distinct_nodes(self, size):
        """The first N distinct subs occupy N DISTINCT nodes (R4.2/R4.3).

        This is the concurrency-up-to-pool-size isolation guarantee: with N
        nodes, up to N distinct users each get their own node.
        """
        keys = [f"n{i}" for i in range(size)]
        pool = LavalinkNodePool(keys)
        assigned = {pool.assign(f"user-{i}") for i in range(size)}
        assert len(assigned) == size
        assert assigned == set(keys)

    @settings(max_examples=100, deadline=None)
    @given(subs=st.lists(_SUB, min_size=1, max_size=30))
    def test_size_one_pool_always_returns_default(self, subs):
        """A size-1 pool maps every sub to the single default node (== today)."""
        pool = LavalinkNodePool(["default"])
        for sub in subs:
            assert pool.assign(sub) == "default"


class TestThreadSafety:
    def test_concurrent_assign_never_corrupts_maps(self):
        """Concurrent assigns from many threads stay consistent + sticky.

        Each thread assigns its own sub repeatedly; the returned node must be
        stable per sub and always a valid pool node. Exercises the internal lock.
        """
        pool = LavalinkNodePool([f"n{i}" for i in range(4)])
        valid = set(pool.node_keys)
        results: dict[str, set[str]] = {}
        lock = threading.Lock()

        def worker(sub: str) -> None:
            seen: set[str] = set()
            for _ in range(50):
                node = pool.assign(sub)
                assert node in valid
                seen.add(node)
            with lock:
                results[sub] = seen

        threads = [threading.Thread(target=worker, args=(f"s{i}",)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # With 4 subs and 4 nodes, no LRU eviction occurs, so each sub is sticky
        # to exactly one node throughout.
        for sub, seen in results.items():
            assert len(seen) == 1, f"{sub} saw multiple nodes: {seen}"

"""Task 4.3 — YouTube node-pool concurrency + isolation tests (examples).

The designated test task for section 4 (the YouTube / YouTube Music multi-tenant
node pool). This module holds the example/unit tests; the Hypothesis P1
isolation properties live in ``test_youtube_node_pool_props`` and the shared
in-memory fakes in ``youtube_node_pool_fakes`` (split to stay under the 500-line
ceiling).

It exercises the FULLY WIRED path — the
:class:`~playback.lavalink_node_pool.LavalinkNodePool` + ``OwnerLookup`` +
:class:`GuildCredentialResolver` (backed by the unified DynamoDB
``DynamoCredentialResolver``) driving
:class:`YouTubeCredentialInjector.node_key_for_guild` / ``swap_lock`` /
``inject_for_guild`` — with in-memory fakes for the DynamoDB store, the
guild→owner lookup, the KMS decrypt, and the Lavalink ``POST /youtube`` push. No
live AWS / Lavalink.

What is asserted (Requirements 4.1, 4.2, 4.4, 6.1):

* **P1 isolation up to pool size N (R4.1/R4.2/R6.1):** up to N distinct owner
  subs each resolve YouTube on a DISTINCT node; a request for owner A never
  lands on the node holding owner B's just-pushed credentials. Sticky: the same
  owner → the same node across calls. LRU reassignment when the pool is full.
* **Per-resolution correctness under the held ``swap_lock`` (R4.1):** the single
  all-fields ``POST /youtube`` (refreshToken + poToken + visitorData TOGETHER)
  is applied per resolution and, on a SHARED node, is never split or interleaved
  with another owner's swap — asyncio concurrency exercises the per-node lock.
* **Size-1 pool == today's serialized-correct behavior (R4.4, strictly
  additive):** every guild collapses onto the single ``DEFAULT_NODE_KEY`` node.
* **No-credential guild → global path (R4.4):** ``node_key_for_guild`` returns
  ``None`` → the caller acquires NO lock and performs NO swap.

Bare imports rely on ``bot/playback`` being on ``sys.path`` (see ``conftest``).

Validates: Requirements 4.1, 4.2, 4.4, 6.1
"""

from __future__ import annotations

import asyncio

import pytest

from guild_credentials import sourcecred_sk, user_pk
from lavalink_node_pool import DEFAULT_NODE_KEY, LavalinkNodePool
from youtube_node_pool_fakes import (
    FakeLavalink,
    FakeOwners,
    FakeStore,
    StepClock,
    build_injector,
    enc_item,
    guild_items,
)

# ── P1 isolation: distinct owners → distinct nodes, sticky, LRU ─────────


class TestNodeIsolationAndAssignment:
    def test_up_to_pool_size_distinct_owners_on_distinct_nodes(self):
        """N distinct owners each resolve YouTube on a DISTINCT node (R4.2/R6.1)."""
        specs = {
            "g1": ("owner-A", "1//A", "POA", "VDA"),
            "g2": ("owner-B", "1//B", "POB", "VDB"),
            "g3": ("owner-C", "1//C", "POC", "VDC"),
        }
        store, owners = guild_items(specs)
        pool = LavalinkNodePool(["n0", "n1", "n2"])
        injector = build_injector(store, owners, pool, FakeLavalink().push)

        keys = {gid: injector.node_key_for_guild(gid, "youtube") for gid in specs}

        # Three credentialed guilds owned by three distinct subs on a 3-node pool
        # → three DISTINCT real pool nodes.
        assert set(keys.values()) == {"n0", "n1", "n2"}
        assert len(set(keys.values())) == 3

    def test_request_for_a_never_lands_on_bs_node(self):
        """A request for owner A never returns owner B's node (R6.1)."""
        specs = {
            "g1": ("owner-A", "1//A", "POA", "VDA"),
            "g2": ("owner-B", "1//B", "POB", "VDB"),
        }
        store, owners = guild_items(specs)
        pool = LavalinkNodePool(["n0", "n1"])
        injector = build_injector(store, owners, pool, FakeLavalink().push)

        node_a = injector.node_key_for_guild("g1", "youtube")
        node_b = injector.node_key_for_guild("g2", "youtube")

        assert node_a != node_b
        # Re-resolving A never drifts onto B's node.
        for _ in range(5):
            assert injector.node_key_for_guild("g1", "youtube") == node_a
            assert injector.node_key_for_guild("g1", "youtube") != node_b

    def test_sticky_same_owner_same_node_across_calls(self):
        """Sticky: the same owner resolves to the same node every time (R4.3)."""
        specs = {"g1": ("owner-A", "1//A", "POA", "VDA")}
        store, owners = guild_items(specs)
        pool = LavalinkNodePool(["n0", "n1", "n2"])
        injector = build_injector(store, owners, pool, FakeLavalink().push)

        first = injector.node_key_for_guild("g1", "youtube")
        for _ in range(10):
            assert injector.node_key_for_guild("g1", "youtube") == first

    def test_two_guilds_same_owner_share_one_node(self):
        """Two guilds owned by the SAME sub collapse onto one node (sub-keyed)."""
        specs = {
            "g1": ("owner-A", "1//A", "POA", "VDA"),
            "g2": ("owner-A", "1//A", "POA", "VDA"),
        }
        store, owners = guild_items(specs)
        pool = LavalinkNodePool(["n0", "n1", "n2"])
        injector = build_injector(store, owners, pool, FakeLavalink().push)

        assert injector.node_key_for_guild(
            "g1", "youtube"
        ) == injector.node_key_for_guild("g2", "youtube")

    def test_lru_reassignment_when_pool_full(self):
        """A new owner beyond pool size reuses the LRU owner's node (R4.3)."""
        clock = StepClock()
        specs = {
            "g1": ("owner-A", "1//A", "POA", "VDA"),
            "g2": ("owner-B", "1//B", "POB", "VDB"),
        }
        store, owners = guild_items(specs)
        # Add a third credentialed guild whose owner triggers LRU eviction.
        owners.mapping["g3"] = "owner-C"
        store.items[(user_pk("owner-C"), sourcecred_sk("youtube"))] = enc_item(
            "1//C", "POC", "VDC"
        )
        pool = LavalinkNodePool(["n0", "n1"], clock=clock)
        injector = build_injector(store, owners, pool, FakeLavalink().push)

        node_a = injector.node_key_for_guild("g1", "youtube")
        clock.tick()
        injector.node_key_for_guild("g2", "youtube")  # fills the 2nd node
        clock.tick()
        # owner-A is now LRU → owner-C reuses A's node.
        node_c = injector.node_key_for_guild("g3", "youtube")
        assert node_c == node_a
        # owner-A's assignment was evicted; it re-picks fresh on next resolution.
        assert pool.node_of("owner-A") is None


# ── size-1 pool == today's serialized-correct behavior ──────────────────


class TestSizeOnePoolCollapsesToDefault:
    def test_every_guild_uses_default_node(self):
        """Size-1 pool → every credentialed guild collapses to DEFAULT_NODE_KEY."""
        specs = {
            "g1": ("owner-A", "1//A", "POA", "VDA"),
            "g2": ("owner-B", "1//B", "POB", "VDB"),
            "g3": ("owner-C", "1//C", "POC", "VDC"),
        }
        store, owners = guild_items(specs)
        pool = LavalinkNodePool([DEFAULT_NODE_KEY])
        injector = build_injector(store, owners, pool, FakeLavalink().push)

        for gid in specs:
            assert injector.node_key_for_guild(gid, "youtube") == DEFAULT_NODE_KEY

    @pytest.mark.asyncio
    async def test_size_one_serialized_correct_per_resolution(self):
        """On the single node, each resolution pushes exactly its guild's creds."""
        specs = {
            "g1": ("owner-A", "1//A", "POA", "VDA"),
            "g2": ("owner-B", "1//B", "POB", "VDB"),
        }
        store, owners = guild_items(specs)
        lava = FakeLavalink()
        pool = LavalinkNodePool([DEFAULT_NODE_KEY])
        injector = build_injector(store, owners, pool, lava.push)

        for gid, (_sub, refresh, pot, visitor) in specs.items():
            key = injector.node_key_for_guild(gid, "youtube")
            assert key == DEFAULT_NODE_KEY
            lava.pushes.clear()
            async with injector.swap_lock(key):
                swapped = await injector.inject_for_guild(gid, "youtube")
            assert swapped is True
            assert len(lava.pushes) == 1
            pushed = lava.pushes[0]
            assert pushed["refreshToken"] == refresh
            assert pushed["poToken"] == pot
            assert pushed["visitorData"] == visitor


# ── no-credential guild → global path preserved (R4.4) ──────────────────


class TestNoCredentialGuildGlobalPath:
    def test_node_key_none_when_no_credential(self):
        """No unified item + no secret → node_key_for_guild is None (no swap)."""
        # Owner recorded but no SOURCECRED item, and no legacy secret.
        owners = FakeOwners({"g9": "owner-X"})
        store = FakeStore({})
        pool = LavalinkNodePool(["n0", "n1"])
        injector = build_injector(store, owners, pool, FakeLavalink().push)

        assert injector.node_key_for_guild("g9", "youtube") is None

    def test_node_key_none_when_no_owner(self):
        """No recorded owner + no secret → None (no swap, global path, R4.4)."""
        owners = FakeOwners({})  # guild g9 has no owner
        store = FakeStore({})
        pool = LavalinkNodePool(["n0", "n1"])
        injector = build_injector(store, owners, pool, FakeLavalink().push)

        assert injector.node_key_for_guild("g9", "youtube") is None

    @pytest.mark.asyncio
    async def test_no_swap_leaves_global_creds_intact(self):
        """A no-credential guild performs NO swap → global node state untouched."""
        owners = FakeOwners({"g9": "owner-X"})
        store = FakeStore({})
        lava = FakeLavalink()
        # Simulate the global push having already loaded creds on the node.
        global_payload = {
            "skipInitialization": False,
            "refreshToken": "GLOBAL",
            "poToken": "GPOT",
            "visitorData": "GVD",
        }
        await lava.push(global_payload)
        pool = LavalinkNodePool(["n0", "n1"])
        injector = build_injector(store, owners, pool, lava.push)

        key = injector.node_key_for_guild("g9", "youtube")
        assert key is None  # → caller takes NO lock, does NO swap
        swapped = await injector.inject_for_guild("g9", "youtube")
        assert swapped is False
        # Global creds still on the node; only the global push ever happened.
        assert lava.current == global_payload
        assert len(lava.pushes) == 1


# ── within a node: per-resolution correctness under the held swap_lock ──


class TestPerNodeLockSerialization:
    @pytest.mark.asyncio
    async def test_shared_node_concurrent_swaps_never_interleave(self):
        """Two owners forced onto ONE shared node serialize under its lock (R4.1).

        With a size-1 pool both owners share ``DEFAULT_NODE_KEY``. Each task
        holds ``swap_lock(key)`` across push + a read-back of the node's
        ``current`` state; a push delay widens the interleaving window. Under the
        lock, the creds a task reads back are ALWAYS its own — never the other
        owner's — proving the single all-fields payload is applied per resolution
        and not split/interleaved.
        """
        specs = {
            "g1": ("owner-A", "1//A", "POA", "VDA"),
            "g2": ("owner-B", "1//B", "POB", "VDB"),
        }
        store, owners = guild_items(specs)
        lava = FakeLavalink(delay=0.01)  # shared single node
        pool = LavalinkNodePool([DEFAULT_NODE_KEY])
        injector = build_injector(store, owners, pool, lava.push)

        observed: dict[str, dict[str, object] | None] = {}

        async def resolve(gid: str) -> None:
            key = injector.node_key_for_guild(gid, "youtube")
            async with injector.swap_lock(key):
                await injector.inject_for_guild(gid, "youtube")
                # Under the held lock, the node's current creds must be ours.
                observed[gid] = dict(lava.current) if lava.current else None

        # Run both concurrently many times to exercise interleaving.
        for _ in range(10):
            observed.clear()
            await asyncio.gather(resolve("g1"), resolve("g2"))
            assert observed["g1"]["refreshToken"] == "1//A"
            assert observed["g1"]["poToken"] == "POA"
            assert observed["g2"]["refreshToken"] == "1//B"
            assert observed["g2"]["poToken"] == "POB"

    @pytest.mark.asyncio
    async def test_distinct_nodes_have_independent_locks(self):
        """Distinct owners on distinct nodes get DISTINCT locks (concurrency, R4.2)."""
        specs = {
            "g1": ("owner-A", "1//A", "POA", "VDA"),
            "g2": ("owner-B", "1//B", "POB", "VDB"),
        }
        store, owners = guild_items(specs)
        pool = LavalinkNodePool(["n0", "n1"])
        injector = build_injector(store, owners, pool, FakeLavalink().push)

        key_a = injector.node_key_for_guild("g1", "youtube")
        key_b = injector.node_key_for_guild("g2", "youtube")
        assert key_a != key_b
        # Distinct node keys → distinct lock objects → true concurrency.
        assert injector.swap_lock(key_a) is not injector.swap_lock(key_b)
        # Same node key → the SAME lock (serializes owners sharing a node).
        assert injector.swap_lock(key_a) is injector.swap_lock(key_a)

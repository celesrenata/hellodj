"""Task 4.3 — YouTube node-pool P1 isolation property tests (Hypothesis).

The property-based half of the section-4 test task. It complements the
example/unit tests in ``test_youtube_node_pool`` and reuses the shared in-memory
fakes in ``youtube_node_pool_fakes`` (split to stay under the 500-line ceiling).

Property 1 — No cross-user leakage (design P1, R4.1/R4.2/R6.1). Across random
sets of distinct owners and random pool sizes, the first ``min(N, #owners)``
distinct owners land on that many DISTINCT nodes (concurrency up to the pool
size), and under each node's ``swap_lock`` a guild pushes EXACTLY its own
``{refreshToken, poToken, visitorData}`` (never another owner's). A size-1 pool
collapses every owner onto the single default node (== today).

Bare imports rely on ``bot/playback`` being on ``sys.path`` (see ``conftest``).

Validates: Requirements 4.1, 4.2, 6.1
"""

from __future__ import annotations

import asyncio

from hypothesis import given, settings
from hypothesis import strategies as st

from guild_credentials import youtube_oauth_payload
from lavalink_node_pool import DEFAULT_NODE_KEY, LavalinkNodePool
from youtube_node_pool_fakes import FakeLavalink, build_injector, guild_items

_SUB = st.text(
    alphabet=st.characters(min_codepoint=97, max_codepoint=122), min_size=1, max_size=6
)


@st.composite
def _owner_specs(draw):
    """Build ``{gid: (owner_sub, refresh, pot, visitor)}`` for distinct owners.

    Each owner is distinct (distinct sub) with distinct credential material, so
    a leak between owners is detectable by the pushed refresh token.
    """
    subs = draw(st.lists(_SUB, min_size=1, max_size=6, unique=True))
    specs: dict[str, tuple[str, str, str, str]] = {}
    for i, sub in enumerate(subs):
        specs[f"g{i}"] = (sub, f"R-{sub}", f"P-{sub}", f"V-{sub}")
    return specs


class TestP1IsolationProperty:
    @settings(max_examples=150, deadline=None)
    @given(size=st.integers(min_value=1, max_value=6), specs=_owner_specs())
    def test_first_n_distinct_owners_on_distinct_nodes(self, size, specs):
        """First min(N, #owners) distinct owners occupy distinct nodes (R4.2/R6.1)."""
        store, owners = guild_items(specs)
        keys = [f"n{i}" for i in range(size)]
        pool = LavalinkNodePool(keys)
        injector = build_injector(store, owners, pool, FakeLavalink().push)

        gids = list(specs.keys())
        assigned = [injector.node_key_for_guild(gid, "youtube") for gid in gids]

        # Every assignment is a real pool node.
        assert all(k in set(keys) for k in assigned)
        # The first min(size, len(gids)) owners land on that many DISTINCT nodes.
        n = min(size, len(gids))
        assert len(set(assigned[:n])) == n

    @settings(max_examples=150, deadline=None)
    @given(size=st.integers(min_value=1, max_value=6), specs=_owner_specs())
    def test_each_owner_pushes_exactly_its_own_creds(self, size, specs):
        """Under the per-node lock, each guild pushes EXACTLY its own creds (R6.1)."""
        store, owners = guild_items(specs)
        keys = [f"n{i}" for i in range(size)]
        pool = LavalinkNodePool(keys)
        lava = FakeLavalink()
        injector = build_injector(store, owners, pool, lava.push)

        async def run() -> None:
            for gid, (_sub, refresh, pot, visitor) in specs.items():
                key = injector.node_key_for_guild(gid, "youtube")
                assert key in set(keys)
                lava.pushes.clear()
                async with injector.swap_lock(key):
                    swapped = await injector.inject_for_guild(gid, "youtube")
                assert swapped is True
                assert len(lava.pushes) == 1
                pushed = lava.pushes[0]
                # Exactly this guild's creds — never another owner's.
                assert pushed == youtube_oauth_payload(
                    {
                        "oauth_refresh_token": refresh,
                        "pot_token": pot,
                        "pot_visitor_data": visitor,
                    },
                    skip_initialization=False,
                )
                assert pushed["refreshToken"] == refresh
                for other_gid, (_os, oref, _op, _ov) in specs.items():
                    if other_gid != gid and oref != refresh:
                        assert pushed["refreshToken"] != oref

        asyncio.run(run())

    @settings(max_examples=100, deadline=None)
    @given(specs=_owner_specs())
    def test_size_one_pool_all_on_default(self, specs):
        """Size-1 pool → every owner collapses to the default node (== today)."""
        store, owners = guild_items(specs)
        pool = LavalinkNodePool([DEFAULT_NODE_KEY])
        injector = build_injector(store, owners, pool, FakeLavalink().push)

        for gid in specs:
            assert injector.node_key_for_guild(gid, "youtube") == DEFAULT_NODE_KEY

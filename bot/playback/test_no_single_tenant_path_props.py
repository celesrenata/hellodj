"""Task 5.3 — YouTube half of the no-single-tenant-path property test (P5).

Property 5 (design): *No code path selects or uses a credential without a
resolved owning* ``sub``\\ *; there is no ambient/default account any request can
fall back to.* This module is the YouTube/YouTube-Music provider's cross-cutting
assertion of that invariant (the Spotify and Tidal halves live in
``spotify-stream/tests/test_no_single_tenant_path_property.py`` and
``tidal-stream/tests/test_no_single_tenant_path_property.py`` — each asserts the
SAME invariant against its own router/resolver seam, because the three providers
live in separate import trees that cannot be imported from one module cleanly).

YouTube framing of P5 (R4.4 vs R10.5)
-------------------------------------

YouTube is the one provider where a NON-credentialed guild is *intentionally*
allowed to fall through to the untouched GLOBAL credential-store push (R4.4) —
that is the Platform_Owner's ambient credential, NOT a per-user single-tenant
fallback, and it is explicitly permitted. So the P5 assertion here is precisely:

    No path serves a **CONNECTED** guild's request using another user's or an
    ambient credential without a resolved owning ``sub``.

Concretely, the behavioral properties are:

* **A connected guild's credential is a function of its resolved owning** ``sub``
  **(R10.4).** :meth:`YouTubeCredentialInjector.node_key_for_guild` for a
  credentialed guild resolves the guild's ``owner_sub`` and assigns a pool node
  keyed by THAT sub; the pushed ``{refreshToken, poToken, visitorData}`` is
  exactly that owner's — never another owner's, never an ambient one.
* **A guild with NO connected credential takes the global path, NOT a per-user
  fallback (R4.4, R10.5).** ``node_key_for_guild`` returns ``None`` (→ NO swap →
  untouched global push); it never borrows another connected guild's node or
  credential. This is the ONLY sanctioned ambient path and it never serves a
  connected guild.

All fakes are in-memory (no boto3 / live AWS / Lavalink). Bare imports rely on
``bot/playback`` being on ``sys.path`` (see ``conftest``).

Validates: Requirements 10.4, 10.5
"""

from __future__ import annotations

import asyncio

from hypothesis import given, settings
from hypothesis import strategies as st

from guild_credentials import youtube_oauth_payload
from lavalink_node_pool import DEFAULT_NODE_KEY, LavalinkNodePool
from youtube_node_pool_fakes import (
    FakeLavalink,
    build_injector,
    guild_items,
)

_SUB = st.text(
    alphabet=st.characters(min_codepoint=97, max_codepoint=122), min_size=1, max_size=6
)


@st.composite
def _owner_specs(draw):
    """Build ``{gid: (owner_sub, refresh, pot, visitor)}`` for distinct owners.

    Each owner is distinct with distinct credential material, so a leak between
    owners (or an ambient substitution) is detectable by the pushed refresh
    token.
    """
    subs = draw(st.lists(_SUB, min_size=1, max_size=6, unique=True))
    specs: dict[str, tuple[str, str, str, str]] = {}
    for i, sub in enumerate(subs):
        specs[f"g{i}"] = (sub, f"R-{sub}", f"P-{sub}", f"V-{sub}")
    return specs


class TestP5NoSingleTenantPath:
    @settings(max_examples=100, deadline=None)
    @given(size=st.integers(min_value=1, max_value=6), specs=_owner_specs())
    def test_connected_guild_credential_is_a_function_of_resolved_owner(
        self, size, specs
    ):
        """Each connected guild pushes EXACTLY its resolved owner's creds (R10.4).

        No connected guild's resolution ever uses another owner's credential or
        an ambient one: the pushed refresh token equals that guild's own owner's
        and differs from every other distinct owner's.
        """
        store, owners = guild_items(specs)
        keys = [f"n{i}" for i in range(size)]
        pool = LavalinkNodePool(keys)
        lava = FakeLavalink()
        injector = build_injector(store, owners, pool, lava.push)

        async def run() -> None:
            for gid, (_sub, refresh, pot, visitor) in specs.items():
                key = injector.node_key_for_guild(gid, "youtube")
                # A connected guild ALWAYS resolves to a real pool node keyed by
                # its owner — never None (never the global/ambient path).
                assert key in set(keys)
                lava.pushes.clear()
                async with injector.swap_lock(key):
                    swapped = await injector.inject_for_guild(gid, "youtube")
                assert swapped is True
                assert len(lava.pushes) == 1
                pushed = lava.pushes[0]
                assert pushed == youtube_oauth_payload(
                    {
                        "oauth_refresh_token": refresh,
                        "pot_token": pot,
                        "pot_visitor_data": visitor,
                    },
                    skip_initialization=False,
                )
                # Exactly this owner's creds — never another owner's / ambient.
                assert pushed["refreshToken"] == refresh
                for other_gid, (_os, oref, _op, _ov) in specs.items():
                    if other_gid != gid and oref != refresh:
                        assert pushed["refreshToken"] != oref

        asyncio.run(run())

    @settings(max_examples=100, deadline=None)
    @given(size=st.integers(min_value=1, max_value=6), specs=_owner_specs())
    def test_uncredentialed_guild_takes_global_path_never_a_per_user_fallback(
        self, size, specs
    ):
        """A guild with NO connected credential → global path, not a fallback.

        ``node_key_for_guild`` returns ``None`` (NO swap → untouched global push,
        the ONLY sanctioned ambient path, R4.4), and crucially it never borrows a
        connected guild's node or credential (R10.5). No swap is performed.
        """
        # `specs` are the CONNECTED guilds; add a sibling guild with NO owner and
        # NO credential item — the uncredentialed case.
        store, owners = guild_items(specs)
        keys = [f"n{i}" for i in range(size)]
        pool = LavalinkNodePool(keys)
        lava = FakeLavalink()
        injector = build_injector(store, owners, pool, lava.push)

        async def run() -> None:
            # A guild absent from the owner map + store has no credential.
            uncredentialed = "no-cred-guild"
            key = injector.node_key_for_guild(uncredentialed, "youtube")
            # None → the caller performs NO swap → untouched global push (R4.4).
            assert key is None
            # Even attempting an inject performs NO push (no ambient borrow).
            lava.pushes.clear()
            swapped = await injector.inject_for_guild(uncredentialed, "youtube")
            assert swapped is False
            assert lava.pushes == []

        asyncio.run(run())

    @settings(max_examples=100, deadline=None)
    @given(specs=_owner_specs())
    def test_size_one_pool_every_connected_guild_on_default_never_a_peer(self, specs):
        """Size-1 pool: every connected guild collapses to the default node.

        The sanctioned single-node (== today) behavior routes every connected
        guild's own-credential swap through the ONE ``default`` node under its
        lock — this is per-resolution correct and never routes one guild's
        request onto a peer's per-user credential (R10.5). Even collapsed to one
        node, each guild still pushes EXACTLY its own owner's refresh token.
        """
        store, owners = guild_items(specs)
        pool = LavalinkNodePool([DEFAULT_NODE_KEY])
        lava = FakeLavalink()
        injector = build_injector(store, owners, pool, lava.push)

        async def run() -> None:
            for gid, (_sub, refresh, _pot, _visitor) in specs.items():
                key = injector.node_key_for_guild(gid, "youtube")
                assert key == DEFAULT_NODE_KEY
                lava.pushes.clear()
                async with injector.swap_lock(key):
                    swapped = await injector.inject_for_guild(gid, "youtube")
                assert swapped is True
                assert len(lava.pushes) == 1
                # Even on the shared default node, the swap is this guild's own.
                assert lava.pushes[0]["refreshToken"] == refresh

        asyncio.run(run())

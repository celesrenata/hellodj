"""Task 5.3 — Tidal half of the no-single-tenant-path property test (P5).

Property 5 (design): *No code path selects or uses a credential without a
resolved owning* ``sub``\\ *; there is no ambient/default account any request can
fall back to.* This module is the Tidal provider's cross-cutting assertion of
that invariant (the Spotify and YouTube halves live in
``spotify-stream/tests/test_no_single_tenant_path_property.py`` and
``bot/playback/test_no_single_tenant_path_props.py`` — each asserts the SAME
invariant against its own router/resolver seam, because the three providers live
in separate import trees that cannot be imported from one module cleanly).

The core of P5 is **behavioral**:

* **No resolved owner → observable failure, never a fallback (R10.5).** With
  ``owner_of`` returning ``None``, :meth:`TidalStreamRouter.client_for_guild`
  raises :class:`TidalCredentialUnavailableError(no_owner)` — it never builds or
  returns a client, and the registry stays empty (no ambient/default account is
  created or served).
* **Owner but unavailable credential → observable failure (R10.5).** Every
  ``CredentialUnavailable`` reason (and a resolved credential with no usable
  access token) fails the request with a specific non-secret reason; no other
  user's client is ever returned.
* **Every client is a function of the resolved owning** ``sub`` **(R10.4).** A
  request whose guild resolves to owner A gets a client whose token source reads
  A's credential; a DISTINCT guild resolving to owner B gets B's. Randomized
  across many guild/owner/credential-presence combinations, a request is served
  ONLY from its own resolved owner's token — never an ambient one.

Complementary **structural guard**: the single startup-bound streaming identity
the design replaced is gone — the ``user_sessions`` module exposes only the
per-``sub`` :data:`TidalSessionRegistry` + the read-only per-user token source,
and no module-level ``refresh_secret_id`` / default-account global.

All fakes are in-memory (no live AWS, no real Tidal backend).

Validates: Requirements 10.4, 10.5
"""

from __future__ import annotations

from hellodj_platform_logic.session_registry import SessionRegistry
from hellodj_platform_logic.types import SessionRegistryConfig
from hellodj_platform_logic.user_credential_resolver import CredentialUnavailable
from hypothesis import given, settings
from hypothesis import strategies as st

import tidal_stream.user_sessions as user_sessions_module
from tidal_stream.user_sessions import (
    PROVIDER_TIDAL,
    TidalCredentialUnavailableError,
    TidalStreamRouter,
    TidalUserClient,
)

# Disjoint alphabets so a generated guild id can never collide with a sub.
_SUB = st.text(alphabet="abcdefghijklmnopqrstuvwxyz", min_size=1, max_size=6)
_GUILD = st.text(alphabet="0123456789", min_size=1, max_size=6)

#: The four typed CredentialUnavailable reasons the resolver can return.
_UNAVAILABLE_REASONS = ("no_owner", "no_credential", "refresh_failed", "decrypt_failed")


class _Owners:
    """In-memory guild→owner ``sub`` lookup; unknown guild → ``None`` (no owner)."""

    def __init__(self, mapping: dict[str, str]) -> None:
        self._map = mapping

    def owner_of(self, guild_id: str) -> str | None:
        return self._map.get(str(guild_id))


class _Resolver:
    """In-memory resolver returning a preprogrammed result per (guild, provider).

    A missing key defaults to ``CredentialUnavailable(no_credential)``.
    """

    def __init__(self, results: dict[tuple[str, str], object]) -> None:
        self._results = results

    def resolve(self, guild_id, provider):
        return self._results.get(
            (str(guild_id), provider), CredentialUnavailable("no_credential")
        )

    def invalidate(self, guild_id, provider):  # pragma: no cover - unused here
        pass


class _FakeStreamer:
    """Fake Tidal streamer that echoes the token its source resolves.

    The token source is a real :class:`ReadOnlyTidalTokenSource`, so the token
    this streamer would use is a function of the resolved owning ``sub``'s
    credential — exactly what P5 requires (never an ambient token).
    """

    def __init__(self, token_source) -> None:
        self._token_source = token_source

    def token(self) -> str:
        return self._token_source.get_access_token()

    async def close(self) -> None:  # pragma: no cover - registry closer hook
        pass


def _make_router(owners_map, results, *, built):
    """Wire a router over in-memory fakes; ``built`` records every client build."""
    registry: SessionRegistry = SessionRegistry(
        SessionRegistryConfig(max_sessions=256, idle_timeout_seconds=900.0)
    )

    def streamer_factory(token_source):
        return _FakeStreamer(token_source)

    def _factory_wrapper(token_source):
        built.append(id(token_source))
        return streamer_factory(token_source)

    return TidalStreamRouter(
        _Owners(owners_map),
        _Resolver(results),
        registry,
        streamer_factory=_factory_wrapper,
        provider=PROVIDER_TIDAL,
    )


# ── Structural guard: the single startup-bound identity is gone ────────────────


def test_no_ambient_startup_bound_identity_in_module():
    """The single startup-bound ``refresh_secret_id`` identity has no residue.

    Guards against a regression that re-introduces a single ambient streaming
    identity any guild would fall back to (the single-tenant path P5 forbids).
    The module's only session container is the per-``sub`` registry.
    """
    module_globals = vars(user_sessions_module)
    assert "refresh_secret_id" not in module_globals
    assert "_refresh_secret_id" not in module_globals
    assert "default_client" not in module_globals
    assert "_client" not in module_globals
    assert "TidalSessionRegistry" in user_sessions_module.__all__
    assert "TidalStreamRouter" in user_sessions_module.__all__


# ── Behavioral P5: no credential use without a resolved owning sub ─────────────


@settings(max_examples=60, deadline=None)
@given(guild=_GUILD)
def test_no_owner_fails_observably_and_builds_no_client(guild):
    """A guild with NO resolved owner never yields a client (R10.5)."""
    built: list[int] = []
    router = _make_router({}, {}, built=built)  # empty owners → owner_of == None

    try:
        router.client_for_guild(guild)
    except TidalCredentialUnavailableError as exc:
        assert exc.reason == "no_owner"
    else:  # pragma: no cover - a returned client would be a P5 violation
        raise AssertionError("no-owner guild must not yield a client")

    assert built == []
    assert dict(router.registry.states()) == {}


@settings(max_examples=80, deadline=None)
@given(guild=_GUILD, sub=_SUB, reason=st.sampled_from(_UNAVAILABLE_REASONS))
def test_owned_but_unavailable_credential_fails_never_falls_back(guild, sub, reason):
    """An owned guild with an unavailable credential fails, never falls back (R10.5)."""
    built: list[int] = []
    results = {(guild, PROVIDER_TIDAL): CredentialUnavailable(reason)}
    router = _make_router({guild: sub}, results, built=built)

    try:
        router.client_for_guild(guild)
    except TidalCredentialUnavailableError as exc:
        # no_owner is impossible here (owner is present); every other reason
        # propagates verbatim from the resolver's failure.
        assert exc.reason == reason
    else:  # pragma: no cover - a client here would be a P5 violation
        raise AssertionError("unavailable credential must not yield a client")

    # No client was built (the factory never produced an ambient fallback).
    assert built == []


@settings(max_examples=60, deadline=None)
@given(guild=_GUILD, sub=_SUB)
def test_owned_credential_without_access_token_fails(guild, sub):
    """A resolved credential lacking an access token is unusable, not ambient."""
    built: list[int] = []
    # A resolved dict credential but with an EMPTY access token.
    results = {(guild, PROVIDER_TIDAL): {"access_token": ""}}
    router = _make_router({guild: sub}, results, built=built)

    try:
        router.client_for_guild(guild)
    except TidalCredentialUnavailableError as exc:
        assert exc.reason == "no_credential"
    else:  # pragma: no cover - a token-less credential must fail
        raise AssertionError("credential without access token must not stream")
    assert built == []


@settings(max_examples=50, deadline=None)
@given(
    pairs=st.lists(
        st.tuples(_GUILD, _SUB), min_size=1, max_size=8, unique_by=lambda t: t[0]
    ),
)
def test_every_client_is_a_function_of_the_resolved_owning_sub(pairs):
    """A request with a resolved sub gets THAT sub's token, never an ambient one.

    Randomized across many distinct guilds (owners may repeat — two guilds owned
    by the same user validly share ONE client), every served client's token
    source resolves the guild's OWN resolved owner's token, and the registry is
    keyed by exactly the set of resolved owning subs — no ambient/default key
    exists (R10.4).
    """
    owners_map = {gid: sub for gid, sub in pairs}
    # Each owning sub's credential carries a token that uniquely identifies it.
    results = {
        (gid, PROVIDER_TIDAL): {"access_token": f"TOKEN::{sub}"}
        for gid, sub in owners_map.items()
    }
    built: list[int] = []
    router = _make_router(owners_map, results, built=built)

    for gid, sub in owners_map.items():
        client = router.client_for_guild(gid)
        assert isinstance(client, TidalUserClient)
        # The client's token source resolves THIS guild's own owner's token.
        assert client.streamer.token() == f"TOKEN::{sub}"

    # Every registry key is a resolved owning sub — nothing ambient/default.
    assert set(router.registry.states().keys()) == set(owners_map.values())

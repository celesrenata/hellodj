"""Task 5.3 — Spotify half of the no-single-tenant-path property test (P5).

Property 5 (design): *No code path selects or uses a credential without a
resolved owning* ``sub``\\ *; there is no ambient/default account any request can
fall back to.* This module is the Spotify provider's cross-cutting assertion of
that invariant (the Tidal and YouTube halves live in
``tidal-stream/tests/test_no_single_tenant_path_property.py`` and
``bot/playback/test_no_single_tenant_path_props.py`` — each asserts the SAME
invariant against its own router/resolver seam, because the three providers live
in separate import trees that cannot be imported from one module cleanly).

The core of P5 is **behavioral** (per the task's "prefer behavioral over pure
static grep"):

* **No resolved owner → observable failure, never a fallback (R10.5).** With
  ``owner_of`` returning ``None`` for a guild, :class:`SpotifyStreamRouter`
  raises :class:`SpotifyCredentialUnavailableError(no_owner)` — it never builds
  or returns a session, and the registry stays empty (no ambient/default session
  is created or served).
* **Owner but no/failed/blob-less credential → observable failure (R10.5).**
  Every ``CredentialUnavailable`` reason and a credential missing the librespot
  blob fail the request with a specific non-secret reason; no other user's
  session is ever returned.
* **Every session is a function of the resolved owning** ``sub`` **(R10.4).**
  A request whose guild resolves to owner A gets A's session (audio bound to A);
  a DISTINCT guild resolving to owner B gets B's session. Randomized across many
  guild/owner/credential-presence combinations, a request is served ONLY from
  its own resolved owner's session — never an ambient one.

Complementary **structural guard** (not the sole basis): the module asserts the
ambient single-``_session`` construct the design deleted is truly gone — the
``session_pool`` module exposes only the per-``sub`` :data:`SpotifySessionPool`
registry and has no module-level ``_session`` / default-account global.

All fakes are in-memory (no live AWS, no real librespot/Spotify).

Validates: Requirements 10.4, 10.5
"""

from __future__ import annotations

from hellodj_platform_logic.session_registry import SessionRegistry
from hellodj_platform_logic.types import SessionRegistryConfig
from hellodj_platform_logic.user_credential_resolver import CredentialUnavailable
from hypothesis import given, settings
from hypothesis import strategies as st

import spotify_stream.session_pool as session_pool_module
from spotify_stream.session_pool import (
    LIBRESPOT_CREDENTIALS_KEY,
    PerUserTrackCache,
    SpotifyCredentialUnavailableError,
    SpotifyStreamRouter,
)

# Disjoint alphabets so a generated guild id can never collide with a sub.
_SUB = st.text(alphabet="abcdefghijklmnopqrstuvwxyz", min_size=1, max_size=6)
_GUILD = st.text(alphabet="0123456789", min_size=1, max_size=6)
_TRACK = st.text(alphabet="ABCDEFGHIJ", min_size=1, max_size=5)

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

    A missing key defaults to ``CredentialUnavailable(no_credential)`` so an
    "owned but no stored credential" guild fails observably.
    """

    def __init__(self, results: dict[tuple[str, str], object]) -> None:
        self._results = results
        self.calls: list[tuple[str, str]] = []

    def resolve(self, guild_id, provider):
        self.calls.append((str(guild_id), provider))
        return self._results.get(
            (str(guild_id), provider), CredentialUnavailable("no_credential")
        )

    def invalidate(self, guild_id, provider):  # pragma: no cover - unused here
        pass


class _OwnerBoundSession:
    """Fake librespot session whose audio uniquely identifies its owner ``sub``."""

    def __init__(self, sub: str) -> None:
        self._sub = sub

    def load_track(self, track_id: str) -> tuple[bytes, str]:
        return f"AUDIO::{self._sub}::{track_id}".encode(), "MP3"

    def close(self) -> None:  # pragma: no cover - registry closer hook
        pass


def _blob() -> dict[str, str]:
    return {"username": "u", "credentials": "R", "type": "T"}


def _make_router(owners_map, results, *, built):
    """Wire a router over in-memory fakes; ``built`` records every factory build."""
    registry: SessionRegistry = SessionRegistry(
        SessionRegistryConfig(max_sessions=256, idle_timeout_seconds=900.0)
    )
    cache = PerUserTrackCache(max_entries=512, ttl_seconds=300.0)

    def builder(blob, cache_dir):
        sub = cache_dir.rsplit("/", 1)[-1]
        built.append(sub)
        return _OwnerBoundSession(sub)

    return SpotifyStreamRouter(
        _Owners(owners_map),
        _Resolver(results),
        registry,
        cache,
        session_builder=builder,
        track_loader=lambda track_id, session: session.load_track(track_id),
        cache_dir_for=lambda sub: f"/tmp/{sub}",
    )


# ── Structural guard: the ambient single-session construct is gone ─────────────


def test_no_ambient_default_session_construct_in_module():
    """The deleted single global ``_session`` / default account has no residue.

    Guards against a regression that re-introduces a module-level ambient
    session any guild could fall back to (the exact single-tenant path P5
    forbids). The module's only session container is the per-``sub`` registry.
    """
    module_globals = vars(session_pool_module)
    assert "_session" not in module_globals
    assert "SESSION" not in module_globals
    assert "default_session" not in module_globals
    # The public API is the per-sub pool + router, never a shared session object.
    assert "SpotifySessionPool" in session_pool_module.__all__
    assert "SpotifyStreamRouter" in session_pool_module.__all__


# ── Behavioral P5: no credential use without a resolved owning sub ─────────────


@settings(max_examples=60, deadline=None)
@given(guild=_GUILD, track=_TRACK)
def test_no_owner_fails_observably_and_builds_no_session(guild, track):
    """A guild with NO resolved owner never yields a session (R10.5)."""
    built: list[str] = []
    router = _make_router({}, {}, built=built)  # empty owners → owner_of == None

    for call in (
        lambda: router.session_for_guild(guild),
        lambda: router.load_track_for_guild(guild, track),
    ):
        try:
            call()
        except SpotifyCredentialUnavailableError as exc:
            assert exc.reason == "no_owner"
        else:  # pragma: no cover - a returned session would be a P5 violation
            raise AssertionError("no-owner guild must not yield a session")

    # No ambient/default session was ever created for a guild with no owner.
    assert built == []
    assert dict(router.registry.states()) == {}


@settings(max_examples=80, deadline=None)
@given(guild=_GUILD, sub=_SUB, reason=st.sampled_from(_UNAVAILABLE_REASONS))
def test_owned_but_unavailable_credential_fails_never_falls_back(guild, sub, reason):
    """An owned guild with an unavailable credential fails, never falls back.

    For every typed ``CredentialUnavailable`` reason the request fails with that
    specific reason — the router never substitutes another user's session or an
    ambient default (R10.5).
    """
    built: list[str] = []
    results = {(guild, "spotify"): CredentialUnavailable(reason)}
    router = _make_router({guild: sub}, results, built=built)

    try:
        router.session_for_guild(guild)
    except SpotifyCredentialUnavailableError as exc:
        assert exc.reason == reason
    else:  # pragma: no cover - a session here would be a P5 violation
        raise AssertionError("unavailable credential must not yield a session")

    # No session was built (the factory never produced an ambient fallback).
    assert built == []


@settings(max_examples=80, deadline=None)
@given(guild=_GUILD, sub=_SUB)
def test_owned_credential_without_librespot_blob_fails(guild, sub):
    """An owned guild whose credential lacks the librespot blob fails (R10.5)."""
    built: list[str] = []
    # A resolved credential (dict) but WITHOUT the reusable librespot blob.
    results = {(guild, "spotify"): {"access_token": "a"}}
    router = _make_router({guild: sub}, results, built=built)

    try:
        router.session_for_guild(guild)
    except SpotifyCredentialUnavailableError as exc:
        assert exc.reason == "no_librespot_credential"
    else:  # pragma: no cover - blob-less credential must fail
        raise AssertionError("credential without librespot blob must not stream")
    assert built == []


@settings(max_examples=50, deadline=None)
@given(
    pairs=st.lists(
        st.tuples(_GUILD, _SUB), min_size=1, max_size=8, unique_by=lambda t: t[0]
    ),
    track=_TRACK,
)
def test_every_session_is_a_function_of_the_resolved_owning_sub(pairs, track):
    """A request with a resolved sub gets THAT sub's session, never an ambient one.

    Randomized across many distinct guilds (owners may repeat — two guilds owned
    by the same user validly share ONE session), every served track's audio is
    bound to the guild's OWN resolved owner, and the registry is keyed by exactly
    the set of resolved owning subs — no ambient/default key exists (R10.4).
    """
    owners_map = {gid: sub for gid, sub in pairs}
    results = {
        (gid, "spotify"): {"access_token": "a", LIBRESPOT_CREDENTIALS_KEY: _blob()}
        for gid in owners_map
    }
    built: list[str] = []
    router = _make_router(owners_map, results, built=built)

    for gid, sub in owners_map.items():
        audio, _codec = router.load_track_for_guild(gid, track)
        # Served ONLY from this guild's own resolved owner's session.
        assert audio == f"AUDIO::{sub}::{track}".encode()

    # Every registry key is a resolved owning sub — nothing ambient/default.
    assert set(router.registry.states().keys()) == set(owners_map.values())
    # One build per distinct owning sub; never an extra ambient session.
    assert sorted(set(built)) == sorted(set(owners_map.values()))

"""Property-based tests for the Spotify session pool (P1 isolation, P4 failure).

These exercise the design's correctness properties with Hypothesis over the
in-memory router:

* **P1 (no cross-user leakage, R6.1/R6.2):** for any set of guilds mapped to
  distinct owning ``sub`` s, each guild's stream is served by ITS owner's session
  and its cached audio is keyed by ``(sub, track)`` — a guild owned by user A
  never receives user B's session or audio, under any interleaving of requests.
* **P4 (honest, isolated failure, R7.2/R7.4):** a non-Premium user's failure is
  recorded as a SPECIFIC ``failed(not_premium)`` per-``sub`` state and never
  affects another user's session (which stays ``ready``).

Validates: Requirements 6.1, 6.2, 7.2, 7.4
"""

from __future__ import annotations

from hellodj_platform_logic.session_registry import SessionRegistry
from hellodj_platform_logic.types import SessionPhase, SessionRegistryConfig
from hypothesis import given, settings
from hypothesis import strategies as st

from spotify_stream.session_pool import (
    LIBRESPOT_CREDENTIALS_KEY,
    PerUserTrackCache,
    SpotifyCredentialUnavailableError,
    SpotifyStreamRouter,
)

_SUB = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz0123456789", min_size=1, max_size=8
)
_GUILD = st.text(alphabet="0123456789", min_size=1, max_size=6)


class _Owners:
    def __init__(self, mapping):
        self._map = mapping

    def owner_of(self, guild_id):
        return self._map.get(str(guild_id))


class _Resolver:
    def __init__(self, results):
        self._results = results

    def resolve(self, guild_id, provider):
        return self._results[(str(guild_id), provider)]

    def invalidate(self, guild_id, provider):
        pass


class _Session:
    def __init__(self, sub, premium=True):
        self._sub = sub
        self._premium = premium

    def load_track(self, track_id):
        if not self._premium:
            from spotify_stream.librespot_session import NotPremiumError

            raise NotPremiumError()
        # Audio uniquely identifies the owning sub so leakage is detectable.
        return f"AUDIO::{self._sub}".encode(), "MP3"

    def close(self):
        pass


def _blob():
    return {"username": "u", "credentials": "R", "type": "T"}


def _build_router(owners_map, premium_by_sub):
    results = {
        (gid, "spotify"): {"access_token": "a", LIBRESPOT_CREDENTIALS_KEY: _blob()}
        for gid in owners_map
    }
    registry = SessionRegistry(
        SessionRegistryConfig(max_sessions=64, idle_timeout_seconds=900.0)
    )
    cache = PerUserTrackCache(max_entries=128, ttl_seconds=300.0)

    def builder(blob, cache_dir):
        sub = cache_dir.rsplit("/", 1)[-1]
        return _Session(sub, premium=premium_by_sub.get(sub, True))

    return SpotifyStreamRouter(
        _Owners(owners_map),
        _Resolver(results),
        registry,
        cache,
        session_builder=builder,
        track_loader=lambda track_id, session: session.load_track(track_id),
        cache_dir_for=lambda sub: f"/tmp/{sub}",
    )


@settings(max_examples=60, deadline=None)
@given(
    pairs=st.lists(
        st.tuples(_GUILD, _SUB), min_size=1, max_size=8, unique_by=lambda t: t[0]
    ),
)
def test_p1_each_guild_served_by_its_owner_no_leakage(pairs):
    owners_map = {gid: sub for gid, sub in pairs}
    router = _build_router(owners_map, premium_by_sub={})

    # Every guild's audio carries its OWN owner's sub — never another's.
    for gid, sub in owners_map.items():
        audio, _ = router.load_track_for_guild(gid, "trk")
        assert audio == f"AUDIO::{sub}".encode()

    # The registry is keyed by the resolved sub only (no guild ids leak in).
    tracked = set(router.registry.states().keys())
    assert tracked == set(owners_map.values())


@settings(max_examples=60, deadline=None)
@given(
    good=st.lists(_GUILD, min_size=1, max_size=4, unique=True),
    bad=st.lists(_GUILD, min_size=1, max_size=4, unique=True),
)
def test_p4_non_premium_failure_isolated_per_sub(good, bad):
    # Distinct guilds for premium vs non-premium users (disjoint ids + subs).
    good = [f"g{g}" for g in good]
    bad = [f"b{b}" for b in bad]
    owners_map = {}
    premium_by_sub = {}
    for gid in good:
        sub = f"prem-{gid}"
        owners_map[gid] = sub
        premium_by_sub[sub] = True
    for gid in bad:
        sub = f"free-{gid}"
        owners_map[gid] = sub
        premium_by_sub[sub] = False

    router = _build_router(owners_map, premium_by_sub)

    # Non-premium users fail observably and are recorded FAILED(not_premium).
    for gid in bad:
        try:
            router.load_track_for_guild(gid, "trk")
            raise AssertionError("expected non-premium failure")
        except SpotifyCredentialUnavailableError as exc:
            assert exc.reason == "not_premium"
        state = router.registry.state_of(owners_map[gid])
        assert state.phase is SessionPhase.FAILED
        assert state.reason == "not_premium"

    # Premium users are entirely unaffected.
    for gid in good:
        audio, _ = router.load_track_for_guild(gid, "trk")
        assert audio == f"AUDIO::{owners_map[gid]}".encode()
        assert router.registry.state_of(owners_map[gid]).phase is SessionPhase.READY

"""Concurrent isolation + health-surface property tests (task 2.6, P1/P4).

Task 2.3 already covers the Spotify session factory and per-``sub`` failure
states with SEQUENTIAL example + property tests
(``test_session_pool.py`` / ``test_session_pool_property.py``). This module adds
the parts task 2.6 calls out that those do not exercise:

* **P1 under randomized CONCURRENT interleaving (R6.1, R6.2).** The existing P1
  property resolves guilds one-at-a-time. Here many threads hammer
  :meth:`SpotifyStreamRouter.load_track_for_guild` for a randomized mix of
  distinct guilds (distinct owning ``sub`` s) simultaneously, asserting every
  request still receives ONLY its own owner's audio and the per-``(sub,track)``
  cache never yields another user's bytes — under any interleaving the shared
  ``SessionRegistry`` / ``PerUserTrackCache`` locks permit. The session factory
  runs at most once per owning ``sub`` even under the concurrent stampede.

* **P4 tied to the health SURFACE across many subs (R3.7, R7.3).** The existing
  P4 property inspects registry states directly and the server test checks the
  endpoints for a single fixed pair. Here a randomized mix of premium and
  non-premium owners is driven through the router, then the LIVE ``/health`` and
  ``/auth/status`` endpoints are asserted to report the SPECIFIC per-``sub``
  states (``ready`` vs ``failed(not_premium)``) with the right live/tracked
  counts, hashed subs, and no token material — never a single global status and
  never fake-green.

All fakes are in-memory (no live AWS, no real librespot/Spotify).

Validates: Requirements 3.7, 6.1, 6.2, 7.3
"""

from __future__ import annotations

import asyncio
import threading

from aiohttp.test_utils import TestClient, TestServer
from hellodj_platform_logic.session_registry import SessionRegistry
from hellodj_platform_logic.types import SessionPhase, SessionRegistryConfig
from hypothesis import given, settings
from hypothesis import strategies as st

from spotify_stream.librespot_session import NotPremiumError
from spotify_stream.server import build_app
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


# ── Fakes ────────────────────────────────────────────────────────────────────


class _Owners:
    """In-memory guild→owner ``sub`` lookup."""

    def __init__(self, mapping: dict[str, str]) -> None:
        self._map = mapping

    def owner_of(self, guild_id: str) -> str | None:
        return self._map.get(str(guild_id))


class _Resolver:
    """In-memory resolver returning a librespot-blob-bearing tokens dict."""

    def __init__(self, results: dict[tuple[str, str], object]) -> None:
        self._results = results

    def resolve(self, guild_id, provider):
        return self._results[(str(guild_id), provider)]

    def invalidate(self, guild_id, provider):  # pragma: no cover - unused here
        pass


class _CountingSession:
    """Fake librespot session whose audio uniquely identifies its owner.

    A non-premium session raises :class:`NotPremiumError` at track-load, exactly
    like the real librespot path (Free accounts authenticate but fail to load).
    """

    def __init__(self, sub: str, *, premium: bool = True) -> None:
        self._sub = sub
        self._premium = premium

    def load_track(self, track_id: str) -> tuple[bytes, str]:
        if not self._premium:
            raise NotPremiumError()
        # Bind the audio to BOTH the owning sub and the track so any cross-user
        # OR cross-track leak is detectable in the assertion.
        return f"AUDIO::{self._sub}::{track_id}".encode(), "MP3"

    def close(self) -> None:  # pragma: no cover - registry closer hook
        pass


def _blob() -> dict[str, str]:
    return {"username": "u", "credentials": "R", "type": "T"}


def _build_router(owners_map, premium_by_sub, *, build_counts=None):
    """Wire a router over in-memory fakes; ``build_counts`` records factory hits."""
    results = {
        (gid, "spotify"): {"access_token": "a", LIBRESPOT_CREDENTIALS_KEY: _blob()}
        for gid in owners_map
    }
    registry: SessionRegistry = SessionRegistry(
        SessionRegistryConfig(max_sessions=128, idle_timeout_seconds=900.0)
    )
    cache = PerUserTrackCache(max_entries=512, ttl_seconds=300.0)
    lock = threading.Lock()

    def builder(blob, cache_dir):
        sub = cache_dir.rsplit("/", 1)[-1]
        if build_counts is not None:
            with lock:
                build_counts[sub] = build_counts.get(sub, 0) + 1
        return _CountingSession(sub, premium=premium_by_sub.get(sub, True))

    return SpotifyStreamRouter(
        _Owners(owners_map),
        _Resolver(results),
        registry,
        cache,
        session_builder=builder,
        track_loader=lambda track_id, session: session.load_track(track_id),
        cache_dir_for=lambda sub: f"/tmp/{sub}",
    )


def _run_concurrently(jobs):
    """Run ``jobs`` (0-arg callables) on threads; return results in order.

    Threads all start together (barrier) to maximize interleaving. Any exception
    raised by a job is captured and returned in that slot rather than crashing
    the harness, so the property assertions can inspect it.
    """
    results: list[object] = [None] * len(jobs)
    barrier = threading.Barrier(len(jobs))

    def _wrap(idx, fn):
        def _inner():
            barrier.wait()
            try:
                results[idx] = fn()
            except Exception as exc:  # noqa: BLE001 - captured for assertion
                results[idx] = exc

        return _inner

    threads = [threading.Thread(target=_wrap(i, fn)) for i, fn in enumerate(jobs)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


# ── P1: no cross-user leakage under randomized concurrent interleaving ─────────


@settings(max_examples=40, deadline=None)
@given(
    pairs=st.lists(
        st.tuples(_GUILD, _SUB), min_size=2, max_size=8, unique_by=lambda t: t[0]
    ),
    track=_TRACK,
    repeats=st.integers(min_value=1, max_value=3),
)
def test_p1_concurrent_requests_never_leak_across_users(pairs, track, repeats):
    # Distinct guilds; owners may repeat (two guilds owned by the same user is
    # valid and must share ONE session). Build a guild->sub map.
    owners_map = {gid: sub for gid, sub in pairs}
    build_counts: dict[str, int] = {}
    router = _build_router(owners_map, premium_by_sub={}, build_counts=build_counts)

    # Fire every guild's request `repeats` times, all threads racing together.
    guild_ids = list(owners_map) * repeats

    def _job(gid):
        return lambda: (gid, router.load_track_for_guild(gid, track))

    results = _run_concurrently([_job(gid) for gid in guild_ids])

    # Every request received ONLY its own owner's audio for the asked track.
    for res in results:
        assert not isinstance(res, Exception), res
        gid, (audio, _codec) = res
        expected = f"AUDIO::{owners_map[gid]}::{track}".encode()
        assert audio == expected

    # The registry is keyed by resolved sub only (no guild ids leak in), and one
    # session was built per distinct owning sub despite the concurrent stampede.
    distinct_subs = set(owners_map.values())
    assert set(router.registry.states().keys()) == distinct_subs
    for sub in distinct_subs:
        assert build_counts.get(sub, 0) == 1
        assert router.registry.state_of(sub).phase is SessionPhase.READY


@settings(max_examples=40, deadline=None)
@given(
    entries=st.lists(
        st.tuples(_SUB, _TRACK, st.binary(min_size=1, max_size=12)),
        min_size=1,
        max_size=12,
        unique_by=lambda t: (t[0], t[1]),
    ),
)
def test_p1_per_user_cache_is_thread_safe_and_never_crosses_users(entries):
    # Concurrently put distinct (sub, track) audio, then concurrently read them
    # back; a hit must return the exact bytes stored for THAT (sub, track).
    cache = PerUserTrackCache(max_entries=1024, ttl_seconds=300.0)

    put_jobs = [
        (lambda s=s, t=t, a=a: cache.put(s, t, a, "MP3")) for s, t, a in entries
    ]
    _run_concurrently(put_jobs)

    def _get(sub, track):
        return lambda: cache.get(sub, track)

    read = _run_concurrently([_get(s, t) for s, t, _a in entries])
    for (sub, track, audio), res in zip(entries, read, strict=True):
        assert not isinstance(res, Exception), res
        assert res is not None
        assert res[0] == audio
        # A different user asking for the same track id never gets these bytes.
        other = cache.get(sub + "X", track)
        assert other is None or other[0] != audio


# ── P4: honest, isolated failure reported by the health SURFACE ────────────────


async def _fetch_health_and_auth(app) -> tuple[dict, dict]:
    """Start the app in-process and GET ``/health`` + ``/auth/status`` (R7.3)."""
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        health = await (await client.get("/health")).json()
        auth = await (await client.get("/auth/status")).json()
        return health, auth
    finally:
        await client.close()


@settings(max_examples=25, deadline=None)
@given(
    prem=st.lists(_GUILD, min_size=1, max_size=4, unique=True),
    free=st.lists(_GUILD, min_size=1, max_size=4, unique=True),
    track=_TRACK,
)
def test_p4_health_surface_reports_specific_per_sub_states(prem, free, track):
    # Disjoint guild id + sub namespaces for premium vs non-premium owners.
    prem = [f"p{g}" for g in prem]
    free = [f"f{g}" for g in free]
    owners_map: dict[str, str] = {}
    premium_by_sub: dict[str, bool] = {}
    for gid in prem:
        sub = f"prem-{gid}"
        owners_map[gid] = sub
        premium_by_sub[sub] = True
    for gid in free:
        sub = f"free-{gid}"
        owners_map[gid] = sub
        premium_by_sub[sub] = False

    router = _build_router(owners_map, premium_by_sub)

    # Drive every guild through the router (concurrently) so premium subs become
    # ready and non-premium subs become failed(not_premium).
    def _job(gid):
        def _run():
            try:
                router.load_track_for_guild(gid, track)
                return None
            except SpotifyCredentialUnavailableError as exc:
                return exc.reason

        return _run

    outcomes = _run_concurrently([_job(gid) for gid in owners_map])
    for gid, outcome in zip(owners_map, outcomes, strict=True):
        if premium_by_sub[owners_map[gid]]:
            assert outcome is None
        else:
            assert outcome == "not_premium"

    prem_subs = {owners_map[g] for g in prem}
    free_subs = {owners_map[g] for g in free}

    health, auth = asyncio.run(_fetch_health_and_auth(build_app(router)))

    # /health: live count == premium subs, tracked == all subs, status ok.
    assert health["status"] == "ok"
    assert health["live_sessions"] == len(prem_subs)
    assert health["tracked_sessions"] == len(prem_subs) + len(free_subs)

    # /auth/status: per-sub map, hashed keys, specific failure reasons, never a
    # single global status and never token material.
    assert auth["status"] == "ok"
    assert auth["live_sessions"] == len(prem_subs)
    assert len(auth["sessions"]) == len(prem_subs) + len(free_subs)
    # Raw subs are never echoed (only short digests).
    for raw in prem_subs | free_subs:
        assert raw not in auth["sessions"]
    phases = [v["phase"] for v in auth["sessions"].values()]
    reasons = [v["reason"] for v in auth["sessions"].values()]
    assert phases.count("ready") == len(prem_subs)
    assert phases.count("failed") == len(free_subs)
    # Every failed session carries the SPECIFIC not_premium reason; ready ones
    # carry no reason (no fake-green, no masking).
    assert reasons.count("not_premium") == len(free_subs)

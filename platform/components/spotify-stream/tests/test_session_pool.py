"""Tests for the per-user Spotify session pool + guild→owner routing (task 2.3).

Covers the design's Spotify factory + isolation contract with in-memory fakes
(no live AWS, no real librespot/Spotify):

* factory builds a session from a stored (non-interactive) librespot credential;
* a non-Premium account is rejected as a per-``sub`` ``failed(not_premium)``
  state, scoped to that user (R3.5, R3.7);
* no owner / no credential / no captured librespot blob / failed-status →
  observable failure with NO cross-user fallback (R3.6, R10.5);
* the per-``(sub, track)`` audio cache never crosses users (P1, R6.2);
* the router resolves ``guild→owner_sub`` server-side (the ``sub`` is never in
  the guild id).

Requirements: 3.2, 3.3, 3.5, 3.6, 3.7, 6.1, 6.2, 10.5
"""

from __future__ import annotations

import pytest
from hellodj_platform_logic.session_registry import SessionRegistry
from hellodj_platform_logic.types import SessionPhase, SessionRegistryConfig
from hellodj_platform_logic.user_credential_resolver import CredentialUnavailable

from spotify_stream.librespot_session import LibrespotSessionError, NotPremiumError
from spotify_stream.session_pool import (
    LIBRESPOT_CREDENTIALS_KEY,
    PerUserTrackCache,
    SpotifyCredentialUnavailableError,
    SpotifyStreamRouter,
    normalize_track_id,
)

# ── Fakes ────────────────────────────────────────────────────────────────────


class FakeOwners:
    """In-memory guild→owner ``sub`` lookup."""

    def __init__(self, mapping: dict[str, str]):
        self._map = mapping

    def owner_of(self, guild_id: str):
        return self._map.get(str(guild_id))


class FakeResolver:
    """In-memory ``UserCredentialResolver`` returning canned results per guild.

    Resolution is keyed by ``(guild_id, provider)`` and returns either a tokens
    dict (success) or a :class:`CredentialUnavailable`.
    """

    def __init__(self, results: dict[tuple[str, str], object]):
        self._results = results
        self.invalidated: list[tuple[str, str]] = []

    def resolve(self, guild_id, provider):
        return self._results[(str(guild_id), provider)]

    def invalidate(self, guild_id, provider):
        self.invalidated.append((str(guild_id), provider))


class FakeLibrespotSession:
    """A fake librespot session that returns audio or raises per configuration."""

    def __init__(self, *, audio: bytes = b"AUDIO", not_premium: bool = False):
        self._audio = audio
        self._not_premium = not_premium
        self.closed = False

    def load_track(self, track_id):
        if self._not_premium:
            raise NotPremiumError()
        return self._audio, "MP3"

    def close(self):
        self.closed = True


def _blob():
    return {"username": "u", "credentials": "REUSABLE", "type": "AUTH_STORED"}


def _tokens_with_blob():
    return {"access_token": "a", LIBRESPOT_CREDENTIALS_KEY: _blob()}


def _fake_track_loader(track_id, session):
    """Delegate to the fake librespot session's own ``load_track``."""
    return session.load_track(track_id)


def _router(owners, resolver, *, builder, max_sessions=8):
    registry: SessionRegistry = SessionRegistry(
        SessionRegistryConfig(max_sessions=max_sessions, idle_timeout_seconds=900.0)
    )
    cache = PerUserTrackCache(max_entries=16, ttl_seconds=300.0)

    return SpotifyStreamRouter(
        owners,
        resolver,
        registry,
        cache,
        session_builder=builder,
        track_loader=_fake_track_loader,
        cache_dir_for=lambda sub: f"/tmp/{sub}",
    )


# ── normalize_track_id ─────────────────────────────────────────────────────────


def test_normalize_strips_spotify_uri_prefix():
    assert normalize_track_id("spotify:track:abc123") == "abc123"
    assert normalize_track_id("abc123") == "abc123"


# ── factory builds from a stored credential (R3.3) ─────────────────────────────


def test_factory_builds_session_from_stored_blob():
    owners = FakeOwners({"g1": "subA"})
    resolver = FakeResolver({("g1", "spotify"): _tokens_with_blob()})
    built = {}

    def builder(blob, cache_dir):
        built["blob"] = blob
        built["cache_dir"] = cache_dir
        return FakeLibrespotSession()

    router = _router(owners, resolver, builder=builder)
    session = router.session_for_guild("g1")

    # The factory received the reusable blob and built with the per-user dir.
    assert built["blob"] == _blob()
    assert built["cache_dir"] == "/tmp/subA"
    assert session.sub == "subA"
    assert router.registry.state_of("subA").phase is SessionPhase.READY


def test_session_reused_for_same_owner_across_guilds():
    # Two guilds owned by the SAME user share ONE session (correct, R6.1).
    owners = FakeOwners({"g1": "subA", "g2": "subA"})
    resolver = FakeResolver(
        {
            ("g1", "spotify"): _tokens_with_blob(),
            ("g2", "spotify"): _tokens_with_blob(),
        }
    )
    builds = []

    def builder(blob, cache_dir):
        builds.append(cache_dir)
        return FakeLibrespotSession()

    router = _router(owners, resolver, builder=builder)
    s1 = router.session_for_guild("g1")
    s2 = router.session_for_guild("g2")
    assert s1 is s2
    assert len(builds) == 1  # built once, reused


# ── non-Premium rejected per-sub (R3.5, R3.7) ──────────────────────────────────


def test_non_premium_fails_that_user_and_isolates_others():
    owners = FakeOwners({"free": "subFree", "prem": "subPrem"})
    resolver = FakeResolver(
        {
            ("free", "spotify"): _tokens_with_blob(),
            ("prem", "spotify"): _tokens_with_blob(),
        }
    )

    def builder(blob, cache_dir):
        if cache_dir.endswith("subFree"):
            return FakeLibrespotSession(not_premium=True)
        return FakeLibrespotSession(audio=b"PREMIUM")

    router = _router(owners, resolver, builder=builder)

    # The non-Premium user's stream fails observably at track-load.
    with pytest.raises(SpotifyCredentialUnavailableError) as exc:
        router.load_track_for_guild("free", "trk")
    assert exc.value.reason == "not_premium"
    # And that user's session is recorded FAILED (not green), scoped to them.
    assert router.registry.state_of("subFree").phase is SessionPhase.FAILED
    assert router.registry.state_of("subFree").reason == "not_premium"

    # The Premium user is completely unaffected.
    audio, _ = router.load_track_for_guild("prem", "trk")
    assert audio == b"PREMIUM"
    assert router.registry.state_of("subPrem").phase is SessionPhase.READY


# ── observable failure, no cross-user fallback (R3.6, R10.5) ────────────────────


def test_no_owner_fails_observably():
    router = _router(
        FakeOwners({}), FakeResolver({}), builder=lambda b, c: FakeLibrespotSession()
    )
    with pytest.raises(SpotifyCredentialUnavailableError) as exc:
        router.session_for_guild("ghost")
    assert exc.value.reason == "no_owner"


def test_credential_unavailable_reason_propagates():
    owners = FakeOwners({"g1": "subA"})
    resolver = FakeResolver({("g1", "spotify"): CredentialUnavailable("refresh_failed")})
    router = _router(owners, resolver, builder=lambda b, c: FakeLibrespotSession())
    with pytest.raises(SpotifyCredentialUnavailableError) as exc:
        router.session_for_guild("g1")
    assert exc.value.reason == "refresh_failed"
    assert router.registry.state_of("subA").phase is SessionPhase.FAILED


def test_missing_librespot_blob_is_not_streamable():
    owners = FakeOwners({"g1": "subA"})
    # A Spotify credential exists but the one-time librespot capture never ran.
    resolver = FakeResolver({("g1", "spotify"): {"access_token": "a"}})
    router = _router(owners, resolver, builder=lambda b, c: FakeLibrespotSession())
    with pytest.raises(SpotifyCredentialUnavailableError) as exc:
        router.session_for_guild("g1")
    assert exc.value.reason == "no_librespot_credential"


def test_invalid_blob_build_failure_is_session_create_failed():
    owners = FakeOwners({"g1": "subA"})
    resolver = FakeResolver({("g1", "spotify"): _tokens_with_blob()})

    def builder(blob, cache_dir):
        raise LibrespotSessionError()

    router = _router(owners, resolver, builder=builder)
    with pytest.raises(SpotifyCredentialUnavailableError) as exc:
        router.session_for_guild("g1")
    assert exc.value.reason == "session_create_failed"


# ── per-(sub, track) cache isolation (P1, R6.2) ────────────────────────────────


def test_track_cache_keyed_by_sub_never_crosses_users():
    cache = PerUserTrackCache(max_entries=8, ttl_seconds=300.0)
    cache.put("subA", "trk", b"A-audio", "MP3")
    cache.put("subB", "trk", b"B-audio", "MP3")

    assert cache.get("subA", "trk")[0] == b"A-audio"
    assert cache.get("subB", "trk")[0] == b"B-audio"
    # A user with no entry for the same track id gets a miss (no cross-user hit).
    assert cache.get("subC", "trk") is None


def test_load_track_caches_per_user_and_serves_from_cache():
    owners = FakeOwners({"g1": "subA"})
    resolver = FakeResolver({("g1", "spotify"): _tokens_with_blob()})
    load_calls = {"n": 0}

    class CountingSession(FakeLibrespotSession):
        def load_track(self, track_id):
            load_calls["n"] += 1
            return b"AUDIO", "MP3"

    router = _router(owners, resolver, builder=lambda b, c: CountingSession())

    a1, _ = router.load_track_for_guild("g1", "trk")
    a2, _ = router.load_track_for_guild("g1", "trk")
    assert a1 == a2 == b"AUDIO"
    # Second call served from the per-(sub, track) cache — track loaded once.
    assert load_calls["n"] == 1


def test_owner_sub_resolved_server_side_not_from_guild_id():
    # The guild id is NOT the sub: distinct guilds map to distinct subs, and the
    # registry is keyed by the resolved sub (server-side), never the guild id.
    owners = FakeOwners({"111": "sub-x", "222": "sub-y"})
    resolver = FakeResolver(
        {
            ("111", "spotify"): _tokens_with_blob(),
            ("222", "spotify"): _tokens_with_blob(),
        }
    )
    router = _router(owners, resolver, builder=lambda b, c: FakeLibrespotSession())
    router.session_for_guild("111")
    router.session_for_guild("222")
    states = router.registry.states()
    assert set(states.keys()) == {"sub-x", "sub-y"}
    assert "111" not in states and "222" not in states

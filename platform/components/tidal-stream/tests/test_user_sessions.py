"""Per-user + isolation tests for the multi-tenant tidal-stream data plane.

Feature: multi-tenant-source-streaming (task 3.2)

Covers the per-request user-token selection realized by task 3.1
(``TidalSessionRegistry`` + ``TidalStreamRouter`` + ``ReadOnlyTidalTokenSource``):

* **Property P1 (no cross-user leakage).** Under randomized concurrent requests
  from different guilds owned by DISTINCT users, each request is served with its
  OWN owning user's Tidal token; a request keyed to guild A never gets guild B's
  token/client. There is no shared mutable token state across the per-user
  clients — each client draws its token from its own guild-bound read-only
  source, and the registry is keyed by the owning ``sub``.
  **Validates: Requirements 5.1, 5.2, 6.1**

* **Refresh behavior unchanged (R5.3).** The sidecar is READ-ONLY: the per-user
  token source only SELECTS the owning user's token from the unified store and
  never refreshes, re-encrypts, or writes it. A ``force`` re-read only
  invalidates the resolver cache and re-resolves (the value the durable watchdog
  refreshed out-of-band), it never calls a refresh/persist path.
  **Validates: Requirements 5.3**

* **No-credential guild (R5.4).** A guild with no recorded owner, or an owner
  with no Tidal credential (no unified item and no legacy secret), fails
  observably with a typed :class:`TidalCredentialUnavailableError` and NO
  fallback to another user's token.
  **Validates: Requirements 5.4**

All tests use in-memory fakes for the store / owner lookup / streamer so no
network, AWS, or live Tidal access is required — mirroring the fake-friendly
pattern of ``test_server.py`` and the shared registry fakes.
"""

from __future__ import annotations

import threading

from hellodj_platform_logic.session_registry import SessionRegistry
from hellodj_platform_logic.types import SessionPhase, SessionRegistryConfig
from hellodj_platform_logic.user_credential_resolver import CredentialUnavailable
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tidal_stream.user_sessions import (
    PROVIDER_TIDAL,
    ReadOnlyTidalTokenSource,
    TidalCredentialUnavailableError,
    TidalStreamRouter,
)

# ---------------------------------------------------------------------------
# In-memory fakes (no network / AWS / live Tidal)
# ---------------------------------------------------------------------------


class FakeStreamer:
    """Async streamer double that echoes the token it was built with.

    The token is captured at construction (from the resolved access token) and
    echoed back in ``search`` / ``get_stream_url`` results, so any cross-user
    leakage would surface as a mismatched token in the response.
    """

    def __init__(self, token: str) -> None:
        self.token = token
        self.closed = False

    async def search(self, query, limit=10):
        return [{"id": "1", "title": query, "artist": self.token}]

    async def get_stream_url(self, track_id):
        return f"https://stream/{track_id}?tok={self.token}"

    async def close(self):
        self.closed = True


class RecordingResolver:
    """Read-only resolver double: guild -> tokens dict or CredentialUnavailable.

    Records every ``resolve`` / ``invalidate`` call so a test can assert the
    reader is read-only (no write/refresh surface exists) and count re-reads.
    A ``resolve`` NEVER mutates the backing tokens, mirroring the real
    resolver's read-only contract (R2.1/R5.3).
    """

    def __init__(self, tokens_by_guild: dict[str, dict]) -> None:
        self._tokens = tokens_by_guild
        self.resolve_calls: list[tuple[str, str]] = []
        self.invalidate_calls: list[tuple[str, str]] = []
        self._lock = threading.Lock()

    def resolve(self, guild_id, provider):
        with self._lock:
            self.resolve_calls.append((str(guild_id), provider))
        value = self._tokens.get(str(guild_id))
        if value is None:
            return CredentialUnavailable("no_credential")
        return dict(value)  # copy: a caller can never mutate our stored blob

    def invalidate(self, guild_id, provider):
        with self._lock:
            self.invalidate_calls.append((str(guild_id), provider))


class FakeOwners:
    """Owner lookup double mapping guild id -> owner sub (or None)."""

    def __init__(self, owners: dict[str, str]) -> None:
        self._owners = owners

    def owner_of(self, guild_id):
        return self._owners.get(str(guild_id))


class RaisingOwners:
    """Owner lookup double whose lookup raises (store unavailable)."""

    def owner_of(self, guild_id):
        raise RuntimeError("owner store unreachable")


def _make_router(*, owners, tokens_by_guild, resolver=None, max_sessions=32):
    """Build a router over fakes; the streamer echoes the resolved token."""
    resolver = resolver or RecordingResolver(tokens_by_guild)
    registry: SessionRegistry = SessionRegistry(
        SessionRegistryConfig(max_sessions=max_sessions, idle_timeout_seconds=1000.0)
    )

    def _factory(token_source: ReadOnlyTidalTokenSource):
        # Return the bare streamer stand-in; the router wraps it in a
        # TidalUserClient. The streamer echoes the resolved token so a leak
        # would surface as a mismatched token on the response.
        token = token_source.get_access_token()
        return FakeStreamer(token)  # type: ignore[return-value]

    router = TidalStreamRouter(
        owners,
        resolver,  # type: ignore[arg-type]
        registry,
        streamer_factory=_factory,  # type: ignore[arg-type]
    )
    return router, resolver, registry


# ---------------------------------------------------------------------------
# ReadOnlyTidalTokenSource — read-only per-user token selection (R5.3, R2.1)
# ---------------------------------------------------------------------------


def test_token_source_returns_owning_users_token():
    """The source returns the resolved owning user's access token (R5.1)."""
    resolver = RecordingResolver({"g1": {"access_token": "tokA"}})
    source = ReadOnlyTidalTokenSource(resolver, "g1")  # type: ignore[arg-type]
    assert source.get_access_token() == "tokA"
    assert resolver.resolve_calls == [("g1", PROVIDER_TIDAL)]


def test_token_source_is_read_only_no_write_surface():
    """The source only resolves/invalidates — no write/refresh/persist (R5.3).

    A ``force`` re-read invalidates the resolver cache and re-resolves; it must
    NOT call any write/refresh/persist method (the resolver double exposes none,
    and the source must not require one).
    """
    resolver = RecordingResolver({"g1": {"access_token": "tokA"}})
    source = ReadOnlyTidalTokenSource(resolver, "g1")  # type: ignore[arg-type]

    source.get_access_token()
    source.get_access_token(force=True)

    # force triggered exactly one invalidate + a re-resolve; no other calls.
    assert resolver.invalidate_calls == [("g1", PROVIDER_TIDAL)]
    assert resolver.resolve_calls == [
        ("g1", PROVIDER_TIDAL),
        ("g1", PROVIDER_TIDAL),
    ]
    # The resolver exposes ONLY resolve/invalidate — no store/refresh surface
    # the read-only source could have called.
    for forbidden in ("store", "put", "update", "refresh", "persist", "write"):
        assert not hasattr(resolver, forbidden)


def test_token_source_unavailable_raises_observably():
    """A CredentialUnavailable resolution surfaces its reason, no dead token."""
    resolver = RecordingResolver({})  # no credential for any guild
    source = ReadOnlyTidalTokenSource(resolver, "g9")  # type: ignore[arg-type]
    try:
        source.get_access_token()
        raise AssertionError("expected TidalCredentialUnavailableError")
    except TidalCredentialUnavailableError as exc:
        assert exc.reason == "no_credential"


def test_token_source_empty_access_token_is_unavailable():
    """A resolved credential with no access token is observably unavailable."""
    resolver = RecordingResolver({"g1": {"access_token": ""}})
    source = ReadOnlyTidalTokenSource(resolver, "g1")  # type: ignore[arg-type]
    try:
        source.get_access_token()
        raise AssertionError("expected TidalCredentialUnavailableError")
    except TidalCredentialUnavailableError as exc:
        assert exc.reason == "no_credential"


# ---------------------------------------------------------------------------
# TidalStreamRouter — no-credential guilds fail observably (R5.4)
# ---------------------------------------------------------------------------


def test_no_owner_guild_fails_observably():
    """A guild with no recorded owner fails with no cross-user fallback (R5.4)."""
    router, _, _ = _make_router(owners=FakeOwners({}), tokens_by_guild={})
    try:
        router.client_for_guild("ghost")
        raise AssertionError("expected TidalCredentialUnavailableError")
    except TidalCredentialUnavailableError as exc:
        assert exc.reason == "no_owner"


def test_owner_lookup_failure_maps_to_no_owner():
    """An owner-store failure is an observable no_owner, not a crash (R5.4)."""
    router, _, _ = _make_router(
        owners=RaisingOwners(), tokens_by_guild={"g1": {"access_token": "t"}}
    )
    try:
        router.client_for_guild("g1")
        raise AssertionError("expected TidalCredentialUnavailableError")
    except TidalCredentialUnavailableError as exc:
        assert exc.reason == "no_owner"


def test_owner_without_credential_fails_observably_no_fallback():
    """An owner with no Tidal credential fails observably; no other user's token.

    The registry records a SPECIFIC per-``sub`` failed state (never green), and
    the router surfaces the typed reason with no fallback (R5.4, R7.2).
    """
    router, _, registry = _make_router(
        owners=FakeOwners({"g1": "subZ"}), tokens_by_guild={}
    )
    try:
        router.client_for_guild("g1")
        raise AssertionError("expected TidalCredentialUnavailableError")
    except TidalCredentialUnavailableError as exc:
        assert exc.reason == "no_credential"

    state = registry.state_of("subZ")
    assert state is not None
    assert state.phase is SessionPhase.FAILED
    assert state.reason == "no_credential"
    assert state.is_ready is False
    assert registry.live_count() == 0


def test_client_for_guild_returns_owning_users_token():
    """A guild's client streams with its owning user's token (R5.1)."""
    router, _, _ = _make_router(
        owners=FakeOwners({"g1": "subA"}),
        tokens_by_guild={"g1": {"access_token": "tokA"}},
    )
    client = router.client_for_guild("g1")
    assert client.streamer.token == "tokA"  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Per-user token selection + shared-owner reuse (R5.1, R5.2, R6.1)
# ---------------------------------------------------------------------------


def test_distinct_owners_get_distinct_tokens_and_clients():
    """Two guilds owned by DISTINCT users get distinct clients + tokens (R6.1)."""
    router, _, _ = _make_router(
        owners=FakeOwners({"g1": "subA", "g2": "subB"}),
        tokens_by_guild={
            "g1": {"access_token": "tokA"},
            "g2": {"access_token": "tokB"},
        },
    )
    client_a = router.client_for_guild("g1")
    client_b = router.client_for_guild("g2")
    assert client_a is not client_b
    assert client_a.streamer.token == "tokA"  # type: ignore[attr-defined]
    assert client_b.streamer.token == "tokB"  # type: ignore[attr-defined]


def test_two_guilds_same_owner_share_that_users_client():
    """Two guilds owned by the SAME user share that user's client (R5.2).

    The registry is keyed by owning ``sub``, so both guilds resolve to one
    client bound to that user's token — correct (same token), not a leak.
    """
    router, _, registry = _make_router(
        owners=FakeOwners({"g1": "subA", "g2": "subA"}),
        tokens_by_guild={
            "g1": {"access_token": "tokA"},
            "g2": {"access_token": "tokA"},
        },
    )
    client_1 = router.client_for_guild("g1")
    client_2 = router.client_for_guild("g2")
    assert client_1 is client_2  # one client per owning sub
    assert registry.live_count() == 1


# ---------------------------------------------------------------------------
# Property P1 — no cross-user leakage under randomized concurrency
# ---------------------------------------------------------------------------

# A small guild/owner space with a KNOWN 1:1 guild->owner->token mapping, so a
# leak (guild A served with B's token) is detectable. Distinct guilds map to
# distinct owners and distinct tokens.
_GUILDS = [f"g{i}" for i in range(8)]


def _owner_for(guild: str) -> str:
    return f"sub-{guild}"


def _token_for(guild: str) -> str:
    return f"tok-{guild}"


@settings(
    max_examples=60,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(
    ops=st.lists(st.sampled_from(_GUILDS), min_size=1, max_size=40),
    cap=st.integers(min_value=1, max_value=8),
)
def test_concurrent_requests_never_cross_users(ops: list[str], cap: int) -> None:
    """P1: concurrent guild requests each use their OWN owner's token (R6.1).

    Many worker threads race ``client_for_guild`` across a small guild space
    with a distinct owner+token per guild. Every returned client MUST carry the
    token of the requested guild's owner — a client built for another user could
    never be returned because the registry is keyed by the owning ``sub`` and
    each per-user client draws its token from its own guild-bound read-only
    source (no shared mutable token state).
    **Validates: Requirements 5.1, 5.2, 6.1**
    """
    owners = FakeOwners({g: _owner_for(g) for g in _GUILDS})
    tokens = {g: {"access_token": _token_for(g)} for g in _GUILDS}
    router, resolver, _ = _make_router(
        owners=owners, tokens_by_guild=tokens, max_sessions=cap
    )

    results: list[tuple[str, str]] = []
    results_lock = threading.Lock()
    errors: list[BaseException] = []
    barrier = threading.Barrier(len(ops))

    def worker(guild: str) -> None:
        try:
            barrier.wait()  # maximize interleaving
            client = router.client_for_guild(guild)
            token = client.streamer.token  # type: ignore[attr-defined]
            with results_lock:
                results.append((guild, token))
        except BaseException as exc:  # noqa: BLE001 - surface to the assertion
            with results_lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker, args=(g,)) for g in ops]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"unexpected worker errors: {errors!r}"
    # No cross-user leakage: every returned token belongs to the requested
    # guild's own owner.
    for guild, token in results:
        assert token == _token_for(guild)

    # No shared mutable token state: the resolver was only ever asked for the
    # provider-scoped token of guilds that were actually requested (never a
    # different guild's), and it hands back per-request copies.
    requested = set(ops)
    for gid, provider in resolver.resolve_calls:
        assert provider == PROVIDER_TIDAL
        assert gid in requested


@settings(max_examples=100, deadline=None)
@given(guild_a=st.sampled_from(_GUILDS), guild_b=st.sampled_from(_GUILDS))
def test_client_token_matches_requested_guild_owner(
    guild_a: str, guild_b: str
) -> None:
    """P1: each guild's client is bound to ITS owner's token, always (R6.1).

    Distinct guilds (distinct owners) never share a client or token; the same
    guild resolves to the same cached client.
    **Validates: Requirements 5.1, 6.1**
    """
    owners = FakeOwners({g: _owner_for(g) for g in _GUILDS})
    tokens = {g: {"access_token": _token_for(g)} for g in _GUILDS}
    router, _, _ = _make_router(owners=owners, tokens_by_guild=tokens)

    client_a = router.client_for_guild(guild_a)
    client_b = router.client_for_guild(guild_b)

    assert client_a.streamer.token == _token_for(guild_a)  # type: ignore[attr-defined]
    assert client_b.streamer.token == _token_for(guild_b)  # type: ignore[attr-defined]
    if guild_a == guild_b:
        assert client_a is client_b
    else:
        assert client_a is not client_b

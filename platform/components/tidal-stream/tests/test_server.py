"""Endpoint tests for the multi-tenant tidal-stream aiohttp server.

Covers the per-user streaming/search endpoints (R5.1), the guild→owner
resolution + observable no-credential failure with no cross-user fallback
(R5.4, R10.5), the per-``sub`` health surface (R7.3), and the OPTIONAL legacy
``/auth/callback`` code-exchange forward (R9.2). Uses in-memory fakes for the
credential store / owner lookup / streamer so no network or AWS access is
needed.
"""

from __future__ import annotations

import pytest
from aiohttp.test_utils import TestClient, TestServer
from hellodj_platform_logic.session_registry import SessionRegistry
from hellodj_platform_logic.types import SessionRegistryConfig
from hellodj_platform_logic.user_credential_resolver import (
    CredentialUnavailable,
)

from tidal_stream.server import build_app
from tidal_stream.streaming import TidalStreamError
from tidal_stream.user_sessions import (
    ReadOnlyTidalTokenSource,
    TidalStreamRouter,
    TidalUserClient,
)


class FakeStreamer:
    """Async streamer double keyed by the access token it was built with."""

    def __init__(self, token: str):
        self.token = token
        self.closed = False

    async def search(self, query, limit=10):
        return [
            {
                "id": "1",
                "title": query,
                "artist": self.token,
                "album": "",
                "duration": 0,
            }
        ]

    async def get_stream_url(self, track_id):
        if track_id == "missing":
            raise TidalStreamError("Tidal resource not found")
        return f"https://stream/{track_id}.flac?tok={self.token}"

    async def close(self):
        self.closed = True


class FakeResolver:
    """Resolver double returning a per-guild tokens dict or CredentialUnavailable."""

    def __init__(self, tokens_by_guild):
        self._tokens = tokens_by_guild

    def resolve(self, guild_id, provider):
        value = self._tokens.get(str(guild_id))
        if value is None:
            return CredentialUnavailable("no_credential")
        return value

    def invalidate(self, guild_id, provider):
        pass


class FakeOwners:
    """Owner lookup double mapping guild id -> owner sub."""

    def __init__(self, owners):
        self._owners = owners

    def owner_of(self, guild_id):
        return self._owners.get(str(guild_id))


def _make_router(*, owners, tokens_by_guild):
    resolver = FakeResolver(tokens_by_guild)
    registry: SessionRegistry = SessionRegistry(SessionRegistryConfig(max_sessions=8))

    def _factory(token_source: ReadOnlyTidalTokenSource):
        # Bind the fake streamer to the resolved access token so a leak would
        # surface (the artist/stream url echoes the token).
        token = token_source.get_access_token()
        return TidalUserClient(FakeStreamer(token))  # type: ignore[arg-type]

    return TidalStreamRouter(
        FakeOwners(owners),
        resolver,  # type: ignore[arg-type]
        registry,
        streamer_factory=_factory,  # type: ignore[arg-type]
    )


@pytest.fixture
async def client():
    router = _make_router(
        owners={"g1": "subA", "g2": "subB"},
        tokens_by_guild={
            "g1": {"access_token": "tokA"},
            "g2": {"access_token": "tokB"},
        },
    )
    app = build_app(router)
    server = TestServer(app)
    async with TestClient(server) as test_client:
        yield test_client


async def test_healthz_reports_pool_state(client):
    resp = await client.get("/healthz")
    assert resp.status == 200
    body = await resp.json()
    assert body["status"] == "ok"
    assert body["live_sessions"] == 0


async def test_search_uses_guild_owner_token(client):
    resp = await client.get("/search/g1", params={"q": "daft punk"})
    assert resp.status == 200
    body = await resp.json()
    # The fake echoes the token as the artist — g1 must see subA's token.
    assert body["results"][0]["artist"] == "tokA"


async def test_search_requires_query(client):
    resp = await client.get("/search/g1")
    assert resp.status == 400


async def test_stream_url_uses_guild_owner_token(client):
    resp = await client.get("/stream/g2/42")
    assert resp.status == 200
    body = await resp.json()
    assert body["stream_url"] == "https://stream/42.flac?tok=tokB"


async def test_stream_missing_track_is_502(client):
    resp = await client.get("/stream/g1/missing")
    assert resp.status == 502


async def test_no_owner_guild_fails_observably(client):
    """A guild with no recorded owner fails with no cross-user fallback (R5.4)."""
    resp = await client.get("/stream/unknown/42")
    assert resp.status == 404
    body = await resp.json()
    assert body["reason"] == "no_owner"


async def test_no_credential_guild_fails_observably():
    """A guild whose owner has no Tidal credential fails observably (R5.4)."""
    router = _make_router(owners={"g9": "subZ"}, tokens_by_guild={})
    app = build_app(router)
    server = TestServer(app)
    async with TestClient(server) as c:
        resp = await c.get("/stream/g9/42")
        assert resp.status == 404
        body = await resp.json()
        assert body["reason"] == "no_credential"


async def test_streaming_routes_not_ready_without_router():
    """No router (store unavailable) → observably not-ready, no fake-green (R7.5)."""
    app = build_app(None)
    server = TestServer(app)
    async with TestClient(server) as c:
        health = await c.get("/healthz")
        assert (await health.json())["status"] == "not_ready"
        resp = await c.get("/stream/g1/42")
        assert resp.status == 503


async def test_no_auth_callback_route_without_token_manager(client):
    """Without a legacy token manager, /auth/callback is not registered."""
    resp = await client.get("/auth/callback", params={"code": "abc"})
    assert resp.status == 404


class FakeToken:
    expires_at = 1234.0


class FakeTokenManager:
    """Legacy token manager double capturing the callback code exchange."""

    def __init__(self):
        self.codes: list[str] = []

    def complete_authorization(self, code):
        if code == "bad":
            raise ValueError("invalid code")
        self.codes.append(code)
        return FakeToken()


@pytest.fixture
async def callback_client():
    router = _make_router(owners={"g1": "subA"}, tokens_by_guild={"g1": {"access_token": "t"}})
    tokens = FakeTokenManager()
    app = build_app(router, token_manager=tokens)  # type: ignore[arg-type]
    server = TestServer(app)
    async with TestClient(server) as c:
        c.tokens = tokens  # type: ignore[attr-defined]
        yield c


async def test_auth_callback_exchanges_code(callback_client):
    resp = await callback_client.get("/auth/callback", params={"code": "abc123"})
    assert resp.status == 200
    body = await resp.json()
    assert body["status"] == "authorized"
    assert callback_client.tokens.codes == ["abc123"]


async def test_auth_callback_requires_code(callback_client):
    resp = await callback_client.get("/auth/callback")
    assert resp.status == 400


async def test_auth_callback_propagates_provider_error(callback_client):
    resp = await callback_client.get(
        "/auth/callback", params={"error": "access_denied", "error_description": "no"}
    )
    assert resp.status == 400
    body = await resp.json()
    assert body["error"] == "access_denied"


async def test_auth_callback_exchange_failure_is_502(callback_client):
    resp = await callback_client.get("/auth/callback", params={"code": "bad"})
    assert resp.status == 502

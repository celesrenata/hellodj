"""Endpoint tests for the tidal-stream aiohttp server.

Covers the streaming/search endpoints (R6.1) and the HelloDJ-owned
``/auth/callback`` OAuth code exchange endpoint the web-ui forwards to (R9.2).
Uses lightweight fakes for the token manager and streamer so no network or AWS
access is needed.
"""

from __future__ import annotations

import pytest
from aiohttp.test_utils import TestClient, TestServer

from tidal_stream.server import build_app
from tidal_stream.streaming import TidalStreamError


class FakeStreamer:
    """Async streamer double."""

    def __init__(self):
        self.closed = False

    async def search(self, query, limit=10):
        return [{"id": "1", "title": query, "artist": "a", "album": "", "duration": 0}]

    async def get_stream_url(self, track_id):
        if track_id == "missing":
            raise TidalStreamError("Tidal resource not found")
        return f"https://stream/{track_id}.flac"

    async def close(self):
        self.closed = True


class FakeToken:
    expires_at = 1234.0


class FakeTokenManager:
    """Token manager double capturing the callback code exchange."""

    def __init__(self):
        self.codes: list[str] = []

    def complete_authorization(self, code):
        if code == "bad":
            raise ValueError("invalid code")
        self.codes.append(code)
        return FakeToken()


@pytest.fixture
async def client():
    streamer = FakeStreamer()
    tokens = FakeTokenManager()
    app = build_app(tokens, streamer)  # type: ignore[arg-type]
    server = TestServer(app)
    async with TestClient(server) as test_client:
        test_client.streamer = streamer  # type: ignore[attr-defined]
        test_client.tokens = tokens  # type: ignore[attr-defined]
        yield test_client


async def test_healthz(client):
    resp = await client.get("/healthz")
    assert resp.status == 200
    assert (await resp.json())["status"] == "ok"


async def test_search_returns_results(client):
    resp = await client.get("/search", params={"q": "daft punk"})
    assert resp.status == 200
    body = await resp.json()
    assert body["results"][0]["title"] == "daft punk"


async def test_search_requires_query(client):
    resp = await client.get("/search")
    assert resp.status == 400


async def test_stream_url_resolution(client):
    resp = await client.get("/tracks/42/stream")
    assert resp.status == 200
    body = await resp.json()
    assert body["stream_url"] == "https://stream/42.flac"


async def test_stream_missing_track_is_502(client):
    resp = await client.get("/tracks/missing/stream")
    assert resp.status == 502


async def test_auth_callback_exchanges_code(client):
    """The callback forwards the code to the token manager (R9.2)."""
    resp = await client.get("/auth/callback", params={"code": "abc123"})
    assert resp.status == 200
    body = await resp.json()
    assert body["status"] == "authorized"
    assert client.tokens.codes == ["abc123"]


async def test_auth_callback_requires_code(client):
    resp = await client.get("/auth/callback")
    assert resp.status == 400


async def test_auth_callback_propagates_provider_error(client):
    resp = await client.get(
        "/auth/callback", params={"error": "access_denied", "error_description": "no"}
    )
    assert resp.status == 400
    body = await resp.json()
    assert body["error"] == "access_denied"


async def test_auth_callback_exchange_failure_is_502(client):
    resp = await client.get("/auth/callback", params={"code": "bad"})
    assert resp.status == 502

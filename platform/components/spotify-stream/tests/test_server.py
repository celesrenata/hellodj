"""Tests for the spotify-stream aiohttp server (routes, health, capture).

Uses aiohttp's test client with in-memory fakes for the router and capture
service (no live AWS / librespot / Spotify). Verifies:

* ``/stream`` and ``/preload`` resolve ``guild→owner`` server-side and serve the
  per-user track (R3.2), and map credential-unavailable reasons to observable,
  non-secret HTTP errors (R7.1);
* ``/health`` + ``/auth/status`` report per-``sub`` session state (never a single
  global status, never token material — R7.3) and report ``not_ready`` when the
  store is unavailable (no fake-green — R7.5);
* the librespot capture endpoints implement the task-2.2 contract shape.

Requirements: 3.2, 3.6, 7.1, 7.3, 7.5, 10.5
"""

from __future__ import annotations

import pytest
from aiohttp.test_utils import TestClient, TestServer
from hellodj_platform_logic.session_registry import SessionRegistry
from hellodj_platform_logic.types import SessionRegistryConfig

from spotify_stream.server import build_app
from spotify_stream.session_pool import SpotifyCredentialUnavailableError

# ── Fake router / capture ──────────────────────────────────────────────────────


class FakeRouter:
    """Minimal router double exposing the surface the server calls."""

    def __init__(self, *, registry, audio=b"AUDIO", codec="MP3", error=None):
        self._registry = registry
        self._audio = audio
        self._codec = codec
        self._error = error
        self.calls: list[tuple[str, str]] = []

    @property
    def registry(self):
        return self._registry

    def load_track_for_guild(self, guild_id, track_id):
        self.calls.append((str(guild_id), str(track_id)))
        if self._error is not None:
            raise SpotifyCredentialUnavailableError(self._error)
        return self._audio, self._codec


class FakeCapture:
    """Capture double implementing the task-2.2 start/complete contract."""

    def __init__(self, *, url="https://accounts.spotify.com/authorize?x", blob=None):
        self._url = url
        self._blob = blob or {"username": "u", "credentials": "C", "type": "T"}
        self.started: list[tuple[str, str]] = []
        self.completed: list[tuple[str, str]] = []

    def start(self, sub, redirect_uri):
        self.started.append((sub, redirect_uri))
        return self._url

    def complete(self, sub, code):
        self.completed.append((sub, code))
        return self._blob


def _registry(states=None):
    reg: SessionRegistry = SessionRegistry(
        SessionRegistryConfig(max_sessions=8, idle_timeout_seconds=900.0)
    )
    if states:
        for sub, factory in states.items():
            try:
                reg.get_or_create(sub, factory)
            except Exception:  # noqa: BLE001 - failure states are intentional
                pass
    return reg


async def _client(app) -> TestClient:
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    return client


# ── stream / preload ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stream_serves_per_user_audio():
    router = FakeRouter(registry=_registry())
    client = await _client(build_app(router))
    try:
        resp = await client.get("/stream/g1/trk")
        assert resp.status == 200
        assert await resp.read() == b"AUDIO"
        assert resp.content_type == "audio/mpeg"
        # The guild id was resolved server-side (passed through to the router).
        assert router.calls == [("g1", "trk")]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_preload_reports_size():
    router = FakeRouter(registry=_registry())
    client = await _client(build_app(router))
    try:
        resp = await client.get("/preload/g1/spotify:track:abc")
        assert resp.status == 200
        body = await resp.json()
        assert body["status"] == "ok"
        assert body["track_id"] == "abc"
        assert body["size"] == len(b"AUDIO")
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_stream_no_credential_is_404_observable():
    router = FakeRouter(registry=_registry(), error="no_owner")
    client = await _client(build_app(router))
    try:
        resp = await client.get("/stream/ghost/trk")
        assert resp.status == 404
        body = await resp.json()
        assert body["reason"] == "no_owner"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_stream_not_premium_is_502_observable():
    router = FakeRouter(registry=_registry(), error="not_premium")
    client = await _client(build_app(router))
    try:
        resp = await client.get("/stream/g1/trk")
        assert resp.status == 502
        body = await resp.json()
        assert body["reason"] == "not_premium"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_stream_not_ready_when_store_unavailable():
    # No router wired -> observably not-ready (no fake-green, R7.5).
    client = await _client(build_app(None))
    try:
        resp = await client.get("/stream/g1/trk")
        assert resp.status == 503
        body = await resp.json()
        assert body["reason"] == "not_ready"
    finally:
        await client.close()


# ── health / auth status (per-sub, no global, no tokens) ────────────────────────


@pytest.mark.asyncio
async def test_health_reports_pool_counts():
    reg = _registry({"subA": lambda s: object()})
    router = FakeRouter(registry=reg)
    client = await _client(build_app(router))
    try:
        resp = await client.get("/health")
        assert resp.status == 200
        body = await resp.json()
        assert body["status"] == "ok"
        assert body["live_sessions"] == 1
        assert body["tracked_sessions"] == 1
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_auth_status_reports_per_sub_states_hashed():
    from hellodj_platform_logic.session_registry import SessionCreateError

    def _fail(_s):
        raise SessionCreateError("not_premium")

    reg = _registry({"subA": lambda s: object(), "subFree": _fail})
    router = FakeRouter(registry=reg)
    client = await _client(build_app(router))
    try:
        resp = await client.get("/auth/status")
        body = await resp.json()
        # Per-sub map, keyed by a short digest (never the raw sub / token).
        assert body["status"] == "ok"
        assert len(body["sessions"]) == 2
        assert "subA" not in body["sessions"]  # raw sub not echoed
        phases = {v["phase"] for v in body["sessions"].values()}
        assert "ready" in phases and "failed" in phases
        # The failed reason is surfaced (honest failure), no token material.
        reasons = {v["reason"] for v in body["sessions"].values()}
        assert "not_premium" in reasons
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_health_not_ready_without_router():
    client = await _client(build_app(None))
    try:
        resp = await client.get("/health")
        body = await resp.json()
        assert body["status"] == "not_ready"
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reason",
    ["no_credential", "refresh_failed", "session_create_failed", "not_premium"],
)
async def test_auth_status_surfaces_each_specific_failed_reason(reason):
    """Each concrete per-``sub`` failure reason is reported honestly (R7.1/R7.3).

    A guild owner whose session build fails for any of the documented reasons
    (``no_credential`` / ``refresh_failed`` / ``session_create_failed`` /
    ``not_premium``) is surfaced as a SPECIFIC ``failed(reason)`` state — not a
    generic "not ready" and never green (no fake-green, R7.5).
    """
    from hellodj_platform_logic.session_registry import SessionCreateError

    def _fail(_s):
        raise SessionCreateError(reason)

    reg = _registry({"subBad": _fail})
    router = FakeRouter(registry=reg)
    client = await _client(build_app(router))
    try:
        resp = await client.get("/auth/status")
        body = await resp.json()
        assert body["status"] == "ok"
        # Exactly one tracked session, in the specific failed state.
        (entry,) = body["sessions"].values()
        assert entry["phase"] == "failed"
        assert entry["reason"] == reason
        # A failed session is never counted as live (no fake-green).
        assert body["live_sessions"] == 0
        assert body["tracked_sessions"] == 1
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_health_reports_no_single_global_session_status():
    """Neither health surface exposes a single global session status (R7.3).

    The multi-tenant sidecar must report per-``sub`` state, so the legacy
    single-global keys (``session`` / ``authenticated`` / ``session_status``)
    must NOT appear on ``/health`` or ``/auth/status``.
    """
    from hellodj_platform_logic.session_registry import SessionCreateError

    def _fail(_s):
        raise SessionCreateError("not_premium")

    reg = _registry({"subA": lambda s: object(), "subFree": _fail})
    router = FakeRouter(registry=reg)
    client = await _client(build_app(router))
    forbidden = {"session", "authenticated", "session_status", "logged_in"}
    try:
        for path in ("/health", "/auth/status"):
            resp = await client.get(path)
            body = await resp.json()
            assert forbidden.isdisjoint(body.keys()), f"{path} leaked a global status"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_auth_status_never_leaks_token_or_raw_sub():
    """The health surface never echoes a raw ``sub`` or any token material (R7.1).

    Per-``sub`` entries are keyed by a short non-reversible digest, and no token
    string a credential might carry appears anywhere in the serialized body.
    """
    from hellodj_platform_logic.session_registry import SessionCreateError

    secret_token = "BQD-super-secret-refresh-token-xyz"  # noqa: S105 - test sentinel

    def _fail(_s):
        # A factory must NEVER put token material in the reason; this asserts the
        # surface stays clean even if a reason string were mishandled upstream.
        raise SessionCreateError("session_create_failed")

    reg = _registry({secret_token: lambda s: object(), "subFree": _fail})
    router = FakeRouter(registry=reg)
    client = await _client(build_app(router))
    try:
        resp = await client.get("/auth/status")
        raw = await resp.text()
        # The raw sub (used here as a stand-in for sensitive material) is hashed,
        # never echoed verbatim.
        assert secret_token not in raw
        body = await resp.json()
        assert secret_token not in body["sessions"]
        # Every key is a 12-char hex digest, not a raw identifier.
        assert all(len(k) == 12 for k in body["sessions"])
    finally:
        await client.close()


# ── librespot capture endpoints (task 2.2 contract shape) ───────────────────────


@pytest.mark.asyncio
async def test_librespot_start_returns_authorize_url():
    capture = FakeCapture()
    client = await _client(build_app(None, capture=capture))
    try:
        resp = await client.post(
            "/auth/librespot/start",
            json={"sub": "subA", "redirect_uri": "https://web/cb"},
        )
        assert resp.status == 200
        body = await resp.json()
        assert body["authorize_url"].startswith("https://accounts.spotify.com/")
        assert capture.started == [("subA", "https://web/cb")]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_librespot_complete_returns_credentials_blob():
    capture = FakeCapture(blob={"username": "u", "credentials": "C", "type": "T"})
    client = await _client(build_app(None, capture=capture))
    try:
        resp = await client.post(
            "/auth/librespot/complete", json={"sub": "subA", "code": "xyz"}
        )
        assert resp.status == 200
        body = await resp.json()
        assert body["credentials"] == {"username": "u", "credentials": "C", "type": "T"}
        assert capture.completed == [("subA", "xyz")]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_librespot_start_missing_fields_is_400():
    capture = FakeCapture()
    client = await _client(build_app(None, capture=capture))
    try:
        resp = await client.post("/auth/librespot/start", json={"sub": "subA"})
        assert resp.status == 400
    finally:
        await client.close()

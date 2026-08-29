"""Tests for the sidecar-side librespot capture service (task 2.2 contract).

Exercises :class:`LibrespotCaptureService` with a fake :class:`CaptureBackend`
(no native librespot / Spotify egress). Verifies the two-step start→complete
flow, per-``sub`` verifier retention, TTL sweep, and clean state teardown.

Requirements: 3.3, 6.4, 10.3
"""

from __future__ import annotations

import pytest

from spotify_stream.librespot_capture import (
    LibrespotCaptureError,
    LibrespotCaptureService,
)


class FakeBackend:
    """In-memory capture backend recording start/complete/discard calls."""

    def __init__(self, *, blob=None, fail_complete=False):
        self._blob = blob or {"username": "u", "credentials": "C", "type": "T"}
        self._fail = fail_complete
        self.pending: set[str] = set()
        self.discarded: list[str] = []

    def authorize_url(self, sub, redirect_uri):
        self.pending.add(sub)
        return f"https://accounts.spotify.com/authorize?sub={sub}"

    def complete(self, sub, code):
        if self._fail or sub not in self.pending:
            return None
        return self._blob

    def discard(self, sub):
        self.pending.discard(sub)
        self.discarded.append(sub)


def test_start_then_complete_happy_path():
    backend = FakeBackend()
    clock = [0.0]
    svc = LibrespotCaptureService(backend, clock=lambda: clock[0])

    url = svc.start("subA", "https://web/cb")
    assert url.startswith("https://accounts.spotify.com/")

    creds = svc.complete("subA", "code123")
    assert creds == {"username": "u", "credentials": "C", "type": "T"}
    # Verifier state cleared after complete.
    assert "subA" in backend.discarded


def test_complete_without_start_fails():
    svc = LibrespotCaptureService(FakeBackend())
    with pytest.raises(LibrespotCaptureError) as exc:
        svc.complete("subA", "code")
    assert "no_pending_capture" in str(exc.value)


def test_start_requires_sub_and_redirect():
    svc = LibrespotCaptureService(FakeBackend())
    with pytest.raises(LibrespotCaptureError):
        svc.start("", "https://web/cb")
    with pytest.raises(LibrespotCaptureError):
        svc.start("subA", "")


def test_complete_failure_surfaces_and_clears_state():
    backend = FakeBackend(fail_complete=True)
    svc = LibrespotCaptureService(backend)
    svc.start("subA", "https://web/cb")
    with pytest.raises(LibrespotCaptureError) as exc:
        svc.complete("subA", "code")
    assert "capture_failed" in str(exc.value)
    # State cleared even on failure.
    assert "subA" in backend.discarded


def test_ttl_sweep_drops_abandoned_capture():
    backend = FakeBackend()
    clock = [0.0]
    svc = LibrespotCaptureService(backend, ttl_seconds=100.0, clock=lambda: clock[0])
    svc.start("subA", "https://web/cb")
    # Advance past the TTL and start another capture (triggers the sweep).
    clock[0] = 200.0
    svc.start("subB", "https://web/cb")
    # subA was swept; completing it now fails as no pending capture.
    with pytest.raises(LibrespotCaptureError):
        svc.complete("subA", "code")
    assert "subA" in backend.discarded


def test_per_sub_isolation_between_captures():
    backend = FakeBackend()
    svc = LibrespotCaptureService(backend)
    svc.start("subA", "https://web/cb")
    svc.start("subB", "https://web/cb")
    # Completing subB does not disturb subA's pending state.
    svc.complete("subB", "code-b")
    creds = svc.complete("subA", "code-a")
    assert creds is not None

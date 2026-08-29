"""Unit tests for the durable token-refresh watchdog (Task 8).

Exercises the four behaviors the design calls out for the watchdog loop:

* **tick refreshes only near-expiry** — the pass is driven entirely by
  ``iter_near_expiry`` and calls ``record_refresh(new_state=...)`` for each
  yielded item on success (R5.2, R5.3).
* **failure isolation** — one item's refresh raising does not stop the pass;
  that item gets ``record_refresh(error=...)``; the others still process; the
  tick never raises (R5.4, Property 5).
* **degraded no-op** — with no datastore / KMS / clients the watchdog does not
  start and ``tick`` on an empty store does nothing (R5.7).
* **lock safety** — ``record_refresh`` is the write path (the optimistic lock
  lives in the service); a raised ``OptimisticLockError`` is logged and the pass
  continues (R5.5, Property 6).

The tests use a fake ``SourceCredentialService`` and fake refresh clients so the
watchdog logic is isolated from AWS, KMS, and the network.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from hellodj_platform_logic.data_access.core import OptimisticLockError
from hellodj_platform_logic.source_refresh import TokenState

from playback_orchestrator.token_watchdog import TokenWatchdog
from playback_orchestrator.watchdog_bootstrap import (
    build_clients_by_provider,
    build_watchdog,
)

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class _NearExpiry:
    """Stand-in for ``NearExpiryCredential`` (identity + status, no token)."""

    sub: str
    provider: str
    expires_at: float = 0.0
    refresh_status: str = ""


@dataclass
class _FakeClient:
    """A refresh client that mints a fresh, long-lived token."""

    provider: str
    ttl: float = 3600.0
    minted_refresh: str = "rotated-refresh"

    def refresh(self, refresh_token: str, now: float) -> TokenState:
        return TokenState(
            access_token=f"{self.provider}-access",
            refresh_token=self.minted_refresh or refresh_token,
            expires_at=now + self.ttl,
        )


@dataclass
class _RaisingClient:
    """A refresh client whose ``refresh`` always raises (failure isolation)."""

    provider: str

    def refresh(self, refresh_token: str, now: float) -> TokenState:
        raise RuntimeError("provider token endpoint said no")


@dataclass
class _FakeService:
    """Minimal fake of the ``SourceCredentialService`` surface the watchdog uses.

    Records every ``record_refresh`` call so tests can assert exactly which
    items were written and whether success (``new_state``) or failure
    (``error``) was recorded — without touching DynamoDB or KMS.
    """

    near: list[_NearExpiry] = field(default_factory=list)
    tokens: dict[tuple[str, str], TokenState] = field(default_factory=dict)
    record_ok: list[tuple[str, str, TokenState]] = field(default_factory=list)
    record_fail: list[tuple[str, str, str]] = field(default_factory=list)
    #: When set, ``record_refresh`` raises this on the success path.
    raise_on_record: Exception | None = None

    def iter_near_expiry(self, now: float, threshold: float):
        return iter(list(self.near))

    def load_token(self, sub: str, provider: str) -> TokenState | None:
        return self.tokens.get((sub, provider))

    def record_refresh(
        self,
        sub: str,
        provider: str,
        *,
        new_state: TokenState | None = None,
        error: str | None = None,
    ) -> None:
        if new_state is not None:
            if self.raise_on_record is not None:
                raise self.raise_on_record
            self.record_ok.append((sub, provider, new_state))
        else:
            self.record_fail.append((sub, provider, error or ""))


def _watchdog(service: _FakeService, clients: dict[str, Any]) -> TokenWatchdog:
    return TokenWatchdog(service, clients, clock=lambda: 1_000.0)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# tick refreshes only near-expiry
# ---------------------------------------------------------------------------


def test_tick_refreshes_only_near_expiry_items() -> None:
    service = _FakeService(
        near=[
            _NearExpiry(sub="u1", provider="youtube"),
            _NearExpiry(sub="u2", provider="spotify"),
        ],
        tokens={
            ("u1", "youtube"): TokenState("a1", "r1", expires_at=1_000.0),
            ("u2", "spotify"): TokenState("a2", "r2", expires_at=1_000.0),
        },
    )
    clients = {
        "youtube": _FakeClient("youtube"),
        "spotify": _FakeClient("spotify"),
    }
    watchdog = _watchdog(service, clients)

    refreshed = watchdog.tick()

    assert refreshed == 2
    assert {(s, p) for s, p, _ in service.record_ok} == {
        ("u1", "youtube"),
        ("u2", "spotify"),
    }
    assert service.record_fail == []
    # Each write-back carries a fresh, non-expired token.
    for _sub, _prov, state in service.record_ok:
        assert state.expires_at > 1_000.0


def test_tick_only_processes_what_iter_near_expiry_yields() -> None:
    # A user with a not-near-expiry credential simply isn't yielded, so the
    # watchdog never touches it (enumeration drives the pass).
    service = _FakeService(
        near=[_NearExpiry(sub="u1", provider="youtube")],
        tokens={
            ("u1", "youtube"): TokenState("a1", "r1", expires_at=1_000.0),
            ("u2", "spotify"): TokenState("a2", "r2", expires_at=9_999.0),
        },
    )
    clients = {"youtube": _FakeClient("youtube"), "spotify": _FakeClient("spotify")}
    watchdog = _watchdog(service, clients)

    watchdog.tick()

    assert [(s, p) for s, p, _ in service.record_ok] == [("u1", "youtube")]


def test_provider_without_client_is_skipped() -> None:
    # discord is identity-only: no client, so nothing is refreshed or recorded.
    service = _FakeService(near=[_NearExpiry(sub="u1", provider="discord")])
    watchdog = _watchdog(service, {"youtube": _FakeClient("youtube")})

    refreshed = watchdog.tick()

    assert refreshed == 0
    assert service.record_ok == []
    assert service.record_fail == []


# ---------------------------------------------------------------------------
# failure isolation (Property 5)
# ---------------------------------------------------------------------------


def test_one_failure_does_not_stop_the_pass() -> None:
    service = _FakeService(
        near=[
            _NearExpiry(sub="u1", provider="spotify"),  # will fail
            _NearExpiry(sub="u2", provider="youtube"),  # must still succeed
        ],
        tokens={
            ("u1", "spotify"): TokenState("a1", "r1", expires_at=1_000.0),
            ("u2", "youtube"): TokenState("a2", "r2", expires_at=1_000.0),
        },
    )
    clients = {
        "spotify": _RaisingClient("spotify"),
        "youtube": _FakeClient("youtube"),
    }
    watchdog = _watchdog(service, clients)

    refreshed = watchdog.tick()  # must not raise

    assert refreshed == 1
    # The failing item recorded a failure (prior blob left intact by the service).
    assert [(s, p) for s, p, _ in service.record_fail] == [("u1", "spotify")]
    # The healthy item still refreshed.
    assert [(s, p) for s, p, _ in service.record_ok] == [("u2", "youtube")]


def test_failure_reason_carries_no_token_material() -> None:
    service = _FakeService(
        near=[_NearExpiry(sub="u1", provider="spotify")],
        tokens={("u1", "spotify"): TokenState("secret-access", "secret-refresh", 1_000.0)},
    )
    watchdog = _watchdog(service, {"spotify": _RaisingClient("spotify")})

    watchdog.tick()

    assert len(service.record_fail) == 1
    _sub, _prov, reason = service.record_fail[0]
    assert "secret-access" not in reason
    assert "secret-refresh" not in reason


def test_missing_token_is_isolated_not_fatal() -> None:
    # iter_near_expiry yielded an item but load_token returns None (vanished):
    # the item is skipped, the pass continues, nothing crashes.
    service = _FakeService(
        near=[
            _NearExpiry(sub="u1", provider="youtube"),
            _NearExpiry(sub="u2", provider="spotify"),
        ],
        tokens={("u2", "spotify"): TokenState("a2", "r2", expires_at=1_000.0)},
    )
    clients = {"youtube": _FakeClient("youtube"), "spotify": _FakeClient("spotify")}
    watchdog = _watchdog(service, clients)

    refreshed = watchdog.tick()

    assert refreshed == 1
    assert [(s, p) for s, p, _ in service.record_ok] == [("u2", "spotify")]


def test_enumeration_failure_is_swallowed() -> None:
    class _BadService(_FakeService):
        def iter_near_expiry(self, now: float, threshold: float):
            raise RuntimeError("scan blew up")

    watchdog = _watchdog(_BadService(), {"youtube": _FakeClient("youtube")})

    assert watchdog.tick() == 0  # must not raise


# ---------------------------------------------------------------------------
# lock safety (Property 6)
# ---------------------------------------------------------------------------


def test_optimistic_lock_error_is_logged_and_pass_continues() -> None:
    lock_error = OptimisticLockError(
        "conflict", error_code="ConditionalCheckFailedException"
    )
    service = _FakeService(
        near=[
            _NearExpiry(sub="u1", provider="youtube"),  # record raises lock error
            _NearExpiry(sub="u2", provider="youtube"),
        ],
        tokens={
            ("u1", "youtube"): TokenState("a1", "r1", expires_at=1_000.0),
            ("u2", "youtube"): TokenState("a2", "r2", expires_at=1_000.0),
        },
        raise_on_record=lock_error,
    )
    watchdog = _watchdog(service, {"youtube": _FakeClient("youtube")})

    refreshed = watchdog.tick()  # must not raise despite lock errors

    # Both success write-backs raised the lock error, so neither counted as a
    # completed refresh, but the loop kept going and recorded each as a failure
    # (a losing writer re-reads/retries at the service layer — here the fake
    # surfaces the conflict and the watchdog isolates it per item).
    assert refreshed == 0
    assert {(s, p) for s, p, _ in service.record_fail} == {
        ("u1", "youtube"),
        ("u2", "youtube"),
    }
    assert len(service.record_fail) == 2


# ---------------------------------------------------------------------------
# degraded no-op (R5.7)
# ---------------------------------------------------------------------------


def test_degraded_build_returns_none(monkeypatch: Any) -> None:
    # No env configured at all → build_watchdog degrades to None (nothing starts).
    for var in (
        "HELLODJ_CORE_TABLE",
        "HELLODJ_SOURCE_CREDS_KMS_KEY_ID",
        "GOOGLE_CLIENT_ID",
        "GOOGLE_CLIENT_SECRET",
        "SPOTIFY_CLIENT_ID",
        "SPOTIFY_CLIENT_SECRET",
        "TIDAL_CLIENT_ID",
        "TIDAL_TOKEN_URL",
    ):
        monkeypatch.delenv(var, raising=False)

    # No datastore/KMS/CMK → the watchdog stays disabled even though it can
    # always build the YouTube device refresh clients (it has nothing to
    # refresh without the store).
    assert build_watchdog() is None
    # YouTube / YouTube Music always have a refresh client (the youtube-source
    # plugin's PUBLIC device client — no operator Google app), so the provider
    # map is never empty even with no OAuth env. Spotify/Tidal are absent here.
    clients = build_clients_by_provider()
    assert set(clients) == {"youtube", "youtube_music"}


def test_degraded_tick_on_empty_store_is_noop() -> None:
    service = _FakeService(near=[])
    watchdog = _watchdog(service, {})

    assert watchdog.tick() == 0
    assert service.record_ok == []
    assert service.record_fail == []


def test_build_clients_includes_configured_providers(monkeypatch: Any) -> None:
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "gid")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "gsecret")
    monkeypatch.setenv("SPOTIFY_CLIENT_ID", "sid")
    monkeypatch.setenv("SPOTIFY_CLIENT_SECRET", "ssecret")
    monkeypatch.delenv("TIDAL_CLIENT_ID", raising=False)
    monkeypatch.delenv("TIDAL_TOKEN_URL", raising=False)

    clients = build_clients_by_provider()

    # youtube AND youtube_music both use the Google client; spotify present.
    assert set(clients) == {"youtube", "youtube_music", "spotify"}
    assert clients["youtube"].provider == "youtube"
    assert clients["youtube_music"].provider == "youtube_music"

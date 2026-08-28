"""Tests for GuildCredentialResolver (bot per-guild source resolution).

Validates Requirement 6:
- 6.1 loads the correct per-guild secret name and parses the tokens
- 6.2 falls back to the global credential when a guild has none; None when
      neither the guild nor a global secret exists
- 6.3 never leaks one guild's tokens to another guild's resolution
- 6.4 caches per (guild, provider) with a bounded TTL and refreshes on expiry
"""

from __future__ import annotations

import json

import pytest
from guild_credentials import (
    GLOBAL_FALLBACK_LEAVES,
    GuildCredentialResolver,
    guild_source_secret_name,
)

STAGE = "beta"


class FakeClock:
    """Deterministic monotonic clock for TTL testing."""

    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class FakeSecrets:
    """Fake secretsmanager client backed by an in-memory name → dict store.

    Records every ``get_secret_value`` call so tests can assert the exact
    secret names requested and count Secrets Manager hits (for TTL/caching).
    """

    def __init__(self, store: dict[str, object] | None = None) -> None:
        # name -> either a token dict (serialized to JSON) or a raw string
        self.store: dict[str, object] = dict(store or {})
        self.calls: list[str] = []

    def get_secret_value(self, **kwargs: object) -> dict[str, object]:
        name = str(kwargs["SecretId"])
        self.calls.append(name)
        if name not in self.store:
            # Mirror boto3: a missing secret raises (ResourceNotFoundException).
            raise KeyError(f"Secrets Manager can't find {name}")
        value = self.store[name]
        raw = value if isinstance(value, str) else json.dumps(value)
        return {"SecretString": raw}


def _resolver(secrets: FakeSecrets, clock: FakeClock, **kwargs) -> GuildCredentialResolver:
    return GuildCredentialResolver(
        secrets, stage=STAGE, time_fn=clock, **kwargs
    )


# ── Secret naming (shared verbatim with web-ui) ─────────────────────────


class TestSecretName:
    def test_name_matches_web_ui_scheme(self):
        assert (
            guild_source_secret_name("beta", "111", "tidal")
            == "hellodj/beta/guild/111/tidal"
        )

    def test_name_is_unique_per_guild(self):
        a = guild_source_secret_name("beta", "111", "tidal")
        b = guild_source_secret_name("beta", "222", "tidal")
        assert a != b


# ── R6.1: load per-guild secret + parse tokens ──────────────────────────


class TestResolveLoadsPerGuildSecret:
    def test_resolves_guild_tokens(self):
        secrets = FakeSecrets(
            {"hellodj/beta/guild/111/tidal": {"refresh_token": "gA-tidal"}}
        )
        clock = FakeClock()
        r = _resolver(secrets, clock)

        tokens = r.resolve("111", "tidal")

        assert tokens == {"refresh_token": "gA-tidal"}
        # requested exactly the guild-scoped secret name
        assert secrets.calls == ["hellodj/beta/guild/111/tidal"]

    def test_accepts_int_guild_id(self):
        secrets = FakeSecrets(
            {"hellodj/beta/guild/111/spotify": {"token": "x"}}
        )
        r = _resolver(secrets, FakeClock())
        assert r.resolve(111, "spotify") == {"token": "x"}

    def test_invalid_json_returns_none(self):
        secrets = FakeSecrets({"hellodj/beta/guild/111/tidal": "not json {{{"})
        r = _resolver(secrets, FakeClock())
        assert r.resolve("111", "tidal") is None

    def test_non_object_json_returns_none(self):
        secrets = FakeSecrets({"hellodj/beta/guild/111/tidal": "[1, 2, 3]"})
        r = _resolver(secrets, FakeClock())
        assert r.resolve("111", "tidal") is None


# ── R6.4: cache TTL + refresh ───────────────────────────────────────────


class TestCacheTtl:
    def test_two_resolves_within_ttl_hit_secrets_once(self):
        secrets = FakeSecrets(
            {"hellodj/beta/guild/111/tidal": {"refresh_token": "v1"}}
        )
        clock = FakeClock()
        r = _resolver(secrets, clock, ttl_seconds=300.0)

        first = r.resolve("111", "tidal")
        clock.advance(299.0)  # still within TTL
        second = r.resolve("111", "tidal")

        assert first == second == {"refresh_token": "v1"}
        assert len(secrets.calls) == 1  # cached — no second GetSecretValue

    def test_resolve_after_ttl_refreshes(self):
        secrets = FakeSecrets(
            {"hellodj/beta/guild/111/tidal": {"refresh_token": "v1"}}
        )
        clock = FakeClock()
        r = _resolver(secrets, clock, ttl_seconds=300.0)

        r.resolve("111", "tidal")
        # rotate the stored token, then advance past the TTL
        secrets.store["hellodj/beta/guild/111/tidal"] = {"refresh_token": "v2"}
        clock.advance(301.0)
        refreshed = r.resolve("111", "tidal")

        assert refreshed == {"refresh_token": "v2"}
        assert len(secrets.calls) == 2  # refreshed after expiry

    def test_ttl_boundary_is_exclusive_refresh(self):
        # At exactly TTL the entry is considered expired (now < expires is False).
        secrets = FakeSecrets({"hellodj/beta/guild/111/tidal": {"t": 1}})
        clock = FakeClock()
        r = _resolver(secrets, clock, ttl_seconds=300.0)
        r.resolve("111", "tidal")
        clock.advance(300.0)
        r.resolve("111", "tidal")
        assert len(secrets.calls) == 2

    def test_invalidate_forces_refresh(self):
        secrets = FakeSecrets({"hellodj/beta/guild/111/tidal": {"t": 1}})
        clock = FakeClock()
        r = _resolver(secrets, clock, ttl_seconds=300.0)
        r.resolve("111", "tidal")
        r.invalidate("111", "tidal")
        r.resolve("111", "tidal")
        assert len(secrets.calls) == 2

    def test_none_result_is_cached_within_ttl(self):
        secrets = FakeSecrets({})  # nothing present anywhere
        clock = FakeClock()
        r = _resolver(secrets, clock, ttl_seconds=300.0, global_fallback_leaves={})

        assert r.resolve("111", "youtube") is None
        calls_after_first = len(secrets.calls)
        assert r.resolve("111", "youtube") is None
        # cached negative result — no additional lookups within TTL
        assert len(secrets.calls) == calls_after_first


# ── R6.2: global fallback ───────────────────────────────────────────────


class TestGlobalFallback:
    def test_falls_back_to_global_when_no_guild_secret(self):
        secrets = FakeSecrets(
            {"hellodj/beta/tidal-refresh": {"refresh_token": "GLOBAL"}}
        )
        r = _resolver(secrets, FakeClock())

        tokens = r.resolve("111", "tidal")

        assert tokens == {"refresh_token": "GLOBAL"}
        # tried the guild secret first, then the global leaf
        assert secrets.calls == [
            "hellodj/beta/guild/111/tidal",
            "hellodj/beta/tidal-refresh",
        ]

    def test_spotify_global_leaf(self):
        secrets = FakeSecrets({"hellodj/beta/spotify": {"token": "gspot"}})
        r = _resolver(secrets, FakeClock())
        assert r.resolve("111", "spotify") == {"token": "gspot"}

    def test_guild_secret_takes_precedence_over_global(self):
        secrets = FakeSecrets(
            {
                "hellodj/beta/guild/111/tidal": {"refresh_token": "GUILD"},
                "hellodj/beta/tidal-refresh": {"refresh_token": "GLOBAL"},
            }
        )
        r = _resolver(secrets, FakeClock())
        tokens = r.resolve("111", "tidal")
        assert tokens == {"refresh_token": "GUILD"}
        # never consulted the global secret — guild secret satisfied it (R5.5)
        assert "hellodj/beta/tidal-refresh" not in secrets.calls

    def test_none_when_neither_guild_nor_global_exists(self):
        secrets = FakeSecrets({})
        r = _resolver(secrets, FakeClock())
        assert r.resolve("111", "tidal") is None

    def test_provider_without_global_leaf_skips_gracefully(self):
        # youtube / youtube_music have no global fallback leaf → straight None.
        secrets = FakeSecrets({})
        r = _resolver(secrets, FakeClock())
        assert r.resolve("111", "youtube") is None
        # only the guild secret was attempted, no global lookup
        assert secrets.calls == ["hellodj/beta/guild/111/youtube"]

    def test_disabled_global_fallback(self):
        secrets = FakeSecrets({"hellodj/beta/tidal-refresh": {"t": 1}})
        r = _resolver(secrets, FakeClock(), global_fallback_leaves={})
        assert r.resolve("111", "tidal") is None
        assert secrets.calls == ["hellodj/beta/guild/111/tidal"]

    def test_default_leaves_constant(self):
        assert GLOBAL_FALLBACK_LEAVES == {"tidal": "tidal-refresh", "spotify": "spotify"}


# ── R6.3: cross-guild isolation ─────────────────────────────────────────


class TestIsolation:
    def test_each_guild_gets_its_own_tokens(self):
        secrets = FakeSecrets(
            {
                "hellodj/beta/guild/111/tidal": {"refresh_token": "A"},
                "hellodj/beta/guild/222/tidal": {"refresh_token": "B"},
            }
        )
        r = _resolver(secrets, FakeClock())

        assert r.resolve("111", "tidal") == {"refresh_token": "A"}
        assert r.resolve("222", "tidal") == {"refresh_token": "B"}

    def test_cached_guild_a_never_returned_for_guild_b(self):
        # Guild A has a secret; guild B has none and no global fallback.
        secrets = FakeSecrets(
            {"hellodj/beta/guild/111/tidal": {"refresh_token": "A"}}
        )
        r = _resolver(secrets, FakeClock(), global_fallback_leaves={})

        assert r.resolve("111", "tidal") == {"refresh_token": "A"}
        # B must resolve to None — the cached A entry must not bleed across.
        assert r.resolve("222", "tidal") is None
        # and A stays A after B was resolved
        assert r.resolve("111", "tidal") == {"refresh_token": "A"}

    def test_isolation_holds_across_providers_for_same_guild(self):
        secrets = FakeSecrets(
            {
                "hellodj/beta/guild/111/tidal": {"p": "tidal"},
                "hellodj/beta/guild/111/spotify": {"p": "spotify"},
            }
        )
        r = _resolver(secrets, FakeClock())
        assert r.resolve("111", "tidal") == {"p": "tidal"}
        assert r.resolve("111", "spotify") == {"p": "spotify"}


class TestSupport:
    @pytest.mark.parametrize(
        "provider", ["youtube", "youtube_music", "tidal", "spotify"]
    )
    def test_supported_providers(self, provider):
        r = _resolver(FakeSecrets({}), FakeClock())
        assert r.is_supported(provider) is True

    def test_unsupported_provider(self):
        r = _resolver(FakeSecrets({}), FakeClock())
        assert r.is_supported("bandcamp") is False

"""Tests for UserEntitlementResolver (bot per-user entitlement resolution).

Validates Requirements 14.1, 14.2, 14.3, 10.1 (Task 6.1):
- 14.1 Discord id → Cognito sub → effective entitlements happy path
- 14.2 effective resolution is cached per sub with a bounded TTL; a second call
       within the TTL does NOT re-read the store (and a call after expiry does)
- 14.3 / Property 7: a datastore-unavailable read returns DEFAULT_ENTITLEMENTS
       (restrictive, never fully-permissive) and is NOT cached; an unlinked
       Discord id likewise returns defaults
- 10.1 record_ai_cost applies the CONFIG#AIPRICING markup and increments AITALLY
       (2x Bedrock cost at the default 1.0 markup)

Fakes follow the in-memory style of ``test_guild_credentials.py`` (FakeClock,
in-memory stores that count calls) — no live AWS, no boto3.
"""

from __future__ import annotations

import copy

import pytest

from user_entitlements import (
    AIPRICING_PK,
    AIPRICING_SK,
    AITALLY_SK,
    DEFAULT_ENTITLEMENTS,
    DEFAULT_MARKUP,
    ENTITLEMENT_SK,
    UserEntitlementResolver,
    merge_effective,
    user_pk,
)


class FakeClock:
    """Deterministic monotonic clock for TTL testing."""

    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class FakeEntitlementStore:
    """In-memory ``hellodj-core`` item store keyed by (pk, sk).

    Records every ``get`` so tests can count store reads (for TTL/caching
    assertions), and supports the optimistic-lock read-modify-write used by
    ``record_ai_cost``. Set ``fail`` to make ``get`` raise (datastore-unavailable
    → Property 7 fail-safe).
    """

    def __init__(self, items: dict[tuple[str, str], dict] | None = None) -> None:
        # (pk, sk) -> item dict shaped like a CoreTable row: {"data": {...}}
        self.items: dict[tuple[str, str], dict] = {
            k: copy.deepcopy(v) for k, v in (items or {}).items()
        }
        self.get_calls: list[tuple[str, str]] = []
        self.fail = False

    def get(self, pk: str, sk: str) -> dict | None:
        self.get_calls.append((pk, sk))
        if self.fail:
            raise RuntimeError("DynamoDB unavailable")
        item = self.items.get((pk, sk))
        return copy.deepcopy(item) if item is not None else None

    def update_with_lock(self, pk, sk, mutator, *, entity_type=None):
        item = self.items.get((pk, sk)) or {"data": {}}
        data = dict(item.get("data", {}))
        item["data"] = mutator(data)
        if entity_type is not None:
            item["entityType"] = entity_type
        self.items[(pk, sk)] = item
        return copy.deepcopy(item)


class FakeProfileIndex:
    """In-memory Discord id → Cognito sub reverse index.

    Records lookups; returns ``None`` for an unlinked id. Set ``fail`` to make the
    lookup raise (reverse-index unavailable → defaults).
    """

    def __init__(self, mapping: dict[str, str] | None = None) -> None:
        self.mapping = dict(mapping or {})
        self.calls: list[str] = []
        self.fail = False

    def user_for_discord(self, discord_id: str) -> str | None:
        self.calls.append(discord_id)
        if self.fail:
            raise RuntimeError("reverse index unavailable")
        return self.mapping.get(discord_id)


def _entitlement_item(data: dict) -> dict:
    return {"data": data}


def _resolver(store, profiles, clock, **kwargs) -> UserEntitlementResolver:
    return UserEntitlementResolver(
        store, profiles, time_fn=clock, **kwargs
    )


# ── R14.1: Discord → sub → effective happy path ─────────────────────────


class TestHappyPath:
    def test_discord_resolves_to_effective_entitlements(self):
        store = FakeEntitlementStore(
            {
                (user_pk("sub-1"), ENTITLEMENT_SK): _entitlement_item(
                    {"video_activities": True, "sources": {"youtube": True}}
                )
            }
        )
        profiles = FakeProfileIndex({"discord-1": "sub-1"})
        r = _resolver(store, profiles, FakeClock())

        eff = r.effective_for_discord("discord-1")

        # explicit stored fields override defaults
        assert eff["video_activities"] is True
        assert eff["sources"]["youtube"] is True
        # unspecified source keys fall back to their per-source default
        assert eff["sources"]["soundcloud"] is True  # default-permitted leaf
        assert eff["sources"]["spotify"] is False
        # unspecified top-level flags fall back to restrictive defaults
        assert eff["ai_integration"] is False

    def test_int_discord_id_is_coerced(self):
        store = FakeEntitlementStore(
            {(user_pk("sub-1"), ENTITLEMENT_SK): _entitlement_item({})}
        )
        profiles = FakeProfileIndex({"123": "sub-1"})
        r = _resolver(store, profiles, FakeClock())

        assert r.effective_for_discord(123) == merge_effective(None)
        assert profiles.calls == ["123"]

    def test_no_stored_record_yields_defaults(self):
        # linked sub but no ENTITLEMENT item → pure defaults (not a failure)
        store = FakeEntitlementStore({})
        profiles = FakeProfileIndex({"discord-1": "sub-1"})
        r = _resolver(store, profiles, FakeClock())

        assert r.effective_for_discord("discord-1") == merge_effective(None)


# ── R14.2: caching with a bounded TTL ───────────────────────────────────


class TestCacheTtl:
    def test_second_call_within_ttl_does_not_reread_store(self):
        store = FakeEntitlementStore(
            {
                (user_pk("sub-1"), ENTITLEMENT_SK): _entitlement_item(
                    {"video_activities": True}
                )
            }
        )
        profiles = FakeProfileIndex({"discord-1": "sub-1"})
        clock = FakeClock()
        r = _resolver(store, profiles, clock, ttl_seconds=60.0)

        first = r.effective_for_discord("discord-1")
        entitlement_reads_after_first = [
            c for c in store.get_calls if c[1] == ENTITLEMENT_SK
        ]
        clock.advance(59.0)  # still within the TTL
        second = r.effective_for_discord("discord-1")

        assert first == second
        # the entitlement item was read exactly once — the second call is cached
        assert len(entitlement_reads_after_first) == 1
        entitlement_reads_total = [
            c for c in store.get_calls if c[1] == ENTITLEMENT_SK
        ]
        assert len(entitlement_reads_total) == 1

    def test_call_after_ttl_expiry_rereads_store(self):
        store = FakeEntitlementStore(
            {
                (user_pk("sub-1"), ENTITLEMENT_SK): _entitlement_item(
                    {"video_activities": False}
                )
            }
        )
        profiles = FakeProfileIndex({"discord-1": "sub-1"})
        clock = FakeClock()
        r = _resolver(store, profiles, clock, ttl_seconds=60.0)

        r.effective_for_discord("discord-1")
        # an admin flips the flag; advance past the TTL
        store.items[(user_pk("sub-1"), ENTITLEMENT_SK)] = _entitlement_item(
            {"video_activities": True}
        )
        clock.advance(61.0)
        refreshed = r.effective_for_discord("discord-1")

        assert refreshed["video_activities"] is True
        entitlement_reads = [c for c in store.get_calls if c[1] == ENTITLEMENT_SK]
        assert len(entitlement_reads) == 2  # refreshed after expiry

    def test_ttl_boundary_is_exclusive_refresh(self):
        # At exactly the TTL the entry is considered expired (now < expires False).
        store = FakeEntitlementStore(
            {(user_pk("sub-1"), ENTITLEMENT_SK): _entitlement_item({})}
        )
        profiles = FakeProfileIndex({"discord-1": "sub-1"})
        clock = FakeClock()
        r = _resolver(store, profiles, clock, ttl_seconds=60.0)

        r.effective_for_discord("discord-1")
        clock.advance(60.0)
        r.effective_for_discord("discord-1")

        entitlement_reads = [c for c in store.get_calls if c[1] == ENTITLEMENT_SK]
        assert len(entitlement_reads) == 2

    def test_invalidate_forces_refresh(self):
        store = FakeEntitlementStore(
            {(user_pk("sub-1"), ENTITLEMENT_SK): _entitlement_item({})}
        )
        profiles = FakeProfileIndex({"discord-1": "sub-1"})
        r = _resolver(store, profiles, FakeClock(), ttl_seconds=60.0)

        r.effective_for_sub("sub-1")
        r.invalidate("sub-1")
        r.effective_for_sub("sub-1")

        entitlement_reads = [c for c in store.get_calls if c[1] == ENTITLEMENT_SK]
        assert len(entitlement_reads) == 2

    def test_returned_top_level_is_a_fresh_copy(self):
        # The resolver's documented contract: the returned mapping is a fresh
        # copy so a caller replacing a top-level field cannot poison the cache.
        store = FakeEntitlementStore(
            {
                (user_pk("sub-1"), ENTITLEMENT_SK): _entitlement_item(
                    {"video_activities": True}
                )
            }
        )
        profiles = FakeProfileIndex({"discord-1": "sub-1"})
        r = _resolver(store, profiles, FakeClock())

        first = r.effective_for_discord("discord-1")
        first["ai_integration"] = True  # mutate a top-level field
        first["video_activities"] = False
        second = r.effective_for_discord("discord-1")

        # the cache is not corrupted by a caller mutating top-level fields
        assert second["ai_integration"] is False
        assert second["video_activities"] is True


# ── R14.3 / Property 7: fail-safe to restrictive defaults ────────────────


class TestFailSafeDefaults:
    def test_datastore_unavailable_returns_defaults(self):
        store = FakeEntitlementStore(
            {(user_pk("sub-1"), ENTITLEMENT_SK): _entitlement_item(
                {"ai_integration": True}
            )}
        )
        store.fail = True  # every store.get raises
        profiles = FakeProfileIndex({"discord-1": "sub-1"})
        r = _resolver(store, profiles, FakeClock())

        eff = r.effective_for_discord("discord-1")

        assert eff == DEFAULT_ENTITLEMENTS
        # restrictive: nothing is fully-permissive
        assert eff["ai_integration"] is False
        assert eff["video_activities"] is False

    def test_datastore_failure_is_not_cached(self):
        # A transient outage must NOT pin the user to defaults for a full TTL.
        store = FakeEntitlementStore(
            {(user_pk("sub-1"), ENTITLEMENT_SK): _entitlement_item(
                {"video_activities": True}
            )}
        )
        profiles = FakeProfileIndex({"discord-1": "sub-1"})
        r = _resolver(store, profiles, FakeClock(), ttl_seconds=60.0)

        store.fail = True
        during_outage = r.effective_for_discord("discord-1")
        assert during_outage == DEFAULT_ENTITLEMENTS

        # datastore recovers within the same TTL window — next call re-reads
        store.fail = False
        after_recovery = r.effective_for_discord("discord-1")
        assert after_recovery["video_activities"] is True

    def test_unlinked_discord_id_returns_defaults(self):
        store = FakeEntitlementStore({})
        profiles = FakeProfileIndex({})  # discord-1 is not linked
        r = _resolver(store, profiles, FakeClock())

        eff = r.effective_for_discord("discord-1")

        assert eff == DEFAULT_ENTITLEMENTS
        # never even attempted an entitlement read (no sub to key on)
        assert store.get_calls == []

    def test_reverse_index_failure_returns_defaults(self):
        store = FakeEntitlementStore({})
        profiles = FakeProfileIndex({"discord-1": "sub-1"})
        profiles.fail = True
        r = _resolver(store, profiles, FakeClock())

        assert r.effective_for_discord("discord-1") == DEFAULT_ENTITLEMENTS


# ── R10.1: record_ai_cost applies markup + increments tally ──────────────


class TestRecordAiCost:
    def test_default_markup_doubles_bedrock_cost(self):
        # no CONFIG#AIPRICING item → default markup 1.0 → 2x Bedrock cost
        store = FakeEntitlementStore({})
        r = _resolver(store, FakeProfileIndex(), FakeClock())

        r.record_ai_cost("sub-1", 0.10)

        tally = store.items[(user_pk("sub-1"), AITALLY_SK)]["data"]
        assert tally["accumulated_cost"] == pytest.approx(0.20)
        assert tally["currency"] == "USD"
        assert DEFAULT_MARKUP == 1.0

    def test_configured_markup_is_applied(self):
        store = FakeEntitlementStore(
            {(AIPRICING_PK, AIPRICING_SK): _entitlement_item({"markup": 0.5})}
        )
        r = _resolver(store, FakeProfileIndex(), FakeClock())

        r.record_ai_cost("sub-1", 1.00)

        tally = store.items[(user_pk("sub-1"), AITALLY_SK)]["data"]
        # 1.00 * (1 + 0.5) == 1.50
        assert tally["accumulated_cost"] == pytest.approx(1.50)

    def test_costs_accumulate_across_calls(self):
        store = FakeEntitlementStore({})
        r = _resolver(store, FakeProfileIndex(), FakeClock())

        r.record_ai_cost("sub-1", 0.10)  # +0.20
        r.record_ai_cost("sub-1", 0.05)  # +0.10

        tally = store.items[(user_pk("sub-1"), AITALLY_SK)]["data"]
        assert tally["accumulated_cost"] == pytest.approx(0.30)

    def test_malformed_markup_falls_back_to_default(self):
        store = FakeEntitlementStore(
            {(AIPRICING_PK, AIPRICING_SK): _entitlement_item({"markup": "bogus"})}
        )
        r = _resolver(store, FakeProfileIndex(), FakeClock())

        r.record_ai_cost("sub-1", 0.10)

        tally = store.items[(user_pk("sub-1"), AITALLY_SK)]["data"]
        assert tally["accumulated_cost"] == pytest.approx(0.20)  # default 1.0

    def test_metering_failure_never_raises(self):
        store = FakeEntitlementStore({})
        store.fail = True  # pricing read raises; must be swallowed
        r = _resolver(store, FakeProfileIndex(), FakeClock())

        # must not propagate — metering is best-effort
        r.record_ai_cost("sub-1", 0.10)

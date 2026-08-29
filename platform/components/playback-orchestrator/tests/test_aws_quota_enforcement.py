"""Property + unit tests for AWS-path entitlement quota enforcement (Task 4).

Covers :class:`playback_orchestrator.instance_runtime.AwsInstanceOrchestrator`
quota enforcement — the overridden ``_resolve_effective`` (injected resolver,
restrictive default on failure) and ``_enforce_quotas`` (shared
``entitlements_core`` decision helpers) — exercised through the INHERITED
``assign_instance`` path so the property covers the real assignment gate, not a
re-implementation.

This mirrors the on-prem orchestrator's quota tests
(``bot/playback/test_entitlement_gates.py::TestQuotaGate``) in shape: a fake
resolver supplies the effective entitlements, assignments are driven for an
owning user, and the boundary raises :class:`QuotaExceededError`.

Property (design.md Property 4 — Quota safety):
* Assignment NEVER exceeds ``effective_max_bots_per_guild`` for a guild.
* Assignment NEVER exceeds ``max_guilds`` distinct guilds for the user.
* A :class:`QuotaExceededError` is raised exactly at the boundary.
* Resolution failure → the restrictive default (limits = 1) is applied, never a
  more-permissive fallback.

**Validates: Requirements 4.1, 4.2, 4.3, 4.4**
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

import pytest
from entitlements_core import (
    DEFAULT_ENTITLEMENTS,
    effective_max_bots_per_guild,
    merge_effective,
)
from hellodj_platform_logic.data_access import CoreTable
from hypothesis import given, settings
from hypothesis import strategies as st

from playback_orchestrator.instance_runtime import (
    AwsInstanceOrchestrator,
    PoolCredentialSource,
    QuotaExceededError,
)

_USER = 99


# ── fakes ────────────────────────────────────────────────────────────────────


@dataclass
class _FakeSecrets:
    payload: str = ""

    def get_secret_value(self, **kwargs: Any) -> dict[str, Any]:
        return {"SecretString": self.payload}


@dataclass
class _FakeTable:
    _items: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)

    def get_item(self, **kwargs: Any) -> dict[str, Any]:
        item = self._items.get((kwargs["Key"]["PK"], kwargs["Key"]["SK"]))
        return {"Item": dict(item)} if item is not None else {}

    def query(self, **kwargs: Any) -> dict[str, Any]:
        values = kwargs.get("ExpressionAttributeValues", {})
        pk = values[":pk"]
        prefix = values.get(":skp")
        items = [
            dict(it)
            for (ipk, isk), it in self._items.items()
            if ipk == pk and (prefix is None or isk.startswith(prefix))
        ]
        return {"Items": items}

    def put_item(self, **kwargs: Any) -> dict[str, Any]:
        item = kwargs["Item"]
        self._items[(item["PK"], item["SK"])] = dict(item)
        return {}


@dataclass
class _FakeResolver:
    """Resolver returning a fixed effective entitlements map for every user."""

    _effective: dict[str, Any]

    def effective_for_discord(self, discord_id: str | int) -> dict[str, Any]:
        return dict(self._effective)


class _RaisingResolver:
    """Resolver whose lookups raise — drives the fail-safe path (R4.3)."""

    def effective_for_discord(self, discord_id: str | int) -> dict[str, Any]:
        raise RuntimeError("datastore unavailable")


def _source() -> PoolCredentialSource:
    core = CoreTable(_FakeTable())
    return PoolCredentialSource(_FakeSecrets(json.dumps([])), core, stage="beta")


def _orch(resolver: Any) -> AwsInstanceOrchestrator:
    return AwsInstanceOrchestrator(object(), object(), _source(), resolver)


def _available(orch: AwsInstanceOrchestrator, n: int) -> None:
    """Populate ``n`` available BotInstances directly (no gateway needed)."""
    from playback_orchestrator.orchestrator import BotInstance

    insts = []
    for i in range(n):
        inst = BotInstance.__new__(BotInstance)
        inst.index = i
        inst.client = object()
        inst.token = ""
        inst.application_id = i
        inst.status = "available"
        inst.channel_id = None
        inst.guild_id = None
        inst.user_id = None
        insts.append(inst)
    orch._instances = insts  # noqa: SLF001


# ── unit tests mirroring on-prem TestQuotaGate ───────────────────────────────


def test_permits_under_per_guild_limit() -> None:
    orch = _orch(
        _FakeResolver(
            merge_effective(
                {"max_bots_per_guild": 2, "max_bots_per_guild_enabled": True,
                 "max_guilds": 5}
            )
        )
    )
    _available(orch, 3)
    assert asyncio.run(orch.assign_instance(7, 100, _USER)) is not None
    # second in same guild: 1 < 2 → permitted
    assert asyncio.run(orch.assign_instance(7, 101, _USER)) is not None


def test_rejects_at_per_guild_limit() -> None:
    orch = _orch(
        _FakeResolver(
            merge_effective(
                {"max_bots_per_guild": 1, "max_bots_per_guild_enabled": True,
                 "max_guilds": 5}
            )
        )
    )
    _available(orch, 3)
    asyncio.run(orch.assign_instance(7, 100, _USER))  # 1st ok
    with pytest.raises(QuotaExceededError):
        asyncio.run(orch.assign_instance(7, 101, _USER))  # 1 >= 1


def test_rejects_at_guild_limit() -> None:
    orch = _orch(
        _FakeResolver(
            merge_effective(
                {"max_bots_per_guild": 5, "max_bots_per_guild_enabled": True,
                 "max_guilds": 1}
            )
        )
    )
    _available(orch, 3)
    asyncio.run(orch.assign_instance(7, 100, _USER))  # active in guild 7
    with pytest.raises(QuotaExceededError):
        asyncio.run(orch.assign_instance(8, 200, _USER))  # new guild → over


def test_same_guild_does_not_grow_guild_count() -> None:
    orch = _orch(
        _FakeResolver(
            merge_effective(
                {"max_bots_per_guild": 5, "max_bots_per_guild_enabled": True,
                 "max_guilds": 1}
            )
        )
    )
    _available(orch, 3)
    asyncio.run(orch.assign_instance(7, 100, _USER))
    # another bot in the SAME guild does not increase distinct-guild count
    assert asyncio.run(orch.assign_instance(7, 101, _USER)) is not None


def test_fail_safe_defaults_limit_one_on_raise() -> None:
    """Resolver failure → DEFAULT_ENTITLEMENTS (limits = 1), R4.3."""
    orch = _orch(_RaisingResolver())
    _available(orch, 3)
    asyncio.run(orch.assign_instance(7, 100, _USER))  # first ok (limit 1)
    with pytest.raises(QuotaExceededError):
        asyncio.run(orch.assign_instance(7, 101, _USER))  # 1 >= 1


def test_fail_safe_defaults_limit_one_when_unwired() -> None:
    """No resolver wired → restrictive default (never permissive), R4.3."""
    orch = _orch(None)
    _available(orch, 3)
    asyncio.run(orch.assign_instance(7, 100, _USER))
    with pytest.raises(QuotaExceededError):
        asyncio.run(orch.assign_instance(8, 200, _USER))  # max_guilds default 1


def test_no_user_id_skips_quota() -> None:
    """Legacy (userless) callers bypass quota entirely (base contract)."""
    orch = _orch(_RaisingResolver())
    _available(orch, 3)
    for g, c in [(1, 1), (2, 2), (3, 3)]:
        assert asyncio.run(orch.assign_instance(g, c)) is not None


# ── property: quota safety (design Property 4) ───────────────────────────────


@settings(max_examples=200, deadline=None)
@given(
    max_bots=st.integers(min_value=1, max_value=6),
    max_guilds=st.integers(min_value=1, max_value=5),
    # sequence of (guild, channel) assignment attempts for one owning user
    attempts=st.lists(
        st.tuples(
            st.integers(min_value=1, max_value=4),  # guild id
            st.integers(min_value=1, max_value=50),  # channel id
        ),
        min_size=0,
        max_size=25,
    ),
)
def test_property_quota_safety(
    max_bots: int, max_guilds: int, attempts: list[tuple[int, int]]
) -> None:
    """Assignment never exceeds either quota; the boundary raises exactly.

    Drives an arbitrary sequence of assignment attempts for one user through the
    inherited ``assign_instance`` under arbitrary entitlement limits, and after
    each attempt asserts the safety invariants hold. A rejection is ALWAYS a
    ``QuotaExceededError`` raised exactly when a fresh assignment would cross a
    limit; a success never crosses one.
    """
    effective = merge_effective(
        {
            "max_bots_per_guild": max_bots,
            "max_bots_per_guild_enabled": True,
            "max_guilds": max_guilds,
        }
    )
    per_guild_limit = effective_max_bots_per_guild(effective)
    orch = _orch(_FakeResolver(effective))
    # Plenty of instances so exhaustion never masks a quota decision.
    _available(orch, 64)

    used_channels: set[tuple[int, int]] = set()

    for guild_id, channel_id in attempts:
        # Snapshot the user's counts BEFORE the attempt.
        before_in_guild = orch._user_bot_count_in_guild(guild_id, _USER)  # noqa: SLF001
        before_guilds = orch._user_active_guilds(_USER)  # noqa: SLF001
        reusing = (guild_id, channel_id) in used_channels

        would_new_guild = guild_id not in before_guilds
        expect_guild_reject = (
            not reusing
            and would_new_guild
            and len(before_guilds) >= max_guilds
        )
        # Per-guild check only reached when the guild check passes.
        expect_bot_reject = (
            not reusing
            and not expect_guild_reject
            and before_in_guild >= per_guild_limit
        )
        expect_reject = expect_guild_reject or expect_bot_reject

        if expect_reject:
            with pytest.raises(QuotaExceededError):
                asyncio.run(orch.assign_instance(guild_id, channel_id, _USER))
        else:
            result = asyncio.run(
                orch.assign_instance(guild_id, channel_id, _USER)
            )
            assert result is not None
            used_channels.add((guild_id, channel_id))

        # -- invariants hold after EVERY attempt (success or reject) --
        # Never exceed the per-guild bot limit in any guild.
        for g in orch._user_active_guilds(_USER):  # noqa: SLF001
            assert (
                orch._user_bot_count_in_guild(g, _USER)  # noqa: SLF001
                <= per_guild_limit
            )
        # Never exceed the distinct-guild limit.
        assert len(orch._user_active_guilds(_USER)) <= max_guilds  # noqa: SLF001


@settings(max_examples=100, deadline=None)
@given(
    attempts=st.lists(
        st.tuples(
            st.integers(min_value=1, max_value=4),
            st.integers(min_value=1, max_value=50),
        ),
        min_size=1,
        max_size=15,
    ),
)
def test_property_resolution_failure_is_restrictive(
    attempts: list[tuple[int, int]],
) -> None:
    """A resolver failure applies the restrictive default (limits = 1), R4.3.

    Under a raising resolver the effective limits MUST be the restrictive
    ``DEFAULT_ENTITLEMENTS`` (max_bots_per_guild = 1, max_guilds = 1), never a
    more-permissive fallback — so no user ever holds >1 bot per guild or is
    active in >1 guild, regardless of the attempt sequence.
    """
    assert effective_max_bots_per_guild(dict(DEFAULT_ENTITLEMENTS)) == 1
    assert int(DEFAULT_ENTITLEMENTS["max_guilds"]) == 1

    orch = _orch(_RaisingResolver())
    _available(orch, 64)

    used_channels: set[tuple[int, int]] = set()
    for guild_id, channel_id in attempts:
        reusing = (guild_id, channel_id) in used_channels
        try:
            result = asyncio.run(
                orch.assign_instance(guild_id, channel_id, _USER)
            )
            if result is not None and not reusing:
                used_channels.add((guild_id, channel_id))
        except QuotaExceededError:
            pass
        # Restrictive default invariants: at most 1 bot per guild, 1 guild.
        for g in orch._user_active_guilds(_USER):  # noqa: SLF001
            assert orch._user_bot_count_in_guild(g, _USER) <= 1  # noqa: SLF001
        assert len(orch._user_active_guilds(_USER)) <= 1  # noqa: SLF001

"""Tests for feature-entitlement command gating (bot half).

A paying customer must never SEE commands for features they don't have. These
cover the pure gating decisions and the guild-owner entitlement resolver over an
in-memory ``hellodj-core`` — no AWS, no discord.py.
"""

from __future__ import annotations

from typing import Any

from discord_bot_core.policy.entitlements import (
    DEFAULT_FEATURE_ENTITLEMENTS,
    EntitlementResolver,
    command_visible_for_entitlements,
    entitlement_allowed_commands,
    feature_entitlement_for,
    merge_feature_effective,
)

# A synthetic command->entitlement map so the mechanism is fully exercised
# regardless of which real feature commands ship today.
_MAP = {
    "visualizer": "visualizations",
    "activity": "video_activities",
    "ask": "ai_integration",
}


# -- pure defaults / merge --------------------------------------------------


def test_defaults_are_all_off() -> None:
    assert all(v is False for v in DEFAULT_FEATURE_ENTITLEMENTS.values())


def test_merge_absent_keys_default_off() -> None:
    eff = merge_feature_effective({"visualizations": True})
    assert eff["visualizations"] is True
    assert eff["video_activities"] is False  # absent -> secure default
    assert eff["ai_integration"] is False


def test_merge_none_is_all_defaults() -> None:
    assert merge_feature_effective(None) == DEFAULT_FEATURE_ENTITLEMENTS


# -- pure command visibility ------------------------------------------------


def test_baseline_command_always_visible() -> None:
    # A command not in the map is baseline: visible regardless of entitlements.
    eff = merge_feature_effective(None)
    assert command_visible_for_entitlements("play", eff, command_map=_MAP) is True


def test_feature_command_hidden_without_entitlement() -> None:
    eff = merge_feature_effective(None)  # all off
    assert (
        command_visible_for_entitlements("visualizer", eff, command_map=_MAP)
        is False
    )


def test_feature_command_visible_with_entitlement() -> None:
    eff = merge_feature_effective({"visualizations": True})
    assert (
        command_visible_for_entitlements("visualizer", eff, command_map=_MAP)
        is True
    )


def test_allowed_commands_filters_by_entitlement() -> None:
    names = {"play", "skip", "visualizer", "activity", "ask"}
    eff = merge_feature_effective({"video_activities": True})
    allowed = entitlement_allowed_commands(names, eff, command_map=_MAP)
    # baseline always; only the video feature is enabled.
    assert allowed == {"play", "skip", "activity"}


def test_feature_entitlement_for_live_map_baseline_none() -> None:
    # Real bot commands today are baseline (no gating entitlement).
    assert feature_entitlement_for("play") is None
    assert feature_entitlement_for("activate") is None


# -- guild-owner entitlement resolver ---------------------------------------


class _MemCore:
    """In-memory CoreTable-like store keyed by (pk, sk)."""

    def __init__(self, items: dict[tuple[str, str], dict[str, Any]]) -> None:
        self._items = items

    def get(self, pk: str, sk: str) -> dict[str, Any] | None:
        return self._items.get((pk, sk))


def test_resolver_reads_owner_entitlements() -> None:
    core = _MemCore(
        {
            ("GUILD#42", "OWNER"): {"data": {"owner_sub": "sub-1"}},
            ("USER#sub-1", "ENTITLEMENT"): {
                "data": {"visualizations": True, "ai_integration": True}
            },
        }
    )
    eff = EntitlementResolver(core).effective_for_guild(42)
    assert eff["visualizations"] is True
    assert eff["ai_integration"] is True
    assert eff["video_activities"] is False  # absent -> default off


def test_resolver_no_owner_is_all_defaults() -> None:
    eff = EntitlementResolver(_MemCore({})).effective_for_guild(99)
    assert eff == DEFAULT_FEATURE_ENTITLEMENTS


def test_resolver_owner_without_entitlement_item_is_defaults() -> None:
    core = _MemCore({("GUILD#7", "OWNER"): {"data": {"owner_sub": "s"}}})
    eff = EntitlementResolver(core).effective_for_guild(7)
    assert eff == DEFAULT_FEATURE_ENTITLEMENTS


def test_resolver_error_is_secure_default() -> None:
    class _Boom:
        def get(self, pk: str, sk: str) -> dict[str, Any] | None:
            raise RuntimeError("ddb down")

    eff = EntitlementResolver(_Boom()).effective_for_guild(1)
    assert eff == DEFAULT_FEATURE_ENTITLEMENTS

"""Per-capability entitlement gate integration tests (Task 13.1).

Exercises each bot-side enforcement point end-to-end against a FAKE
``UserEntitlementResolver`` (in-memory, no live AWS / Discord), asserting that
every gate PERMITS when the governing capability is enabled and DECLINES / CAPS
when it is disabled, and that each gate FAILS SAFE (restrictive) when the
resolver is unavailable — the resolver returns ``DEFAULT_ENTITLEMENTS`` on any
datastore failure and a missing resolver (``None``) is treated identically.

Enforcement points covered (design "Bot enforcement" table):

* source reject          — ``player._source_allowed_for_user`` (R3.2, R3.4)
* bitrate cap at start   — ``player._audio_above_96k_allowed_for_user`` +
  ``player._enforce_bitrate_cap_at_start`` (R5.2)
* video decline+response — ``cogs.video._video_activities_allowed`` (R6.2, R6.3)
* visualization decline  — ``cogs.visualizer._visualizations_allowed`` (R7.2)
* wake-word block-all    — ``voice.voice_commands._wakeword_allowed`` (R8.2)
* AI decline-no-cost     — ``voice.ai_gate.gate_ai_request`` (R9.2, R9.3)
* quota reject at limit  — ``orchestrator._enforce_quotas`` (R11.2, R12.3)

The pure/decision-level gates (``gate_ai_request``, ``_enforce_quotas``) take a
resolver directly, so they are tested with an injected fake. The lazy-``import
bot`` helpers (``_source_allowed_for_user``, ``_audio_above_96k_allowed_for_user``,
``_video_activities_allowed``, ``_visualizations_allowed``, ``_wakeword_allowed``)
reach the process-wide resolver via ``bot.get_user_entitlements()``; a lightweight
stub ``bot`` module is installed in ``sys.modules`` so the seam is controllable
(return a fake resolver / ``None`` / raise) without importing the heavy real bot.

The gated modules live in ``bot/`` (the parent of ``bot/playback``), so the
parent dir is put on ``sys.path`` here, mirroring ``test_bot_identity_apply``.

Requirements: 3.2, 3.4, 5.2, 6.2, 6.3, 7.2, 8.2, 9.2, 9.3, 11.2, 12.3
"""

from __future__ import annotations

import os
import sys
import types
from typing import Any

import pytest

# bot/playback/ -> bot/ so ``import player`` / ``import cogs.video`` etc resolve.
_BOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BOT_DIR not in sys.path:
    sys.path.insert(0, _BOT_DIR)

from user_entitlements import (  # noqa: I001
    DEFAULT_ENTITLEMENTS,
    merge_effective,
)


# ── fake resolver + stub bot module ─────────────────────────────────────────


class FakeResolver:
    """In-memory stand-in for ``UserEntitlementResolver``.

    ``effective_for_discord`` returns a fixed effective map (merged over the
    secure defaults so callers see a full record). ``record_ai_cost`` records
    each metering call so the AI gate's "decline WITHOUT cost" contract (R9.2)
    is asserted by observing that NO cost was recorded on a declined request.
    """

    def __init__(self, effective: dict[str, Any], *, sub: str | None = "sub-1"):
        self._effective = merge_effective(effective)
        self._sub = sub
        self.recorded: list[tuple[str, float]] = []
        self.tally: float = 0.0

    def effective_for_discord(self, discord_id: str | int) -> dict[str, Any]:
        return dict(self._effective)

    def sub_for_discord(self, discord_id: str | int) -> str | None:
        return self._sub

    def ai_tally_for_sub(self, sub: str) -> float:
        return self.tally

    def record_ai_cost(self, sub: str, bedrock_cost: float) -> None:
        self.recorded.append((sub, bedrock_cost))


class _RaisingResolver:
    """A resolver whose lookups raise — proves the gate's fail-safe path."""

    def effective_for_discord(self, discord_id: str | int) -> dict[str, Any]:
        raise RuntimeError("datastore unavailable")


@pytest.fixture
def stub_bot(monkeypatch):
    """Install a stub ``bot`` module with a settable ``get_user_entitlements``.

    Returns a setter ``set_resolver(resolver)`` the tests call to control what
    the lazy ``import bot`` helpers resolve. Restores any previous ``bot`` module
    on teardown.
    """
    previous = sys.modules.get("bot")
    stub = types.ModuleType("bot")
    stub._resolver = None
    stub.get_user_entitlements = lambda: stub._resolver
    sys.modules["bot"] = stub

    def set_resolver(resolver) -> None:
        stub._resolver = resolver

    yield set_resolver

    if previous is not None:
        sys.modules["bot"] = previous
    else:
        sys.modules.pop("bot", None)


# Convenience effective-map builders (merged over defaults inside FakeResolver).
def _sources(**flags: bool) -> dict[str, Any]:
    return {"sources": dict(flags)}


# ── source reject (R3.2/R3.4) ───────────────────────────────────────────────


class TestSourceGate:
    def test_permits_enabled_source(self, stub_bot):
        import player

        # A non-premium source needs only its per-source flag.
        stub_bot(FakeResolver(_sources(youtube=True)))
        assert player._source_allowed_for_user("discord-1", "youtube") is True

    def test_declines_disabled_source(self, stub_bot):
        import player

        stub_bot(FakeResolver(_sources(youtube=False)))
        assert player._source_allowed_for_user("discord-1", "youtube") is False

    def test_default_soundcloud_permitted_youtube_declined(self, stub_bot):
        import player

        # No explicit sources → defaults: soundcloud on, youtube off.
        stub_bot(FakeResolver({}))
        assert player._source_allowed_for_user("discord-1", "soundcloud") is True
        assert player._source_allowed_for_user("discord-1", "youtube") is False

    def test_fail_safe_no_resolver_declines_non_default(self, stub_bot):
        import player

        stub_bot(None)  # resolver unavailable → restrictive defaults
        assert player._source_allowed_for_user("discord-1", "spotify") is False
        # the baseline no-auth source stays permitted under defaults
        assert player._source_allowed_for_user("discord-1", "soundcloud") is True

    def test_unknown_provider_never_granted(self, stub_bot):
        import player

        stub_bot(FakeResolver(_sources(spotify=True)))
        assert player._source_allowed_for_user("discord-1", "bandcamp") is False


# ── premium source gate (Spotify/Tidal + premium_sources) ───────────────────


class TestPremiumSourceGate:
    def test_premium_source_needs_flag_and_premium_capability(self, stub_bot):
        import player

        # Per-source flag on but premium capability off → declined.
        stub_bot(FakeResolver({"sources": {"spotify": True}, "premium_sources": False}))
        assert player._source_allowed_for_user("discord-1", "spotify") is False

    def test_premium_source_allowed_with_both(self, stub_bot):
        import player

        stub_bot(
            FakeResolver(
                {
                    "sources": {"spotify": True, "tidal": True},
                    "premium_sources": True,
                }
            )
        )
        assert player._source_allowed_for_user("discord-1", "spotify") is True
        assert player._source_allowed_for_user("discord-1", "tidal") is True

    def test_premium_capability_without_flag_still_declined(self, stub_bot):
        import player

        # Premium capability on but the per-source flag off → still declined.
        stub_bot(FakeResolver({"sources": {"spotify": False}, "premium_sources": True}))
        assert player._source_allowed_for_user("discord-1", "spotify") is False

    def test_premium_gate_does_not_affect_non_premium(self, stub_bot):
        import player

        # Premium off, but a non-premium source with its flag on is allowed.
        stub_bot(FakeResolver({"sources": {"youtube": True}, "premium_sources": False}))
        assert player._source_allowed_for_user("discord-1", "youtube") is True


# ── bitrate cap at start (R5.2) ──────────────────────────────────────────────


class _FakeChannel:
    """A voice channel whose ``edit(bitrate=...)`` records the applied value."""

    def __init__(self, bitrate: int):
        self.bitrate = bitrate
        self.edited_to: int | None = None

    async def edit(self, *, bitrate: int) -> None:
        self.edited_to = bitrate
        self.bitrate = bitrate


class _FakePlayer:
    def __init__(self, channel):
        self.channel = channel


def _run(coro):
    import asyncio

    return asyncio.run(coro)


class TestBitrateCapGate:
    def test_flag_enabled_permits_high_bitrate(self, stub_bot):
        import player

        stub_bot(FakeResolver({"audio_above_96k": True}))
        assert player._audio_above_96k_allowed_for_user("discord-1") is True

    def test_flag_disabled_caps(self, stub_bot):
        import player

        stub_bot(FakeResolver({"audio_above_96k": False}))
        assert player._audio_above_96k_allowed_for_user("discord-1") is False

    def test_fail_safe_no_resolver_capped(self, stub_bot):
        import player

        stub_bot(None)
        assert player._audio_above_96k_allowed_for_user("discord-1") is False

    def test_enforce_caps_channel_bitrate_at_start_when_disabled(self, stub_bot):
        import player

        stub_bot(FakeResolver({"audio_above_96k": False}))
        chan = _FakeChannel(bitrate=256_000)
        player_obj = _FakePlayer(chan)

        _run(player._enforce_bitrate_cap_at_start(player_obj, 42, "discord-1"))

        assert chan.edited_to == player._AUDIO_96K_CAP_BPS  # 96 kbps

    def test_enforce_leaves_channel_untouched_when_enabled(self, stub_bot):
        import player

        stub_bot(FakeResolver({"audio_above_96k": True}))
        chan = _FakeChannel(bitrate=256_000)
        player_obj = _FakePlayer(chan)

        _run(player._enforce_bitrate_cap_at_start(player_obj, 42, "discord-1"))

        assert chan.edited_to is None  # never edited
        assert chan.bitrate == 256_000

    def test_enforce_skips_when_already_below_cap(self, stub_bot):
        import player

        stub_bot(FakeResolver({"audio_above_96k": False}))
        chan = _FakeChannel(bitrate=64_000)  # already under the cap
        player_obj = _FakePlayer(chan)

        _run(player._enforce_bitrate_cap_at_start(player_obj, 42, "discord-1"))

        assert chan.edited_to is None


# ── video decline-with-response (R6.2/R6.3) ──────────────────────────────────


class TestVideoGate:
    def test_permits_when_enabled(self, stub_bot):
        from cogs import video

        stub_bot(FakeResolver({"video_activities": True}))
        assert video._video_activities_allowed(111) is True

    def test_declines_when_disabled(self, stub_bot):
        from cogs import video

        stub_bot(FakeResolver({"video_activities": False}))
        assert video._video_activities_allowed(111) is False

    def test_fail_safe_no_resolver_declines(self, stub_bot):
        from cogs import video

        stub_bot(None)
        assert video._video_activities_allowed(111) is False

    def test_fail_safe_resolver_raises_declines(self, stub_bot):
        from cogs import video

        stub_bot(_RaisingResolver())
        assert video._video_activities_allowed(111) is False


# ── visualization decline (R7.2) ─────────────────────────────────────────────


class TestVisualizationGate:
    def test_permits_when_enabled(self, stub_bot):
        from cogs import visualizer

        stub_bot(FakeResolver({"visualizations": True}))
        assert visualizer._visualizations_allowed(111) is True

    def test_declines_when_disabled(self, stub_bot):
        from cogs import visualizer

        stub_bot(FakeResolver({"visualizations": False}))
        assert visualizer._visualizations_allowed(111) is False

    def test_fail_safe_no_resolver_declines(self, stub_bot):
        from cogs import visualizer

        stub_bot(None)
        assert visualizer._visualizations_allowed(111) is False


# ── wake-word block-all-when-off (R8.2) ──────────────────────────────────────


class TestWakewordGate:
    def test_permits_when_enabled(self, stub_bot):
        from voice import voice_commands

        stub_bot(FakeResolver({"wakeword": True}))
        assert voice_commands._wakeword_allowed(111) is True

    def test_blocks_all_when_disabled(self, stub_bot):
        from voice import voice_commands

        stub_bot(FakeResolver({"wakeword": False}))
        assert voice_commands._wakeword_allowed(111) is False

    def test_fail_safe_no_resolver_blocks(self, stub_bot):
        from voice import voice_commands

        stub_bot(None)
        assert voice_commands._wakeword_allowed(111) is False


# ── AI decline-no-cost + block non-declined (R9.2/R9.3) ──────────────────────


class TestAiGate:
    def test_declines_without_cost_when_disabled(self):
        from voice.ai_gate import gate_ai_request

        resolver = FakeResolver({"ai_integration": False})
        decision = gate_ai_request(resolver, "discord-1")

        assert decision.permitted is False
        assert decision.reason  # a decline message is surfaced
        # R9.2: declined request incurs NO cost.
        assert resolver.recorded == []

    def test_permits_and_meters_immediately_when_enabled(self):
        from voice.ai_gate import AI_REQUEST_BEDROCK_COST_ESTIMATE, gate_ai_request

        resolver = FakeResolver({"ai_integration": True})
        decision = gate_ai_request(resolver, "discord-1")

        assert decision.permitted is True
        # metered immediately at permit time (R9.4/R10.1)
        assert resolver.recorded == [("sub-1", AI_REQUEST_BEDROCK_COST_ESTIMATE)]

    def test_no_resolver_declines_without_cost(self):
        from voice.ai_gate import gate_ai_request

        # R9.3: a request that cannot be positively permitted is blocked
        # entirely (treated as an error) — never allowed to proceed.
        decision = gate_ai_request(None, "discord-1")

        assert decision.permitted is False
        assert decision.reason

    def test_over_cap_permits_with_warning_not_blocked(self):
        from voice.ai_gate import gate_ai_request

        resolver = FakeResolver({"ai_integration": True, "ai_spend_cap": 0.0})
        resolver.tally = 5.0  # already at/over the cap
        decision = gate_ai_request(resolver, "discord-1")

        # over cap warns but does NOT hard-block (R10.5)
        assert decision.permitted is True
        assert decision.warning is not None


# ── quota reject at limit (R11.2/R12.3) ──────────────────────────────────────


def _orchestrator_with(monkeypatch, effective: dict[str, Any]):
    """Build an orchestrator whose ``_resolve_effective`` returns ``effective``.

    ``_enforce_quotas`` resolves entitlements via ``_resolve_effective`` (which
    reads ``bot.get_user_entitlements``); patching that method directly keeps the
    quota test focused on the quota decision, independent of the resolver seam.
    """
    from orchestrator import InstanceOrchestrator

    orch = InstanceOrchestrator.__new__(InstanceOrchestrator)
    orch._instances = []
    monkeypatch.setattr(
        orch, "_resolve_effective", lambda user_id: merge_effective(effective)
    )
    return orch


def _connected(index, guild_id, user_id):
    from orchestrator import BotInstance

    inst = BotInstance.__new__(BotInstance)
    inst.index = index
    inst.status = "connected"
    inst.guild_id = guild_id
    inst.user_id = user_id
    inst.channel_id = 1000 + index
    return inst


class TestQuotaGate:
    def test_permits_under_per_guild_limit(self, monkeypatch):
        orch = _orchestrator_with(
            monkeypatch,
            {"max_bots_per_guild": 2, "max_bots_per_guild_enabled": True,
             "max_guilds": 5},
        )
        orch._instances = [_connected(0, guild_id=7, user_id=99)]  # 1 < 2

        # under the limit → no raise
        orch._enforce_quotas(guild_id=7, user_id=99)

    def test_rejects_at_per_guild_limit(self, monkeypatch):
        from orchestrator import QuotaExceededError

        orch = _orchestrator_with(
            monkeypatch,
            {"max_bots_per_guild": 1, "max_bots_per_guild_enabled": True,
             "max_guilds": 5},
        )
        orch._instances = [_connected(0, guild_id=7, user_id=99)]  # 1 >= 1

        with pytest.raises(QuotaExceededError):
            orch._enforce_quotas(guild_id=7, user_id=99)

    def test_rejects_at_guild_limit(self, monkeypatch):
        from orchestrator import QuotaExceededError

        orch = _orchestrator_with(
            monkeypatch,
            {"max_bots_per_guild": 5, "max_bots_per_guild_enabled": True,
             "max_guilds": 1},
        )
        # user already active in guild 7; a NEW guild 8 would exceed max_guilds=1
        orch._instances = [_connected(0, guild_id=7, user_id=99)]

        with pytest.raises(QuotaExceededError):
            orch._enforce_quotas(guild_id=8, user_id=99)

    def test_same_guild_does_not_grow_guild_count(self, monkeypatch):
        # An additional bot in an ALREADY-active guild does not increase the
        # distinct-guild count, so max_guilds=1 is not tripped by it.
        orch = _orchestrator_with(
            monkeypatch,
            {"max_bots_per_guild": 5, "max_bots_per_guild_enabled": True,
             "max_guilds": 1},
        )
        orch._instances = [_connected(0, guild_id=7, user_id=99)]

        orch._enforce_quotas(guild_id=7, user_id=99)  # same guild → no raise

    def test_fail_safe_defaults_limit_one(self, monkeypatch):
        from orchestrator import QuotaExceededError

        # DEFAULT_ENTITLEMENTS → max_bots_per_guild 1, max_guilds 1 (restrictive)
        orch = _orchestrator_with(monkeypatch, dict(DEFAULT_ENTITLEMENTS))
        orch._instances = [_connected(0, guild_id=7, user_id=99)]

        with pytest.raises(QuotaExceededError):
            orch._enforce_quotas(guild_id=7, user_id=99)

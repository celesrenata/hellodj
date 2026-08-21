"""Tests for the unified SessionRegistry.

Covers core CRUD, query helpers, grace period management, and
Property 7 (composite key session independence) via hypothesis.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from bot.playback.session_registry import ChannelSession, CompositeKey, SessionRegistry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_session(
    guild_id: int = 1,
    channel_id: int = 100,
    session_type: str = "audio",
    started_at: float | None = None,
) -> ChannelSession:
    return ChannelSession(
        guild_id=guild_id,
        channel_id=channel_id,
        session_type=session_type,  # type: ignore[arg-type]
        started_at=started_at or time.time(),
    )


# ---------------------------------------------------------------------------
# Unit Tests — Core CRUD
# ---------------------------------------------------------------------------


class TestRegisterAndGet:
    def test_register_and_get(self) -> None:
        reg = SessionRegistry()
        session = _make_session(guild_id=1, channel_id=100)
        reg.register(session)
        assert reg.get(1, 100) is session

    def test_get_missing_returns_none(self) -> None:
        reg = SessionRegistry()
        assert reg.get(1, 100) is None

    def test_register_replaces_existing(self) -> None:
        reg = SessionRegistry()
        s1 = _make_session(guild_id=1, channel_id=100, session_type="audio")
        s2 = _make_session(guild_id=1, channel_id=100, session_type="video")
        reg.register(s1)
        reg.register(s2)
        assert reg.get(1, 100) is s2

    def test_unregister_removes_session(self) -> None:
        reg = SessionRegistry()
        session = _make_session(guild_id=1, channel_id=100)
        reg.register(session)
        reg.unregister(1, 100)
        assert reg.get(1, 100) is None

    def test_unregister_nonexistent_is_noop(self) -> None:
        reg = SessionRegistry()
        # Should not raise
        reg.unregister(999, 888)


class TestQueryHelpers:
    def test_get_by_guild(self) -> None:
        reg = SessionRegistry()
        s1 = _make_session(guild_id=1, channel_id=100, session_type="audio")
        s2 = _make_session(guild_id=1, channel_id=200, session_type="video")
        s3 = _make_session(guild_id=2, channel_id=300, session_type="audio")
        reg.register(s1)
        reg.register(s2)
        reg.register(s3)

        guild_1_sessions = reg.get_by_guild(1)
        assert len(guild_1_sessions) == 2
        assert s1 in guild_1_sessions
        assert s2 in guild_1_sessions
        assert s3 not in guild_1_sessions

    def test_get_audio_sessions(self) -> None:
        reg = SessionRegistry()
        s_audio = _make_session(guild_id=1, channel_id=100, session_type="audio")
        s_video = _make_session(guild_id=1, channel_id=200, session_type="video")
        reg.register(s_audio)
        reg.register(s_video)

        audio = reg.get_audio_sessions(1)
        assert len(audio) == 1
        assert audio[0] is s_audio

    def test_get_video_sessions(self) -> None:
        reg = SessionRegistry()
        s_audio = _make_session(guild_id=1, channel_id=100, session_type="audio")
        s_video = _make_session(guild_id=1, channel_id=200, session_type="video")
        reg.register(s_audio)
        reg.register(s_video)

        video = reg.get_video_sessions(1)
        assert len(video) == 1
        assert video[0] is s_video

    def test_active_keys(self) -> None:
        reg = SessionRegistry()
        s1 = _make_session(guild_id=1, channel_id=100)
        s2 = _make_session(guild_id=2, channel_id=200)
        reg.register(s1)
        reg.register(s2)

        keys = reg.active_keys()
        assert set(keys) == {(1, 100), (2, 200)}

    def test_active_keys_empty(self) -> None:
        reg = SessionRegistry()
        assert reg.active_keys() == []

    def test_get_by_guild_empty(self) -> None:
        reg = SessionRegistry()
        assert reg.get_by_guild(999) == []


# ---------------------------------------------------------------------------
# Unit Tests — Grace Period
# ---------------------------------------------------------------------------


class TestGracePeriod:
    @pytest.mark.asyncio
    async def test_grace_period_unregisters_after_timeout(self) -> None:
        reg = SessionRegistry()
        session = _make_session(guild_id=1, channel_id=100)
        reg.register(session)

        reg.start_grace_period(1, 100, timeout=0.05)
        assert reg.has_grace_period(1, 100)
        await asyncio.sleep(0.1)
        assert reg.get(1, 100) is None
        assert not reg.has_grace_period(1, 100)

    @pytest.mark.asyncio
    async def test_cancel_grace_period_keeps_session(self) -> None:
        reg = SessionRegistry()
        session = _make_session(guild_id=1, channel_id=100)
        reg.register(session)

        reg.start_grace_period(1, 100, timeout=0.1)
        reg.cancel_grace_period(1, 100)
        await asyncio.sleep(0.15)
        # Session should still be there
        assert reg.get(1, 100) is session

    @pytest.mark.asyncio
    async def test_grace_period_callback_invoked(self) -> None:
        reg = SessionRegistry()
        session = _make_session(guild_id=1, channel_id=100)
        reg.register(session)

        called_with: list[tuple[int, int]] = []

        async def callback(gid: int, cid: int) -> None:
            called_with.append((gid, cid))

        reg.start_grace_period(1, 100, timeout=0.05, callback=callback)
        await asyncio.sleep(0.1)
        assert called_with == [(1, 100)]

    @pytest.mark.asyncio
    async def test_grace_period_no_session_is_noop(self) -> None:
        reg = SessionRegistry()
        # Should not raise when no session exists
        reg.start_grace_period(999, 888, timeout=0.05)
        assert not reg.has_grace_period(999, 888)

    @pytest.mark.asyncio
    async def test_register_cancels_grace_period(self) -> None:
        reg = SessionRegistry()
        session = _make_session(guild_id=1, channel_id=100)
        reg.register(session)

        reg.start_grace_period(1, 100, timeout=0.1)
        assert reg.has_grace_period(1, 100)

        # Re-register (as if someone rejoined)
        new_session = _make_session(guild_id=1, channel_id=100, session_type="video")
        reg.register(new_session)
        assert not reg.has_grace_period(1, 100)

        await asyncio.sleep(0.15)
        # Session should still exist (grace was cancelled)
        assert reg.get(1, 100) is new_session

    @pytest.mark.asyncio
    async def test_has_grace_period_false_when_none(self) -> None:
        reg = SessionRegistry()
        assert not reg.has_grace_period(1, 100)

    @pytest.mark.asyncio
    async def test_grace_period_callback_error_still_unregisters(self) -> None:
        reg = SessionRegistry()
        session = _make_session(guild_id=1, channel_id=100)
        reg.register(session)

        async def bad_callback(gid: int, cid: int) -> None:
            raise RuntimeError("oops")

        reg.start_grace_period(1, 100, timeout=0.05, callback=bad_callback)
        await asyncio.sleep(0.1)
        # Session should still be unregistered despite callback error
        assert reg.get(1, 100) is None


# ---------------------------------------------------------------------------
# Property-Based Test — Property 7: Composite key session independence
# ---------------------------------------------------------------------------

# Feature: unified-playback, Property 7: Composite key session independence
# **Validates: Requirements 4.1, 4.2, 4.3, 4.4**


# Strategy for guild/channel IDs (keep small for readability but diverse enough)
guild_ids = st.integers(min_value=1, max_value=1000)
channel_ids = st.integers(min_value=1, max_value=1000)
session_types = st.sampled_from(["audio", "video"])


@settings(max_examples=100)
@given(
    guild_id=guild_ids,
    channel_a=channel_ids,
    channel_b=channel_ids,
    type_a=session_types,
    type_b=session_types,
)
def test_property_7_composite_key_independence(
    guild_id: int,
    channel_a: int,
    channel_b: int,
    type_a: str,
    type_b: str,
) -> None:
    """For any set of sessions registered under the same guild_id but different
    channel_ids, registering, retrieving, or unregistering a session at one
    composite key SHALL NOT affect sessions at other composite keys within the
    same guild.

    **Validates: Requirements 4.1, 4.2, 4.3, 4.4**
    """
    # Skip when channels are the same (same key = not independent)
    if channel_a == channel_b:
        return

    reg = SessionRegistry()
    session_a = _make_session(
        guild_id=guild_id, channel_id=channel_a, session_type=type_a
    )
    session_b = _make_session(
        guild_id=guild_id, channel_id=channel_b, session_type=type_b
    )

    # Register both
    reg.register(session_a)
    reg.register(session_b)

    # Both are independently retrievable
    assert reg.get(guild_id, channel_a) is session_a
    assert reg.get(guild_id, channel_b) is session_b

    # Unregistering A does not affect B
    reg.unregister(guild_id, channel_a)
    assert reg.get(guild_id, channel_a) is None
    assert reg.get(guild_id, channel_b) is session_b

    # Re-register A does not affect B
    session_a2 = _make_session(
        guild_id=guild_id, channel_id=channel_a, session_type=type_a
    )
    reg.register(session_a2)
    assert reg.get(guild_id, channel_a) is session_a2
    assert reg.get(guild_id, channel_b) is session_b

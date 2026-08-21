"""Property-based tests for dual-session tie-breaking (Property 13).

# Feature: unified-playback, Property 13: Dual-session tie-breaking by timestamp

Validates Requirements 7.4
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from hypothesis import given, settings, assume
from hypothesis import strategies as st

# Ensure the bot directory is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bot"))

from playback.router import PlaybackRouter
from playback.session_registry import ChannelSession, SessionRegistry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_interaction(
    *,
    guild_id: int = 100,
    channel_id: int | None = 200,
    user_id: int = 999,
) -> MagicMock:
    """Create a mock discord.Interaction with voice state."""
    interaction = MagicMock()
    interaction.guild_id = guild_id
    interaction.guild = MagicMock()
    interaction.user = MagicMock()
    interaction.user.id = user_id
    interaction.response = MagicMock()
    interaction.response.send_message = AsyncMock()

    if channel_id is not None:
        voice = MagicMock()
        voice.channel = MagicMock()
        voice.channel.id = channel_id
        interaction.user.voice = voice
    else:
        interaction.user.voice = None

    def get_channel(cid: int) -> MagicMock:
        ch = MagicMock()
        ch.name = f"channel-{cid}"
        return ch

    interaction.guild.get_channel = get_channel
    return interaction


def _make_router(
    registry: SessionRegistry | None = None,
    orchestrator: MagicMock | None = None,
) -> PlaybackRouter:
    """Create a PlaybackRouter with mocked dependencies."""
    import playback.classifier as classifier_module

    if registry is None:
        registry = SessionRegistry()
    if orchestrator is None:
        orchestrator = MagicMock()
        orchestrator.available_count = 5
        orchestrator.get_instance_for_channel = MagicMock(return_value=None)
        orchestrator.assign_instance = AsyncMock(
            return_value=MagicMock(index=0, display_name="Instance #1")
        )
        orchestrator.release_instance = AsyncMock()

    activity_backend = MagicMock()

    return PlaybackRouter(
        classifier=classifier_module,
        registry=registry,
        orchestrator=orchestrator,
        activity_backend=activity_backend,
    )


def _make_session(
    guild_id: int = 100,
    channel_id: int = 200,
    session_type: str = "audio",
    started_at: float | None = None,
) -> ChannelSession:
    """Create a ChannelSession for testing."""
    return ChannelSession(
        guild_id=guild_id,
        channel_id=channel_id,
        session_type=session_type,
        started_at=started_at or time.time(),
    )


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

guild_ids = st.integers(min_value=1, max_value=10**18)
channel_ids = st.integers(min_value=1, max_value=10**18)
# Timestamps: positive floats, with some separation for clear ordering
timestamps = st.floats(min_value=1.0, max_value=10**10, allow_nan=False, allow_infinity=False)


# ---------------------------------------------------------------------------
# Property 13: Dual-session tie-breaking by timestamp
# ---------------------------------------------------------------------------


class TestProperty13DualSessionTieBreaking:
    """Property 13: Dual-session tie-breaking by timestamp.

    **Validates: Requirements 7.4**

    When both audio and video sessions exist for the same channel,
    PlaybackRouter routes commands to the session with the more recent
    `started_at`.
    """

    @settings(max_examples=100)
    @given(
        guild_id=guild_ids,
        channel_id=channel_ids,
        ts_audio=timestamps,
        ts_video=timestamps,
    )
    def test_resolve_session_picks_most_recent(
        self, guild_id: int, channel_id: int, ts_audio: float, ts_video: float
    ) -> None:
        """_resolve_session returns the session with the more recent started_at."""
        assume(ts_audio != ts_video)

        audio_session = _make_session(
            guild_id=guild_id,
            channel_id=channel_id,
            session_type="audio",
            started_at=ts_audio,
        )
        video_session = _make_session(
            guild_id=guild_id,
            channel_id=channel_id,
            session_type="video",
            started_at=ts_video,
        )

        registry = SessionRegistry()

        # Simulate dual-session by putting both sessions in the internal dict.
        # The registry normally only holds one per key, so we use internal access
        # to simulate the dual-session scenario that _resolve_session handles
        # via get_by_guild fallback.
        # Store one normally, then inject the other with a slightly modified key
        # approach — but _resolve_session's dual-session path uses get_by_guild.
        # So we need both sessions accessible via get_by_guild but neither via
        # a single .get() call for this key.

        # Approach: Don't register either at the exact (guild, channel) key.
        # Instead, inject both into _sessions with synthetic keys that share
        # the same guild_id and channel_id. This forces the dual-session path.
        # Actually — looking at the router code, _resolve_session first tries
        # registry.get(guild_id, channel_id), and if that returns a session,
        # it returns it directly. The dual-session tie-breaking path only
        # triggers when registry.get() returns None but get_by_guild() returns
        # multiple sessions with the same channel_id.

        # To exercise the tie-breaking path properly, we need to NOT have
        # a session at the exact composite key, but have sessions accessible
        # via get_by_guild. This is tricky with the current registry design
        # which uses (guild_id, channel_id) as the key.

        # The cleanest approach: test _resolve_session's logic directly by
        # mocking the registry's internal state to simulate dual-session.
        # We can inject two sessions that both report the same channel_id
        # but are stored under different internal keys — this won't work
        # with the real registry.

        # Better: use the real registry and verify the router's _resolve_session
        # tie-breaking logic by setting up the scenario where get() returns None
        # and get_by_guild returns multiple channel sessions.

        # Since SessionRegistry can only store one session per (guild, channel),
        # we'll mock the registry to simulate the dual-session scenario.
        mock_registry = MagicMock()
        mock_registry.get = MagicMock(return_value=None)
        mock_registry.get_by_guild = MagicMock(
            return_value=[audio_session, video_session]
        )
        mock_registry.get_audio_sessions = MagicMock(return_value=[audio_session])
        mock_registry.get_video_sessions = MagicMock(return_value=[video_session])

        router = _make_router(registry=mock_registry)
        # Override the registry reference so _resolve_session uses our mock
        router._registry = mock_registry

        result = router._resolve_session(guild_id, channel_id)

        # The most recent started_at should win
        if ts_video > ts_audio:
            assert result is video_session
        else:
            assert result is audio_session

    @settings(max_examples=100)
    @given(
        guild_id=guild_ids,
        channel_id=channel_ids,
        ts_older=st.floats(min_value=1.0, max_value=5000.0, allow_nan=False, allow_infinity=False),
        ts_delta=st.floats(min_value=0.1, max_value=5000.0, allow_nan=False, allow_infinity=False),
    )
    def test_newer_session_always_wins(
        self, guild_id: int, channel_id: int, ts_older: float, ts_delta: float
    ) -> None:
        """The session with the strictly newer timestamp always wins resolution."""
        ts_newer = ts_older + ts_delta

        older_session = _make_session(
            guild_id=guild_id,
            channel_id=channel_id,
            session_type="audio",
            started_at=ts_older,
        )
        newer_session = _make_session(
            guild_id=guild_id,
            channel_id=channel_id,
            session_type="video",
            started_at=ts_newer,
        )

        mock_registry = MagicMock()
        mock_registry.get = MagicMock(return_value=None)
        mock_registry.get_by_guild = MagicMock(
            return_value=[older_session, newer_session]
        )

        router = _make_router()
        router._registry = mock_registry

        result = router._resolve_session(guild_id, channel_id)
        assert result is newer_session

    def test_single_session_returned_directly(self) -> None:
        """When only one session exists, it's returned regardless of tie-breaking."""
        registry = SessionRegistry()
        session = _make_session(guild_id=100, channel_id=200, session_type="audio")
        registry.register(session)

        router = _make_router(registry=registry)
        result = router._resolve_session(100, 200)
        assert result is session

    def test_no_sessions_returns_none(self) -> None:
        """When no sessions exist for the key, returns None."""
        registry = SessionRegistry()
        router = _make_router(registry=registry)
        result = router._resolve_session(100, 200)
        assert result is None

    @settings(max_examples=100)
    @given(
        guild_id=guild_ids,
        channel_id=channel_ids,
        ts_audio=timestamps,
        ts_video=timestamps,
    )
    def test_tie_breaking_is_deterministic(
        self, guild_id: int, channel_id: int, ts_audio: float, ts_video: float
    ) -> None:
        """Calling _resolve_session multiple times with same state gives same result."""
        assume(ts_audio != ts_video)

        audio_session = _make_session(
            guild_id=guild_id,
            channel_id=channel_id,
            session_type="audio",
            started_at=ts_audio,
        )
        video_session = _make_session(
            guild_id=guild_id,
            channel_id=channel_id,
            session_type="video",
            started_at=ts_video,
        )

        mock_registry = MagicMock()
        mock_registry.get = MagicMock(return_value=None)
        mock_registry.get_by_guild = MagicMock(
            return_value=[audio_session, video_session]
        )

        router = _make_router()
        router._registry = mock_registry

        result1 = router._resolve_session(guild_id, channel_id)
        result2 = router._resolve_session(guild_id, channel_id)
        assert result1 is result2

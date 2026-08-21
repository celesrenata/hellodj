"""Property-based tests for audio channel exclusivity (Properties 8, 9, 10).

# Feature: unified-playback, Property 8: Single-instance audio channel exclusivity
# Feature: unified-playback, Property 9: Audio does not block video
# Feature: unified-playback, Property 10: Same-channel audio enqueues

Validates Requirements 5.1, 5.2, 5.3, 5.5
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
    primary_bot: MagicMock | None = None,
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
        primary_bot=primary_bot,
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

# Guild and channel IDs are positive integers (Discord snowflakes)
guild_ids = st.integers(min_value=1, max_value=10**18)
channel_ids = st.integers(min_value=1, max_value=10**18)

# Audio queries that will classify as audio (Spotify URLs)
audio_queries = st.sampled_from([
    "https://open.spotify.com/track/abc123",
    "https://open.spotify.com/track/xyz789",
    "spsearch:test song",
    "https://soundcloud.com/artist/track",
    "https://music.youtube.com/watch?v=abc",
])


# ---------------------------------------------------------------------------
# Property 8: Single-instance audio channel exclusivity
# ---------------------------------------------------------------------------


class TestProperty8ChannelExclusivity:
    """Property 8: Single-instance audio channel exclusivity.

    **Validates: Requirements 5.1, 5.5**

    For any guild where a Bot_Instance has an active audio session in channel A,
    if user in channel B requests audio, PlaybackRouter rejects. After channel A
    session ends, channel B succeeds.
    """

    @settings(max_examples=100)
    @given(
        guild_id=guild_ids,
        channel_a=channel_ids,
        channel_b=channel_ids,
    )
    @pytest.mark.asyncio
    async def test_audio_rejected_in_different_channel(
        self, guild_id: int, channel_a: int, channel_b: int
    ) -> None:
        """Audio in channel B rejected when instance is busy in channel A."""
        assume(channel_a != channel_b)

        registry = SessionRegistry()
        session = _make_session(
            guild_id=guild_id, channel_id=channel_a, session_type="audio"
        )
        registry.register(session)

        # Orchestrator: instance busy in channel A, no others available
        instance_mock = MagicMock(index=0, display_name="Instance #1")
        orchestrator = MagicMock()
        orchestrator.available_count = 0
        orchestrator.get_instance_for_channel = MagicMock(return_value=instance_mock)
        orchestrator.assign_instance = AsyncMock(return_value=None)
        orchestrator.release_instance = AsyncMock()

        # Primary bot also busy in channel A
        primary_bot = MagicMock()
        vc_mock = MagicMock()
        vc_mock.guild = MagicMock()
        vc_mock.guild.id = guild_id
        vc_mock.channel = MagicMock()
        vc_mock.channel.id = channel_a
        primary_bot.voice_clients = [vc_mock]

        router = _make_router(
            registry=registry, orchestrator=orchestrator, primary_bot=primary_bot
        )
        interaction = _make_interaction(
            guild_id=guild_id, channel_id=channel_b, user_id=999
        )

        await router.play(interaction, "https://open.spotify.com/track/test123")

        # Should be rejected with ephemeral error
        interaction.response.send_message.assert_called_once()
        call_kwargs = interaction.response.send_message.call_args[1]
        assert call_kwargs.get("ephemeral") is True
        msg = call_kwargs.get("content") or interaction.response.send_message.call_args[0][0]
        assert "Music is playing in" in msg

    @settings(max_examples=100)
    @given(
        guild_id=guild_ids,
        channel_a=channel_ids,
        channel_b=channel_ids,
    )
    @pytest.mark.asyncio
    async def test_audio_succeeds_after_session_ends(
        self, guild_id: int, channel_a: int, channel_b: int
    ) -> None:
        """After session in channel A ends, channel B request succeeds."""
        assume(channel_a != channel_b)

        registry = SessionRegistry()
        # Session in channel A existed but is now ended (not registered)
        # Registry is empty — no sessions

        orchestrator = MagicMock()
        orchestrator.available_count = 5
        orchestrator.get_instance_for_channel = MagicMock(return_value=None)
        orchestrator.assign_instance = AsyncMock(
            return_value=MagicMock(index=0, display_name="Instance #1")
        )
        orchestrator.release_instance = AsyncMock()

        # Primary bot available (not in any VC)
        primary_bot = MagicMock()
        primary_bot.voice_clients = []

        router = _make_router(
            registry=registry, orchestrator=orchestrator, primary_bot=primary_bot
        )
        interaction = _make_interaction(
            guild_id=guild_id, channel_id=channel_b, user_id=999
        )

        await router.play(interaction, "https://open.spotify.com/track/test123")

        # Should succeed (start playback)
        interaction.response.send_message.assert_called_once()
        call_kwargs = interaction.response.send_message.call_args[1]
        # Not ephemeral means success
        assert call_kwargs.get("ephemeral", False) is False
        msg = interaction.response.send_message.call_args[0][0]
        assert "Starting playback" in msg


# ---------------------------------------------------------------------------
# Property 9: Audio does not block video
# ---------------------------------------------------------------------------


class TestProperty9AudioDoesNotBlockVideo:
    """Property 9: Audio does not block video.

    **Validates: Requirements 5.2**

    For any guild with active audio in channel A, video requests from any
    channel succeed.
    """

    @settings(max_examples=100)
    @given(
        guild_id=guild_ids,
        audio_channel=channel_ids,
        video_channel=channel_ids,
    )
    @pytest.mark.asyncio
    async def test_video_succeeds_despite_active_audio(
        self, guild_id: int, audio_channel: int, video_channel: int
    ) -> None:
        """Video request from any channel succeeds regardless of audio state."""
        registry = SessionRegistry()
        audio_session = _make_session(
            guild_id=guild_id, channel_id=audio_channel, session_type="audio"
        )
        registry.register(audio_session)

        orchestrator = MagicMock()
        orchestrator.available_count = 5
        orchestrator.get_instance_for_channel = MagicMock(return_value=None)
        orchestrator.assign_instance = AsyncMock(
            return_value=MagicMock(index=0, display_name="Instance #1")
        )

        router = _make_router(registry=registry, orchestrator=orchestrator)
        interaction = _make_interaction(
            guild_id=guild_id, channel_id=video_channel, user_id=999
        )

        # Request video explicitly
        await router.play(
            interaction, "https://example.org/movie.mp4", mode="video"
        )

        # Should succeed — video is never blocked by audio
        interaction.response.send_message.assert_called_once()
        call_kwargs = interaction.response.send_message.call_args[1]
        assert call_kwargs.get("ephemeral", False) is False
        msg = interaction.response.send_message.call_args[0][0]
        assert "🎬" in msg


# ---------------------------------------------------------------------------
# Property 10: Same-channel audio enqueues
# ---------------------------------------------------------------------------


class TestProperty10SameChannelEnqueues:
    """Property 10: Same-channel audio enqueues.

    **Validates: Requirements 5.3**

    For any active audio session, user in same channel requesting audio
    appends to queue (queue grows by 1).
    """

    @settings(max_examples=100)
    @given(
        guild_id=guild_ids,
        channel_id=channel_ids,
        initial_queue_size=st.integers(min_value=0, max_value=50),
    )
    @pytest.mark.asyncio
    async def test_same_channel_audio_enqueues(
        self, guild_id: int, channel_id: int, initial_queue_size: int
    ) -> None:
        """Audio request in same channel as active session appends to queue."""
        registry = SessionRegistry()
        session = _make_session(
            guild_id=guild_id, channel_id=channel_id, session_type="audio"
        )
        # Pre-fill queue
        session.queue = [{"query": f"track_{i}"} for i in range(initial_queue_size)]
        registry.register(session)

        orchestrator = MagicMock()
        orchestrator.available_count = 5
        orchestrator.get_instance_for_channel = MagicMock(return_value=None)

        router = _make_router(registry=registry, orchestrator=orchestrator)
        interaction = _make_interaction(
            guild_id=guild_id, channel_id=channel_id, user_id=999
        )

        await router.play(interaction, "https://open.spotify.com/track/new123")

        # Queue should grow by exactly 1
        assert len(session.queue) == initial_queue_size + 1

        # Response should confirm addition
        interaction.response.send_message.assert_called_once()
        call_kwargs = interaction.response.send_message.call_args[1]
        assert call_kwargs.get("ephemeral", False) is False
        msg = interaction.response.send_message.call_args[0][0]
        assert "Added to queue" in msg

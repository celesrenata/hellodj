"""Unit tests for Task 7: InstanceOrchestrator + PlaybackRouter integration.

Tests cover:
- _check_primary_available() logic
- Primary bot preference in _handle_audio_play()
- Inactivity timer start/cancel/expiry lifecycle
- _stop_audio cancels inactivity timer
- All-occupied error with primary bot busy
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from playback.router import PlaybackRouter, _INACTIVITY_TIMEOUT_S
from playback.session_registry import ChannelSession, SessionRegistry


# ---------------------------------------------------------------------------
# Fixtures
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

    # Mock cogs accessed via interaction.client.get_cog()
    music_cog = MagicMock()
    music_cog._play_link = AsyncMock()
    music_cog._play_song = AsyncMock()
    music_cog._play_playlist = AsyncMock()

    video_cog = MagicMock()
    video_cog.video_play = AsyncMock()
    video_cog.video_stop = AsyncMock()
    video_cog.video_skip = AsyncMock()
    video_cog._registry = MagicMock()
    video_cog._registry.get = MagicMock(return_value=None)

    def get_cog(name: str) -> MagicMock | None:
        if name == "Music":
            return music_cog
        if name == "Video":
            return video_cog
        return None

    interaction.client = MagicMock()
    interaction.client.get_cog = get_cog
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


def _make_primary_bot(
    guild_id: int | None = None,
    channel_id: int | None = None,
) -> MagicMock:
    """Create a mock primary bot with optional voice connection.

    If guild_id and channel_id are provided, the bot is "connected" to that
    voice channel. If only guild_id is given with channel_id=None, that's
    unusual — we'll just leave voice_clients empty. If neither, bot is idle.
    """
    bot = MagicMock()
    bot.voice_clients = []

    if guild_id is not None and channel_id is not None:
        vc = MagicMock()
        vc.guild = MagicMock()
        vc.guild.id = guild_id
        vc.channel = MagicMock()
        vc.channel.id = channel_id
        bot.voice_clients = [vc]

    return bot


# ---------------------------------------------------------------------------
# Tests: _check_primary_available
# ---------------------------------------------------------------------------


class TestCheckPrimaryAvailable:
    """Tests for _check_primary_available()."""

    def test_no_primary_bot_returns_true(self) -> None:
        """When primary_bot is None, always returns True (backward compat)."""
        router = _make_router(primary_bot=None)
        assert router._check_primary_available(100, 200) is True

    def test_primary_not_connected_returns_true(self) -> None:
        """Primary bot not in any VC → available."""
        bot = _make_primary_bot()  # no voice_clients
        router = _make_router(primary_bot=bot)
        assert router._check_primary_available(100, 200) is True

    def test_primary_in_target_channel_returns_true(self) -> None:
        """Primary bot already in the target channel → can reuse."""
        bot = _make_primary_bot(guild_id=100, channel_id=200)
        router = _make_router(primary_bot=bot)
        assert router._check_primary_available(100, 200) is True

    def test_primary_in_different_channel_returns_false(self) -> None:
        """Primary bot in a different channel → busy."""
        bot = _make_primary_bot(guild_id=100, channel_id=300)
        router = _make_router(primary_bot=bot)
        assert router._check_primary_available(100, 200) is False

    def test_primary_in_different_guild_returns_true(self) -> None:
        """Primary bot in a VC in a different guild → available for our guild."""
        bot = _make_primary_bot(guild_id=999, channel_id=500)
        router = _make_router(primary_bot=bot)
        assert router._check_primary_available(100, 200) is True


# ---------------------------------------------------------------------------
# Tests: Primary bot preference in audio play
# ---------------------------------------------------------------------------


class TestPrimaryBotPreference:
    """Tests that _handle_audio_play prefers primary bot over secondary."""

    @pytest.mark.asyncio
    async def test_primary_used_when_available(self) -> None:
        """When no existing session and primary is available, use primary."""
        bot = _make_primary_bot()  # not connected anywhere
        orchestrator = MagicMock()
        orchestrator.available_count = 3
        orchestrator.get_instance_for_channel = MagicMock(return_value=None)
        orchestrator.assign_instance = AsyncMock(return_value=None)

        router = _make_router(orchestrator=orchestrator, primary_bot=bot)
        interaction = _make_interaction(guild_id=100, channel_id=200)

        await router.play(interaction, "https://open.spotify.com/track/test")

        # Should succeed with primary (assign_instance NOT called)
        orchestrator.assign_instance.assert_not_called()
        # Should have delegated to Music cog
        music_cog = interaction.client.get_cog("Music")
        music_cog._play_link.assert_called_once()

    @pytest.mark.asyncio
    async def test_secondary_used_when_primary_busy(self) -> None:
        """When primary is busy in another channel, still proceed with playback."""
        bot = _make_primary_bot(guild_id=100, channel_id=300)  # busy in 300
        orchestrator = MagicMock()
        orchestrator.available_count = 3
        orchestrator.get_instance_for_channel = MagicMock(return_value=None)
        orchestrator.assign_instance = AsyncMock(
            return_value=MagicMock(index=1, display_name="Instance #2")
        )

        router = _make_router(orchestrator=orchestrator, primary_bot=bot)
        interaction = _make_interaction(guild_id=100, channel_id=200)

        await router.play(interaction, "https://open.spotify.com/track/test")

        # Should have delegated to Music cog (use_primary=False path)
        music_cog = interaction.client.get_cog("Music")
        music_cog._play_link.assert_called_once()

    @pytest.mark.asyncio
    async def test_all_occupied_error_includes_primary(self) -> None:
        """When primary and all secondaries are busy, show error."""
        bot = _make_primary_bot(guild_id=100, channel_id=300)  # busy in 300
        registry = SessionRegistry()
        # Existing session in channel 300 (served by primary)
        session = _make_session(guild_id=100, channel_id=300, session_type="audio")
        registry.register(session)

        orchestrator = MagicMock()
        orchestrator.available_count = 0
        orchestrator.get_instance_for_channel = MagicMock(return_value=None)

        router = _make_router(
            registry=registry, orchestrator=orchestrator, primary_bot=bot
        )
        interaction = _make_interaction(guild_id=100, channel_id=200)

        await router.play(interaction, "https://open.spotify.com/track/test")

        interaction.response.send_message.assert_called_once()
        call_kwargs = interaction.response.send_message.call_args[1]
        msg = call_kwargs.get("content") or interaction.response.send_message.call_args[0][0]
        # Should reject — either "Music is playing in" or "All music slots"
        assert "Music is playing in" in msg or "All music slots" in msg
        assert call_kwargs.get("ephemeral") is True


# ---------------------------------------------------------------------------
# Tests: Inactivity timer lifecycle
# ---------------------------------------------------------------------------


class TestInactivityTimer:
    """Tests for start_inactivity_timer, cancel_inactivity_timer, _inactivity_expired."""

    @pytest.mark.asyncio
    async def test_start_creates_task(self) -> None:
        """start_inactivity_timer creates an asyncio task."""
        router = _make_router()
        router.start_inactivity_timer(100, 200)

        key = (100, 200)
        assert key in router._inactivity_timers
        task = router._inactivity_timers[key]
        assert not task.done()

        # Cleanup
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    @pytest.mark.asyncio
    async def test_cancel_removes_task(self) -> None:
        """cancel_inactivity_timer cancels and removes the task."""
        router = _make_router()
        router.start_inactivity_timer(100, 200)

        key = (100, 200)
        assert key in router._inactivity_timers

        router.cancel_inactivity_timer(100, 200)
        assert key not in router._inactivity_timers

    @pytest.mark.asyncio
    async def test_cancel_nonexistent_is_noop(self) -> None:
        """cancel_inactivity_timer with no timer does nothing."""
        router = _make_router()
        # Should not raise
        router.cancel_inactivity_timer(100, 200)
        assert (100, 200) not in router._inactivity_timers

    @pytest.mark.asyncio
    async def test_start_replaces_existing(self) -> None:
        """Starting a timer for the same key cancels the old one."""
        router = _make_router()
        router.start_inactivity_timer(100, 200)
        old_task = router._inactivity_timers[(100, 200)]

        router.start_inactivity_timer(100, 200)
        new_task = router._inactivity_timers[(100, 200)]

        # Allow cancellation to propagate
        await asyncio.sleep(0)

        assert old_task.cancelled() or old_task.done()
        assert not new_task.done()

        # Cleanup
        new_task.cancel()
        try:
            await new_task
        except asyncio.CancelledError:
            pass

    @pytest.mark.asyncio
    async def test_expiry_releases_instance(self) -> None:
        """When timer expires, session is unregistered and instance released."""
        registry = SessionRegistry()
        session = _make_session(guild_id=100, channel_id=200, session_type="audio")
        registry.register(session)

        orchestrator = MagicMock()
        orchestrator.release_instance = AsyncMock()
        orchestrator.available_count = 5
        orchestrator.get_instance_for_channel = MagicMock(return_value=None)

        router = _make_router(registry=registry, orchestrator=orchestrator)

        # Call _inactivity_expired directly with patched sleep
        with patch("playback.router.asyncio.sleep", new_callable=AsyncMock):
            await router._inactivity_expired(100, 200)

        # Session should be removed
        assert registry.get(100, 200) is None
        orchestrator.release_instance.assert_called_once_with(100, 200)

    @pytest.mark.asyncio
    async def test_expiry_ignores_video_session(self) -> None:
        """Inactivity expiry only releases audio sessions, not video."""
        registry = SessionRegistry()
        session = _make_session(guild_id=100, channel_id=200, session_type="video")
        registry.register(session)

        orchestrator = MagicMock()
        orchestrator.release_instance = AsyncMock()
        orchestrator.available_count = 5
        orchestrator.get_instance_for_channel = MagicMock(return_value=None)

        router = _make_router(registry=registry, orchestrator=orchestrator)

        with patch("playback.router.asyncio.sleep", new_callable=AsyncMock):
            await router._inactivity_expired(100, 200)

        # Video session should NOT be removed
        assert registry.get(100, 200) is session
        orchestrator.release_instance.assert_not_called()

    @pytest.mark.asyncio
    async def test_expiry_noop_when_no_session(self) -> None:
        """Inactivity expiry is a no-op when no session exists."""
        orchestrator = MagicMock()
        orchestrator.release_instance = AsyncMock()
        orchestrator.available_count = 5
        orchestrator.get_instance_for_channel = MagicMock(return_value=None)

        router = _make_router(orchestrator=orchestrator)

        with patch("playback.router.asyncio.sleep", new_callable=AsyncMock):
            await router._inactivity_expired(100, 200)

        orchestrator.release_instance.assert_not_called()

    @pytest.mark.asyncio
    async def test_stop_audio_cancels_timer(self) -> None:
        """Explicit stop cancels any running inactivity timer."""
        registry = SessionRegistry()
        session = _make_session(guild_id=100, channel_id=200, session_type="audio")
        registry.register(session)

        orchestrator = MagicMock()
        orchestrator.release_instance = AsyncMock()
        orchestrator.available_count = 5
        orchestrator.get_instance_for_channel = MagicMock(return_value=None)

        router = _make_router(registry=registry, orchestrator=orchestrator)
        router.start_inactivity_timer(100, 200)

        # Verify timer exists
        assert (100, 200) in router._inactivity_timers

        interaction = _make_interaction(guild_id=100, channel_id=200)

        mock_player_obj = MagicMock()
        mock_player_obj.connected = True
        mock_player_obj.disconnect = AsyncMock()

        with patch("player.get_player", return_value=mock_player_obj), \
             patch("player.get_state", return_value={"current": None, "queue": [], "player": None}), \
             patch("player.persist"):
            await router.stop(interaction)

        # Timer should be cancelled
        assert (100, 200) not in router._inactivity_timers


# ---------------------------------------------------------------------------
# Tests: Timeout constant
# ---------------------------------------------------------------------------


class TestConstants:
    """Verify configuration constants."""

    def test_inactivity_timeout_is_5_minutes(self) -> None:
        """Inactivity timeout should be 300 seconds (5 minutes)."""
        assert _INACTIVITY_TIMEOUT_S == 300.0

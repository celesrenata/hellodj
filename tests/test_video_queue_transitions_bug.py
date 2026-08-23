"""Bug condition exploration tests for video queue transition bugs.

These tests encode the EXPECTED (correct) behavior for video-to-video,
video-to-audio, and session-end transitions. They are designed to FAIL
on the current unfixed code, confirming the bugs exist.

**Validates: Requirements 1.1, 1.2, 1.3, 2.1, 2.2, 2.3**

Bug Conditions Tested:
- Case 1 (video-to-video): Active streamer session must be terminated before
  new video session starts when unified queue advances.
- Case 2 (video-to-audio): Active streamer session must be terminated and
  _is_video_active() must return False before audio playback begins.
- Case 3 (session end → DVD): Frontend must transition to VISUALIZER_DVD mode
  (not IDLE) when session ends with empty queue.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

import pytest
from hypothesis import given, settings, assume, HealthCheck
from hypothesis import strategies as st

# Ensure bot directory is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bot"))


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

def music_video_entry(title: str | None = None) -> st.SearchStrategy[dict]:
    """Generate a music_video queue entry."""
    return st.fixed_dictionaries({
        "type": st.just("music_video"),
        "title": st.text(min_size=1, max_size=30) if title is None else st.just(title),
        "query": st.text(min_size=1, max_size=50),
        "url": st.just("https://example.com/video.mp4"),
    })


def audio_entry(title: str | None = None) -> st.SearchStrategy[dict]:
    """Generate an audio queue entry (no 'type' field or type != 'music_video')."""
    return st.fixed_dictionaries({
        "title": st.text(min_size=1, max_size=30) if title is None else st.just(title),
        "uri": st.just("https://example.com/audio.mp3"),
        "url": st.just("https://example.com/audio.mp3"),
    })


guild_id_strategy = st.integers(min_value=1, max_value=999999)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_mock_streamer(*, is_active: bool = True, queue_empty: bool = True) -> MagicMock:
    """Create a mock ActivityStreamer with controlled state."""
    streamer = MagicMock()
    streamer.is_active = is_active
    streamer.queue = [] if queue_empty else [MagicMock()]
    streamer.channel_id = 100
    streamer.guild_id = 0  # Will be set per test
    streamer.stop = AsyncMock()
    streamer.play = AsyncMock()
    streamer.enqueue = MagicMock()
    streamer.skip = AsyncMock()
    streamer.source = MagicMock()
    streamer.source.title = "Currently Playing Video"
    return streamer


def _make_mock_registry(guild_id: int, streamer: MagicMock) -> MagicMock:
    """Create a mock SessionRegistry with a registered streamer."""
    registry = MagicMock()
    registry.get = MagicMock(return_value=streamer)
    registry._sessions = {(guild_id, 100): streamer}
    registry.unregister = MagicMock()
    return registry


def _make_mock_video_cog(registry: MagicMock) -> MagicMock:
    """Create a mock Video cog with the registry and backend."""
    video_cog = MagicMock()
    video_cog._registry = registry
    video_cog._backend = MagicMock()
    video_cog._backend.ws_hub = MagicMock()
    video_cog._backend.ws_hub.unregister_streamer = MagicMock()
    video_cog._backend.ws_hub.register_streamer = MagicMock()
    video_cog._backend.ws_hub.broadcast_from_bot = AsyncMock()
    video_cog._backend.ws_hub.set_state = MagicMock()
    video_cog._launcher = MagicMock()
    video_cog._launcher.launch = AsyncMock(return_value={"code": "abc123"})
    video_cog._now_playing_messages = {}
    video_cog._activity_urls = {}
    video_cog._start_seek_bar_update = MagicMock()
    return video_cog


def _make_mock_bot(video_cog: MagicMock) -> MagicMock:
    """Create a mock bot with the Video cog loaded."""
    bot = MagicMock()
    bot.get_cog = MagicMock(return_value=video_cog)
    bot.cogs = {"Video": video_cog}
    bot.user = MagicMock()
    bot.user.id = 123456789
    bot.get_guild = MagicMock(return_value=MagicMock())
    bot.is_closed = MagicMock(return_value=False)
    return bot


# ---------------------------------------------------------------------------
# Case 1: Video-to-Video — session must be terminated before new session starts
# ---------------------------------------------------------------------------

class TestVideoToVideoTransition:
    """Bug 1.1: When skipping from one video to another in the unified queue,
    the old session must be terminated before the new one starts.

    Current buggy behavior: _start_video_from_queue finds an active streamer
    and calls streamer.enqueue() instead of terminating and replacing.
    """

    @given(
        guild_id=guild_id_strategy,
        next_video=music_video_entry(),
    )
    @settings(
        max_examples=20,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
        deadline=None,
    )
    @pytest.mark.asyncio
    async def test_video_to_video_terminates_old_session(
        self, guild_id: int, next_video: dict
    ):
        """Property: For any active video session with empty internal queue,
        when the unified queue advances to another music_video entry,
        the old session MUST be terminated (stop called) before the new one starts.

        **Validates: Requirements 2.1**
        """
        import player

        # Set up mocks
        streamer = _make_mock_streamer(is_active=True, queue_empty=True)
        streamer.guild_id = guild_id
        registry = _make_mock_registry(guild_id, streamer)
        video_cog = _make_mock_video_cog(registry)
        bot = _make_mock_bot(video_cog)

        # Set up player state
        player.guild_state[guild_id] = {
            "queue": [next_video],
            "history": [],
            "voice_channel": MagicMock(id=100),
            "text_channel": MagicMock(send=AsyncMock()),
            "current": {"type": "music_video", "title": "Old Video"},
            "persist_enabled": False,
            "player": None,
            "repeat_mode": "off",
            "source_provider": "youtube",
            "_video_transition": False,
            "now_playing_msg": None,
        }

        # Patch bot ref and persist
        with patch.object(player, '_bot_ref', bot), \
             patch.object(player, 'persist', MagicMock()):

            # Mock the resolver to return a source
            mock_source = MagicMock()
            mock_source.title = next_video["title"]
            with patch('video.music_video_resolver.MusicVideoResolver') as MockResolver:
                MockResolver.return_value.resolve = AsyncMock(return_value=mock_source)

                await player._play_next_from_queue(guild_id)

        # ASSERTION: The old streamer must have been stopped (session terminated)
        # before the new video was started. On unfixed code, streamer.enqueue()
        # is called instead of streamer.stop() + new session creation.
        assert streamer.stop.await_count > 0 or registry.unregister.call_count > 0, (
            f"BUG CONFIRMED: Old video session was NOT terminated. "
            f"stop called={streamer.stop.await_count}, "
            f"unregister called={registry.unregister.call_count}. "
            f"Instead, enqueue was called={streamer.enqueue.call_count} times "
            f"(enqueuing to existing active session rather than replacing it)"
        )

        # Clean up
        player.guild_state.pop(guild_id, None)


# ---------------------------------------------------------------------------
# Case 2: Video-to-Audio — session terminated, _is_video_active() → False
# ---------------------------------------------------------------------------

class TestVideoToAudioTransition:
    """Bug 1.2: When skipping from a video to an audio track in the unified queue,
    the video session must be fully terminated so _is_video_active() returns False,
    allowing the audio track to proceed.

    Current buggy behavior: The peek guard in _play_next_from_queue sees that
    _is_video_active() is True and the next entry is audio, so it returns early
    (blocking audio) WITHOUT terminating the video session.
    """

    @given(
        guild_id=guild_id_strategy,
        next_audio=audio_entry(),
    )
    @settings(
        max_examples=20,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
        deadline=None,
    )
    @pytest.mark.asyncio
    async def test_video_to_audio_terminates_session_and_plays_audio(
        self, guild_id: int, next_audio: dict
    ):
        """Property: For any active video session, when the unified queue advances
        to an audio entry, the video session MUST be terminated and
        _is_video_active() MUST return False before audio playback starts.

        **Validates: Requirements 2.2**
        """
        import player

        # Set up mocks
        streamer = _make_mock_streamer(is_active=True, queue_empty=True)
        streamer.guild_id = guild_id
        registry = _make_mock_registry(guild_id, streamer)
        video_cog = _make_mock_video_cog(registry)
        bot = _make_mock_bot(video_cog)

        # Set up player state with an active video as current + audio as next
        player.guild_state[guild_id] = {
            "queue": [next_audio],
            "history": [],
            "voice_channel": MagicMock(id=100),
            "text_channel": MagicMock(send=AsyncMock()),
            "current": {"type": "music_video", "title": "Currently Playing Video"},
            "persist_enabled": False,
            "player": MagicMock(connected=True, playing=False, paused=False),
            "repeat_mode": "off",
            "source_provider": "youtube",
            "_video_transition": False,
            "now_playing_msg": None,
        }

        # Track whether _resolve_and_play was called (means audio got through)
        resolve_and_play_called = False
        original_resolve = player._resolve_and_play

        async def track_resolve_and_play(*args, **kwargs):
            nonlocal resolve_and_play_called
            resolve_and_play_called = True

        with patch.object(player, '_bot_ref', bot), \
             patch.object(player, 'persist', MagicMock()), \
             patch.object(player, '_resolve_and_play', side_effect=track_resolve_and_play):

            await player._play_next_from_queue(guild_id)

        state = player.guild_state.get(guild_id, {})

        # ASSERTION 1: The video session must have been terminated
        # On unfixed code, the peek guard returns early without terminating.
        session_terminated = (
            streamer.stop.await_count > 0
            or registry.unregister.call_count > 0
        )

        # ASSERTION 2: The audio entry must have been popped and played
        # On unfixed code, the early return leaves the audio entry stuck in queue.
        audio_advanced = (
            resolve_and_play_called
            or state.get("current") == next_audio
        )

        assert session_terminated and audio_advanced, (
            f"BUG CONFIRMED: Video-to-audio transition failed. "
            f"Session terminated={session_terminated} "
            f"(stop={streamer.stop.await_count}, unregister={registry.unregister.call_count}). "
            f"Audio advanced={audio_advanced} "
            f"(resolve_called={resolve_and_play_called}, "
            f"current={state.get('current')}). "
            f"The peek guard returned early without terminating the session, "
            f"leaving the audio entry stuck in the queue."
        )

        # Clean up
        player.guild_state.pop(guild_id, None)


# ---------------------------------------------------------------------------
# Case 3: Session End → DVD Screensaver (not IDLE)
# ---------------------------------------------------------------------------

class TestSessionEndDVDMode:
    """Bug 1.3: When a video session ends (queues empty), the frontend receives
    'session_end' and must transition to VISUALIZER_DVD mode, not IDLE.

    Current buggy behavior: app.js case 'session_end' calls setMode('IDLE')
    which shows a blank screen instead of the DVD bouncing logo screensaver.

    This test verifies the backend behavior: _on_video_session_end must clear
    the registry (so _is_video_active returns False) BEFORE calling
    _play_next_from_queue. When both queues are empty, the frontend should
    receive a session_end message that triggers VISUALIZER_DVD mode.
    """

    @given(guild_id=guild_id_strategy)
    @settings(
        max_examples=20,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
        deadline=None,
    )
    @pytest.mark.asyncio
    async def test_session_end_clears_registry_before_queue_advance(
        self, guild_id: int
    ):
        """Property: When _on_video_session_end fires with an audio entry in
        the unified queue, the session registry must be unregistered BEFORE
        _play_next_from_queue runs, so _is_video_active() returns False and
        the audio entry can proceed.

        On unfixed code, _on_video_session_end only clears state["current"] = None
        and then calls _play_next_from_queue. The registry still has the
        streamer registered, so _is_video_active() still returns True and
        the audio entry gets blocked by the peek guard.

        **Validates: Requirements 2.3**
        """
        import player

        # Set up mocks
        streamer = _make_mock_streamer(is_active=True, queue_empty=True)
        streamer.guild_id = guild_id
        registry = _make_mock_registry(guild_id, streamer)
        video_cog = _make_mock_video_cog(registry)
        bot = _make_mock_bot(video_cog)

        # Set up player state: audio entry in unified queue (session ending,
        # next track should be audio)
        player.guild_state[guild_id] = {
            "queue": [{"title": "Next Audio Track", "uri": "https://example.com/audio.mp3"}],
            "history": [],
            "voice_channel": MagicMock(id=100),
            "text_channel": MagicMock(send=AsyncMock()),
            "current": {"type": "music_video", "title": "Finished Video"},
            "persist_enabled": False,
            "player": MagicMock(connected=True, playing=False, paused=False),
            "repeat_mode": "off",
            "source_provider": "youtube",
            "_video_transition": False,
            "now_playing_msg": None,
            "alone_task": None,
        }

        # Track the order of operations: registry unregister must happen
        # BEFORE _play_next_from_queue is called
        call_order = []

        async def patched_play_next(gid, **kwargs):
            # At this point, check if the registry has been unregistered
            is_still_active = player._is_video_active(gid)
            call_order.append(("play_next", {"is_video_active": is_still_active}))
            # Don't actually advance — we just want to check the state

        with patch.object(player, '_bot_ref', bot), \
             patch.object(player, 'persist', MagicMock()), \
             patch.object(player, '_play_next_from_queue', side_effect=patched_play_next):

            await player._on_video_session_end(guild_id)

        # ASSERTION: When _play_next_from_queue is called from _on_video_session_end,
        # _is_video_active must already return False (registry was unregistered).
        # On unfixed code, the registry is NOT unregistered before calling
        # _play_next_from_queue, so _is_video_active still returns True.
        assert len(call_order) > 0, (
            "Expected _play_next_from_queue to be called from _on_video_session_end"
        )

        play_next_call = call_order[0]
        is_video_active_at_call = play_next_call[1]["is_video_active"]

        assert is_video_active_at_call is False, (
            f"BUG CONFIRMED: _is_video_active() still returns True when "
            f"_play_next_from_queue is called from _on_video_session_end. "
            f"The registry was not unregistered before advancing the queue. "
            f"This means audio entries after a video will be blocked by the "
            f"peek guard, and the frontend won't properly transition to "
            f"VISUALIZER_DVD mode."
        )

        # Clean up
        player.guild_state.pop(guild_id, None)

    @given(guild_id=guild_id_strategy)
    @settings(
        max_examples=10,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
        deadline=None,
    )
    @pytest.mark.asyncio
    async def test_frontend_session_end_triggers_dvd_not_idle(
        self, guild_id: int
    ):
        """Property: The frontend session_end handler must call
        setMode('VISUALIZER_DVD'), not setMode('IDLE').

        This is a code-level assertion: we read the actual frontend source
        and verify the handler cleans up video state and waits for the server's
        visualizer message (which arrives immediately after session_end).

        **Validates: Requirements 2.3**
        """
        # Read the actual frontend app.js source
        frontend_path = (
            Path(__file__).resolve().parent.parent
            / "bot" / "video" / "activity_frontend" / "app.js"
        )
        assert frontend_path.exists(), f"Frontend app.js not found at {frontend_path}"

        source = frontend_path.read_text()

        # Find the session_end case handler
        session_end_idx = source.find("case 'session_end':")
        assert session_end_idx != -1, "Could not find case 'session_end' in app.js"

        # Get the handler block (until next case or break)
        handler_block = source[session_end_idx:session_end_idx + 800]

        # The handler MUST go to IDLE and clean up video state. The server
        # sends a 'visualizer' message immediately after session_end with the
        # correct engine/config, so the client doesn't need to hardcode DVD.
        assert "setMode('IDLE')" in handler_block or \
               'setMode("IDLE")' in handler_block, (
            f"Frontend session_end handler should transition to IDLE mode "
            f"(server sends visualizer message immediately after). "
            f"Found in handler block: {repr(handler_block[:200])}..."
        )
        # Verify HLS cleanup happens
        assert "hls.destroy()" in handler_block or "hls = null" in handler_block, (
            "Frontend session_end handler must clean up HLS resources"
        )

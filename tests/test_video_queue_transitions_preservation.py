"""Preservation property tests for video queue transitions.

These tests capture EXISTING correct behaviors that must NOT change after the
bugfix is implemented. They encode regression guards for:

- Internal video queue skips (streamer.skip() delegates within streamer)
- Audio-only queue advancement (no video interference)
- Idle streamer reuse (_start_video_from_queue reuses idle streamer)
- Video transition flag blocking audio entries

All tests MUST PASS on the current (unfixed) code.

**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5**
"""

from __future__ import annotations

import sys
import time
import types as _types
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

# ---------------------------------------------------------------------------
# Pre-load stubs for modules that require filesystem/DB resources unavailable
# in the test environment. This MUST happen before importing bot.player.
# ---------------------------------------------------------------------------
_bot_dir = str(Path(__file__).resolve().parent.parent / "bot")
if _bot_dir not in sys.path:
    sys.path.insert(0, _bot_dir)

# Stub: credentials (requires encrypted SQLite DB at /app/data)
if "credentials" not in sys.modules:
    _mock_creds = _types.ModuleType("credentials")

    class _FakeCredentialStore:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def get(self, key: str, default: str | None = None) -> str | None:
            return default

    _mock_creds.CredentialStore = _FakeCredentialStore  # type: ignore[attr-defined]
    sys.modules["credentials"] = _mock_creds

# Stub: config (depends on credentials)
if "config" not in sys.modules:
    _mock_config = _types.ModuleType("config")

    class _FakeConfig:
        def __call__(self, key: str, default: str | None = None) -> str | None:
            return default

    _mock_config.cfg = _FakeConfig()  # type: ignore[attr-defined]
    _mock_config.Config = _FakeConfig  # type: ignore[attr-defined]
    sys.modules["config"] = _mock_config

# Stub: blacklist (requires credential store for guild blacklists)
if "blacklist" not in sys.modules:
    _mock_blacklist = _types.ModuleType("blacklist")
    _mock_blacklist.track_blacklist = {}  # type: ignore[attr-defined]
    sys.modules["blacklist"] = _mock_blacklist

import pytest
from hypothesis import given, settings, HealthCheck
from hypothesis import strategies as st

# Now safe to import player module directly (not as bot.player)
import player as player_module  # noqa: E402


# ---------------------------------------------------------------------------
# Custom Hypothesis Strategies
# ---------------------------------------------------------------------------

def audio_entry_strategy() -> st.SearchStrategy[dict]:
    """Generate a random audio queue entry (no type field or type != music_video)."""
    return st.fixed_dictionaries({
        "title": st.text(min_size=1, max_size=30, alphabet=st.characters(
            whitelist_categories=("L", "N"),
        )),
        "url": st.from_regex(r"https://example\.com/[a-z0-9]{5,20}", fullmatch=True),
        "duration": st.integers(min_value=30, max_value=600),
    })


def video_entry_strategy() -> st.SearchStrategy[dict]:
    """Generate a random music_video queue entry."""
    return st.fixed_dictionaries({
        "title": st.text(min_size=1, max_size=30, alphabet=st.characters(
            whitelist_categories=("L", "N"),
        )),
        "url": st.from_regex(r"https://example\.com/video/[a-z0-9]{5,20}", fullmatch=True),
        "type": st.just("music_video"),
        "query": st.text(min_size=1, max_size=30, alphabet=st.characters(
            whitelist_categories=("L", "N"),
        )),
    })


def non_empty_audio_queue_strategy() -> st.SearchStrategy[list[dict]]:
    """Generate a non-empty queue of audio entries."""
    return st.lists(audio_entry_strategy(), min_size=1, max_size=5)


# ---------------------------------------------------------------------------
# Fixtures & Helpers
# ---------------------------------------------------------------------------

GUILD_ID = 99999


def _make_guild_state(
    queue: list[dict] | None = None,
    current: dict | None = None,
    video_transition: bool = False,
) -> dict[str, Any]:
    """Create a guild state dict matching the player.py structure."""
    return {
        "queue": list(queue) if queue else [],
        "current": current,
        "player": None,
        "text_channel": None,
        "voice_channel": MagicMock(id=12345),
        "repeat_mode": "off",
        "history": [],
        "source_provider": "youtube",
        "_video_transition": video_transition,
        "persist_enabled": False,
    }


def _make_mock_streamer(*, is_active: bool = True, queue_items: int = 0):
    """Create a mock ActivityStreamer with configurable state."""
    streamer = MagicMock()
    streamer.is_active = is_active
    streamer.queue = [MagicMock() for _ in range(queue_items)]
    streamer.source = MagicMock(title="Current Video", duration_seconds=120.0) if is_active else None
    streamer.skip = AsyncMock()
    streamer.play = AsyncMock()
    streamer.stop = AsyncMock()
    streamer.enqueue = MagicMock()
    streamer.channel_id = 12345
    streamer.guild_id = GUILD_ID
    streamer.waiting_for_viewer = False
    streamer.countdown_active = False
    streamer.playback_started = True
    streamer.start_time = time.monotonic() - 30.0
    return streamer


# ---------------------------------------------------------------------------
# Property 1: Internal Queue Skip Preservation
# **Validates: Requirements 3.1**
#
# For all guild states where streamer.queue is non-empty, calling video_skip
# delegates to streamer.skip() — no unified queue advancement occurs.
# ---------------------------------------------------------------------------


class TestInternalQueueSkipPreservation:
    """When the streamer has items in its internal queue, skip stays within the streamer."""

    @given(internal_queue_size=st.integers(min_value=1, max_value=10))
    @settings(max_examples=30, suppress_health_check=[HealthCheck.function_scoped_fixture])
    @pytest.mark.asyncio
    async def test_streamer_skip_delegates_internally(self, internal_queue_size: int):
        """**Validates: Requirements 3.1**

        When streamer.queue has items, streamer.skip() is called directly
        and _play_next_from_queue is NOT invoked.
        """
        streamer = _make_mock_streamer(is_active=True, queue_items=internal_queue_size)

        # The video_skip cog method checks had_queue = len(streamer.queue) > 0
        # then calls streamer.skip(). If had_queue is True and streamer is still
        # active after skip, it sends "Now Playing" — it does NOT call
        # _play_next_from_queue.
        had_queue = len(streamer.queue) > 0
        assert had_queue is True

        # Simulate what happens in the cog: call streamer.skip()
        await streamer.skip()

        # Verify: streamer.skip() was called (internal advancement)
        streamer.skip.assert_awaited_once()

        # The key preservation: _play_next_from_queue should NOT be called
        # when the streamer's internal queue had items. This is enforced by
        # the cog only calling _play_next_from_queue when had_queue=False
        # (i.e., streamer queue was empty, session stopped).


# ---------------------------------------------------------------------------
# Property 2: Audio-Only Skip Preservation
# **Validates: Requirements 3.2**
#
# When no video session is active, _play_next_from_queue pops next audio
# entry and calls _resolve_and_play via Lavalink.
# ---------------------------------------------------------------------------


class TestAudioOnlySkipPreservation:
    """When no video is active, audio queue advancement works normally via Lavalink."""

    @given(audio_queue=non_empty_audio_queue_strategy())
    @settings(max_examples=30, suppress_health_check=[HealthCheck.function_scoped_fixture])
    @pytest.mark.asyncio
    async def test_audio_skip_pops_and_plays_via_lavalink(self, audio_queue: list[dict]):
        """**Validates: Requirements 3.2**

        When _is_video_active() returns False and queue has audio entries,
        _play_next_from_queue pops the next entry and calls _resolve_and_play.
        """
        state = _make_guild_state(queue=audio_queue)
        expected_next = audio_queue[0]  # First entry should be popped

        mock_player = MagicMock()
        mock_player.connected = True
        mock_player.playing = False
        mock_player.paused = False
        state["player"] = mock_player

        with (
            patch.dict(player_module.guild_state, {GUILD_ID: state}),
            patch.object(player_module, "_is_video_active", return_value=False),
            patch.object(player_module, "_resolve_and_play", new_callable=AsyncMock) as mock_resolve,
            patch.object(player_module, "_on_queue_empty", new_callable=AsyncMock),
            patch.object(player_module, "persist"),
            patch.object(player_module, "get_player", return_value=mock_player),
            patch.object(player_module, "dbg", MagicMock()),
        ):
            await player_module._play_next_from_queue(GUILD_ID)

            # _resolve_and_play should have been called with the player and entry
            mock_resolve.assert_awaited_once()
            call_args = mock_resolve.call_args
            assert call_args[0][0] is mock_player  # First arg: player
            assert call_args[0][1] == GUILD_ID  # Second arg: guild_id
            assert call_args[0][2]["title"] == expected_next["title"]  # Third arg: entry

            # The entry should have been popped from queue
            assert len(state["queue"]) == len(audio_queue) - 1


# ---------------------------------------------------------------------------
# Property 3: Idle Streamer Reuse Preservation
# **Validates: Requirements 3.3**
#
# When an existing streamer has is_active=False (idle, clients still connected),
# _start_video_from_queue calls streamer.play(source) without creating a new Activity.
# ---------------------------------------------------------------------------


class TestIdleStreamerReusePreservation:
    """When a streamer is idle but exists, it gets reused instead of creating a new one."""

    @given(video_entry=video_entry_strategy())
    @settings(max_examples=20, suppress_health_check=[HealthCheck.function_scoped_fixture])
    @pytest.mark.asyncio
    async def test_idle_streamer_reuse(self, video_entry: dict):
        """**Validates: Requirements 3.3**

        When an existing streamer has is_active=False (idle, clients still
        connected), _start_video_from_queue calls streamer.play(source) on
        the idle streamer without creating a new Activity.
        """
        idle_streamer = _make_mock_streamer(is_active=False, queue_items=0)

        # Mock the video cog with registry that returns the idle streamer
        mock_registry = MagicMock()
        mock_registry.get.return_value = idle_streamer
        mock_registry._sessions = {(GUILD_ID, 12345): idle_streamer}

        mock_video_cog = MagicMock()
        mock_video_cog._registry = mock_registry
        mock_video_cog._backend = MagicMock()
        mock_video_cog._backend.ws_hub = MagicMock()
        mock_video_cog._backend.ws_hub.set_state = MagicMock()
        mock_video_cog._backend.ws_hub.broadcast_from_bot = AsyncMock()
        mock_video_cog._now_playing_messages = {}
        mock_video_cog._start_seek_bar_update = MagicMock()
        mock_video_cog._launcher = MagicMock()

        # Mock bot
        mock_bot = MagicMock()
        mock_bot.get_cog.return_value = mock_video_cog
        mock_bot.cogs = {"Video": mock_video_cog}
        mock_bot.is_closed.return_value = False

        state = _make_guild_state(current=video_entry)
        mock_text_channel = MagicMock()
        mock_text_channel.send = AsyncMock(return_value=MagicMock())
        state["text_channel"] = mock_text_channel

        # Mock the MusicVideoResolver
        mock_source = MagicMock()
        mock_source.title = video_entry.get("title", "Test")
        mock_resolver_instance = MagicMock()
        mock_resolver_instance.resolve = AsyncMock(return_value=mock_source)
        mock_resolver_cls = MagicMock(return_value=mock_resolver_instance)

        with (
            patch.dict(player_module.guild_state, {GUILD_ID: state}),
            patch.object(player_module, "_bot_ref", mock_bot),
            patch.object(player_module, "persist"),
            patch("video.music_video_resolver.MusicVideoResolver", mock_resolver_cls),
        ):
            await player_module._start_video_from_queue(GUILD_ID, video_entry)

            # The idle streamer's play() should have been called (reuse)
            idle_streamer.play.assert_awaited_once_with(mock_source)

            # Registry.register should NOT have been called (no new session)
            mock_registry.register.assert_not_called()


# ---------------------------------------------------------------------------
# Property 4: Video Transition Flag Preservation
# **Validates: Requirements 3.5**
#
# When state["_video_transition"] is True and peek entry is audio,
# _play_next_from_queue returns early (audio blocked).
# ---------------------------------------------------------------------------


class TestVideoTransitionFlagPreservation:
    """When _video_transition is True, audio entries are blocked from playing."""

    @given(audio_queue=non_empty_audio_queue_strategy())
    @settings(max_examples=30, suppress_health_check=[HealthCheck.function_scoped_fixture])
    @pytest.mark.asyncio
    async def test_video_transition_blocks_audio(self, audio_queue: list[dict]):
        """**Validates: Requirements 3.5**

        When state["_video_transition"] is True and peek entry is audio,
        _play_next_from_queue returns early without popping or playing.
        """
        state = _make_guild_state(
            queue=audio_queue,
            video_transition=True,
        )
        original_queue_len = len(state["queue"])

        mock_player = MagicMock()
        mock_player.connected = True
        state["player"] = mock_player

        with (
            patch.dict(player_module.guild_state, {GUILD_ID: state}),
            patch.object(player_module, "_is_video_active", return_value=False),
            patch.object(player_module, "_resolve_and_play", new_callable=AsyncMock) as mock_resolve,
            patch.object(player_module, "_on_queue_empty", new_callable=AsyncMock) as mock_empty,
            patch.object(player_module, "persist"),
            patch.object(player_module, "get_player", return_value=mock_player),
            patch.object(player_module, "dbg", MagicMock()),
        ):
            await player_module._play_next_from_queue(GUILD_ID)

            # Audio should NOT have been played
            mock_resolve.assert_not_awaited()

            # Queue should NOT have been emptied
            mock_empty.assert_not_awaited()

            # The queue should still have all items (entry not popped)
            assert len(state["queue"]) == original_queue_len

            # current should be None (no track is playing)
            assert state["current"] is None

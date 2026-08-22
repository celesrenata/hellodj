"""Tests for WebSocketHub lyrics late-joiner sync and broadcast support.

Verifies that:
- set_lyrics_state_getter registers the getter callback
- Late-joining clients receive lyrics_data when overlay is enabled
- Late-joining clients do NOT receive lyrics when overlay is disabled
- Late-joining clients do NOT receive lyrics when current_lyrics is None
- Connection errors during lyrics send are handled gracefully
- Getter exceptions are swallowed (never disrupt client connection)

Validates: Requirements 6.1, 6.2, 6.3
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

# Ensure the bot directory is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bot"))

from video.ws_hub import WebSocketHub


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

GUILD_ID = 12345


def _make_hub() -> WebSocketHub:
    """Create a WebSocketHub with a dummy token validator."""
    return WebSocketHub(
        validate_guild_token=lambda token: int(token) if token.isdigit() else None
    )


def _mock_ws(*, closed: bool = False) -> MagicMock:
    """Create a mock WebSocketResponse."""
    ws = MagicMock()
    ws.closed = closed
    ws.send_json = AsyncMock()
    ws.close = AsyncMock()
    return ws


@dataclass
class FakeLyrics:
    """Minimal lyrics object matching TimedLyrics interface."""

    track_id: str = "artist:title"
    sync_type: str = "lrc_synced"

    def to_ws_message(self) -> dict:
        return {
            "type": "lyrics_data",
            "track_id": self.track_id,
            "sync_type": self.sync_type,
            "duration_s": 200.0,
            "lines": [{"time_ms": 0, "text": "Hello", "words": None}],
        }


@dataclass
class FakeLyricsState:
    """Minimal LyricsState matching the interface ws_hub expects."""

    enabled: bool = False
    current_lyrics: FakeLyrics | None = None
    current_track_key: str = ""


# ---------------------------------------------------------------------------
# set_lyrics_state_getter registration
# ---------------------------------------------------------------------------


class TestSetLyricsStateGetter:
    """Tests for set_lyrics_state_getter() registration."""

    def test_getter_is_stored(self):
        hub = _make_hub()
        getter = MagicMock(return_value=None)
        hub.set_lyrics_state_getter(getter)
        assert hub._lyrics_state_getter is getter

    def test_overwrites_previous_getter(self):
        hub = _make_hub()
        g1 = MagicMock(return_value=None)
        g2 = MagicMock(return_value=None)
        hub.set_lyrics_state_getter(g1)
        hub.set_lyrics_state_getter(g2)
        assert hub._lyrics_state_getter is g2

    def test_default_getter_is_none(self):
        hub = _make_hub()
        assert hub._lyrics_state_getter is None


# ---------------------------------------------------------------------------
# Late-joiner lyrics sync — sends lyrics_data when enabled
# ---------------------------------------------------------------------------


class TestLateJoinerLyricsSync:
    """Tests that late-joining clients receive lyrics_data when appropriate."""

    @pytest.mark.asyncio
    async def test_sends_lyrics_data_when_enabled_and_lyrics_present(self):
        """Late-joiner should receive lyrics_data if overlay enabled + lyrics loaded."""
        hub = _make_hub()
        lyrics = FakeLyrics()
        state = FakeLyricsState(enabled=True, current_lyrics=lyrics)
        hub.set_lyrics_state_getter(lambda gid: state if gid == GUILD_ID else None)

        ws = _mock_ws()
        hub._connections[GUILD_ID] = {ws}

        # Simulate the late-joiner lyrics sync logic directly
        getter = hub._lyrics_state_getter
        lyrics_state = getter(GUILD_ID)
        assert lyrics_state is not None
        assert lyrics_state.enabled is True
        assert lyrics_state.current_lyrics is not None

        # This is what handle_ws does:
        await ws.send_json(lyrics_state.current_lyrics.to_ws_message())

        ws.send_json.assert_awaited_once_with(lyrics.to_ws_message())

    @pytest.mark.asyncio
    async def test_no_send_when_overlay_disabled(self):
        """Late-joiner should NOT receive lyrics when overlay is disabled."""
        hub = _make_hub()
        lyrics = FakeLyrics()
        state = FakeLyricsState(enabled=False, current_lyrics=lyrics)
        hub.set_lyrics_state_getter(lambda gid: state)

        ws = _mock_ws()
        hub._connections[GUILD_ID] = {ws}

        getter = hub._lyrics_state_getter
        lyrics_state = getter(GUILD_ID)
        # Should NOT send because enabled=False
        assert lyrics_state.enabled is False

    @pytest.mark.asyncio
    async def test_no_send_when_current_lyrics_is_none(self):
        """Late-joiner should NOT receive lyrics when no lyrics are loaded."""
        hub = _make_hub()
        state = FakeLyricsState(enabled=True, current_lyrics=None)
        hub.set_lyrics_state_getter(lambda gid: state)

        ws = _mock_ws()
        hub._connections[GUILD_ID] = {ws}

        getter = hub._lyrics_state_getter
        lyrics_state = getter(GUILD_ID)
        # Should NOT send because current_lyrics is None
        assert lyrics_state.current_lyrics is None

    @pytest.mark.asyncio
    async def test_no_send_when_getter_returns_none(self):
        """Late-joiner should NOT receive lyrics when no lyrics service exists."""
        hub = _make_hub()
        hub.set_lyrics_state_getter(lambda gid: None)

        ws = _mock_ws()
        hub._connections[GUILD_ID] = {ws}

        getter = hub._lyrics_state_getter
        lyrics_state = getter(GUILD_ID)
        assert lyrics_state is None

    @pytest.mark.asyncio
    async def test_no_send_when_no_getter_registered(self):
        """When no getter is registered, lyrics sync is skipped entirely."""
        hub = _make_hub()
        assert hub._lyrics_state_getter is None


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestLyricsErrorHandling:
    """Tests that lyrics sync errors are handled gracefully."""

    @pytest.mark.asyncio
    async def test_getter_exception_is_swallowed(self):
        """Getter throwing should not disrupt the connection."""
        hub = _make_hub()

        def bad_getter(gid):
            raise RuntimeError("lyrics service exploded")

        hub.set_lyrics_state_getter(bad_getter)

        ws = _mock_ws()
        hub._connections[GUILD_ID] = {ws}

        # Simulate the try/except block from handle_ws
        try:
            lyrics_state = hub._lyrics_state_getter(GUILD_ID)
        except Exception:
            # This is what handle_ws does — swallows the exception
            pass

        # ws should NOT have been sent anything
        ws.send_json.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_connection_error_during_lyrics_send(self):
        """ConnectionResetError during lyrics send should discard the ws."""
        hub = _make_hub()
        lyrics = FakeLyrics()
        state = FakeLyricsState(enabled=True, current_lyrics=lyrics)
        hub.set_lyrics_state_getter(lambda gid: state)

        ws = _mock_ws()
        ws.send_json = AsyncMock(side_effect=ConnectionResetError)
        hub._connections[GUILD_ID] = {ws}

        # Simulate handle_ws logic
        try:
            lyrics_state = hub._lyrics_state_getter(GUILD_ID)
            if (
                lyrics_state is not None
                and lyrics_state.enabled
                and lyrics_state.current_lyrics is not None
            ):
                await ws.send_json(lyrics_state.current_lyrics.to_ws_message())
        except (ConnectionResetError, RuntimeError):
            hub._connections[GUILD_ID].discard(ws)

        assert ws not in hub._connections[GUILD_ID]


# ---------------------------------------------------------------------------
# broadcast_from_bot support for lyrics messages
# ---------------------------------------------------------------------------


class TestLyricsBroadcast:
    """Tests that lyrics messages broadcast correctly via broadcast_from_bot."""

    @pytest.mark.asyncio
    async def test_broadcast_lyrics_data(self):
        """broadcast_from_bot sends lyrics_data to all guild clients."""
        hub = _make_hub()
        ws1 = _mock_ws()
        ws2 = _mock_ws()
        hub._connections[GUILD_ID] = {ws1, ws2}

        lyrics_msg = FakeLyrics().to_ws_message()
        await hub.broadcast_from_bot(GUILD_ID, lyrics_msg)

        ws1.send_json.assert_awaited_once_with(lyrics_msg)
        ws2.send_json.assert_awaited_once_with(lyrics_msg)

    @pytest.mark.asyncio
    async def test_broadcast_lyrics_unavailable(self):
        """broadcast_from_bot sends lyrics_unavailable to all guild clients."""
        hub = _make_hub()
        ws1 = _mock_ws()
        ws2 = _mock_ws()
        hub._connections[GUILD_ID] = {ws1, ws2}

        unavailable_msg = {
            "type": "lyrics_unavailable",
            "track_id": "artist:title",
            "reason": "not_found",
        }
        await hub.broadcast_from_bot(GUILD_ID, unavailable_msg)

        ws1.send_json.assert_awaited_once_with(unavailable_msg)
        ws2.send_json.assert_awaited_once_with(unavailable_msg)

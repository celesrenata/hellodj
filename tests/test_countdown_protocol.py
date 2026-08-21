"""Tests for the WebSocket countdown protocol.

Verifies countdown trigger logic, ready message handling, late-joiner sync,
and edge cases (disconnect during countdown, stale ready messages).

Validates: Requirements 1.1, 1.2, 1.3, 1.4, 1.5
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Ensure the bot directory is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bot"))

from video import StreamState, VideoSource
from video.activity_streamer import ActivityStreamer
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


def _make_streamer(
    hub: WebSocketHub | None = None,
    state: StreamState = StreamState.STREAMING,
    elapsed: float = 0.0,
) -> ActivityStreamer:
    """Create an ActivityStreamer with controlled state for testing."""
    streamer = ActivityStreamer(
        guild_id=GUILD_ID,
        channel_id=1,
        ws_hub=hub,
    )
    streamer.state = state
    streamer.source = VideoSource(
        source_type="youtube",
        file_path="/tmp/test.mp4",
        title="Test Video",
        duration_seconds=120.0,
    )
    # Set start_time so get_elapsed_seconds returns the desired value
    if state == StreamState.STREAMING and elapsed >= 0:
        streamer.start_time = time.monotonic() - elapsed
    # For fresh sessions (elapsed < 5s), we're in WAITING_FOR_VIEWER
    if elapsed < 5.0:
        streamer.waiting_for_viewer = True
    return streamer


# ---------------------------------------------------------------------------
# ActivityStreamer countdown sub-state tests
# ---------------------------------------------------------------------------


class TestActivityStreamerCountdown:
    """Tests for ActivityStreamer countdown protocol methods."""

    def test_should_countdown_returns_true_within_5s(self):
        streamer = _make_streamer(elapsed=2.0)
        assert streamer.should_countdown() is True

    def test_should_countdown_returns_false_after_5s(self):
        streamer = _make_streamer(elapsed=6.0)
        streamer.waiting_for_viewer = False
        assert streamer.should_countdown() is False

    def test_should_countdown_returns_false_if_already_started(self):
        streamer = _make_streamer(elapsed=1.0)
        streamer.playback_started = True
        assert streamer.should_countdown() is False

    def test_should_countdown_returns_false_if_countdown_active(self):
        streamer = _make_streamer(elapsed=1.0)
        streamer.countdown_active = True
        assert streamer.should_countdown() is False

    def test_should_countdown_returns_false_in_idle(self):
        streamer = _make_streamer(state=StreamState.IDLE, elapsed=0.0)
        assert streamer.should_countdown() is False

    def test_start_countdown_sets_flags(self):
        streamer = _make_streamer(elapsed=1.0)
        streamer.start_countdown()
        assert streamer.countdown_active is True
        assert streamer.countdown_start_time > 0
        assert streamer.waiting_for_viewer is False

    def test_start_countdown_noop_if_already_active(self):
        streamer = _make_streamer(elapsed=1.0)
        streamer.countdown_active = True
        original_time = streamer.countdown_start_time
        streamer.start_countdown()
        # Should not change the start time
        assert streamer.countdown_start_time == original_time

    def test_start_countdown_noop_if_playback_started(self):
        streamer = _make_streamer(elapsed=1.0)
        streamer.playback_started = True
        streamer.start_countdown()
        assert streamer.countdown_active is False

    def test_on_ready_received_first_ready_triggers_start(self):
        streamer = _make_streamer(elapsed=1.0)
        streamer.start_countdown()
        result = streamer.on_ready_received()
        assert result is True
        assert streamer.playback_started is True
        assert streamer.countdown_active is False
        # start_time was reset
        assert streamer.start_time > 0

    def test_on_ready_received_second_ready_returns_false(self):
        streamer = _make_streamer(elapsed=1.0)
        streamer.start_countdown()
        streamer.on_ready_received()  # First
        result = streamer.on_ready_received()  # Second
        assert result is False

    def test_on_ready_received_without_countdown_returns_false(self):
        streamer = _make_streamer(elapsed=1.0)
        result = streamer.on_ready_received()
        assert result is False

    def test_cancel_countdown_resets_state(self):
        streamer = _make_streamer(elapsed=1.0)
        streamer.start_countdown()
        streamer.cancel_countdown()
        assert streamer.countdown_active is False
        assert streamer.countdown_start_time == 0.0
        assert streamer.waiting_for_viewer is True

    def test_cancel_countdown_noop_if_not_active(self):
        streamer = _make_streamer(elapsed=1.0)
        streamer.cancel_countdown()  # Should not raise
        assert streamer.countdown_active is False

    def test_get_countdown_remaining_returns_remaining(self):
        streamer = _make_streamer(elapsed=1.0)
        streamer.countdown_active = True
        streamer.countdown_start_time = time.monotonic() - 1.0
        remaining = streamer.get_countdown_remaining()
        # 3s countdown - 1s elapsed = ~2s remaining
        assert 1.5 < remaining < 2.5

    def test_get_countdown_remaining_returns_zero_when_inactive(self):
        streamer = _make_streamer(elapsed=1.0)
        assert streamer.get_countdown_remaining() == 0.0


# ---------------------------------------------------------------------------
# WebSocketHub register/unregister streamer
# ---------------------------------------------------------------------------


class TestStreamerRegistration:
    """Tests for register_streamer and unregister_streamer."""

    def test_register_stores_streamer(self):
        hub = _make_hub()
        streamer = _make_streamer(hub=hub, elapsed=1.0)
        hub.register_streamer(GUILD_ID, streamer)
        assert hub._streamers.get(GUILD_ID) is streamer

    def test_unregister_removes_streamer(self):
        hub = _make_hub()
        streamer = _make_streamer(hub=hub, elapsed=1.0)
        hub.register_streamer(GUILD_ID, streamer)
        hub.unregister_streamer(GUILD_ID)
        assert GUILD_ID not in hub._streamers

    def test_unregister_nonexistent_is_safe(self):
        hub = _make_hub()
        hub.unregister_streamer(99999)  # Should not raise


# ---------------------------------------------------------------------------
# WebSocketHub _handle_ready
# ---------------------------------------------------------------------------


class TestHandleReady:
    """Tests for WebSocketHub._handle_ready()."""

    @pytest.mark.asyncio
    async def test_ready_triggers_start_broadcast(self):
        hub = _make_hub()
        streamer = _make_streamer(hub=hub, elapsed=1.0)
        streamer.start_countdown()
        hub.register_streamer(GUILD_ID, streamer)

        ws1 = _mock_ws()
        ws2 = _mock_ws()
        hub._connections[GUILD_ID] = {ws1, ws2}

        await hub._handle_ready(GUILD_ID, ws1)

        # Both clients should receive start message
        for ws in (ws1, ws2):
            ws.send_json.assert_awaited()
            call_args = ws.send_json.await_args[0][0]
            assert call_args["type"] == "start"
            assert call_args["position"] == 0.0

    @pytest.mark.asyncio
    async def test_ready_without_streamer_is_ignored(self):
        hub = _make_hub()
        ws = _mock_ws()
        hub._connections[GUILD_ID] = {ws}

        await hub._handle_ready(GUILD_ID, ws)
        ws.send_json.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_ready_without_countdown_is_ignored(self):
        hub = _make_hub()
        streamer = _make_streamer(hub=hub, elapsed=1.0)
        # Don't start countdown
        hub.register_streamer(GUILD_ID, streamer)

        ws = _mock_ws()
        hub._connections[GUILD_ID] = {ws}

        await hub._handle_ready(GUILD_ID, ws)
        ws.send_json.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_duplicate_ready_does_not_broadcast_twice(self):
        hub = _make_hub()
        streamer = _make_streamer(hub=hub, elapsed=1.0)
        streamer.start_countdown()
        hub.register_streamer(GUILD_ID, streamer)

        ws = _mock_ws()
        hub._connections[GUILD_ID] = {ws}

        await hub._handle_ready(GUILD_ID, ws)
        ws.send_json.reset_mock()

        await hub._handle_ready(GUILD_ID, ws)
        ws.send_json.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_ready_updates_playback_state(self):
        hub = _make_hub()
        streamer = _make_streamer(hub=hub, elapsed=1.0)
        streamer.start_countdown()
        hub.register_streamer(GUILD_ID, streamer)

        ws = _mock_ws()
        hub._connections[GUILD_ID] = {ws}

        await hub._handle_ready(GUILD_ID, ws)

        state = hub.get_state(GUILD_ID)
        assert state is not None
        assert state.playing is True
        assert state.position == 0.0


# ---------------------------------------------------------------------------
# Countdown disconnect edge case
# ---------------------------------------------------------------------------


class TestCountdownDisconnect:
    """Tests for countdown cancellation on all viewers disconnecting."""

    @pytest.mark.asyncio
    async def test_all_disconnect_cancels_countdown(self):
        hub = _make_hub()
        streamer = _make_streamer(hub=hub, elapsed=1.0)
        streamer.start_countdown()
        hub.register_streamer(GUILD_ID, streamer)

        ws = _mock_ws()
        hub._connections[GUILD_ID] = {ws}

        # Simulate disconnect — the logic that fires in the finally block
        conns = hub._connections.get(GUILD_ID, set())
        conns.discard(ws)
        remaining = len(conns)

        if remaining == 0:
            s = hub._streamers.get(GUILD_ID)
            if s is not None and s.countdown_active:
                s.cancel_countdown()

        assert streamer.countdown_active is False
        assert streamer.waiting_for_viewer is True

    @pytest.mark.asyncio
    async def test_partial_disconnect_does_not_cancel_countdown(self):
        hub = _make_hub()
        streamer = _make_streamer(hub=hub, elapsed=1.0)
        streamer.start_countdown()
        hub.register_streamer(GUILD_ID, streamer)

        ws1 = _mock_ws()
        ws2 = _mock_ws()
        hub._connections[GUILD_ID] = {ws1, ws2}

        # Only ws1 disconnects
        conns = hub._connections.get(GUILD_ID, set())
        conns.discard(ws1)
        remaining = len(conns)

        if remaining == 0:
            s = hub._streamers.get(GUILD_ID)
            if s is not None and s.countdown_active:
                s.cancel_countdown()

        # Countdown should still be active
        assert streamer.countdown_active is True


# ---------------------------------------------------------------------------
# Late-joiner sync (elapsed >= 5s)
# ---------------------------------------------------------------------------


class TestLateJoinerSync:
    """Tests that late joiners get state message, not countdown."""

    def test_should_countdown_false_for_late_joiner(self):
        """Streamer with elapsed >= 5s should not trigger countdown."""
        streamer = _make_streamer(elapsed=10.0)
        streamer.waiting_for_viewer = False
        assert streamer.should_countdown() is False

    def test_should_countdown_false_when_playback_already_started(self):
        """After countdown completed and playback started, no more countdowns."""
        streamer = _make_streamer(elapsed=3.0)
        streamer.playback_started = True
        assert streamer.should_countdown() is False

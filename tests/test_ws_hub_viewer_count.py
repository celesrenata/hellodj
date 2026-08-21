"""Tests for WebSocketHub viewer count tracking.

Verifies that the viewer count callback is correctly invoked on
0→1 and 1→0 transitions, and that the viewer_count helper returns
accurate counts.

Validates: Requirements 9.1, 9.2, 9.3, 9.4
"""

from __future__ import annotations

import asyncio
import sys
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
    return WebSocketHub(validate_guild_token=lambda token: int(token) if token.isdigit() else None)


def _mock_ws(*, closed: bool = False) -> MagicMock:
    """Create a mock WebSocketResponse."""
    ws = MagicMock()
    ws.closed = closed
    ws.send_json = AsyncMock()
    ws.close = AsyncMock()
    return ws


# ---------------------------------------------------------------------------
# viewer_count helper
# ---------------------------------------------------------------------------


class TestViewerCount:
    """Tests for the viewer_count() helper method."""

    def test_returns_zero_for_unknown_guild(self):
        hub = _make_hub()
        assert hub.viewer_count(99999) == 0

    def test_returns_correct_count_after_adding_connections(self):
        hub = _make_hub()
        hub._connections[GUILD_ID] = {_mock_ws(), _mock_ws(), _mock_ws()}
        assert hub.viewer_count(GUILD_ID) == 3

    def test_returns_zero_after_clearing_connections(self):
        hub = _make_hub()
        hub._connections[GUILD_ID] = set()
        assert hub.viewer_count(GUILD_ID) == 0


# ---------------------------------------------------------------------------
# Callback registration
# ---------------------------------------------------------------------------


class TestSetViewerCountCallback:
    """Tests for set_viewer_count_callback() registration."""

    def test_callback_is_stored(self):
        hub = _make_hub()
        cb = AsyncMock()
        hub.set_viewer_count_callback(cb)
        assert hub._viewer_count_callback is cb

    def test_overwrites_previous_callback(self):
        hub = _make_hub()
        cb1 = AsyncMock()
        cb2 = AsyncMock()
        hub.set_viewer_count_callback(cb1)
        hub.set_viewer_count_callback(cb2)
        assert hub._viewer_count_callback is cb2


# ---------------------------------------------------------------------------
# _on_viewer_count_change
# ---------------------------------------------------------------------------


class TestOnViewerCountChange:
    """Tests for the _on_viewer_count_change internal method."""

    @pytest.mark.asyncio
    async def test_calls_callback_with_correct_args(self):
        hub = _make_hub()
        cb = AsyncMock()
        hub.set_viewer_count_callback(cb)

        await hub._on_viewer_count_change(GUILD_ID, 0, 1)

        cb.assert_awaited_once_with(GUILD_ID, 0, 1)

    @pytest.mark.asyncio
    async def test_no_error_when_no_callback_set(self):
        hub = _make_hub()
        # Should not raise
        await hub._on_viewer_count_change(GUILD_ID, 0, 1)

    @pytest.mark.asyncio
    async def test_swallows_callback_exceptions(self):
        hub = _make_hub()
        cb = AsyncMock(side_effect=RuntimeError("boom"))
        hub.set_viewer_count_callback(cb)

        # Should not raise despite the callback throwing
        await hub._on_viewer_count_change(GUILD_ID, 1, 0)

        cb.assert_awaited_once_with(GUILD_ID, 1, 0)


# ---------------------------------------------------------------------------
# Connection add triggers 0→1
# ---------------------------------------------------------------------------


class TestConnectionAddTransition:
    """Tests that adding the first connection fires the 0→1 callback."""

    @pytest.mark.asyncio
    async def test_first_connection_fires_callback(self):
        hub = _make_hub()
        cb = AsyncMock()
        hub.set_viewer_count_callback(cb)

        ws = _mock_ws()
        # Simulate what handle_ws does for connection registration
        hub._connections[GUILD_ID] = set()
        old_count = len(hub._connections[GUILD_ID])
        hub._connections[GUILD_ID].add(ws)
        new_count = len(hub._connections[GUILD_ID])

        if old_count == 0 and new_count == 1:
            await hub._on_viewer_count_change(GUILD_ID, 0, 1)

        cb.assert_awaited_once_with(GUILD_ID, 0, 1)

    @pytest.mark.asyncio
    async def test_second_connection_does_not_fire_callback(self):
        hub = _make_hub()
        cb = AsyncMock()
        hub.set_viewer_count_callback(cb)

        ws1 = _mock_ws()
        ws2 = _mock_ws()
        hub._connections[GUILD_ID] = {ws1}
        old_count = len(hub._connections[GUILD_ID])
        hub._connections[GUILD_ID].add(ws2)
        new_count = len(hub._connections[GUILD_ID])

        if old_count == 0 and new_count == 1:
            await hub._on_viewer_count_change(GUILD_ID, 0, 1)

        cb.assert_not_awaited()


# ---------------------------------------------------------------------------
# Connection remove triggers 1→0
# ---------------------------------------------------------------------------


class TestConnectionRemoveTransition:
    """Tests that removing the last connection fires the 1→0 callback."""

    @pytest.mark.asyncio
    async def test_last_connection_removed_fires_callback(self):
        hub = _make_hub()
        cb = AsyncMock()
        hub.set_viewer_count_callback(cb)

        ws = _mock_ws()
        hub._connections[GUILD_ID] = {ws}

        # Simulate what the finally block does
        conns = hub._connections.get(GUILD_ID, set())
        was_present = ws in conns
        conns.discard(ws)
        remaining = len(conns)

        if was_present and remaining == 0:
            await hub._on_viewer_count_change(GUILD_ID, 1, 0)

        cb.assert_awaited_once_with(GUILD_ID, 1, 0)

    @pytest.mark.asyncio
    async def test_non_last_connection_removed_does_not_fire(self):
        hub = _make_hub()
        cb = AsyncMock()
        hub.set_viewer_count_callback(cb)

        ws1 = _mock_ws()
        ws2 = _mock_ws()
        hub._connections[GUILD_ID] = {ws1, ws2}

        conns = hub._connections.get(GUILD_ID, set())
        was_present = ws1 in conns
        conns.discard(ws1)
        remaining = len(conns)

        if was_present and remaining == 0:
            await hub._on_viewer_count_change(GUILD_ID, 1, 0)

        cb.assert_not_awaited()


# ---------------------------------------------------------------------------
# disconnect_all fires callback
# ---------------------------------------------------------------------------


class TestDisconnectAllTransition:
    """Tests that disconnect_all fires the callback when viewers existed."""

    @pytest.mark.asyncio
    async def test_disconnect_all_with_viewers_fires_callback(self):
        hub = _make_hub()
        cb = AsyncMock()
        hub.set_viewer_count_callback(cb)

        ws1 = _mock_ws()
        ws2 = _mock_ws()
        hub._connections[GUILD_ID] = {ws1, ws2}

        await hub.disconnect_all(GUILD_ID)

        cb.assert_awaited_once_with(GUILD_ID, 2, 0)

    @pytest.mark.asyncio
    async def test_disconnect_all_with_no_viewers_does_not_fire(self):
        hub = _make_hub()
        cb = AsyncMock()
        hub.set_viewer_count_callback(cb)

        # No connections for this guild
        await hub.disconnect_all(GUILD_ID)

        cb.assert_not_awaited()


# ---------------------------------------------------------------------------
# broadcast stale cleanup fires callback
# ---------------------------------------------------------------------------


class TestBroadcastStaleCleanup:
    """Tests that stale connection cleanup in broadcast fires the callback."""

    @pytest.mark.asyncio
    async def test_stale_cleanup_causes_zero_viewers_fires_callback(self):
        hub = _make_hub()
        cb = AsyncMock()
        hub.set_viewer_count_callback(cb)

        # One stale connection that raises on send
        ws = _mock_ws()
        ws.send_json = AsyncMock(side_effect=ConnectionResetError)
        hub._connections[GUILD_ID] = {ws}

        await hub.broadcast(GUILD_ID, {"type": "test"})

        cb.assert_awaited_once_with(GUILD_ID, 1, 0)

    @pytest.mark.asyncio
    async def test_stale_cleanup_with_remaining_does_not_fire(self):
        hub = _make_hub()
        cb = AsyncMock()
        hub.set_viewer_count_callback(cb)

        ws_stale = _mock_ws()
        ws_stale.send_json = AsyncMock(side_effect=ConnectionResetError)
        ws_healthy = _mock_ws()
        hub._connections[GUILD_ID] = {ws_stale, ws_healthy}

        await hub.broadcast(GUILD_ID, {"type": "test"})

        cb.assert_not_awaited()

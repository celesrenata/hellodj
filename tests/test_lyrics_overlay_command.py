"""Tests for the /lyrics overlay command extension.

Validates the overlay parameter behavior in cogs/lyrics.py:
- overlay:on with playing track → enable, fetch, broadcast enable
- overlay:on with nothing playing → ephemeral "Nothing is playing right now."
- overlay:off → disable, broadcast disable
- no overlay → existing chat embed behavior preserved

Requirements: 5.1, 5.2, 5.3, 5.4, 5.5
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Add bot/ to sys.path so we can import cogs, player, video, etc.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bot"))

# Set env for credential store before importing anything that touches config
os.environ.setdefault("HELLODJ_DB_KEY", "test-key-for-lyrics-overlay-tests")


def _make_interaction(guild_id: int = 123):
    """Create a mock discord.Interaction."""
    interaction = MagicMock()
    interaction.guild = MagicMock()
    interaction.guild.id = guild_id
    interaction.response = MagicMock()
    interaction.response.send_message = AsyncMock()
    interaction.response.defer = AsyncMock()
    interaction.followup = MagicMock()
    interaction.followup.send = AsyncMock()
    return interaction


class TestLyricsOverlayOn:
    """Tests for /lyrics overlay:on."""

    @pytest.mark.asyncio
    async def test_overlay_on_nothing_playing(self):
        """overlay:on with no current track → ephemeral message."""
        with patch("player.get_state", return_value={"current": None}):
            from cogs.lyrics import Lyrics

            cog = Lyrics.__new__(Lyrics)
            cog.bot = MagicMock()
            cog._genius = MagicMock()

            interaction = _make_interaction()
            await cog.lyrics.callback(cog, interaction, overlay="on")

            interaction.response.send_message.assert_awaited_once_with(
                "Nothing is playing right now.", ephemeral=True
            )

    @pytest.mark.asyncio
    async def test_overlay_on_with_track_enables_and_broadcasts(self):
        """overlay:on with a playing track → enable service, fetch, broadcast."""
        mock_svc = MagicMock()
        mock_svc.enabled = False
        mock_svc.fetch_and_broadcast = AsyncMock()
        mock_svc._ws_hub = MagicMock()
        mock_svc._ws_hub.broadcast_from_bot = AsyncMock()

        with (
            patch("player.get_state", return_value={
                "current": {"title": "Test Song", "author": "Test Artist", "duration": 180000}
            }),
            patch("cogs.lyrics.get_lyrics_service", return_value=mock_svc),
        ):
            from cogs.lyrics import Lyrics

            cog = Lyrics.__new__(Lyrics)
            cog.bot = MagicMock()
            cog._genius = MagicMock()

            interaction = _make_interaction(guild_id=456)
            await cog.lyrics.callback(cog, interaction, overlay="on")

            # Service enabled
            assert mock_svc.enabled is True

            # fetch_and_broadcast called with track metadata
            mock_svc.fetch_and_broadcast.assert_awaited_once_with(
                "Test Artist", "Test Song", 180000
            )

            # Broadcast lyrics_overlay_enable
            mock_svc._ws_hub.broadcast_from_bot.assert_awaited_once_with(
                456, {"type": "lyrics_overlay_enable"}
            )

            # Ephemeral confirmation
            interaction.response.send_message.assert_awaited_once_with(
                "🎤 Lyrics overlay enabled for all viewers.", ephemeral=True
            )


class TestLyricsOverlayOff:
    """Tests for /lyrics overlay:off."""

    @pytest.mark.asyncio
    async def test_overlay_off_disables_and_broadcasts(self):
        """overlay:off → disable service, broadcast disable."""
        mock_svc = MagicMock()
        mock_svc.enabled = True
        mock_svc._ws_hub = MagicMock()
        mock_svc._ws_hub.broadcast_from_bot = AsyncMock()

        with (
            patch("player.get_state", return_value={"current": None}),
            patch("cogs.lyrics.get_lyrics_service", return_value=mock_svc),
        ):
            from cogs.lyrics import Lyrics

            cog = Lyrics.__new__(Lyrics)
            cog.bot = MagicMock()
            cog._genius = MagicMock()

            interaction = _make_interaction(guild_id=789)
            await cog.lyrics.callback(cog, interaction, overlay="off")

            # Service disabled
            assert mock_svc.enabled is False

            # Broadcast lyrics_overlay_disable
            mock_svc._ws_hub.broadcast_from_bot.assert_awaited_once_with(
                789, {"type": "lyrics_overlay_disable"}
            )

            # Ephemeral confirmation
            interaction.response.send_message.assert_awaited_once_with(
                "Lyrics overlay disabled.", ephemeral=True
            )


class TestLyricsNoOverlay:
    """Tests for /lyrics without overlay (existing behavior preserved)."""

    @pytest.mark.asyncio
    async def test_no_overlay_nothing_playing(self):
        """No overlay, nothing playing → non-ephemeral message."""
        with patch("player.get_state", return_value={"current": None}):
            from cogs.lyrics import Lyrics

            cog = Lyrics.__new__(Lyrics)
            cog.bot = MagicMock()
            cog._genius = MagicMock()

            interaction = _make_interaction()
            await cog.lyrics.callback(cog, interaction, overlay=None)

            # Non-ephemeral "Nothing is playing" (existing behavior)
            interaction.response.send_message.assert_awaited_once_with(
                "Nothing is playing right now."
            )

    @pytest.mark.asyncio
    async def test_no_overlay_with_track_defers_and_fetches(self):
        """No overlay with a track → defers, fetches lyrics, sends embed."""
        with patch("player.get_state", return_value={
            "current": {"title": "My Song", "author": "My Artist", "duration": 200000}
        }):
            from cogs.lyrics import Lyrics

            cog = Lyrics.__new__(Lyrics)
            cog.bot = MagicMock()
            cog.access_token = "fake_token"
            cog.api_key = ""
            cog._genius = MagicMock()
            cog._genius.fetch = AsyncMock(return_value="Line 1\nLine 2\nLine 3")

            interaction = _make_interaction()
            await cog.lyrics.callback(cog, interaction, overlay=None)

            # Should defer (long-running fetch)
            interaction.response.defer.assert_awaited_once()

            # Should send an embed via followup
            interaction.followup.send.assert_awaited_once()
            call_kwargs = interaction.followup.send.await_args[1]
            assert "embed" in call_kwargs

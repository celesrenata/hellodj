"""Unit tests for the enhanced /remote command (cogs/remote.py).

Tests embed generation, idle state, queue preview, button interactions,
and persistent view registration.
"""

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Ensure bot directory is on path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bot"))


# ── Test helper functions ────────────────────────────────────────────────────

from cogs.remote import _progress_bar, _fmt_duration_ms, _build_remote_embed, _build_idle_embed


class TestProgressBar:
    def test_zero_duration_returns_flat_bar(self):
        bar = _progress_bar(0, 0)
        assert "🔘" not in bar
        assert len(bar) == 12

    def test_start_position(self):
        bar = _progress_bar(0, 100000)
        assert bar[0] == "🔘"

    def test_middle_position(self):
        bar = _progress_bar(50000, 100000, width=10)
        assert bar[5] == "🔘"

    def test_end_position(self):
        bar = _progress_bar(99000, 100000, width=10)
        # Should be at the last possible position (width - 1)
        assert "🔘" in bar

    def test_negative_duration(self):
        bar = _progress_bar(5000, -1)
        assert "🔘" not in bar


class TestFmtDuration:
    def test_zero(self):
        assert _fmt_duration_ms(0) == "0:00"

    def test_negative(self):
        assert _fmt_duration_ms(-1) == "0:00"

    def test_one_minute(self):
        assert _fmt_duration_ms(60000) == "1:00"

    def test_three_minutes_42(self):
        assert _fmt_duration_ms(222000) == "3:42"

    def test_over_an_hour(self):
        assert _fmt_duration_ms(3661000) == "1:01:01"


class TestBuildIdleEmbed:
    def test_idle_embed_has_title(self):
        embed = _build_idle_embed()
        assert "Not Playing" in embed.title

    def test_idle_embed_has_dashboard_link(self):
        embed = _build_idle_embed()
        assert "hellodj.celestium.life" in embed.description

    def test_idle_embed_with_user(self):
        user = MagicMock()
        user.display_name = "TestUser"
        user.display_avatar.url = "https://cdn.discordapp.com/avatars/123/abc.png"
        embed = _build_idle_embed(user=user)
        assert embed.author.name == "TestUser"


class TestBuildRemoteEmbed:
    @patch("cogs.remote.player")
    def test_no_current_returns_idle(self, mock_player):
        mock_player.get_state.return_value = {"current": None, "queue": []}
        embed = _build_remote_embed(12345)
        assert "Not Playing" in embed.title

    @patch("cogs.remote.player")
    def test_with_current_track(self, mock_player):
        mock_player.get_state.return_value = {
            "current": {
                "title": "Test Song",
                "author": "Test Artist",
                "duration": 180000,
                "artwork_url": "https://example.com/art.jpg",
                "webpage_url": "https://example.com/track",
            },
            "queue": [],
            "repeat_mode": "off",
            "autoplay_enabled": False,
        }
        mock_player.get_player.return_value = MagicMock(
            position=60000, volume=0.75
        )
        embed = _build_remote_embed(12345)
        assert embed.title == "Test Song"
        # Check fields
        field_names = [f.name for f in embed.fields]
        assert "Artist" in field_names
        assert "Volume" in field_names
        assert "State" in field_names

    @patch("cogs.remote.player")
    def test_queue_preview_shows_next_5(self, mock_player):
        queue = [
            {"title": f"Track {i}", "author": f"Artist {i}", "duration": 200000}
            for i in range(8)
        ]
        mock_player.get_state.return_value = {
            "current": {
                "title": "Now Playing",
                "author": "Curr Artist",
                "duration": 180000,
            },
            "queue": queue,
            "repeat_mode": "off",
            "autoplay_enabled": True,
        }
        mock_player.get_player.return_value = MagicMock(position=0, volume=1.0)
        embed = _build_remote_embed(12345)
        # Should have "Up Next" field
        up_next_field = next((f for f in embed.fields if f.name == "Up Next"), None)
        assert up_next_field is not None
        assert "Track 0" in up_next_field.value
        assert "Track 4" in up_next_field.value
        assert "…and 3 more" in up_next_field.value

    @patch("cogs.remote.player")
    def test_autoplay_state_displayed(self, mock_player):
        mock_player.get_state.return_value = {
            "current": {
                "title": "Test",
                "author": "Artist",
                "duration": 100000,
            },
            "queue": [],
            "repeat_mode": "one",
            "autoplay_enabled": True,
        }
        mock_player.get_player.return_value = MagicMock(position=0, volume=1.0)
        embed = _build_remote_embed(12345)
        state_field = next((f for f in embed.fields if f.name == "State"), None)
        assert state_field is not None
        assert "🔂" in state_field.value  # repeat one icon
        assert "AutoPlay: ON" in state_field.value

    @patch("cogs.remote.player")
    def test_user_avatar_as_author(self, mock_player):
        mock_player.get_state.return_value = {
            "current": {"title": "Test", "author": "A", "duration": 1000},
            "queue": [],
            "repeat_mode": "off",
            "autoplay_enabled": False,
        }
        mock_player.get_player.return_value = MagicMock(position=0, volume=1.0)
        user = MagicMock()
        user.display_name = "DJ Celes"
        user.display_avatar.url = "https://cdn.discordapp.com/avatars/1/a.png"
        embed = _build_remote_embed(12345, user=user)
        assert embed.author.name == "DJ Celes"
        assert embed.author.icon_url == "https://cdn.discordapp.com/avatars/1/a.png"


# ── Test button interactions ─────────────────────────────────────────────────

from cogs.remote import EnhancedRemoteView


def _get_button(view: EnhancedRemoteView, custom_id: str):
    """Get a button from the view by its custom_id."""
    for item in view.children:
        if hasattr(item, "custom_id") and item.custom_id == custom_id:
            return item
    raise ValueError(f"No button with custom_id={custom_id!r}")


class TestPauseResumeButton:
    """Validates: Requirement 18.4 — button clicks execute action and update embed."""

    @pytest.fixture
    def view(self):
        return EnhancedRemoteView()

    @pytest.fixture
    def interaction(self):
        inter = MagicMock(spec=["guild_id", "response", "user"])
        inter.guild_id = 99999
        inter.user = MagicMock()
        inter.user.display_name = "TestUser"
        inter.user.display_avatar.url = "https://cdn.discordapp.com/avatars/1/a.png"
        inter.response = MagicMock()
        inter.response.edit_message = AsyncMock()
        inter.response.defer = AsyncMock()
        return inter

    @pytest.mark.asyncio
    @patch("cogs.remote.player")
    async def test_pause_when_playing(self, mock_player, view, interaction):
        """Clicking pause/resume when playing should pause."""
        p = MagicMock()
        p.paused = False
        p.playing = True
        p.pause = AsyncMock()
        mock_player.get_player.return_value = p
        mock_player.get_state.return_value = {"current": None, "queue": []}

        btn = _get_button(view, "hellodj:remote:pause")
        await btn.callback(interaction)
        p.pause.assert_called_once_with(True)

    @pytest.mark.asyncio
    @patch("cogs.remote.player")
    async def test_resume_when_paused(self, mock_player, view, interaction):
        """Clicking pause/resume when paused should resume."""
        p = MagicMock()
        p.paused = True
        p.playing = False
        p.pause = AsyncMock()
        mock_player.get_player.return_value = p
        mock_player.get_state.return_value = {"current": None, "queue": []}

        btn = _get_button(view, "hellodj:remote:pause")
        await btn.callback(interaction)
        p.pause.assert_called_once_with(False)

    @pytest.mark.asyncio
    @patch("cogs.remote.player")
    async def test_pause_no_guild(self, mock_player, view):
        """No action when guild_id is None (DM context)."""
        inter = MagicMock()
        inter.guild_id = None
        inter.response = MagicMock()
        inter.response.edit_message = AsyncMock()

        btn = _get_button(view, "hellodj:remote:pause")
        await btn.callback(inter)
        mock_player.get_player.assert_not_called()


class TestSkipButton:
    """Validates: Requirement 18.4 — skip button executes and updates embed."""

    @pytest.fixture
    def view(self):
        return EnhancedRemoteView()

    @pytest.fixture
    def interaction(self):
        inter = MagicMock(spec=["guild_id", "response", "user"])
        inter.guild_id = 99999
        inter.user = MagicMock()
        inter.user.display_name = "TestUser"
        inter.user.display_avatar.url = "https://cdn.discordapp.com/avatars/1/a.png"
        inter.response = MagicMock()
        inter.response.edit_message = AsyncMock()
        inter.response.defer = AsyncMock()
        return inter

    @pytest.mark.asyncio
    @patch("cogs.remote.player")
    async def test_skip_calls_unified_skip(self, mock_player, view, interaction):
        """Skip button should call unified_skip for the guild."""
        mock_player.get_state.return_value = {"current": None, "queue": []}

        with patch("playback.unified_controls.unified_skip", new_callable=AsyncMock) as mock_uskip:
            btn = _get_button(view, "hellodj:remote:skip")
            await btn.callback(interaction)
            mock_uskip.assert_called_once_with(99999)


class TestVolumeButtons:
    """Validates: Requirement 18.4 — volume buttons adjust by 10%."""

    @pytest.fixture
    def view(self):
        return EnhancedRemoteView()

    @pytest.fixture
    def interaction(self):
        inter = MagicMock(spec=["guild_id", "response", "user"])
        inter.guild_id = 99999
        inter.user = MagicMock()
        inter.user.display_name = "TestUser"
        inter.user.display_avatar.url = "https://cdn.discordapp.com/avatars/1/a.png"
        inter.response = MagicMock()
        inter.response.edit_message = AsyncMock()
        inter.response.defer = AsyncMock()
        return inter

    @pytest.mark.asyncio
    @patch("cogs.remote.player")
    async def test_volume_up_increases_by_10(self, mock_player, view, interaction):
        """Volume up should increase by 0.10."""
        p = MagicMock()
        p.volume = 0.5
        p.set_volume = AsyncMock()
        mock_player.get_player.return_value = p
        mock_player.get_state.return_value = {"current": None, "queue": []}

        btn = _get_button(view, "hellodj:remote:vol_up")
        await btn.callback(interaction)
        p.set_volume.assert_called_once()
        new_vol = p.set_volume.call_args[0][0]
        assert abs(new_vol - 0.6) < 0.001

    @pytest.mark.asyncio
    @patch("cogs.remote.player")
    async def test_volume_down_decreases_by_10(self, mock_player, view, interaction):
        """Volume down should decrease by 0.10."""
        p = MagicMock()
        p.volume = 0.5
        p.set_volume = AsyncMock()
        mock_player.get_player.return_value = p
        mock_player.get_state.return_value = {"current": None, "queue": []}

        btn = _get_button(view, "hellodj:remote:vol_down")
        await btn.callback(interaction)
        p.set_volume.assert_called_once()
        new_vol = p.set_volume.call_args[0][0]
        assert abs(new_vol - 0.4) < 0.001

    @pytest.mark.asyncio
    @patch("cogs.remote.player")
    async def test_volume_up_clamped_at_100(self, mock_player, view, interaction):
        """Volume up should not exceed 1.0."""
        p = MagicMock()
        p.volume = 0.95
        p.set_volume = AsyncMock()
        mock_player.get_player.return_value = p
        mock_player.get_state.return_value = {"current": None, "queue": []}

        btn = _get_button(view, "hellodj:remote:vol_up")
        await btn.callback(interaction)
        new_vol = p.set_volume.call_args[0][0]
        assert new_vol == 1.0

    @pytest.mark.asyncio
    @patch("cogs.remote.player")
    async def test_volume_down_clamped_at_0(self, mock_player, view, interaction):
        """Volume down should not go below 0.0."""
        p = MagicMock()
        p.volume = 0.05
        p.set_volume = AsyncMock()
        mock_player.get_player.return_value = p
        mock_player.get_state.return_value = {"current": None, "queue": []}

        btn = _get_button(view, "hellodj:remote:vol_down")
        await btn.callback(interaction)
        new_vol = p.set_volume.call_args[0][0]
        assert new_vol == 0.0


# ── Test persistent view registration ────────────────────────────────────────


class TestPersistentViewRegistration:
    """Validates: Requirement 18.7 — view persists (timeout=None, fixed custom_ids)."""

    def test_view_timeout_is_none(self):
        """View must have timeout=None for persistence across bot restarts."""
        view = EnhancedRemoteView()
        assert view.timeout is None

    def test_all_buttons_have_fixed_custom_ids(self):
        """All buttons must have deterministic custom_ids for persistent views."""
        view = EnhancedRemoteView()
        expected_custom_ids = {
            "hellodj:remote:prev",
            "hellodj:remote:pause",
            "hellodj:remote:skip",
            "hellodj:remote:vol_down",
            "hellodj:remote:vol_up",
            "hellodj:remote:shuffle",
            "hellodj:remote:autoplay",
            "hellodj:remote:like",
            "hellodj:remote:stop",
        }
        actual_custom_ids = {
            item.custom_id for item in view.children
            if hasattr(item, "custom_id") and item.custom_id is not None
        }
        assert expected_custom_ids.issubset(actual_custom_ids)

    def test_custom_ids_are_prefixed(self):
        """All custom_ids should use the 'hellodj:remote:' prefix for namespacing."""
        view = EnhancedRemoteView()
        for item in view.children:
            if hasattr(item, "custom_id") and item.custom_id is not None:
                assert item.custom_id.startswith("hellodj:remote:"), (
                    f"Button custom_id '{item.custom_id}' missing prefix"
                )

    def test_button_count(self):
        """View should have the expected number of interactive buttons (9 non-link)."""
        view = EnhancedRemoteView()
        interactive = [
            item for item in view.children
            if hasattr(item, "custom_id") and item.custom_id is not None
        ]
        assert len(interactive) == 9

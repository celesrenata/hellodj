"""Tests for the /visualizer command group (config, preset, projectm subcommands).

Validates command parameter validation, autocomplete behavior, and
correct interaction with guild_settings and config_schema.

Requirements: Req 14 (AC 1-5), Req 15 (AC 1-7), Req 17 (AC 4, 5)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Add bot/ to sys.path so we can import cogs, guild_settings, etc.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bot"))

# Set env for credential store before importing anything that touches config
os.environ.setdefault("HELLODJ_DB_KEY", "test-key-for-visualizer-cog-tests")


def _make_interaction(guild_id: int = 123) -> MagicMock:
    """Create a mock discord.Interaction."""
    interaction = MagicMock()
    interaction.guild = MagicMock()
    interaction.guild.id = guild_id
    interaction.guild_id = guild_id
    interaction.user = MagicMock()
    interaction.user.voice = None
    interaction.response = MagicMock()
    interaction.response.send_message = AsyncMock()
    interaction.response.defer = AsyncMock()
    interaction.followup = MagicMock()
    interaction.followup.send = AsyncMock()
    interaction.namespace = MagicMock()
    return interaction


def _make_bot() -> MagicMock:
    """Create a mock bot instance."""
    bot = MagicMock()
    bot.get_cog.return_value = None
    return bot


class TestEngineCommand:
    """Tests for /visualizer engine <engine>."""

    @pytest.mark.asyncio
    async def test_valid_engine_sets_and_confirms(self):
        """Valid engine name → guild_settings updated, embed response."""
        from cogs.visualizer import VisualizerCog

        cog = VisualizerCog.__new__(VisualizerCog)
        cog.bot = _make_bot()

        interaction = _make_interaction()

        with patch("cogs.visualizer.guild_settings") as mock_gs:
            mock_gs.VALID_VISUALIZER_ENGINES = {"dvd", "projectm", "off"}
            mock_gs.set_visualizer_engine = MagicMock()
            await cog.engine.callback(cog, interaction, engine="projectm")

        mock_gs.set_visualizer_engine.assert_called_once_with(123, "projectm")
        interaction.response.send_message.assert_awaited_once()
        call_kwargs = interaction.response.send_message.await_args[1]
        assert "embed" in call_kwargs
        assert call_kwargs["ephemeral"] is True

    @pytest.mark.asyncio
    async def test_invalid_engine_sends_error(self):
        """Invalid engine name → error message with valid options."""
        from cogs.visualizer import VisualizerCog

        cog = VisualizerCog.__new__(VisualizerCog)
        cog.bot = _make_bot()

        interaction = _make_interaction()

        with patch("cogs.visualizer.guild_settings") as mock_gs:
            mock_gs.VALID_VISUALIZER_ENGINES = {"dvd", "off"}
            mock_gs.set_visualizer_engine = MagicMock(
                side_effect=ValueError("Invalid")
            )
            await cog.engine.callback(cog, interaction, engine="banana")

        interaction.response.send_message.assert_awaited_once()
        msg = interaction.response.send_message.await_args[0][0]
        assert "Invalid engine" in msg
        assert "banana" in msg

    @pytest.mark.asyncio
    async def test_off_engine_disabled_message(self):
        """Setting engine to 'off' → disabled embed."""
        from cogs.visualizer import VisualizerCog

        cog = VisualizerCog.__new__(VisualizerCog)
        cog.bot = _make_bot()

        interaction = _make_interaction()

        with patch("cogs.visualizer.guild_settings") as mock_gs:
            mock_gs.VALID_VISUALIZER_ENGINES = {"dvd", "off"}
            mock_gs.set_visualizer_engine = MagicMock()
            await cog.engine.callback(cog, interaction, engine="off")

        interaction.response.send_message.assert_awaited_once()
        call_kwargs = interaction.response.send_message.await_args[1]
        embed = call_kwargs["embed"]
        assert "disabled" in embed.description.lower()


class TestConfigCommand:
    """Tests for /visualizer config <engine> <setting> <value>."""

    @pytest.mark.asyncio
    async def test_valid_config_stores_and_confirms(self):
        """Valid config value → stored via guild_settings, embed confirmation."""
        from cogs.visualizer import VisualizerCog

        cog = VisualizerCog.__new__(VisualizerCog)
        cog.bot = _make_bot()

        interaction = _make_interaction(guild_id=456)

        with patch("cogs.visualizer.guild_settings") as mock_gs:
            mock_gs.set_visualizer_config = MagicMock()
            mock_gs.get_visualizer_config = MagicMock(
                return_value={"brightness": 1.5}
            )
            await cog.config.callback(cog, interaction, engine="projectm", setting="brightness", value="1.5")

        mock_gs.set_visualizer_config.assert_called_once_with(456, "projectm", "brightness", "1.5")
        interaction.response.send_message.assert_awaited_once()
        call_kwargs = interaction.response.send_message.await_args[1]
        assert "embed" in call_kwargs

    @pytest.mark.asyncio
    async def test_invalid_config_value_sends_error(self):
        """Invalid config value → error message from schema validation."""
        from cogs.visualizer import VisualizerCog

        cog = VisualizerCog.__new__(VisualizerCog)
        cog.bot = _make_bot()

        interaction = _make_interaction()

        with patch("cogs.visualizer.guild_settings") as mock_gs:
            mock_gs.set_visualizer_config = MagicMock(
                side_effect=ValueError("value 99.0 is above maximum 2.0")
            )
            await cog.config.callback(cog, interaction, engine="projectm", setting="brightness", value="99")

        interaction.response.send_message.assert_awaited_once()
        msg = interaction.response.send_message.await_args[0][0]
        assert "above maximum" in msg

    @pytest.mark.asyncio
    async def test_unknown_engine_sends_error(self):
        """Unknown engine in config → error message."""
        from cogs.visualizer import VisualizerCog

        cog = VisualizerCog.__new__(VisualizerCog)
        cog.bot = _make_bot()

        interaction = _make_interaction()

        with patch("cogs.visualizer.guild_settings") as mock_gs:
            mock_gs.set_visualizer_config = MagicMock(
                side_effect=ValueError("Unknown engine: 'nonexistent'")
            )
            await cog.config.callback(
                cog, interaction, engine="nonexistent", setting="foo", value="bar"
            )

        interaction.response.send_message.assert_awaited_once()
        msg = interaction.response.send_message.await_args[0][0]
        assert "Unknown engine" in msg


class TestSettingsCommand:
    """Tests for /visualizer settings [engine]."""

    @pytest.mark.asyncio
    async def test_shows_engine_settings_embed(self):
        """Settings for a known engine → embed with field per setting."""
        from cogs.visualizer import VisualizerCog

        cog = VisualizerCog.__new__(VisualizerCog)
        cog.bot = _make_bot()

        interaction = _make_interaction()

        with patch("cogs.visualizer.guild_settings") as mock_gs:
            mock_gs.get_visualizer_engine = MagicMock(return_value="dvd")
            mock_gs.get_visualizer_config = MagicMock(
                return_value={"speed": 1.0, "hue_shift": True, "icon_size": 15}
            )
            await cog.settings.callback(cog, interaction, engine="dvd")

        interaction.response.send_message.assert_awaited_once()
        call_kwargs = interaction.response.send_message.await_args[1]
        embed = call_kwargs["embed"]
        # dvd has 3 settings: speed, hue_shift, icon_size
        assert len(embed.fields) == 3

    @pytest.mark.asyncio
    async def test_defaults_to_active_engine(self):
        """No engine specified → uses the guild's active engine."""
        from cogs.visualizer import VisualizerCog

        cog = VisualizerCog.__new__(VisualizerCog)
        cog.bot = _make_bot()

        interaction = _make_interaction(guild_id=789)

        with patch("cogs.visualizer.guild_settings") as mock_gs:
            mock_gs.get_visualizer_engine = MagicMock(return_value="audiovis")
            mock_gs.get_visualizer_config = MagicMock(
                return_value={
                    "style": "bars",
                    "color_scheme": "neon",
                    "fft_bins": 7,
                    "glow_intensity": 0.5,
                    "background_opacity": 0.9,
                }
            )
            await cog.settings.callback(cog, interaction, engine=None)

        mock_gs.get_visualizer_engine.assert_called_once_with(789)
        interaction.response.send_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_unknown_engine_error(self):
        """Engine with no schema → error message."""
        from cogs.visualizer import VisualizerCog

        cog = VisualizerCog.__new__(VisualizerCog)
        cog.bot = _make_bot()

        interaction = _make_interaction()

        with patch("cogs.visualizer.guild_settings") as mock_gs:
            mock_gs.get_visualizer_engine = MagicMock(return_value="random")
            await cog.settings.callback(cog, interaction, engine="random")

        interaction.response.send_message.assert_awaited_once()
        msg = interaction.response.send_message.await_args[0][0]
        assert "no configurable settings" in msg.lower()


class TestPresetSave:
    """Tests for /visualizer preset save <name>."""

    @pytest.mark.asyncio
    async def test_saves_current_engine_and_config(self):
        """Save captures active engine + config into guild presets."""
        from cogs.visualizer import VisualizerCog

        cog = VisualizerCog.__new__(VisualizerCog)
        cog.bot = _make_bot()

        interaction = _make_interaction(guild_id=100)

        with patch("cogs.visualizer.guild_settings") as mock_gs:
            mock_gs.get_visualizer_engine = MagicMock(return_value="fosfora")
            mock_gs.get_visualizer_config = MagicMock(
                return_value={"particle_count": 8000, "gravity": 0.2}
            )
            mock_gs.save_visualizer_preset = MagicMock()

            await cog.preset_save.callback(cog, interaction, name="my-particles")

        mock_gs.save_visualizer_preset.assert_called_once_with(
            100,
            "my-particles",
            {"engine": "fosfora", "config": {"particle_count": 8000, "gravity": 0.2}},
        )
        interaction.response.send_message.assert_awaited_once()
        call_kwargs = interaction.response.send_message.await_args[1]
        assert "Preset Saved" in call_kwargs["embed"].title


class TestPresetLoad:
    """Tests for /visualizer preset load <name>."""

    @pytest.mark.asyncio
    async def test_loads_user_preset(self):
        """Loading an existing user preset → applies engine + config."""
        from cogs.visualizer import VisualizerCog

        cog = VisualizerCog.__new__(VisualizerCog)
        cog.bot = _make_bot()

        interaction = _make_interaction(guild_id=200)

        with patch("cogs.visualizer.guild_settings") as mock_gs:
            mock_gs.VALID_VISUALIZER_ENGINES = {"dvd", "projectm", "audiovis"}
            mock_gs.load_visualizer_preset = MagicMock(
                return_value={"engine": "audiovis", "config": {"style": "waveform"}}
            )
            mock_gs.set_visualizer_engine = MagicMock()
            mock_gs.set_visualizer_config = MagicMock()

            await cog.preset_load.callback(cog, interaction, name="my-wave")

        mock_gs.set_visualizer_engine.assert_called_once_with(200, "audiovis")
        mock_gs.set_visualizer_config.assert_called_once_with(200, "audiovis", "style", "waveform")

    @pytest.mark.asyncio
    async def test_nonexistent_preset_error(self):
        """Loading a preset that doesn't exist → error message."""
        from cogs.visualizer import VisualizerCog

        cog = VisualizerCog.__new__(VisualizerCog)
        cog.bot = _make_bot()

        interaction = _make_interaction()

        with patch("cogs.visualizer.guild_settings") as mock_gs:
            mock_gs.load_visualizer_preset = MagicMock(return_value=None)
            await cog.preset_load.callback(cog, interaction, name="ghost")

        interaction.response.send_message.assert_awaited_once()
        msg = interaction.response.send_message.await_args[0][0]
        assert "not found" in msg.lower()

    @pytest.mark.asyncio
    async def test_unavailable_engine_in_preset_error(self):
        """Preset with removed engine → error suggesting alternatives."""
        from cogs.visualizer import VisualizerCog

        cog = VisualizerCog.__new__(VisualizerCog)
        cog.bot = _make_bot()

        interaction = _make_interaction()

        with patch("cogs.visualizer.guild_settings") as mock_gs:
            mock_gs.VALID_VISUALIZER_ENGINES = {"dvd", "off"}
            mock_gs.load_visualizer_preset = MagicMock(
                return_value={"engine": "vgalizer", "config": {}}
            )
            await cog.preset_load.callback(cog, interaction, name="old-preset")

        interaction.response.send_message.assert_awaited_once()
        msg = interaction.response.send_message.await_args[0][0]
        assert "unavailable" in msg.lower()


class TestPresetList:
    """Tests for /visualizer preset list."""

    @pytest.mark.asyncio
    async def test_lists_user_and_factory_presets(self):
        """Preset list shows both user and factory presets."""
        from cogs.visualizer import VisualizerCog

        cog = VisualizerCog.__new__(VisualizerCog)
        cog.bot = _make_bot()

        interaction = _make_interaction(guild_id=300)

        with (
            patch("cogs.visualizer.guild_settings") as mock_gs,
            patch("cogs.visualizer.list_factory_presets") as mock_factory,
        ):
            mock_gs.get_visualizer_presets = MagicMock(
                return_value={"my-custom": {"engine": "dvd", "config": {}}}
            )
            mock_factory.return_value = {
                "plasma": {"engine": "varda", "config": {}, "factory": True},
                "spectrum-bars": {"engine": "audiovis", "config": {}, "factory": True},
            }
            await cog.preset_list.callback(cog, interaction)

        interaction.response.send_message.assert_awaited_once()
        call_kwargs = interaction.response.send_message.await_args[1]
        embed = call_kwargs["embed"]
        assert "Presets" in embed.title
        # Should have user presets field + at least one factory engine field
        assert len(embed.fields) >= 2


class TestPresetDelete:
    """Tests for /visualizer preset delete <name>."""

    @pytest.mark.asyncio
    async def test_deletes_user_preset(self):
        """Deleting an existing user preset → success embed."""
        from cogs.visualizer import VisualizerCog

        cog = VisualizerCog.__new__(VisualizerCog)
        cog.bot = _make_bot()

        interaction = _make_interaction(guild_id=400)

        with patch("cogs.visualizer.guild_settings") as mock_gs:
            mock_gs.delete_visualizer_preset = MagicMock()
            await cog.preset_delete.callback(cog, interaction, name="my-old")

        mock_gs.delete_visualizer_preset.assert_called_once_with(400, "my-old")
        interaction.response.send_message.assert_awaited_once()
        call_kwargs = interaction.response.send_message.await_args[1]
        assert "Deleted" in call_kwargs["embed"].title

    @pytest.mark.asyncio
    async def test_factory_preset_error(self):
        """Deleting a factory preset → error message."""
        from cogs.visualizer import VisualizerCog

        cog = VisualizerCog.__new__(VisualizerCog)
        cog.bot = _make_bot()

        interaction = _make_interaction()

        with patch("cogs.visualizer.guild_settings") as mock_gs:
            mock_gs.delete_visualizer_preset = MagicMock(
                side_effect=ValueError("Cannot delete factory preset 'plasma'.")
            )
            await cog.preset_delete.callback(cog, interaction, name="plasma")

        interaction.response.send_message.assert_awaited_once()
        msg = interaction.response.send_message.await_args[0][0]
        assert "Cannot delete factory preset" in msg

    @pytest.mark.asyncio
    async def test_nonexistent_preset_error(self):
        """Deleting a nonexistent preset → error message."""
        from cogs.visualizer import VisualizerCog

        cog = VisualizerCog.__new__(VisualizerCog)
        cog.bot = _make_bot()

        interaction = _make_interaction()

        with patch("cogs.visualizer.guild_settings") as mock_gs:
            mock_gs.delete_visualizer_preset = MagicMock(
                side_effect=KeyError("Preset 'nope' not found for guild 123")
            )
            await cog.preset_delete.callback(cog, interaction, name="nope")

        interaction.response.send_message.assert_awaited_once()
        msg = interaction.response.send_message.await_args[0][0]
        assert "not found" in msg.lower()


class TestProjectMListCategories:
    """Tests for /visualizer projectm list-categories."""

    @pytest.mark.asyncio
    async def test_lists_categories_with_counts(self):
        """Categories found → embed with category names and preset counts."""
        from cogs.visualizer import VisualizerCog

        cog = VisualizerCog.__new__(VisualizerCog)
        cog.bot = _make_bot()

        interaction = _make_interaction()

        with patch("cogs.visualizer._get_projectm_categories") as mock_cats:
            mock_cats.return_value = {
                "Abstract": 42,
                "Geometric": 18,
                "Space": 7,
            }
            await cog.projectm_list_categories.callback(cog, interaction)

        interaction.response.send_message.assert_awaited_once()
        call_kwargs = interaction.response.send_message.await_args[1]
        embed = call_kwargs["embed"]
        assert "Categories" in embed.title
        assert len(embed.fields) == 3
        # Check footer has totals
        assert "67 presets" in embed.footer.text

    @pytest.mark.asyncio
    async def test_no_categories_found_error(self):
        """No categories available → error message."""
        from cogs.visualizer import VisualizerCog

        cog = VisualizerCog.__new__(VisualizerCog)
        cog.bot = _make_bot()

        interaction = _make_interaction()

        with patch("cogs.visualizer._get_projectm_categories") as mock_cats:
            mock_cats.return_value = {}
            await cog.projectm_list_categories.callback(cog, interaction)

        interaction.response.send_message.assert_awaited_once()
        msg = interaction.response.send_message.await_args[0][0]
        assert "No projectM preset categories found" in msg


class TestAutocomplete:
    """Tests for autocomplete functions."""

    @pytest.mark.asyncio
    async def test_engine_autocomplete_filters(self):
        """Engine autocomplete filters by partial match."""
        from cogs.visualizer import VisualizerCog

        cog = VisualizerCog.__new__(VisualizerCog)
        cog.bot = _make_bot()

        interaction = _make_interaction()

        with patch("cogs.visualizer.guild_settings") as mock_gs:
            mock_gs.VALID_VISUALIZER_ENGINES = {"dvd", "projectm", "audiovis", "off"}
            result = await cog._engine_autocomplete(interaction, "pro")

        assert len(result) == 1
        assert result[0].value == "projectm"

    @pytest.mark.asyncio
    async def test_setting_autocomplete_for_engine(self):
        """Setting autocomplete returns settings for the specified engine."""
        from cogs.visualizer import VisualizerCog

        cog = VisualizerCog.__new__(VisualizerCog)
        cog.bot = _make_bot()

        interaction = _make_interaction()
        interaction.namespace.engine = "dvd"

        result = await cog._setting_autocomplete(interaction, "")
        setting_values = [c.value for c in result]
        assert "speed" in setting_values
        assert "hue_shift" in setting_values
        assert "icon_size" in setting_values

    @pytest.mark.asyncio
    async def test_setting_autocomplete_unknown_engine(self):
        """Setting autocomplete for unknown engine → empty list."""
        from cogs.visualizer import VisualizerCog

        cog = VisualizerCog.__new__(VisualizerCog)
        cog.bot = _make_bot()

        interaction = _make_interaction()
        interaction.namespace.engine = "nonexistent"

        result = await cog._setting_autocomplete(interaction, "")
        assert result == []

    @pytest.mark.asyncio
    async def test_preset_autocomplete_merges_user_and_factory(self):
        """Preset autocomplete merges user + factory presets."""
        from cogs.visualizer import VisualizerCog

        cog = VisualizerCog.__new__(VisualizerCog)
        cog.bot = _make_bot()

        interaction = _make_interaction(guild_id=500)

        with (
            patch("cogs.visualizer.guild_settings") as mock_gs,
            patch("cogs.visualizer.list_factory_presets") as mock_factory,
        ):
            mock_gs.get_visualizer_presets = MagicMock(
                return_value={"my-custom": {"engine": "dvd"}}
            )
            mock_factory.return_value = {
                "plasma": {"engine": "varda"},
                "spectrum-bars": {"engine": "audiovis"},
            }
            result = await cog._preset_name_autocomplete(interaction, "")

        values = [c.value for c in result]
        assert "my-custom" in values
        assert "plasma" in values
        assert "spectrum-bars" in values

    @pytest.mark.asyncio
    async def test_category_autocomplete(self):
        """Category autocomplete returns category names."""
        from cogs.visualizer import VisualizerCog

        cog = VisualizerCog.__new__(VisualizerCog)
        cog.bot = _make_bot()

        interaction = _make_interaction()

        with patch("cogs.visualizer._get_projectm_categories") as mock_cats:
            mock_cats.return_value = {"Abstract": 42, "Space": 7}
            result = await cog._category_autocomplete(interaction, "")

        values = [c.value for c in result]
        assert "all" in values
        assert "Abstract" in values
        assert "Space" in values


class TestHotReload:
    """Tests for hot-reload config behavior."""

    @pytest.mark.asyncio
    async def test_hot_reload_calls_manager_when_available(self):
        """Hot-reload attempts to call manager.hot_reload_config when available."""
        from cogs.visualizer import VisualizerCog

        cog = VisualizerCog.__new__(VisualizerCog)
        bot = _make_bot()
        cog.bot = bot

        # Mock the Video cog with a registry
        video_cog = MagicMock()
        manager = MagicMock()
        manager.hot_reload_config = AsyncMock()
        registry = MagicMock()
        registry.get.return_value = manager
        video_cog._visualizer_registry = registry
        bot.get_cog.return_value = video_cog

        await cog._try_hot_reload(123, "projectm", "brightness")

        manager.hot_reload_config.assert_awaited_once_with("projectm", "brightness")

    @pytest.mark.asyncio
    async def test_hot_reload_no_video_cog_is_silent(self):
        """No Video cog → hot-reload is a no-op (no crash)."""
        from cogs.visualizer import VisualizerCog

        cog = VisualizerCog.__new__(VisualizerCog)
        cog.bot = _make_bot()
        cog.bot.get_cog.return_value = None

        # Should not raise
        await cog._try_hot_reload(123, "dvd", "speed")

    @pytest.mark.asyncio
    async def test_hot_reload_error_is_swallowed(self):
        """Exception in hot_reload_config → logged, not propagated."""
        from cogs.visualizer import VisualizerCog

        cog = VisualizerCog.__new__(VisualizerCog)
        bot = _make_bot()
        cog.bot = bot

        video_cog = MagicMock()
        manager = MagicMock()
        manager.hot_reload_config = AsyncMock(side_effect=RuntimeError("GPU gone"))
        registry = MagicMock()
        registry.get.return_value = manager
        video_cog._visualizer_registry = registry
        bot.get_cog.return_value = video_cog

        # Should not raise
        await cog._try_hot_reload(999, "varda", "speed")

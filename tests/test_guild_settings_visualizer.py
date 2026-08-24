"""Tests for guild_settings visualizer config and preset CRUD operations.

Covers:
  - get_visualizer_config: merges defaults with stored overrides
  - set_visualizer_config: validates via schema before storing
  - get_visualizer_presets: returns user-saved presets dict
  - save_visualizer_preset: stores engine + config as named preset
  - delete_visualizer_preset: removes user preset; raises for factory presets
  - load_visualizer_preset: user presets first, then factory fallback

Requirements: Req 14 (AC 1, 3), Req 15 (AC 1-7)
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

# Ensure bot/ is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bot"))

import guild_settings


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolate_settings(tmp_path, monkeypatch):
    """Isolate guild settings to a temp directory for each test."""
    settings_file = str(tmp_path / "guild_settings.json")
    monkeypatch.setattr(guild_settings, "GUILD_SETTINGS_FILE", settings_file)
    monkeypatch.setattr(guild_settings, "_settings", {})
    # Ensure data directory creation uses tmp_path
    monkeypatch.chdir(tmp_path)
    yield


@pytest.fixture
def guild_id():
    """A sample guild ID for testing."""
    return 123456789


# ---------------------------------------------------------------------------
# get_visualizer_config
# ---------------------------------------------------------------------------


class TestGetVisualizerConfig:
    """Test get_visualizer_config merges defaults with stored overrides."""

    def test_returns_defaults_when_no_overrides(self, guild_id):
        config = guild_settings.get_visualizer_config(guild_id, "projectm")
        assert config["preset_category"] == "all"
        assert config["blend_duration"] == 3.0
        assert config["preset_duration"] == 30.0
        assert config["brightness"] == 1.0
        assert config["sensitivity"] == 1.0

    def test_returns_defaults_for_dvd(self, guild_id):
        config = guild_settings.get_visualizer_config(guild_id, "dvd")
        assert config["speed"] == 1.0
        assert config["hue_shift"] is True
        assert config["icon_size"] == 15

    def test_merges_stored_overrides_with_defaults(self, guild_id):
        # Store an override
        guild_settings.set_visualizer_config(guild_id, "projectm", "brightness", 1.5)

        config = guild_settings.get_visualizer_config(guild_id, "projectm")
        # Override applied
        assert config["brightness"] == 1.5
        # Other defaults remain
        assert config["blend_duration"] == 3.0
        assert config["preset_category"] == "all"

    def test_multiple_overrides_merged(self, guild_id):
        guild_settings.set_visualizer_config(guild_id, "audiovis", "style", "waveform")
        guild_settings.set_visualizer_config(guild_id, "audiovis", "glow_intensity", 0.8)

        config = guild_settings.get_visualizer_config(guild_id, "audiovis")
        assert config["style"] == "waveform"
        assert config["glow_intensity"] == 0.8
        # Defaults for non-overridden
        assert config["fft_bins"] == 7
        assert config["color_scheme"] == "neon"

    def test_unknown_engine_returns_empty_dict(self, guild_id):
        config = guild_settings.get_visualizer_config(guild_id, "nonexistent")
        assert config == {}

    def test_different_guilds_isolated(self):
        guild_a = 111
        guild_b = 222
        guild_settings.set_visualizer_config(guild_a, "dvd", "speed", 2.0)

        config_a = guild_settings.get_visualizer_config(guild_a, "dvd")
        config_b = guild_settings.get_visualizer_config(guild_b, "dvd")
        assert config_a["speed"] == 2.0
        assert config_b["speed"] == 1.0  # default


# ---------------------------------------------------------------------------
# set_visualizer_config
# ---------------------------------------------------------------------------


class TestSetVisualizerConfig:
    """Test set_visualizer_config validates and persists."""

    def test_valid_float_stored(self, guild_id):
        guild_settings.set_visualizer_config(guild_id, "projectm", "brightness", 1.8)
        config = guild_settings.get_visualizer_config(guild_id, "projectm")
        assert config["brightness"] == 1.8

    def test_valid_int_stored(self, guild_id):
        guild_settings.set_visualizer_config(guild_id, "fosfora", "particle_count", 8000)
        config = guild_settings.get_visualizer_config(guild_id, "fosfora")
        assert config["particle_count"] == 8000

    def test_valid_bool_stored(self, guild_id):
        guild_settings.set_visualizer_config(guild_id, "dvd", "hue_shift", False)
        config = guild_settings.get_visualizer_config(guild_id, "dvd")
        assert config["hue_shift"] is False

    def test_valid_choice_stored(self, guild_id):
        guild_settings.set_visualizer_config(guild_id, "audiovis", "style", "circular")
        config = guild_settings.get_visualizer_config(guild_id, "audiovis")
        assert config["style"] == "circular"

    def test_valid_string_stored(self, guild_id):
        guild_settings.set_visualizer_config(guild_id, "varda", "shader_name", "tunnel")
        config = guild_settings.get_visualizer_config(guild_id, "varda")
        assert config["shader_name"] == "tunnel"

    def test_invalid_engine_raises(self, guild_id):
        with pytest.raises(ValueError, match="Unknown engine"):
            guild_settings.set_visualizer_config(guild_id, "fake_engine", "speed", 1.0)

    def test_invalid_setting_raises(self, guild_id):
        with pytest.raises(ValueError, match="Unknown setting"):
            guild_settings.set_visualizer_config(guild_id, "dvd", "nonexistent", 1.0)

    def test_invalid_value_raises(self, guild_id):
        with pytest.raises(ValueError, match="above maximum"):
            guild_settings.set_visualizer_config(guild_id, "projectm", "brightness", 5.0)

    def test_value_normalized_before_storage(self, guild_id):
        # String "1" for int choice should be coerced to int 7
        guild_settings.set_visualizer_config(guild_id, "audiovis", "fft_bins", "64")
        config = guild_settings.get_visualizer_config(guild_id, "audiovis")
        assert config["fft_bins"] == 64
        assert isinstance(config["fft_bins"], int)

    def test_persists_to_disk(self, guild_id, tmp_path):
        guild_settings.set_visualizer_config(guild_id, "dvd", "speed", 2.5)

        # Reload from disk
        guild_settings.load()
        config = guild_settings.get_visualizer_config(guild_id, "dvd")
        assert config["speed"] == 2.5


# ---------------------------------------------------------------------------
# get_visualizer_presets
# ---------------------------------------------------------------------------


class TestGetVisualizerPresets:
    """Test get_visualizer_presets returns user presets dict."""

    def test_empty_when_no_presets(self, guild_id):
        presets = guild_settings.get_visualizer_presets(guild_id)
        assert presets == {}

    def test_returns_saved_presets(self, guild_id):
        preset_data = {"engine": "varda", "config": {"shader_name": "plasma", "speed": 1.5}}
        guild_settings.save_visualizer_preset(guild_id, "my-preset", preset_data)

        presets = guild_settings.get_visualizer_presets(guild_id)
        assert "my-preset" in presets
        assert presets["my-preset"]["engine"] == "varda"

    def test_returns_copy_not_reference(self, guild_id):
        preset_data = {"engine": "dvd", "config": {"speed": 2.0}}
        guild_settings.save_visualizer_preset(guild_id, "test", preset_data)

        presets = guild_settings.get_visualizer_presets(guild_id)
        presets["injected"] = {"engine": "hacked"}
        # Original unaffected
        presets2 = guild_settings.get_visualizer_presets(guild_id)
        assert "injected" not in presets2


# ---------------------------------------------------------------------------
# save_visualizer_preset
# ---------------------------------------------------------------------------


class TestSaveVisualizerPreset:
    """Test save_visualizer_preset stores engine + config."""

    def test_save_new_preset(self, guild_id):
        preset_data = {"engine": "fosfora", "config": {"particle_count": 6000}}
        guild_settings.save_visualizer_preset(guild_id, "party", preset_data)

        presets = guild_settings.get_visualizer_presets(guild_id)
        assert "party" in presets
        assert presets["party"]["engine"] == "fosfora"
        assert presets["party"]["config"]["particle_count"] == 6000

    def test_overwrite_existing_preset(self, guild_id):
        guild_settings.save_visualizer_preset(
            guild_id, "demo", {"engine": "dvd", "config": {"speed": 1.0}}
        )
        guild_settings.save_visualizer_preset(
            guild_id, "demo", {"engine": "varda", "config": {"speed": 2.0}}
        )

        presets = guild_settings.get_visualizer_presets(guild_id)
        assert presets["demo"]["engine"] == "varda"

    def test_multiple_presets_coexist(self, guild_id):
        guild_settings.save_visualizer_preset(
            guild_id, "a", {"engine": "dvd", "config": {}}
        )
        guild_settings.save_visualizer_preset(
            guild_id, "b", {"engine": "varda", "config": {}}
        )

        presets = guild_settings.get_visualizer_presets(guild_id)
        assert len(presets) == 2
        assert "a" in presets
        assert "b" in presets

    def test_persists_to_disk(self, guild_id, tmp_path):
        preset_data = {"engine": "audiovis", "config": {"style": "bars"}}
        guild_settings.save_visualizer_preset(guild_id, "saved", preset_data)

        guild_settings.load()
        presets = guild_settings.get_visualizer_presets(guild_id)
        assert "saved" in presets


# ---------------------------------------------------------------------------
# delete_visualizer_preset
# ---------------------------------------------------------------------------


class TestDeleteVisualizerPreset:
    """Test delete_visualizer_preset removes user presets."""

    def test_delete_existing_user_preset(self, guild_id):
        guild_settings.save_visualizer_preset(
            guild_id, "temp", {"engine": "dvd", "config": {}}
        )
        guild_settings.delete_visualizer_preset(guild_id, "temp")

        presets = guild_settings.get_visualizer_presets(guild_id)
        assert "temp" not in presets

    def test_delete_factory_preset_raises_valueerror(self, guild_id):
        # "milkdrop-classic" is a factory preset
        with pytest.raises(ValueError, match="Cannot delete factory preset"):
            guild_settings.delete_visualizer_preset(guild_id, "milkdrop-classic")

    def test_delete_nonexistent_preset_raises_keyerror(self, guild_id):
        with pytest.raises(KeyError):
            guild_settings.delete_visualizer_preset(guild_id, "nonexistent")

    def test_delete_from_guild_with_no_presets_raises_keyerror(self, guild_id):
        with pytest.raises(KeyError):
            guild_settings.delete_visualizer_preset(guild_id, "anything")

    def test_other_presets_unaffected_by_delete(self, guild_id):
        guild_settings.save_visualizer_preset(
            guild_id, "keep", {"engine": "dvd", "config": {}}
        )
        guild_settings.save_visualizer_preset(
            guild_id, "remove", {"engine": "varda", "config": {}}
        )
        guild_settings.delete_visualizer_preset(guild_id, "remove")

        presets = guild_settings.get_visualizer_presets(guild_id)
        assert "keep" in presets
        assert "remove" not in presets


# ---------------------------------------------------------------------------
# load_visualizer_preset
# ---------------------------------------------------------------------------


class TestLoadVisualizerPreset:
    """Test load_visualizer_preset checks user first, then factory."""

    def test_load_user_preset(self, guild_id):
        preset_data = {"engine": "fosfora", "config": {"gravity": 0.1}}
        guild_settings.save_visualizer_preset(guild_id, "custom", preset_data)

        result = guild_settings.load_visualizer_preset(guild_id, "custom")
        assert result == preset_data

    def test_load_factory_preset_fallback(self, guild_id):
        result = guild_settings.load_visualizer_preset(guild_id, "milkdrop-classic")
        assert result is not None
        assert result["engine"] == "projectm"
        assert result["factory"] is True

    def test_user_preset_shadows_factory(self, guild_id):
        # Save a user preset with the same name as a factory preset
        custom = {"engine": "dvd", "config": {"speed": 3.0}}
        guild_settings.save_visualizer_preset(guild_id, "milkdrop-classic", custom)

        result = guild_settings.load_visualizer_preset(guild_id, "milkdrop-classic")
        # User preset takes priority
        assert result["engine"] == "dvd"
        assert result["config"]["speed"] == 3.0

    def test_returns_none_for_unknown_preset(self, guild_id):
        result = guild_settings.load_visualizer_preset(guild_id, "totally-unknown")
        assert result is None

    def test_returns_none_for_empty_guild(self):
        result = guild_settings.load_visualizer_preset(999999, "anything")
        assert result is None


# ---------------------------------------------------------------------------
# Existing settings unaffected
# ---------------------------------------------------------------------------


class TestExistingSettingsUnaffected:
    """Verify visualizer config/presets don't break existing settings."""

    def test_mode_still_works(self, guild_id):
        guild_settings.set_guild_mode(guild_id, "allow_all")
        assert guild_settings.get_guild_mode(guild_id) == "allow_all"

    def test_visualizer_engine_still_works(self, guild_id):
        guild_settings.set_visualizer_engine(guild_id, "projectm")
        assert guild_settings.get_visualizer_engine(guild_id) == "projectm"

    def test_config_and_engine_coexist(self, guild_id):
        guild_settings.set_visualizer_engine(guild_id, "varda")
        guild_settings.set_visualizer_config(guild_id, "varda", "speed", 2.0)

        assert guild_settings.get_visualizer_engine(guild_id) == "varda"
        config = guild_settings.get_visualizer_config(guild_id, "varda")
        assert config["speed"] == 2.0

    def test_preset_and_mode_coexist(self, guild_id):
        guild_settings.set_guild_mode(guild_id, "allow_all")
        guild_settings.save_visualizer_preset(
            guild_id, "test", {"engine": "dvd", "config": {}}
        )

        assert guild_settings.get_guild_mode(guild_id) == "allow_all"
        presets = guild_settings.get_visualizer_presets(guild_id)
        assert "test" in presets


# ---------------------------------------------------------------------------
# validate_preset_name
# ---------------------------------------------------------------------------


class TestValidatePresetName:
    """Test validate_preset_name regex validation."""

    def test_valid_simple_name(self):
        assert guild_settings.validate_preset_name("my-preset") is True

    def test_valid_with_spaces(self):
        assert guild_settings.validate_preset_name("my cool preset") is True

    def test_valid_alphanumeric(self):
        assert guild_settings.validate_preset_name("Preset123") is True

    def test_valid_single_char(self):
        assert guild_settings.validate_preset_name("a") is True

    def test_valid_50_chars(self):
        assert guild_settings.validate_preset_name("a" * 50) is True

    def test_invalid_empty_string(self):
        assert guild_settings.validate_preset_name("") is False

    def test_invalid_51_chars(self):
        assert guild_settings.validate_preset_name("a" * 51) is False

    def test_invalid_special_chars(self):
        assert guild_settings.validate_preset_name("hello@world") is False

    def test_invalid_underscore(self):
        assert guild_settings.validate_preset_name("under_score") is False

    def test_invalid_newline(self):
        assert guild_settings.validate_preset_name("line\nbreak") is False

    def test_invalid_non_string(self):
        assert guild_settings.validate_preset_name(123) is False  # type: ignore
        assert guild_settings.validate_preset_name(None) is False  # type: ignore

    def test_valid_hyphens_and_spaces_mixed(self):
        assert guild_settings.validate_preset_name("Chill Bars - v2") is True


# ---------------------------------------------------------------------------
# get_user_presets
# ---------------------------------------------------------------------------


class TestGetUserPresets:
    """Test get_user_presets filters by engine."""

    def test_returns_empty_when_no_presets(self, guild_id):
        result = guild_settings.get_user_presets(guild_id, "audiovis")
        assert result == {}

    def test_filters_by_engine(self, guild_id):
        guild_settings.save_visualizer_preset(
            guild_id, "bars-glow", {"engine": "audiovis", "config": {"style": "bars"}, "factory": False}
        )
        guild_settings.save_visualizer_preset(
            guild_id, "plasma-fast", {"engine": "varda", "config": {"speed": 2.0}, "factory": False}
        )

        audiovis_presets = guild_settings.get_user_presets(guild_id, "audiovis")
        assert "bars-glow" in audiovis_presets
        assert "plasma-fast" not in audiovis_presets

        varda_presets = guild_settings.get_user_presets(guild_id, "varda")
        assert "plasma-fast" in varda_presets
        assert "bars-glow" not in varda_presets

    def test_returns_empty_for_engine_with_no_presets(self, guild_id):
        guild_settings.save_visualizer_preset(
            guild_id, "test", {"engine": "dvd", "config": {}, "factory": False}
        )
        result = guild_settings.get_user_presets(guild_id, "fosfora")
        assert result == {}

    def test_returns_empty_for_unknown_guild(self):
        result = guild_settings.get_user_presets(999999, "audiovis")
        assert result == {}


# ---------------------------------------------------------------------------
# save_user_preset
# ---------------------------------------------------------------------------


class TestSaveUserPreset:
    """Test save_user_preset with name validation."""

    def test_saves_valid_preset(self, guild_id):
        guild_settings.save_user_preset(
            guild_id, "chill-bars", "audiovis", {"style": "bars", "glow_intensity": 0.3}
        )
        presets = guild_settings.get_visualizer_presets(guild_id)
        assert "chill-bars" in presets
        assert presets["chill-bars"]["engine"] == "audiovis"
        assert presets["chill-bars"]["config"]["style"] == "bars"
        assert presets["chill-bars"]["factory"] is False

    def test_invalid_name_raises_valueerror(self, guild_id):
        with pytest.raises(ValueError, match="Invalid preset name"):
            guild_settings.save_user_preset(guild_id, "bad@name!", "audiovis", {})

    def test_empty_name_raises_valueerror(self, guild_id):
        with pytest.raises(ValueError, match="Invalid preset name"):
            guild_settings.save_user_preset(guild_id, "", "audiovis", {})

    def test_too_long_name_raises_valueerror(self, guild_id):
        with pytest.raises(ValueError, match="Invalid preset name"):
            guild_settings.save_user_preset(guild_id, "x" * 51, "audiovis", {})

    def test_stores_factory_false(self, guild_id):
        guild_settings.save_user_preset(guild_id, "test", "dvd", {"speed": 2.0})
        presets = guild_settings.get_visualizer_presets(guild_id)
        assert presets["test"]["factory"] is False

    def test_config_is_copied(self, guild_id):
        original_config = {"style": "bars"}
        guild_settings.save_user_preset(guild_id, "test", "audiovis", original_config)
        # Mutating original shouldn't affect stored
        original_config["injected"] = True
        presets = guild_settings.get_visualizer_presets(guild_id)
        assert "injected" not in presets["test"]["config"]

    def test_persists_to_disk(self, guild_id, tmp_path):
        guild_settings.save_user_preset(guild_id, "saved", "varda", {"speed": 1.0})
        guild_settings.load()
        presets = guild_settings.get_visualizer_presets(guild_id)
        assert "saved" in presets


# ---------------------------------------------------------------------------
# delete_user_preset
# ---------------------------------------------------------------------------


class TestDeleteUserPreset:
    """Test delete_user_preset with factory protection."""

    def test_deletes_existing_user_preset(self, guild_id):
        guild_settings.save_user_preset(guild_id, "temp", "dvd", {"speed": 1.0})
        guild_settings.delete_user_preset(guild_id, "temp")
        presets = guild_settings.get_visualizer_presets(guild_id)
        assert "temp" not in presets

    def test_factory_preset_raises_valueerror(self, guild_id):
        with pytest.raises(ValueError, match="Cannot delete factory preset"):
            guild_settings.delete_user_preset(guild_id, "neon-city")

    def test_nonexistent_preset_raises_valueerror(self, guild_id):
        with pytest.raises(ValueError, match="not found"):
            guild_settings.delete_user_preset(guild_id, "ghost")

    def test_no_guild_data_raises_valueerror(self):
        with pytest.raises(ValueError, match="not found"):
            guild_settings.delete_user_preset(999999, "anything")

    def test_other_presets_unaffected(self, guild_id):
        guild_settings.save_user_preset(guild_id, "keep", "audiovis", {"style": "bars"})
        guild_settings.save_user_preset(guild_id, "remove", "varda", {"speed": 1.0})
        guild_settings.delete_user_preset(guild_id, "remove")

        presets = guild_settings.get_visualizer_presets(guild_id)
        assert "keep" in presets
        assert "remove" not in presets

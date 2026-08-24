"""Tests for build_settings_schema() in engine_metadata.py.

Validates:
- Enriched schema entries contain correct fields (setting, type, label, default, current, min, max, group)
- Current values resolve from guild_config, falling back to defaults
- Labels are human-readable title-case
- Groups come from SETTING_GROUPS
- Choices included for choice-type settings
- Raises ValueError for unknown engine_id
"""

from __future__ import annotations

import pytest

from video.visualizer_engines.config_schema import ENGINE_CONFIG_SCHEMAS
from video.visualizer_engines.engine_metadata import (
    SETTING_GROUPS,
    build_settings_schema,
)


class TestBuildSettingsSchemaBasic:
    """Basic structure and field presence."""

    def test_returns_list_of_dicts(self):
        result = build_settings_schema("audiovis", {})
        assert isinstance(result, list)
        assert all(isinstance(entry, dict) for entry in result)

    def test_one_entry_per_setting(self):
        for engine_id in ENGINE_CONFIG_SCHEMAS:
            result = build_settings_schema(engine_id, {})
            expected_count = len(ENGINE_CONFIG_SCHEMAS[engine_id])
            assert len(result) == expected_count, (
                f"Engine {engine_id}: expected {expected_count} entries, got {len(result)}"
            )

    def test_required_keys_present(self):
        result = build_settings_schema("audiovis", {})
        required_keys = {"setting", "type", "label", "default", "current", "min", "max", "group"}
        for entry in result:
            assert required_keys.issubset(entry.keys()), (
                f"Missing keys in entry for {entry.get('setting')}: "
                f"{required_keys - entry.keys()}"
            )

    def test_unknown_engine_raises_valueerror(self):
        with pytest.raises(ValueError, match="Unknown engine"):
            build_settings_schema("nonexistent_engine", {})


class TestCurrentValueResolution:
    """Current value resolution from guild_config with fallback to defaults."""

    def test_current_from_guild_config(self):
        guild_config = {"glow_intensity": 0.9, "style": "waveform"}
        result = build_settings_schema("audiovis", guild_config)

        glow_entry = next(e for e in result if e["setting"] == "glow_intensity")
        assert glow_entry["current"] == 0.9

        style_entry = next(e for e in result if e["setting"] == "style")
        assert style_entry["current"] == "waveform"

    def test_current_falls_back_to_default(self):
        # Empty guild config — all currents should be defaults
        result = build_settings_schema("audiovis", {})
        for entry in result:
            assert entry["current"] == entry["default"], (
                f"Setting {entry['setting']}: current={entry['current']} != default={entry['default']}"
            )

    def test_partial_guild_config(self):
        guild_config = {"speed": 2.5}  # Only one setting overridden
        result = build_settings_schema("dvd", guild_config)

        speed_entry = next(e for e in result if e["setting"] == "speed")
        assert speed_entry["current"] == 2.5

        hue_entry = next(e for e in result if e["setting"] == "hue_shift")
        assert hue_entry["current"] is True  # default


class TestLabels:
    """Human-readable label generation."""

    def test_single_word(self):
        result = build_settings_schema("fosfora", {})
        gravity_entry = next(e for e in result if e["setting"] == "gravity")
        assert gravity_entry["label"] == "Gravity"

    def test_multi_word(self):
        result = build_settings_schema("audiovis", {})
        glow_entry = next(e for e in result if e["setting"] == "glow_intensity")
        assert glow_entry["label"] == "Glow Intensity"

    def test_fft_bins_label(self):
        result = build_settings_schema("audiovis", {})
        fft_entry = next(e for e in result if e["setting"] == "fft_bins")
        assert fft_entry["label"] == "Fft Bins"


class TestGroupAssignment:
    """Group assignment from SETTING_GROUPS."""

    def test_audiovis_groups(self):
        result = build_settings_schema("audiovis", {})
        for entry in result:
            expected_group = SETTING_GROUPS["audiovis"].get(entry["setting"])
            assert entry["group"] == expected_group

    def test_dvd_groups(self):
        result = build_settings_schema("dvd", {})
        speed_entry = next(e for e in result if e["setting"] == "speed")
        assert speed_entry["group"] == "Animation"

        hue_entry = next(e for e in result if e["setting"] == "hue_shift")
        assert hue_entry["group"] == "Visual"


class TestTypeAndConstraints:
    """Type, min, max, and choices fields."""

    def test_float_type_has_min_max(self):
        result = build_settings_schema("audiovis", {})
        glow_entry = next(e for e in result if e["setting"] == "glow_intensity")
        assert glow_entry["type"] == "float"
        assert glow_entry["min"] == 0.0
        assert glow_entry["max"] == 1.0

    def test_int_type_has_min_max(self):
        result = build_settings_schema("fosfora", {})
        particles_entry = next(e for e in result if e["setting"] == "particle_count")
        assert particles_entry["type"] == "int"
        assert particles_entry["min"] == 1000
        assert particles_entry["max"] == 10000

    def test_bool_type_no_min_max(self):
        result = build_settings_schema("dvd", {})
        hue_entry = next(e for e in result if e["setting"] == "hue_shift")
        assert hue_entry["type"] == "bool"
        assert hue_entry["min"] is None
        assert hue_entry["max"] is None

    def test_choice_type_includes_choices(self):
        result = build_settings_schema("audiovis", {})
        style_entry = next(e for e in result if e["setting"] == "style")
        assert style_entry["type"] == "choice"
        assert "choices" in style_entry
        assert "bars" in style_entry["choices"]

    def test_non_choice_type_no_choices_key(self):
        result = build_settings_schema("audiovis", {})
        glow_entry = next(e for e in result if e["setting"] == "glow_intensity")
        assert "choices" not in glow_entry

    def test_string_type_no_min_max(self):
        result = build_settings_schema("projectm", {})
        cat_entry = next(e for e in result if e["setting"] == "preset_category")
        assert cat_entry["type"] == "string"
        assert cat_entry["min"] is None
        assert cat_entry["max"] is None


class TestDesignDocExample:
    """Verify the exact example from the design document works."""

    def test_design_doc_glow_intensity_example(self):
        guild_config = {"style": "bars", "glow_intensity": 0.9}
        result = build_settings_schema("audiovis", guild_config)

        glow_entry = next(e for e in result if e["setting"] == "glow_intensity")
        assert glow_entry == {
            "setting": "glow_intensity",
            "type": "float",
            "label": "Glow Intensity",
            "default": 0.5,
            "current": 0.9,
            "min": 0.0,
            "max": 1.0,
            "group": "Visual",
        }

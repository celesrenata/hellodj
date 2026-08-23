"""Tests for factory presets data model.

Verifies: all preset configs validate against ENGINE_CONFIG_SCHEMAS,
is_factory_preset() correctness, get_factory_preset() structure,
list_factory_presets() filtering.
"""

from __future__ import annotations

import pytest

from video.visualizer_engines.config_schema import (
    ENGINE_CONFIG_SCHEMAS,
    validate_config_value,
)
from video.visualizer_engines.factory_presets import (
    FACTORY_PRESETS,
    get_factory_preset,
    is_factory_preset,
    list_factory_presets,
)


# --- Expected preset counts per engine ---

EXPECTED_COUNTS = {
    "projectm": 8,
    "audiovis": 7,
    "fosfora": 7,
    "varda": 10,
    "dvd": 4,
}

TOTAL_PRESETS = sum(EXPECTED_COUNTS.values())  # 36


# --- All preset configs validate against schema ---


class TestPresetConfigValidation:
    """Every factory preset's config values must validate against ENGINE_CONFIG_SCHEMAS."""

    @pytest.mark.parametrize(
        "preset_name",
        list(FACTORY_PRESETS.keys()),
    )
    def test_preset_config_validates(self, preset_name: str):
        preset = FACTORY_PRESETS[preset_name]
        engine = preset["engine"]
        config = preset["config"]

        assert engine in ENGINE_CONFIG_SCHEMAS, (
            f"Preset {preset_name!r} references unknown engine {engine!r}"
        )

        for setting, value in config.items():
            # Should not raise
            result = validate_config_value(engine, setting, value)
            assert result == value, (
                f"Preset {preset_name!r}: {engine}.{setting}={value!r} "
                f"normalized to {result!r} (expected identity)"
            )

    def test_all_preset_engines_exist_in_schema(self):
        engines_used = {p["engine"] for p in FACTORY_PRESETS.values()}
        for engine in engines_used:
            assert engine in ENGINE_CONFIG_SCHEMAS


# --- is_factory_preset() ---


class TestIsFactoryPreset:
    """Test is_factory_preset() returns correct boolean."""

    @pytest.mark.parametrize(
        "preset_name",
        list(FACTORY_PRESETS.keys()),
    )
    def test_returns_true_for_all_factory_names(self, preset_name: str):
        assert is_factory_preset(preset_name) is True

    @pytest.mark.parametrize(
        "name",
        [
            "nonexistent",
            "my-custom-preset",
            "",
            "MILKDROP-CLASSIC",  # case-sensitive
            "classic ",  # trailing space
            " classic",  # leading space
            "projectm",  # engine name, not preset name
        ],
    )
    def test_returns_false_for_non_factory_names(self, name: str):
        assert is_factory_preset(name) is False


# --- get_factory_preset() ---


class TestGetFactoryPreset:
    """Test get_factory_preset() returns correct structure or None."""

    def test_returns_none_for_unknown(self):
        assert get_factory_preset("nonexistent") is None

    def test_returns_dict_with_correct_keys(self):
        preset = get_factory_preset("classic")
        assert preset is not None
        assert set(preset.keys()) == {"engine", "config", "factory"}

    def test_factory_flag_is_true(self):
        preset = get_factory_preset("fractal-zoom")
        assert preset is not None
        assert preset["factory"] is True

    def test_engine_field_matches(self):
        preset = get_factory_preset("spectrum-bars")
        assert preset is not None
        assert preset["engine"] == "audiovis"

    def test_config_is_dict(self):
        preset = get_factory_preset("stardust")
        assert preset is not None
        assert isinstance(preset["config"], dict)

    def test_config_values_match_source(self):
        preset = get_factory_preset("fireworks")
        assert preset is not None
        assert preset["config"]["particle_count"] == 5000
        assert preset["config"]["gravity"] == 1.0
        assert preset["config"]["emission_style"] == "burst"
        assert preset["config"]["trail_length"] == 0.2

    def test_returns_copy_not_reference(self):
        """Mutation of returned preset must not affect factory data."""
        preset1 = get_factory_preset("classic")
        assert preset1 is not None
        preset1["config"]["speed"] = 999.0
        preset1["engine"] = "tampered"

        preset2 = get_factory_preset("classic")
        assert preset2 is not None
        assert preset2["config"]["speed"] == 1.0
        assert preset2["engine"] == "dvd"


# --- list_factory_presets() ---


class TestListFactoryPresets:
    """Test list_factory_presets() with and without engine filter."""

    def test_returns_all_presets_when_no_filter(self):
        all_presets = list_factory_presets()
        assert len(all_presets) == TOTAL_PRESETS

    def test_all_preset_names_present(self):
        all_presets = list_factory_presets()
        assert set(all_presets.keys()) == set(FACTORY_PRESETS.keys())

    @pytest.mark.parametrize(
        "engine,expected_count",
        list(EXPECTED_COUNTS.items()),
    )
    def test_filter_by_engine(self, engine: str, expected_count: int):
        presets = list_factory_presets(engine=engine)
        assert len(presets) == expected_count
        for name, preset in presets.items():
            assert preset["engine"] == engine

    def test_filter_unknown_engine_returns_empty(self):
        presets = list_factory_presets(engine="nonexistent")
        assert presets == {}

    def test_returned_presets_have_correct_structure(self):
        all_presets = list_factory_presets()
        for name, preset in all_presets.items():
            assert set(preset.keys()) == {"engine", "config", "factory"}
            assert preset["factory"] is True
            assert isinstance(preset["config"], dict)
            assert isinstance(preset["engine"], str)

    def test_returned_presets_are_copies(self):
        """Mutation of returned presets must not affect factory data."""
        presets = list_factory_presets(engine="dvd")
        for name, preset in presets.items():
            preset["config"]["speed"] = 999.0

        fresh = list_factory_presets(engine="dvd")
        for name, preset in fresh.items():
            assert preset["config"]["speed"] != 999.0


# --- Preset completeness checks ---


class TestPresetCompleteness:
    """Verify expected preset names are all present per engine."""

    def test_projectm_presets(self):
        presets = list_factory_presets(engine="projectm")
        expected = {
            "milkdrop-classic", "psychedelic", "chill", "trippy",
            "geometric", "space", "energy", "minimal",
        }
        assert set(presets.keys()) == expected

    def test_audiovis_presets(self):
        presets = list_factory_presets(engine="audiovis")
        expected = {
            "spectrum-bars", "full-spectrum", "waveform", "waterfall",
            "circular", "vinyl", "neon-city",
        }
        assert set(presets.keys()) == expected

    def test_fosfora_presets(self):
        presets = list_factory_presets(engine="fosfora")
        expected = {
            "stardust", "fireworks", "aurora", "vortex",
            "rain", "nebula", "pulse",
        }
        assert set(presets.keys()) == expected

    def test_varda_presets(self):
        presets = list_factory_presets(engine="varda")
        expected = {
            "fractal-zoom", "tunnel", "plasma", "voronoi-pulse",
            "raymarched-orbs", "kaleidoscope", "neon-grid",
            "star-field", "liquid-metal", "cosmic-web",
        }
        assert set(presets.keys()) == expected

    def test_dvd_presets(self):
        presets = list_factory_presets(engine="dvd")
        expected = {"classic", "fast", "slow", "no-hue"}
        assert set(presets.keys()) == expected

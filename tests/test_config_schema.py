"""Tests for engine configuration schema and validation.

Covers: valid inputs, type errors, range violations, invalid choices,
unknown engines, and unknown settings.
"""

from __future__ import annotations

import pytest

from video.visualizer_engines.config_schema import (
    ENGINE_CONFIG_SCHEMAS,
    get_default_config,
    get_setting_schema,
    validate_config_value,
)


# --- Schema structure tests ---


class TestSchemaStructure:
    """Verify schema dict has expected engines and settings."""

    def test_all_engines_present(self):
        expected = {"projectm", "audiovis", "fosfora", "varda", "dvd"}
        assert set(ENGINE_CONFIG_SCHEMAS.keys()) == expected

    def test_projectm_settings(self):
        settings = set(ENGINE_CONFIG_SCHEMAS["projectm"].keys())
        assert settings == {
            "preset_category", "blend_duration", "preset_duration",
            "brightness", "sensitivity",
        }

    def test_audiovis_settings(self):
        settings = set(ENGINE_CONFIG_SCHEMAS["audiovis"].keys())
        assert settings == {
            "style", "color_scheme", "fft_bins",
            "glow_intensity", "background_opacity",
        }

    def test_fosfora_settings(self):
        settings = set(ENGINE_CONFIG_SCHEMAS["fosfora"].keys())
        assert settings == {
            "particle_count", "gravity", "emission_style",
            "color_mode", "trail_length",
        }

    def test_varda_settings(self):
        settings = set(ENGINE_CONFIG_SCHEMAS["varda"].keys())
        assert settings == {"shader_name", "color_intensity", "speed", "complexity"}

    def test_dvd_settings(self):
        settings = set(ENGINE_CONFIG_SCHEMAS["dvd"].keys())
        assert settings == {"speed", "hue_shift", "icon_size"}

    def test_every_setting_has_type_and_default(self):
        for engine, settings in ENGINE_CONFIG_SCHEMAS.items():
            for name, schema in settings.items():
                assert "type" in schema, f"{engine}.{name} missing type"
                assert "default" in schema, f"{engine}.{name} missing default"


# --- validate_config_value: valid inputs ---


class TestValidateFloat:
    """Float validation with min/max constraints."""

    def test_valid_float_within_range(self):
        assert validate_config_value("projectm", "blend_duration", 5.0) == 5.0

    def test_float_at_min_boundary(self):
        assert validate_config_value("projectm", "blend_duration", 1.0) == 1.0

    def test_float_at_max_boundary(self):
        assert validate_config_value("projectm", "blend_duration", 10.0) == 10.0

    def test_int_coerced_to_float(self):
        result = validate_config_value("projectm", "brightness", 1)
        assert result == 1.0
        assert isinstance(result, float)

    def test_string_number_coerced_to_float(self):
        result = validate_config_value("fosfora", "gravity", "1.5")
        assert result == 1.5

    def test_float_below_min_raises(self):
        with pytest.raises(ValueError, match="below minimum"):
            validate_config_value("projectm", "blend_duration", 0.5)

    def test_float_above_max_raises(self):
        with pytest.raises(ValueError, match="above maximum"):
            validate_config_value("projectm", "blend_duration", 11.0)

    def test_non_numeric_raises(self):
        with pytest.raises(ValueError, match="requires a float"):
            validate_config_value("projectm", "brightness", "not_a_number")

    def test_none_raises(self):
        with pytest.raises(ValueError, match="requires a float"):
            validate_config_value("fosfora", "gravity", None)

    def test_zero_is_valid_when_min_is_zero(self):
        assert validate_config_value("fosfora", "gravity", 0.0) == 0.0
        assert validate_config_value("audiovis", "glow_intensity", 0.0) == 0.0


class TestValidateInt:
    """Int validation with min/max constraints."""

    def test_valid_int_within_range(self):
        assert validate_config_value("fosfora", "particle_count", 5000) == 5000

    def test_int_at_min_boundary(self):
        assert validate_config_value("fosfora", "particle_count", 1000) == 1000

    def test_int_at_max_boundary(self):
        assert validate_config_value("fosfora", "particle_count", 10000) == 10000

    def test_string_number_coerced_to_int(self):
        assert validate_config_value("dvd", "icon_size", "20") == 20

    def test_whole_float_coerced_to_int(self):
        assert validate_config_value("dvd", "icon_size", 20.0) == 20

    def test_non_whole_float_raises(self):
        with pytest.raises(ValueError, match="requires an int"):
            validate_config_value("dvd", "icon_size", 20.5)

    def test_int_below_min_raises(self):
        with pytest.raises(ValueError, match="below minimum"):
            validate_config_value("fosfora", "particle_count", 500)

    def test_int_above_max_raises(self):
        with pytest.raises(ValueError, match="above maximum"):
            validate_config_value("fosfora", "particle_count", 20000)

    def test_non_numeric_raises(self):
        with pytest.raises(ValueError, match="requires an int"):
            validate_config_value("dvd", "icon_size", "large")

    def test_bool_not_accepted_as_int(self):
        with pytest.raises(ValueError, match="requires an int"):
            validate_config_value("dvd", "icon_size", True)


class TestValidateBool:
    """Bool validation and coercion."""

    def test_true_value(self):
        assert validate_config_value("dvd", "hue_shift", True) is True

    def test_false_value(self):
        assert validate_config_value("dvd", "hue_shift", False) is False

    def test_string_true_variants(self):
        for val in ("true", "True", "TRUE", "1", "yes", "on"):
            assert validate_config_value("dvd", "hue_shift", val) is True

    def test_string_false_variants(self):
        for val in ("false", "False", "FALSE", "0", "no", "off"):
            assert validate_config_value("dvd", "hue_shift", val) is False

    def test_int_coercion(self):
        assert validate_config_value("dvd", "hue_shift", 1) is True
        assert validate_config_value("dvd", "hue_shift", 0) is False

    def test_invalid_string_raises(self):
        with pytest.raises(ValueError, match="requires a bool"):
            validate_config_value("dvd", "hue_shift", "maybe")


class TestValidateString:
    """String validation."""

    def test_valid_string(self):
        assert validate_config_value("projectm", "preset_category", "Abstract") == "Abstract"

    def test_string_stripped(self):
        assert validate_config_value("varda", "shader_name", "  plasma  ") == "plasma"

    def test_empty_string_raises(self):
        with pytest.raises(ValueError, match="cannot be empty"):
            validate_config_value("projectm", "preset_category", "")

    def test_whitespace_only_raises(self):
        with pytest.raises(ValueError, match="cannot be empty"):
            validate_config_value("projectm", "preset_category", "   ")

    def test_non_string_raises(self):
        with pytest.raises(ValueError, match="requires a string"):
            validate_config_value("varda", "shader_name", 123)


class TestValidateChoice:
    """Choice validation for string and int choices."""

    def test_valid_string_choice(self):
        assert validate_config_value("audiovis", "style", "bars") == "bars"
        assert validate_config_value("audiovis", "style", "waveform") == "waveform"

    def test_valid_int_choice(self):
        assert validate_config_value("audiovis", "fft_bins", 64) == 64
        assert validate_config_value("audiovis", "fft_bins", 512) == 512

    def test_int_choice_from_string(self):
        assert validate_config_value("audiovis", "fft_bins", "128") == 128

    def test_case_insensitive_string_choice(self):
        assert validate_config_value("audiovis", "style", "BARS") == "bars"
        assert validate_config_value("varda", "complexity", "HIGH") == "high"

    def test_invalid_choice_raises(self):
        with pytest.raises(ValueError, match="not a valid choice"):
            validate_config_value("audiovis", "style", "invalid_style")

    def test_invalid_int_choice_raises(self):
        with pytest.raises(ValueError, match="not a valid choice"):
            validate_config_value("audiovis", "fft_bins", 256)

    def test_fosfora_emission_styles(self):
        for style in ("burst", "stream", "rain", "fountain"):
            assert validate_config_value("fosfora", "emission_style", style) == style

    def test_fosfora_color_modes(self):
        for mode in ("spectrum", "mono", "gradient"):
            assert validate_config_value("fosfora", "color_mode", mode) == mode

    def test_varda_complexity_levels(self):
        for level in ("low", "medium", "high"):
            assert validate_config_value("varda", "complexity", level) == level


# --- Unknown engine/setting ---


class TestUnknownEngineOrSetting:
    """Verify proper errors for unknown engines/settings."""

    def test_unknown_engine_raises(self):
        with pytest.raises(ValueError, match="Unknown engine"):
            validate_config_value("nonexistent", "speed", 1.0)

    def test_unknown_setting_raises(self):
        with pytest.raises(ValueError, match="Unknown setting"):
            validate_config_value("dvd", "nonexistent_setting", 1.0)

    def test_unknown_setting_lists_valid_ones(self):
        with pytest.raises(ValueError, match="Valid settings"):
            validate_config_value("dvd", "bogus", 1.0)


# --- get_default_config ---


class TestGetDefaultConfig:
    """Test default config retrieval."""

    def test_projectm_defaults(self):
        defaults = get_default_config("projectm")
        assert defaults == {
            "preset_category": "all",
            "blend_duration": 3.0,
            "preset_duration": 30.0,
            "brightness": 1.0,
            "sensitivity": 1.0,
        }

    def test_audiovis_defaults(self):
        defaults = get_default_config("audiovis")
        assert defaults["style"] == "bars"
        assert defaults["fft_bins"] == 7
        assert defaults["glow_intensity"] == 0.5

    def test_dvd_defaults(self):
        defaults = get_default_config("dvd")
        assert defaults == {
            "speed": 1.0,
            "hue_shift": True,
            "icon_size": 15,
        }

    def test_unknown_engine_raises(self):
        with pytest.raises(ValueError, match="Unknown engine"):
            get_default_config("nonexistent")

    def test_all_defaults_validate(self):
        """Every engine's defaults must pass their own validation."""
        for engine in ENGINE_CONFIG_SCHEMAS:
            defaults = get_default_config(engine)
            for setting, value in defaults.items():
                result = validate_config_value(engine, setting, value)
                assert result == value, (
                    f"Default {engine}.{setting}={value!r} failed validation"
                )


# --- get_setting_schema ---


class TestGetSettingSchema:
    """Test schema introspection for autocomplete."""

    def test_returns_schema_dict(self):
        schema = get_setting_schema("projectm", "blend_duration")
        assert schema["type"] == "float"
        assert schema["default"] == 3.0
        assert schema["min"] == 1.0
        assert schema["max"] == 10.0

    def test_choice_schema_has_choices(self):
        schema = get_setting_schema("audiovis", "style")
        assert schema["type"] == "choice"
        assert "bars" in schema["choices"]
        assert len(schema["choices"]) == 18

    def test_bool_schema(self):
        schema = get_setting_schema("dvd", "hue_shift")
        assert schema["type"] == "bool"
        assert schema["default"] is True

    def test_returns_copy_not_reference(self):
        schema1 = get_setting_schema("dvd", "speed")
        schema1["extra_key"] = "tampered"
        schema2 = get_setting_schema("dvd", "speed")
        assert "extra_key" not in schema2

    def test_unknown_engine_raises(self):
        with pytest.raises(ValueError, match="Unknown engine"):
            get_setting_schema("bogus", "speed")

    def test_unknown_setting_raises(self):
        with pytest.raises(ValueError, match="Unknown setting"):
            get_setting_schema("dvd", "bogus")

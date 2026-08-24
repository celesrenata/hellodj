"""Engine configuration schema and validation for GPU visualizer engines.

Defines per-engine configurable parameters with types, defaults, and constraints.
Provides validation, default retrieval, and schema introspection for autocomplete.
"""

from __future__ import annotations

from typing import Any


# Schema entry structure:
#   "setting_name": {
#       "type": "string" | "float" | "int" | "bool" | "choice",
#       "default": <value>,
#       "min": <number>,           # for float/int
#       "max": <number>,           # for float/int
#       "choices": [<values>],     # for choice type
#   }

ENGINE_CONFIG_SCHEMAS: dict[str, dict[str, dict[str, Any]]] = {
    "projectm": {
        "preset_category": {
            "type": "string",
            "default": "all",
        },
        "blend_duration": {
            "type": "float",
            "default": 3.0,
            "min": 1.0,
            "max": 10.0,
        },
        "preset_duration": {
            "type": "float",
            "default": 30.0,
            "min": 10.0,
            "max": 300.0,
        },
        "brightness": {
            "type": "float",
            "default": 1.0,
            "min": 0.5,
            "max": 2.0,
        },
        "sensitivity": {
            "type": "float",
            "default": 1.0,
            "min": 0.5,
            "max": 2.0,
        },
    },
    "audiovis": {
        "style": {
            "type": "choice",
            "default": "bars",
            "choices": [
                # Classic
                "bars", "waveform", "waterfall", "circular",
                # Psychedelic
                "kaleidoscope", "plasma", "fractal", "hypnotic",
                # Aggressive
                "glitch", "storm", "shatter",
                # Ambient
                "aurora", "nebula", "ocean", "fireflies",
                # Retro
                "synthwave", "retrowave", "cyber",
            ],
        },
        "color_scheme": {
            "type": "string",
            "default": "neon",
        },
        "fft_bins": {
            "type": "choice",
            "default": 7,
            "choices": [7, 32, 64, 128, 512],
        },
        "glow_intensity": {
            "type": "float",
            "default": 0.5,
            "min": 0.0,
            "max": 1.0,
        },
        "background_opacity": {
            "type": "float",
            "default": 0.9,
            "min": 0.0,
            "max": 1.0,
        },
    },
    "fosfora": {
        "particle_count": {
            "type": "int",
            "default": 5000,
            "min": 1000,
            "max": 10000,
        },
        "gravity": {
            "type": "float",
            "default": 0.5,
            "min": 0.0,
            "max": 2.0,
        },
        "emission_style": {
            "type": "choice",
            "default": "burst",
            "choices": ["burst", "stream", "rain", "fountain"],
        },
        "color_mode": {
            "type": "choice",
            "default": "spectrum",
            "choices": ["spectrum", "mono", "gradient"],
        },
        "trail_length": {
            "type": "float",
            "default": 0.3,
            "min": 0.0,
            "max": 1.0,
        },
    },
    "varda": {
        "shader_name": {
            "type": "string",
            "default": "plasma",
        },
        "color_intensity": {
            "type": "float",
            "default": 1.0,
            "min": 0.5,
            "max": 2.0,
        },
        "speed": {
            "type": "float",
            "default": 1.0,
            "min": 0.25,
            "max": 4.0,
        },
        "complexity": {
            "type": "choice",
            "default": "medium",
            "choices": ["low", "medium", "high"],
        },
    },
    "dvd": {
        "speed": {
            "type": "float",
            "default": 1.0,
            "min": 0.5,
            "max": 3.0,
        },
        "hue_shift": {
            "type": "bool",
            "default": True,
        },
        "icon_size": {
            "type": "int",
            "default": 15,
            "min": 10,
            "max": 30,
        },
    },
}


def validate_config_value(engine: str, setting: str, value: Any) -> Any:
    """Validate and normalize a configuration value for a given engine setting.

    Args:
        engine: Engine name (e.g. "projectm", "audiovis").
        setting: Setting name within the engine schema.
        value: The value to validate.

    Returns:
        The normalized value (coerced to correct type if possible).

    Raises:
        ValueError: If the engine or setting is unknown, or the value is invalid.
    """
    if engine not in ENGINE_CONFIG_SCHEMAS:
        raise ValueError(f"Unknown engine: {engine!r}")

    schema = ENGINE_CONFIG_SCHEMAS[engine]
    if setting not in schema:
        raise ValueError(
            f"Unknown setting {setting!r} for engine {engine!r}. "
            f"Valid settings: {list(schema.keys())}"
        )

    setting_schema = schema[setting]
    setting_type = setting_schema["type"]

    if setting_type == "float":
        return _validate_float(value, setting, setting_schema)
    elif setting_type == "int":
        return _validate_int(value, setting, setting_schema)
    elif setting_type == "bool":
        return _validate_bool(value, setting)
    elif setting_type == "string":
        return _validate_string(value, setting)
    elif setting_type == "choice":
        return _validate_choice(value, setting, setting_schema)
    else:
        raise ValueError(f"Unknown schema type: {setting_type!r}")


def get_default_config(engine: str) -> dict[str, Any]:
    """Return a dict of all default values for an engine.

    Args:
        engine: Engine name.

    Returns:
        Dict mapping setting names to their default values.

    Raises:
        ValueError: If the engine is unknown.
    """
    if engine not in ENGINE_CONFIG_SCHEMAS:
        raise ValueError(f"Unknown engine: {engine!r}")

    return {
        setting: schema["default"]
        for setting, schema in ENGINE_CONFIG_SCHEMAS[engine].items()
    }


def get_setting_schema(engine: str, setting: str) -> dict[str, Any]:
    """Return the full schema dict for a specific engine setting.

    Useful for autocomplete and UI display.

    Args:
        engine: Engine name.
        setting: Setting name.

    Returns:
        Schema dict with type, default, and constraints.

    Raises:
        ValueError: If the engine or setting is unknown.
    """
    if engine not in ENGINE_CONFIG_SCHEMAS:
        raise ValueError(f"Unknown engine: {engine!r}")

    schema = ENGINE_CONFIG_SCHEMAS[engine]
    if setting not in schema:
        raise ValueError(
            f"Unknown setting {setting!r} for engine {engine!r}. "
            f"Valid settings: {list(schema.keys())}"
        )

    return dict(schema[setting])


# --- Internal validators ---


def _validate_float(value: Any, setting: str, schema: dict) -> float:
    """Validate and coerce a float value within min/max bounds."""
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise ValueError(
            f"Setting {setting!r} requires a float, got {type(value).__name__!r}"
        )

    min_val = schema.get("min")
    max_val = schema.get("max")

    if min_val is not None and result < min_val:
        raise ValueError(
            f"Setting {setting!r} value {result} is below minimum {min_val}"
        )
    if max_val is not None and result > max_val:
        raise ValueError(
            f"Setting {setting!r} value {result} is above maximum {max_val}"
        )

    return result


def _validate_int(value: Any, setting: str, schema: dict) -> int:
    """Validate and coerce an int value within min/max bounds."""
    # Accept int or float that is a whole number
    if isinstance(value, bool):
        raise ValueError(
            f"Setting {setting!r} requires an int, got 'bool'"
        )
    try:
        if isinstance(value, float):
            if value != int(value):
                raise ValueError(
                    f"Setting {setting!r} requires an int, got float {value}"
                )
            result = int(value)
        else:
            result = int(value)
    except (TypeError, ValueError) as e:
        if "requires an int" in str(e):
            raise
        raise ValueError(
            f"Setting {setting!r} requires an int, got {type(value).__name__!r}"
        )

    min_val = schema.get("min")
    max_val = schema.get("max")

    if min_val is not None and result < min_val:
        raise ValueError(
            f"Setting {setting!r} value {result} is below minimum {min_val}"
        )
    if max_val is not None and result > max_val:
        raise ValueError(
            f"Setting {setting!r} value {result} is above maximum {max_val}"
        )

    return result


def _validate_bool(value: Any, setting: str) -> bool:
    """Validate and coerce a boolean value."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lower = value.lower()
        if lower in ("true", "1", "yes", "on"):
            return True
        if lower in ("false", "0", "no", "off"):
            return False
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return bool(value)
    raise ValueError(
        f"Setting {setting!r} requires a bool, got {type(value).__name__!r}: {value!r}"
    )


def _validate_string(value: Any, setting: str) -> str:
    """Validate a string value (non-empty)."""
    if not isinstance(value, str):
        raise ValueError(
            f"Setting {setting!r} requires a string, got {type(value).__name__!r}"
        )
    if not value.strip():
        raise ValueError(f"Setting {setting!r} cannot be empty")
    return value.strip()


def _validate_choice(value: Any, setting: str, schema: dict) -> Any:
    """Validate a value against a fixed set of choices."""
    choices = schema["choices"]
    # Try direct match first
    if value in choices:
        return value
    # Try type coercion for numeric choices
    if all(isinstance(c, int) for c in choices):
        try:
            int_val = int(value)
            if int_val in choices:
                return int_val
        except (TypeError, ValueError):
            pass
    # Try string matching for string choices
    if all(isinstance(c, str) for c in choices) and isinstance(value, str):
        lower = value.lower()
        for choice in choices:
            if choice.lower() == lower:
                return choice
    raise ValueError(
        f"Setting {setting!r} value {value!r} is not a valid choice. "
        f"Valid choices: {choices}"
    )

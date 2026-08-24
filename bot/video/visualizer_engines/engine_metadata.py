"""Engine metadata and setting group definitions for the visualizer menu system.

Provides static metadata for each visualizer engine (display name, description, icon,
rendering mode) and logical setting groupings for the settings panel UI.

Used by the WS hub to serve menu_init_response and settings_schema_response messages.
"""

from __future__ import annotations

from video.visualizer_engines.config_schema import ENGINE_CONFIG_SCHEMAS


ENGINE_METADATA: dict[str, dict] = {
    "projectm": {
        "name": "ProjectM",
        "description": "Milkdrop-compatible music visualizer presets",
        "icon": "projectm",
        "server_rendered": True,
    },
    "audiovis": {
        "name": "AudioVis",
        "description": "Spectrum analyzer with bars, waveforms, and effects",
        "icon": "audiovis",
        "server_rendered": True,
    },
    "fosfora": {
        "name": "Fosfora",
        "description": "GPU particle system driven by audio energy",
        "icon": "fosfora",
        "server_rendered": True,
    },
    "varda": {
        "name": "Varda",
        "description": "GLSL shader gallery with audio-reactive effects",
        "icon": "varda",
        "server_rendered": True,
    },
    "drift": {
        "name": "Drift",
        "description": "Multipass feedback visualizer with organic trails",
        "icon": "drift",
        "server_rendered": True,
    },
    "dvd": {
        "name": "DVD Bounce",
        "description": "Classic bouncing logo screensaver with hue shift",
        "icon": "dvd",
        "server_rendered": False,
    },
}


SETTING_GROUPS: dict[str, dict[str, str]] = {
    "projectm": {
        "preset_category": "Presets",
        "blend_duration": "Transitions",
        "preset_duration": "Transitions",
        "brightness": "Visual",
        "sensitivity": "Audio",
    },
    "audiovis": {
        "style": "Style",
        "color_scheme": "Style",
        "fft_bins": "Audio",
        "glow_intensity": "Visual",
        "background_opacity": "Visual",
    },
    "fosfora": {
        "particle_count": "Particles",
        "gravity": "Physics",
        "emission_style": "Particles",
        "color_mode": "Visual",
        "trail_length": "Visual",
    },
    "varda": {
        "shader_name": "Shader",
        "color_intensity": "Visual",
        "speed": "Animation",
        "complexity": "Quality",
    },
    "drift": {
        "warp_zoom": "Warp",
        "rotation": "Warp",
        "decay": "Feedback",
        "bloom_intensity": "Visual",
        "wave_enabled": "Composite",
    },
    "dvd": {
        "speed": "Animation",
        "hue_shift": "Visual",
        "icon_size": "Visual",
    },
}


def get_engine_metadata(engine_id: str) -> dict | None:
    """Return metadata for a specific engine.

    Args:
        engine_id: Engine identifier (e.g. "projectm", "audiovis").

    Returns:
        A copy of the engine metadata dict, or None if the engine is unknown.
    """
    meta = ENGINE_METADATA.get(engine_id)
    if meta is None:
        return None
    return dict(meta)


def get_setting_group(engine_id: str, setting_key: str) -> str | None:
    """Return the display group label for a setting within an engine.

    Args:
        engine_id: Engine identifier.
        setting_key: The setting name (e.g. "glow_intensity").

    Returns:
        The group label string (e.g. "Visual"), or None if the engine
        or setting is not found in SETTING_GROUPS.
    """
    engine_groups = SETTING_GROUPS.get(engine_id)
    if engine_groups is None:
        return None
    return engine_groups.get(setting_key)


def build_settings_schema(engine_id: str, guild_config: dict) -> list[dict]:
    """Build an enriched settings schema for an engine merged with current guild values.

    For each setting defined in ENGINE_CONFIG_SCHEMAS[engine_id], produces a dict with:
    - setting: the setting key name
    - type: the schema type (float, int, bool, choice, string)
    - label: human-readable label derived from the setting name
    - default: the default value from the schema
    - current: the guild's current value (from guild_config), falling back to default
    - min: minimum bound (None if not applicable)
    - max: maximum bound (None if not applicable)
    - group: the display group from SETTING_GROUPS (None if not mapped)
    - choices: list of valid choices (only present for choice-type settings)

    Args:
        engine_id: Engine identifier (e.g. "audiovis", "projectm").
        guild_config: The guild's current config dict for this engine
            (e.g. {"style": "bars", "glow_intensity": 0.9}).

    Returns:
        A list of enriched schema entry dicts, one per setting.

    Raises:
        ValueError: If the engine_id is not found in ENGINE_CONFIG_SCHEMAS.
    """
    if engine_id not in ENGINE_CONFIG_SCHEMAS:
        valid = ", ".join(sorted(ENGINE_CONFIG_SCHEMAS.keys()))
        raise ValueError(
            f"Unknown engine: {engine_id!r}. Valid engines: {valid}"
        )

    schema = ENGINE_CONFIG_SCHEMAS[engine_id]
    engine_groups = SETTING_GROUPS.get(engine_id, {})
    result: list[dict] = []

    for setting_key, setting_schema in schema.items():
        entry: dict = {
            "setting": setting_key,
            "type": setting_schema["type"],
            "label": _setting_label(setting_key),
            "default": setting_schema["default"],
            "current": guild_config.get(setting_key, setting_schema["default"]),
            "min": setting_schema.get("min"),
            "max": setting_schema.get("max"),
            "group": engine_groups.get(setting_key),
        }

        # Include choices for choice-type settings
        if setting_schema["type"] == "choice" and "choices" in setting_schema:
            entry["choices"] = setting_schema["choices"]

        result.append(entry)

    return result


def _setting_label(key: str) -> str:
    """Convert a snake_case setting key to a human-readable title-case label.

    Examples:
        "glow_intensity" -> "Glow Intensity"
        "fft_bins" -> "Fft Bins"
        "color_scheme" -> "Color Scheme"
    """
    return key.replace("_", " ").title()


# --- Preset tag generation ---

# Keys considered "interesting" or distinguishing — prioritized for tag generation.
# These are style/mode/qualitative choices that best describe a preset's character.
_PRIORITY_KEYS: list[str] = [
    "style",
    "color_scheme",
    "color_mode",
    "emission_style",
    "shader_name",
    "preset_category",
    "complexity",
]


def generate_preset_tags(config: dict) -> list[str]:
    """Generate up to 4 human-readable metadata tags from a preset config.

    Tags are derived from the config's key-value pairs, prioritizing
    qualitative/distinguishing settings (style, color, mode) over
    plain numeric values.

    Rules:
    - String values: use the value directly (e.g., "bars", "synthwave")
    - Numeric values (int/float): format with key context
      (e.g., "32 bins" for fft_bins=32, "glow: 0.9" for glow_intensity=0.9)
    - Boolean values: include a humanized key name when True, skip when False
    - At most 4 tags returned

    Args:
        config: A preset configuration dictionary mapping setting names to values.

    Returns:
        A list of up to 4 human-readable tag strings.
    """
    if not config:
        return []

    priority_tags: list[str] = []
    secondary_tags: list[str] = []

    for key, value in config.items():
        tag = _format_tag(key, value)
        if tag is None:
            continue

        if key in _PRIORITY_KEYS:
            priority_tags.append(tag)
        else:
            secondary_tags.append(tag)

    # Combine priority first, then secondary, capped at 4
    combined = priority_tags + secondary_tags
    return combined[:4]


def _format_tag(key: str, value) -> str | None:
    """Format a single config key-value pair into a human-readable tag.

    Returns None if the value should not produce a tag (e.g., False booleans).
    """
    if isinstance(value, bool):
        if value:
            # "hue_shift" -> "hue shift"
            return _humanize_key(key)
        return None

    if isinstance(value, str):
        return value

    if isinstance(value, int):
        # e.g., fft_bins=32 -> "32 bins"
        short_key = _short_key_label(key)
        return f"{value} {short_key}"

    if isinstance(value, float):
        # e.g., glow_intensity=0.9 -> "glow: 0.9"
        short_key = _short_key_label(key)
        formatted = f"{value:g}"
        return f"{short_key}: {formatted}"

    # Fallback for unexpected types
    return str(value)


def _humanize_key(key: str) -> str:
    """Convert a snake_case key to a human-readable label.

    Examples:
        "hue_shift" -> "hue shift"
        "glow_intensity" -> "glow intensity"
    """
    return key.replace("_", " ")


# Specific short forms for common keys used in numeric tag formatting
_SHORT_FORMS: dict[str, str] = {
    "fft_bins": "bins",
    "glow_intensity": "glow",
    "particle_count": "particles",
    "background_opacity": "bg opacity",
    "trail_length": "trail",
    "blend_duration": "blend",
    "preset_duration": "duration",
    "icon_size": "icon size",
    "color_intensity": "color",
    "brightness": "brightness",
    "sensitivity": "sensitivity",
    "gravity": "gravity",
    "speed": "speed",
    "bloom_intensity": "bloom",
    "warp_zoom": "zoom",
    "rotation": "rotation",
    "decay": "decay",
}


def _short_key_label(key: str) -> str:
    """Produce a short label from a snake_case key for numeric tag formatting.

    Examples:
        "fft_bins" -> "bins"
        "glow_intensity" -> "glow"
        "particle_count" -> "particles"
    """
    if key in _SHORT_FORMS:
        return _SHORT_FORMS[key]

    # Generic fallback: take the last segment of snake_case
    parts = key.split("_")
    if len(parts) >= 2:
        return parts[-1]
    return key

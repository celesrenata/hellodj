"""HelloDJ — Per-guild settings module.

Stores guild-level configuration in data/guild_settings.json.
Currently supports:
  - mode: "restrictive" (default) or "allow_all"
  - visualizer_engine: one of dvd, projectm, vgalizer, varda, fosfora, audiovis, native, random, off (default: "dvd")

Shape of data/guild_settings.json:
    { "<guild_id>": { "mode": "restrictive" | "allow_all", "visualizer_engine": "dvd" | ... }, ... }
"""

import json
import logging
import os

log = logging.getLogger(__name__)

GUILD_SETTINGS_FILE = "data/guild_settings.json"

# Guild → dict of settings
_settings: dict[int, dict] = {}


def load() -> None:
    """Load data/guild_settings.json into memory."""
    os.makedirs("data", exist_ok=True)
    if not os.path.exists(GUILD_SETTINGS_FILE):
        log.info("HelloDJ: %s not found — starting with empty guild settings.", GUILD_SETTINGS_FILE)
        _settings.clear()
        return

    try:
        with open(GUILD_SETTINGS_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        log.error(
            "HelloDJ could not read %s (%s); keeping current settings.",
            GUILD_SETTINGS_FILE, exc,
        )
        return

    new: dict[int, dict] = {}
    for gid_str, data in raw.items():
        try:
            gid = int(gid_str)
        except (ValueError, TypeError):
            continue
        new[gid] = data if isinstance(data, dict) else {}

    _settings.clear()
    _settings.update(new)
    log.info("HelloDJ guild settings loaded %d guild entries from %s", len(_settings), GUILD_SETTINGS_FILE)


def reload() -> None:
    """Reload guild settings from disk (same as load())."""
    load()


def save() -> None:
    """Write the in-memory settings to data/guild_settings.json atomically."""
    os.makedirs("data", exist_ok=True)
    tmp = f"{GUILD_SETTINGS_FILE}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_settings, f, ensure_ascii=False, indent=2)
        os.replace(tmp, GUILD_SETTINGS_FILE)
        log.info("HelloDJ guild settings saved to %s (%d guild entries)", GUILD_SETTINGS_FILE, len(_settings))
    except OSError as exc:
        log.error("HelloDJ could not write %s (%s)", GUILD_SETTINGS_FILE, exc)


def get_setting(guild_id: int, key: str, default=None):
    """Get a guild setting by key. Returns default if the key is not set."""
    guild_settings = _settings.get(guild_id)
    if guild_settings is None:
        return default
    return guild_settings.get(key, default)


def set_setting(guild_id: int, key: str, value) -> None:
    """Set a guild setting and persist to disk."""
    if guild_id not in _settings:
        _settings[guild_id] = {}
    _settings[guild_id][key] = value
    save()


def get_guild_mode(guild_id: int) -> str:
    """Return the restriction mode for a guild: 'restrictive' or 'allow_all'.

    Default is 'restrictive' (only blacklisted users are blocked).
    """
    mode = get_setting(guild_id, "mode", "restrictive")
    if mode not in ("restrictive", "allow_all"):
        return "restrictive"
    return mode


def set_guild_mode(guild_id: int, mode: str) -> None:
    """Set the restriction mode for a guild and persist."""
    if mode not in ("restrictive", "allow_all"):
        raise ValueError(f"Invalid mode '{mode}'; must be 'restrictive' or 'allow_all'")
    set_setting(guild_id, "mode", mode)


# ---------------------------------------------------------------------------
# Visualizer engine configuration
# ---------------------------------------------------------------------------

VALID_VISUALIZER_ENGINES: set[str] = {
    "dvd", "projectm", "varda", "fosfora", "audiovis", "native", "random", "off",
}

DEFAULT_VISUALIZER_ENGINE: str = "dvd"


def get_visualizer_engine(guild_id: int) -> str:
    """Return the configured visualizer engine for a guild.

    Falls back to DEFAULT_VISUALIZER_ENGINE if the stored value is missing,
    invalid, or references the removed "vgalizer" engine (legacy configs).
    """
    engine = get_setting(guild_id, "visualizer_engine", DEFAULT_VISUALIZER_ENGINE)
    if engine == "vgalizer" or engine not in VALID_VISUALIZER_ENGINES:
        return DEFAULT_VISUALIZER_ENGINE
    return engine


def set_visualizer_engine(guild_id: int, engine: str) -> None:
    """Set the visualizer engine for a guild and persist.

    Raises ValueError if the engine is not in VALID_VISUALIZER_ENGINES.
    """
    if engine not in VALID_VISUALIZER_ENGINES:
        raise ValueError(
            f"Invalid visualizer engine '{engine}'; must be one of: {', '.join(sorted(VALID_VISUALIZER_ENGINES))}"
        )
    set_setting(guild_id, "visualizer_engine", engine)


# ---------------------------------------------------------------------------
# Visualizer per-engine configuration
# ---------------------------------------------------------------------------


def get_visualizer_config(guild_id: int, engine: str) -> dict:
    """Return the merged configuration for a given engine in a guild.

    Merges schema defaults with any stored overrides. Unknown engines
    return an empty dict (the schema module handles validation).

    Args:
        guild_id: The guild to retrieve config for.
        engine: Engine name (e.g. "projectm", "audiovis").

    Returns:
        Dict mapping setting names to their effective values.
    """
    from video.visualizer_engines.config_schema import get_default_config

    try:
        defaults = get_default_config(engine)
    except ValueError:
        return {}

    stored = _get_guild_nested(guild_id, "visualizer_config", engine)
    if stored and isinstance(stored, dict):
        merged = dict(defaults)
        merged.update(stored)
        return merged
    return dict(defaults)


def set_visualizer_config(guild_id: int, engine: str, setting: str, value) -> None:
    """Validate and store a single config setting for an engine.

    Validates the value against the engine's schema before persisting.

    Args:
        guild_id: The guild to update.
        engine: Engine name.
        setting: Setting key within the engine schema.
        value: The value to set (will be validated and normalized).

    Raises:
        ValueError: If engine, setting, or value is invalid per schema.
    """
    from video.visualizer_engines.config_schema import validate_config_value

    normalized = validate_config_value(engine, setting, value)
    _set_guild_nested(guild_id, "visualizer_config", engine, setting, normalized)


# ---------------------------------------------------------------------------
# Visualizer presets (user-saved per guild)
# ---------------------------------------------------------------------------


def get_visualizer_presets(guild_id: int) -> dict:
    """Return all user-saved visualizer presets for a guild.

    Returns:
        Dict mapping preset names to their preset data
        (each entry: {"engine": str, "config": dict}).
    """
    guild_data = _settings.get(guild_id)
    if guild_data is None:
        return {}
    presets = guild_data.get("visualizer_presets")
    if presets is None or not isinstance(presets, dict):
        return {}
    return dict(presets)


def save_visualizer_preset(guild_id: int, name: str, preset_data: dict) -> None:
    """Save a named visualizer preset for a guild.

    Args:
        guild_id: The guild to save in.
        name: Preset name (user-chosen).
        preset_data: Dict with at least "engine" and "config" keys.
    """
    if guild_id not in _settings:
        _settings[guild_id] = {}
    if "visualizer_presets" not in _settings[guild_id]:
        _settings[guild_id]["visualizer_presets"] = {}
    _settings[guild_id]["visualizer_presets"][name] = preset_data
    save()


def delete_visualizer_preset(guild_id: int, name: str) -> None:
    """Delete a user-saved visualizer preset.

    Raises ValueError if the name is a factory preset (factory presets
    cannot be deleted).

    Args:
        guild_id: The guild to delete from.
        name: Preset name to remove.

    Raises:
        ValueError: If the preset is a factory preset.
        KeyError: If the preset does not exist in user presets.
    """
    from video.visualizer_engines.factory_presets import is_factory_preset

    if is_factory_preset(name):
        raise ValueError(
            f"Cannot delete factory preset '{name}'. "
            "Factory presets are immutable."
        )

    guild_data = _settings.get(guild_id)
    if guild_data is None:
        raise KeyError(f"No presets found for guild {guild_id}")

    presets = guild_data.get("visualizer_presets")
    if presets is None or name not in presets:
        raise KeyError(f"Preset '{name}' not found for guild {guild_id}")

    del presets[name]
    save()


def load_visualizer_preset(guild_id: int, name: str) -> dict | None:
    """Load a visualizer preset by name.

    Checks user presets first, then falls back to factory presets.

    Args:
        guild_id: The guild to look up.
        name: Preset name.

    Returns:
        Preset data dict ({"engine": ..., "config": ...}), or None if not found.
    """
    from video.visualizer_engines.factory_presets import get_factory_preset

    # Check user presets first
    guild_data = _settings.get(guild_id)
    if guild_data:
        presets = guild_data.get("visualizer_presets")
        if presets and isinstance(presets, dict) and name in presets:
            return presets[name]

    # Fall back to factory presets
    return get_factory_preset(name)


# ---------------------------------------------------------------------------
# Internal helpers for nested settings storage
# ---------------------------------------------------------------------------


def _get_guild_nested(guild_id: int, top_key: str, sub_key: str):
    """Get a nested value from guild settings: _settings[guild_id][top_key][sub_key]."""
    guild_data = _settings.get(guild_id)
    if guild_data is None:
        return None
    container = guild_data.get(top_key)
    if container is None or not isinstance(container, dict):
        return None
    return container.get(sub_key)


def _set_guild_nested(guild_id: int, top_key: str, sub_key: str, setting: str, value) -> None:
    """Set a nested value in guild settings and persist.

    Creates intermediate dicts as needed.
    Resulting structure: _settings[guild_id][top_key][sub_key][setting] = value
    """
    if guild_id not in _settings:
        _settings[guild_id] = {}
    if top_key not in _settings[guild_id]:
        _settings[guild_id][top_key] = {}
    if sub_key not in _settings[guild_id][top_key]:
        _settings[guild_id][top_key][sub_key] = {}
    _settings[guild_id][top_key][sub_key][setting] = value
    save()

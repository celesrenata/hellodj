"""HelloDJ — Per-guild settings module.

Stores guild-level configuration in data/guild_settings.json.
Currently supports:
  - mode: "restrictive" (default) or "allow_all"

Shape of data/guild_settings.json:
    { "<guild_id>": { "mode": "restrictive" | "allow_all" }, ... }
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

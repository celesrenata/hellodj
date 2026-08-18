"""HelloDJ — Per-guild sleep (auto-leave) settings module.

Stores the /sleep idle timeout per guild in data/sleep_settings.json.
The value is the idle timeout in SECONDS (0 = disabled / no auto-leave).

Shape of data/sleep_settings.json:
    { "<guild_id>": { "sleep_timeout": <seconds:int>, "updated_at": "<iso>" }, ... }
"""

import json
import logging
import os
from datetime import datetime, timezone

log = logging.getLogger(__name__)

SLEEP_SETTINGS_FILE = "data/sleep_settings.json"

# Guild → dict of sleep settings
_settings: dict[int, dict] = {}


def load() -> None:
    """Load data/sleep_settings.json into memory."""
    os.makedirs("data", exist_ok=True)
    if not os.path.exists(SLEEP_SETTINGS_FILE):
        log.info("HelloDJ: %s not found — starting with no sleep settings.", SLEEP_SETTINGS_FILE)
        _settings.clear()
        return

    try:
        with open(SLEEP_SETTINGS_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        log.error(
            "HelloDJ could not read %s (%s); keeping current settings.",
            SLEEP_SETTINGS_FILE, exc,
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
    log.info("HelloDJ sleep settings loaded %d guild entries from %s", len(_settings), SLEEP_SETTINGS_FILE)


def reload() -> None:
    """Reload sleep settings from disk (same as load())."""
    load()


def save() -> None:
    """Write the in-memory sleep settings to data/sleep_settings.json atomically."""
    os.makedirs("data", exist_ok=True)
    tmp = f"{SLEEP_SETTINGS_FILE}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_settings, f, ensure_ascii=False, indent=2)
        os.replace(tmp, SLEEP_SETTINGS_FILE)
        log.info("HelloDJ sleep settings saved to %s (%d guild entries)", SLEEP_SETTINGS_FILE, len(_settings))
    except OSError as exc:
        log.error("HelloDJ could not write %s (%s)", SLEEP_SETTINGS_FILE, exc)


def get_sleep_timeout(guild_id: int) -> int:
    """Return the guild's sleep idle timeout in seconds (0 = disabled)."""
    entry = _settings.get(guild_id)
    if entry is None:
        return 0
    try:
        return max(0, int(entry.get("sleep_timeout", 0) or 0))
    except (TypeError, ValueError):
        return 0


def set_sleep_timeout(guild_id: int, seconds: int) -> int:
    """Set the guild's sleep idle timeout and persist. 0 disables auto-leave.

    Values are clamped to the [0, 7200] second range (max 2 hours).
    """
    seconds = max(0, min(int(seconds), 7200))
    if guild_id not in _settings:
        _settings[guild_id] = {}
    _settings[guild_id]["sleep_timeout"] = seconds
    _settings[guild_id]["updated_at"] = datetime.now(timezone.utc).isoformat()
    save()
    return seconds


def clear_sleep_timeout(guild_id: int) -> None:
    """Disable auto-leave for a guild (set timeout to 0)."""
    set_sleep_timeout(guild_id, 0)

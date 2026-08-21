"""HelloDJ — Multi-instance credential and configuration helpers.

Provides functions to read/write Bot Instance credentials (tokens, app IDs,
display names) to the encrypted credential store, and manage the global
multi-instance configuration keys.

Credential store key patterns:
    instance.<N>.token       — Bot token for instance N
    instance.<N>.app_id      — Application ID for instance N
    instance.<N>.name        — Display name (e.g. "HelloDJ #2")
    playback.instance_count  — Number of secondary instances ("2" to "10")
    playback.legacy_video_enabled — "true" or "false"

Usage:
    from playback.instance_config import (
        get_instance_count,
        get_instance_credentials,
        set_instance_credentials,
        remove_instance_credentials,
        is_legacy_video_enabled,
        set_legacy_video_enabled,
    )
"""

from __future__ import annotations

import logging
from typing import TypedDict

from config import cfg

log = logging.getLogger(__name__)


class InstanceCredentials(TypedDict):
    """Credential dict returned by get_instance_credentials."""

    token: str
    app_id: str
    name: str


# ── Instance count ─────────────────────────────────────────────────────────────


def get_instance_count() -> int:
    """Return the configured number of secondary bot instances (0–10).

    Reads ``playback.instance_count`` from the credential store.
    Returns 0 if unset or invalid.
    """
    return cfg.int("playback.instance_count", 0)


def set_instance_count(count: int) -> None:
    """Store the number of secondary bot instances.

    Args:
        count: Integer between 0 and 10 inclusive.

    Raises:
        ValueError: If count is outside the valid range.
    """
    if not 0 <= count <= 10:
        raise ValueError(f"instance_count must be 0–10, got {count}")
    cfg.set("playback.instance_count", str(count))
    log.info("Set playback.instance_count = %d", count)


# ── Per-instance credentials ───────────────────────────────────────────────────


def get_instance_credentials(index: int) -> InstanceCredentials | None:
    """Read token, app_id, and name for a secondary bot instance.

    Args:
        index: Zero-based instance index.

    Returns:
        A dict with keys ``token``, ``app_id``, ``name`` or None if
        the instance is not configured (token missing).
    """
    prefix = f"instance.{index}"
    token = cfg(f"{prefix}.token")
    if not token:
        return None
    app_id = cfg(f"{prefix}.app_id", "")
    name = cfg(f"{prefix}.name", f"HelloDJ #{index + 2}")
    return InstanceCredentials(token=token, app_id=app_id, name=name)


def set_instance_credentials(
    index: int, *, token: str, app_id: str, name: str
) -> None:
    """Store credentials for a secondary bot instance.

    Args:
        index: Zero-based instance index.
        token: Discord bot token.
        app_id: Discord application ID (snowflake string).
        name: Human-readable display name (e.g. "HelloDJ #2").

    Raises:
        ValueError: If token or app_id is empty.
    """
    if not token:
        raise ValueError("token must not be empty")
    if not app_id:
        raise ValueError("app_id must not be empty")

    prefix = f"instance.{index}"
    cfg.set(f"{prefix}.token", token)
    cfg.set(f"{prefix}.app_id", app_id)
    cfg.set(f"{prefix}.name", name or f"HelloDJ #{index + 2}")
    log.info("Stored credentials for instance %d (%s)", index, name)


def remove_instance_credentials(index: int) -> None:
    """Remove all credential keys for a secondary bot instance.

    Uses the credential store's delete method directly since cfg
    does not expose deletion.

    Args:
        index: Zero-based instance index.
    """
    try:
        from credentials import creds
    except Exception as exc:
        log.warning("Cannot remove instance %d credentials — store unavailable: %s", index, exc)
        return

    prefix = f"instance.{index}"
    for suffix in ("token", "app_id", "name"):
        creds.delete(f"{prefix}.{suffix}")
    log.info("Removed credentials for instance %d", index)


# ── Legacy video toggle ────────────────────────────────────────────────────────


def is_legacy_video_enabled() -> bool:
    """Return whether the legacy /video command transition period is active.

    Reads ``playback.legacy_video_enabled`` from the credential store.
    Defaults to True (transition period active) if unset.
    """
    return cfg.bool("playback.legacy_video_enabled", default=True)


def set_legacy_video_enabled(enabled: bool) -> None:
    """Set the legacy video transition period toggle.

    Args:
        enabled: True to keep /video commands active with deprecation
                 notices, False to reject them entirely.
    """
    cfg.set("playback.legacy_video_enabled", "true" if enabled else "false")
    log.info("Set playback.legacy_video_enabled = %s", enabled)


# ── Utility ────────────────────────────────────────────────────────────────────


def list_all_instances() -> list[InstanceCredentials]:
    """Return credentials for all configured instances.

    Iterates from index 0 up to ``get_instance_count()`` and returns
    only those with valid credentials stored.
    """
    count = get_instance_count()
    instances: list[InstanceCredentials] = []
    for i in range(count):
        cred = get_instance_credentials(i)
        if cred is not None:
            instances.append(cred)
    return instances

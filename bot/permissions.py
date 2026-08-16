"""HelloDJ — Canonical bot-permission requirements and checks.

Used by bot.py (_build_guilds_data) to surface each guild's permission health to
the web UI, and by player.py to log which voice permissions are missing when a
voice connect fails.
"""

import discord

# Canonical set of permissions the bot expects across a guild. Missing any of
# these degrades core features (playback, reactions, message management).
#
# Each entry is a snake_case attribute name on discord.py's Permissions object
# (e.g. perms.view_channel). We deliberately use the string names rather than
# Permissions.* flags: the installed discord.py builds per-flag `flag_value`
# namedtuples that expose no bitfield value, so bitwise comparisons against the
# per-flag objects crash. Reading the boolean attribute off the member's
# `guild_permissions` object is the API-correct way to test a flag.
REQUIRED_PERMISSIONS = {
    "view_channel",
    "send_messages",
    "connect",
    "speak",
    "add_reactions",
    "read_message_history",
    "manage_channels",
    "manage_roles",
    "manage_messages",
    "embed_links",
    "attach_files",
}

# Voice-specific subset, checked on voice connect failures.
VOICE_PERMISSIONS = {
    "view_channel",
    "connect",
    "speak",
}


def _perms_of(member: discord.Member):
    """Return ``member.guild_permissions`` safely.

    Returns ``None`` when the attribute is unavailable so callers can degrade
    gracefully instead of crashing.
    """
    return getattr(member, "guild_permissions", None)


def check_permissions(member: discord.Member) -> tuple[dict[str, bool], list[str]]:
    """Return ``(granted_map, missing)`` for REQUIRED_PERMISSIONS on ``member``.

    ``granted_map`` maps each permission flag name to whether the member holds
    it. ``missing`` lists the flag names the member lacks.

    If the member's permissions are unavailable, every required permission is
    reported missing (no crash).
    """
    perms = _perms_of(member)
    granted: dict[str, bool] = {}
    missing: list[str] = []
    for flag in REQUIRED_PERMISSIONS:
        # Test the boolean attribute off the Permissions object. If the object
        # is missing or lacks the attribute, treat the permission as absent.
        held = bool(getattr(perms, flag, False)) if perms is not None else False
        granted[flag] = held
        if not held:
            missing.append(flag)
    return granted, missing


def missing_voice_permissions(member: discord.Member) -> list[str]:
    """Return the names of missing voice permissions for ``member``."""
    perms = _perms_of(member)
    if perms is None:
        # Permissions unavailable -> report every voice permission missing.
        return list(VOICE_PERMISSIONS)
    return [flag for flag in VOICE_PERMISSIONS if not bool(getattr(perms, flag, False))]

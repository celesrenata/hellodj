"""Guild authorization policy for discord-bot-core.

New guilds are ``PENDING`` until an administrator approves them via the web-ui
admin portal; unapproved guilds are auto-denied (and left) after an expiry
window. See :mod:`discord_bot_core.policy.guild_policy`.
"""

from __future__ import annotations

from .guild_policy import (
    GuildPolicy,
    GuildStatus,
    PolicyEntry,
    PolicyStore,
)

__all__ = ["GuildPolicy", "GuildStatus", "PolicyEntry", "PolicyStore"]

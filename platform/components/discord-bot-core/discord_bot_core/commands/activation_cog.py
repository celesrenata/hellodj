"""Activation command + global command gate (on-prem ``/activate`` parity).

Ports the on-prem behavior: a guild is LOCKED until an administrator runs
``/activate <key>`` with the key shown on the web dashboard. While locked, the
bot refuses every command except ``activate`` itself. This stops anyone from
adding the bot to a server and using it without the owner's key.

The gate is a global command check installed on the bot; ``build_activation_cog``
returns both the cog (which owns the ``activate`` command) and installs the
check. discord.py is imported lazily so this module imports for unit tests of
the pure gate decision without the runtime dependency.
"""

from __future__ import annotations

import logging
from typing import Any

from ..policy.activation import GuildActivation

log = logging.getLogger(__name__)

__all__ = ["build_activation_cog", "command_allowed"]

#: Commands that are ALWAYS allowed even when a guild is not activated.
_ALWAYS_ALLOWED = frozenset({"activate"})


def command_allowed(
    activation: GuildActivation,
    *,
    command_name: str,
    guild_id: int | None,
) -> bool:
    """Pure gate decision: may this command run in this guild?

    * A DM (no guild) is allowed — activation is a per-guild gate.
    * The ``activate`` command is always allowed (so a locked guild can unlock).
    * Any other command requires the guild to be activated.
    """
    if guild_id is None:
        return True
    if command_name in _ALWAYS_ALLOWED:
        return True
    return activation.is_activated(int(guild_id))


def build_activation_cog(bot: Any, activation: GuildActivation) -> Any:
    """Build the activation cog and install the global command gate on ``bot``.

    The gate blocks every command in an unactivated guild (except ``activate``),
    replying with the on-prem-style prompt to run ``/activate <key>``. The cog
    owns the ``activate`` command that validates the key and unlocks the guild.
    """
    from discord.ext import commands

    async def _global_gate(ctx: Any) -> bool:
        command_name = getattr(getattr(ctx, "command", None), "name", "") or ""
        guild = getattr(ctx, "guild", None)
        guild_id = int(guild.id) if guild is not None else None
        if command_allowed(
            activation, command_name=command_name, guild_id=guild_id
        ):
            return True
        await ctx.reply(
            "🔒 This server has not been activated. An administrator must run "
            "`/activate <key>` (get the key from the HelloDJ web dashboard) to "
            "enable HelloDJ."
        )
        return False

    bot.add_check(_global_gate)

    class ActivationCog(commands.Cog):
        """Owns the ``activate`` command that unlocks a guild."""

        def __init__(self) -> None:
            self._activation = activation

        @commands.command(name="activate")
        async def activate(self, ctx: Any, key: str) -> None:
            """Activate HelloDJ in this server with the dashboard key."""
            guild = getattr(ctx, "guild", None)
            if guild is None:
                await ctx.reply("This command can only be used in a server.")
                return
            gid = int(guild.id)
            if self._activation.is_activated(gid):
                await ctx.reply("✅ This server is already activated.")
                return
            if self._activation.activate(gid, key):
                await ctx.reply(
                    "✅ HelloDJ activated! All commands are now available in "
                    "this server."
                )
            else:
                await ctx.reply(
                    "❌ Invalid activation key. Get the current key from the "
                    "HelloDJ web dashboard."
                )

    return ActivationCog()

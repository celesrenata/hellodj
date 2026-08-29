"""Activation command + global command gate (on-prem ``/activate`` parity).

Ports the on-prem behavior: a guild is LOCKED until an administrator runs
``/activate <key>`` with the key shown on the web dashboard. While locked, the
bot refuses every command except ``activate`` itself. This stops anyone from
adding the bot to a server and using it without the owner's key.

``activate`` is a real Discord **slash command** (``app_commands``) so it shows
up in the client's command picker — the on-prem parity the user expects when
they type ``/activate``. The gate is installed at two layers so it covers BOTH
command styles the bot exposes:

* an ``app_commands`` tree check for slash commands, and
* a ``bot.add_check`` for the legacy prefix commands.

discord.py is imported lazily so this module imports for unit tests of the pure
gate decision (:func:`command_allowed`) without the runtime dependency.
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
    """Build the activation cog and install the global command gates on ``bot``.

    Installs the gate for BOTH command styles (slash + prefix) and returns the
    cog that owns the ``/activate`` slash command. The slash command validates
    the dashboard key and unlocks the guild.
    """
    from discord import app_commands
    from discord.ext import commands

    # -- prefix-command gate (legacy text commands) ------------------------- #
    async def _prefix_gate(ctx: Any) -> bool:
        command_name = getattr(getattr(ctx, "command", None), "name", "") or ""
        guild = getattr(ctx, "guild", None)
        guild_id = int(guild.id) if guild is not None else None
        if command_allowed(
            activation, command_name=command_name, guild_id=guild_id
        ):
            return True
        await ctx.reply(_LOCKED_MESSAGE)
        return False

    bot.add_check(_prefix_gate)

    # -- slash-command (app_commands) gate ---------------------------------- #
    # discord.py's CommandTree has no add_check(); the documented extension
    # point is overriding tree.interaction_check, a global check run before any
    # slash command dispatches. It is invoked as ``interaction_check(interaction)``
    # so a one-arg coroutine assigned to the instance attribute is correct.
    async def _app_gate(interaction: Any) -> bool:
        command_name = getattr(
            getattr(interaction, "command", None), "name", ""
        ) or ""
        guild_id = getattr(interaction, "guild_id", None)
        if command_allowed(
            activation,
            command_name=command_name,
            guild_id=int(guild_id) if guild_id is not None else None,
        ):
            return True
        await _reply_locked(interaction)
        return False

    bot.tree.interaction_check = _app_gate

    class ActivationCog(commands.Cog):
        """Owns the ``/activate`` slash command that unlocks a guild."""

        def __init__(self) -> None:
            self._activation = activation

        @app_commands.command(
            name="activate",
            description="Activate HelloDJ in this server with the dashboard key.",
        )
        @app_commands.describe(key="The activation key from the HelloDJ web dashboard")
        async def activate(self, interaction: Any, key: str) -> None:
            """Activate HelloDJ in this server with the dashboard key."""
            guild_id = getattr(interaction, "guild_id", None)
            if guild_id is None:
                await interaction.response.send_message(
                    "This command can only be used in a server.", ephemeral=True
                )
                return
            gid = int(guild_id)
            if self._activation.is_activated(gid):
                await interaction.response.send_message(
                    "✅ This server is already activated.", ephemeral=True
                )
                return
            if self._activation.activate(gid, key):
                await interaction.response.send_message(
                    "✅ HelloDJ activated! All commands are now available in "
                    "this server."
                )
            else:
                await interaction.response.send_message(
                    "❌ Invalid activation key. Get the current key from the "
                    "HelloDJ web dashboard.",
                    ephemeral=True,
                )

    return ActivationCog()


#: On-prem-style prompt shown when a command is blocked in a locked guild.
_LOCKED_MESSAGE = (
    "🔒 This server has not been activated. An administrator must run "
    "`/activate <key>` (get the key from the HelloDJ web dashboard) to "
    "enable HelloDJ."
)


async def _reply_locked(interaction: Any) -> None:
    """Reply to a blocked slash interaction with the locked prompt (ephemeral)."""
    try:
        await interaction.response.send_message(_LOCKED_MESSAGE, ephemeral=True)
    except Exception:  # noqa: BLE001 - a gate reply must never crash the bot
        log.debug("activation gate: could not send locked message")

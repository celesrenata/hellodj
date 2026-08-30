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

__all__ = [
    "ALWAYS_ALLOWED",
    "allowed_command_names",
    "build_activation_cog",
    "command_allowed",
]

#: Commands ALWAYS allowed (and shown) even when a guild is not activated:
#: ``activate`` (so a locked guild can unlock) and ``help`` (so users can get
#: help before activating). Everything else is hidden + blocked until activated.
ALWAYS_ALLOWED = frozenset({"activate", "help"})


def command_allowed(
    activation: GuildActivation,
    *,
    command_name: str,
    guild_id: int | None,
) -> bool:
    """Pure gate decision: may this command run in this guild?

    * A DM (no guild) is allowed — activation is a per-guild gate.
    * ``activate``/``help`` are always allowed (so a locked guild can unlock and
      users can still get help).
    * Any other command requires the guild to be activated.

    This is the runtime backstop; :func:`allowed_command_names` decides what is
    even *visible* per guild. Both must agree so a hidden command is also
    blocked if a stale client cache still shows it.
    """
    if guild_id is None:
        return True
    if command_name in ALWAYS_ALLOWED:
        return True
    return activation.is_activated(int(guild_id))


def allowed_command_names(
    activated: bool,
    all_names: frozenset[str] | set[str],
) -> set[str]:
    """Pure decision: which command names should be VISIBLE in a guild.

    Drives the per-guild slash-command sync so the picker matches the spec:

    * **Unactivated** — only ``activate`` and ``help`` (whichever of those the
      bot actually defines). Every other command is hidden.
    * **Activated** — everything the bot defines EXCEPT ``activate`` (it has
      served its purpose and disappears); ``help`` and all other commands show.

    Entitlement-based filtering of the activated set (showing only the commands
    a guild's entitlements permit) layers on top of this result — see the caller
    in the gateway sync — and is intentionally not decided here so this stays a
    pure activation/visibility function.
    """
    names = set(all_names)
    if not activated:
        return names & ALWAYS_ALLOWED
    return names - {"activate"}


def _entitlement_blocks(
    entitlements: Any | None, *, command_name: str, guild_id: int | None
) -> bool:
    """Return whether feature entitlements should BLOCK this command (backstop).

    Mirrors the gateway's visibility filter as a runtime defense: a feature
    command whose gating entitlement the guild owner lacks is blocked even if a
    stale client cache still surfaces it. Baseline commands (not gated) are
    never blocked. DMs and an absent resolver do not block here — the gateway's
    secure-default HIDE already governs visibility; this only stops a resolvable
    denial. Never raises.
    """
    if guild_id is None or entitlements is None:
        return False
    from ..policy.entitlements import command_visible_for_entitlements

    try:
        effective = entitlements.effective_for_guild(int(guild_id))
        return not command_visible_for_entitlements(command_name, effective)
    except Exception as exc:  # noqa: BLE001 - never block on a resolution error
        log.warning("activation: entitlement backstop error: %s", exc)
        return False


def build_activation_cog(
    bot: Any,
    activation: GuildActivation,
    *,
    on_activated: Any | None = None,
    entitlements: Any | None = None,
) -> Any:
    """Build the activation cog and install the global command gates on ``bot``.

    Installs the gate for BOTH command styles (slash + prefix) and returns the
    cog that owns the ``/activate`` slash command. The slash command validates
    the dashboard key and unlocks the guild.

    Args:
        bot: The discord.py bot (needs ``tree`` + ``add_check``).
        activation: The per-guild activation reader/validator.
        on_activated: Optional ``async (guild_id: int) -> None`` callback invoked
            right after a guild is successfully activated. Wired to a per-guild
            re-sync so ``/activate`` disappears and the full command set appears
            immediately — without waiting for a reconnect. Best-effort: a failure
            is logged and never fails the activation reply.
        entitlements: Optional feature-entitlement resolver. When present the
            gate also BLOCKS a feature command the guild owner's entitlement
            doesn't include (defense in depth behind the gateway's visibility
            hide). ``None`` disables the runtime entitlement backstop.
    """
    from discord import app_commands
    from discord.ext import commands

    # -- prefix-command gate (legacy text commands) ------------------------- #
    async def _prefix_gate(ctx: Any) -> bool:
        command_name = getattr(getattr(ctx, "command", None), "name", "") or ""
        guild = getattr(ctx, "guild", None)
        guild_id = int(guild.id) if guild is not None else None
        if not command_allowed(
            activation, command_name=command_name, guild_id=guild_id
        ):
            await ctx.reply(_LOCKED_MESSAGE)
            return False
        if _entitlement_blocks(
            entitlements, command_name=command_name, guild_id=guild_id
        ):
            await ctx.reply(_ENTITLEMENT_MESSAGE)
            return False
        return True

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
        raw_gid = getattr(interaction, "guild_id", None)
        guild_id = int(raw_gid) if raw_gid is not None else None
        if not command_allowed(
            activation, command_name=command_name, guild_id=guild_id
        ):
            await _reply_locked(interaction)
            return False
        if _entitlement_blocks(
            entitlements, command_name=command_name, guild_id=guild_id
        ):
            await _reply_locked(interaction, message=_ENTITLEMENT_MESSAGE)
            return False
        return True

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
                log.info("activation: guild %s ACTIVATED", gid)
                await interaction.response.send_message(
                    "✅ HelloDJ activated! All commands are now available in "
                    "this server."
                )
                # Re-sync so /activate disappears and the full command set
                # appears now (not on the next reconnect). Best-effort.
                if on_activated is not None:
                    try:
                        await on_activated(gid)
                    except Exception as exc:  # noqa: BLE001 - never fail the reply
                        log.warning(
                            "activation: post-activate resync failed for "
                            "guild %s: %s",
                            gid,
                            exc,
                        )
            else:
                # INFO (not WARNING): a wrong key is an expected user error, but
                # worth an audit line since activation is the security gate.
                log.info("activation: guild %s rejected an invalid key", gid)
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

#: Prompt shown when a feature command is blocked by the owner's entitlements.
_ENTITLEMENT_MESSAGE = (
    "✨ This feature isn't included in this server's plan. Upgrade in the "
    "HelloDJ web dashboard to enable it."
)


async def _reply_locked(interaction: Any, *, message: str = _LOCKED_MESSAGE) -> None:
    """Reply to a blocked slash interaction with a gate prompt (ephemeral)."""
    try:
        await interaction.response.send_message(message, ephemeral=True)
    except Exception:  # noqa: BLE001 - a gate reply must never crash the bot
        log.debug("activation gate: could not send gate message")

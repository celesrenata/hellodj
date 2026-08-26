"""Cog and command registration for discord-bot-core.

:mod:`discord_bot_core.commands.registry` provides the scaffolding that
registers cogs against a discord.py ``Bot``. :mod:`playback_cog` is a thin cog
that translates commands into :class:`~discord_bot_core.playback.client.PlaybackRequest`
objects forwarded to the orchestrator.
"""

from __future__ import annotations

from .registry import CogFactory, CommandRegistry

__all__ = ["CogFactory", "CommandRegistry"]

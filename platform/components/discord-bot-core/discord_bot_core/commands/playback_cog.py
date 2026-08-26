"""Thin playback command cog.

This cog owns *no* playback logic. Each command:

1. checks the guild is authorized (guild policy), then
2. builds a typed :class:`~discord_bot_core.playback.client.PlaybackRequest`, and
3. forwards it to the ``playback-orchestrator`` via the injected playback client,
4. replying to the user with the orchestrator's result.

discord.py is imported lazily inside :func:`build_playback_cog` so this module
imports cleanly (for syntax checks and unit tests of the request-building logic)
even when discord.py is not installed.
"""

from __future__ import annotations

import logging
from typing import Any

from ..playback.client import PlaybackAction, PlaybackClient, PlaybackError, PlaybackRequest
from ..policy.guild_policy import GuildPolicy

log = logging.getLogger(__name__)

__all__ = ["build_request", "build_playback_cog"]


def build_request(
    action: PlaybackAction,
    *,
    guild_id: int,
    channel_id: int,
    requested_by: int,
    query: str | None = None,
    source: str | None = None,
) -> PlaybackRequest:
    """Build a typed playback request from command context.

    Factored out of the cog so the (pure) request-building logic is unit
    testable without a discord.py context object.
    """
    return PlaybackRequest(
        action=action,
        guild_id=guild_id,
        channel_id=channel_id,
        requested_by=requested_by,
        query=query,
        source=source,
    )


def build_playback_cog(playback: PlaybackClient, guild_policy: GuildPolicy) -> Any:
    """Construct the discord.py playback cog bound to the given dependencies.

    Args:
        playback: The client that forwards requests to the orchestrator.
        guild_policy: The policy consulted before acting in a guild.

    Returns:
        A ``discord.ext.commands.Cog`` instance.
    """
    from discord.ext import commands  # local import: optional runtime dependency

    class PlaybackCog(commands.Cog):
        """Discord commands that delegate playback to the orchestrator."""

        def __init__(self) -> None:
            self._playback = playback
            self._policy = guild_policy

        async def _delegate(self, ctx: Any, request: PlaybackRequest) -> None:
            if not self._policy.is_authorized(request.guild_id):
                await ctx.reply(
                    "HelloDJ is awaiting administrator approval for this server."
                )
                return
            try:
                result = await self._playback.submit(request)
            except PlaybackError as exc:
                log.warning("playback delegation failed: %s", exc)
                await ctx.reply("Playback service is unavailable right now.")
                return
            await ctx.reply(result.message or ("OK" if result.ok else "Failed."))

        @commands.command(name="play")
        async def play(self, ctx: Any, *, query: str) -> None:
            """Play or enqueue a track by search query."""
            await self._delegate(
                ctx,
                build_request(
                    PlaybackAction.PLAY,
                    guild_id=int(ctx.guild.id),
                    channel_id=int(ctx.channel.id),
                    requested_by=int(ctx.author.id),
                    query=query,
                ),
            )

        @commands.command(name="skip")
        async def skip(self, ctx: Any) -> None:
            """Skip the current track."""
            await self._delegate(
                ctx,
                build_request(
                    PlaybackAction.SKIP,
                    guild_id=int(ctx.guild.id),
                    channel_id=int(ctx.channel.id),
                    requested_by=int(ctx.author.id),
                ),
            )

        @commands.command(name="pause")
        async def pause(self, ctx: Any) -> None:
            """Pause playback."""
            await self._delegate(
                ctx,
                build_request(
                    PlaybackAction.PAUSE,
                    guild_id=int(ctx.guild.id),
                    channel_id=int(ctx.channel.id),
                    requested_by=int(ctx.author.id),
                ),
            )

    return PlaybackCog()

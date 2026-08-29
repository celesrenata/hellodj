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

from ..playback.client import (
    PlaybackAction,
    PlaybackClient,
    PlaybackError,
    PlaybackRequest,
)
from ..policy.guild_policy import GuildPolicy

log = logging.getLogger(__name__)

__all__ = ["build_playback_cog", "build_request"]


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
    from discord import app_commands  # local import: optional runtime dependency
    from discord.ext import commands

    class PlaybackCog(commands.Cog):
        """Discord SLASH commands that delegate playback to the orchestrator.

        These are ``app_commands`` (slash) commands so they show up in Discord's
        command picker and are synced to each guild by the gateway on ready/join
        — the same path ``/activate`` uses. Prefix (``!hellodj``) commands do
        NOT appear as slash commands, which is why an earlier prefix-only build
        surfaced no commands after activation.
        """

        def __init__(self) -> None:
            self._playback = playback
            self._policy = guild_policy

        async def _delegate(
            self, interaction: Any, request: PlaybackRequest
        ) -> None:
            if not self._policy.is_authorized(request.guild_id):
                await interaction.response.send_message(
                    "HelloDJ is awaiting administrator approval for this server.",
                    ephemeral=True,
                )
                return
            # Playback delegation is a network hop to the orchestrator; defer so
            # we don't blow Discord's 3s initial-response deadline, then follow
            # up with the result.
            await interaction.response.defer(thinking=True)
            try:
                result = await self._playback.submit(request)
            except PlaybackError as exc:
                log.warning("playback delegation failed: %s", exc)
                await interaction.followup.send(
                    "Playback service is unavailable right now."
                )
                return
            await interaction.followup.send(
                result.message or ("OK" if result.ok else "Failed.")
            )

        @staticmethod
        def _request_ids(interaction: Any) -> tuple[int, int, int]:
            """Return ``(guild_id, channel_id, user_id)`` from an interaction."""
            return (
                int(interaction.guild_id),
                int(interaction.channel_id),
                int(interaction.user.id),
            )

        @app_commands.command(
            name="play", description="Play or enqueue a track by search query."
        )
        @app_commands.describe(query="Song name, artist, or URL to play")
        async def play(self, interaction: Any, query: str) -> None:
            """Play or enqueue a track by search query."""
            gid, cid, uid = self._request_ids(interaction)
            await self._delegate(
                interaction,
                build_request(
                    PlaybackAction.PLAY,
                    guild_id=gid,
                    channel_id=cid,
                    requested_by=uid,
                    query=query,
                ),
            )

        @app_commands.command(name="skip", description="Skip the current track.")
        async def skip(self, interaction: Any) -> None:
            """Skip the current track."""
            gid, cid, uid = self._request_ids(interaction)
            await self._delegate(
                interaction,
                build_request(
                    PlaybackAction.SKIP,
                    guild_id=gid,
                    channel_id=cid,
                    requested_by=uid,
                ),
            )

        @app_commands.command(name="pause", description="Pause playback.")
        async def pause(self, interaction: Any) -> None:
            """Pause playback."""
            gid, cid, uid = self._request_ids(interaction)
            await self._delegate(
                interaction,
                build_request(
                    PlaybackAction.PAUSE,
                    guild_id=gid,
                    channel_id=cid,
                    requested_by=uid,
                ),
            )

    return PlaybackCog()

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

log = logging.getLogger(__name__)

__all__ = ["build_playback_cog", "build_request"]

#: Max query length logged (a full URL/search shouldn't bloat a log line).
_MAX_LOGGED_QUERY = 120


def _truncate(value: str | None) -> str:
    """Return a log-safe, length-capped rendering of a user-supplied query."""
    if not value:
        return "(none)"
    text = value.strip()
    return text if len(text) <= _MAX_LOGGED_QUERY else text[:_MAX_LOGGED_QUERY] + "…"


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


def build_playback_cog(playback: PlaybackClient) -> Any:
    """Construct the discord.py playback cog bound to the given dependencies.

    Args:
        playback: The client that forwards requests to the orchestrator.

    Returns:
        A ``discord.ext.commands.Cog`` instance.

    Authorization note: per-guild authorization on the AWS platform is the
    ``/activate <key>`` gate (``GuildActivation``), installed once and globally
    on the bot by the gateway as ``bot.tree.interaction_check`` (and the prefix
    ``bot.add_check``) in :func:`~discord_bot_core.commands.activation_cog.build_activation_cog`.
    That single gate refuses EVERY command in an unactivated guild before it
    dispatches, so an activated guild can run playback commands and an
    unactivated one cannot. The cog therefore does NOT re-check authorization
    itself: the legacy on-prem ``GuildPolicy`` PENDING→APPROVED admin-portal gate
    has no approval path wired on AWS (nothing ever calls ``approve``), so
    consulting it here permanently refused every command as "awaiting
    administrator approval" even after a guild was activated — the activation
    gate is the authoritative and only per-guild gate here.
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

        Per-guild authorization is the activation gate the gateway installs
        globally (see :func:`build_playback_cog`), not a per-command check here.
        """

        def __init__(self) -> None:
            self._playback = playback

        async def _delegate(
            self, interaction: Any, request: PlaybackRequest
        ) -> None:
            # INFO: one line per command invocation — the core operational +
            # audit signal (who ran what, where). Query is truncated so a long
            # URL/search doesn't blow up the log line.
            log.info(
                "command: %s guild=%s channel=%s user=%s query=%s",
                request.action.value,
                request.guild_id,
                request.channel_id,
                request.requested_by,
                _truncate(request.query),
            )
            # Playback delegation is a network hop to the orchestrator; defer so
            # we don't blow Discord's 3s initial-response deadline, then follow
            # up with the result.
            await interaction.response.defer(thinking=True)
            log.debug(
                "command: delegating %s for guild=%s to orchestrator",
                request.action.value,
                request.guild_id,
            )
            try:
                result = await self._playback.submit(request)
            except PlaybackError as exc:
                log.warning(
                    "command: %s delegation FAILED for guild=%s: %s",
                    request.action.value,
                    request.guild_id,
                    exc,
                )
                await interaction.followup.send(
                    "Playback service is unavailable right now."
                )
                return
            # DEBUG: the orchestrator's decision (ok + message + any data) so a
            # beta/staging trace shows the full round-trip outcome.
            log.debug(
                "command: %s result guild=%s ok=%s message=%s data=%s",
                request.action.value,
                request.guild_id,
                result.ok,
                result.message,
                result.data,
            )
            if not result.ok:
                log.info(
                    "command: %s not accepted for guild=%s: %s",
                    request.action.value,
                    request.guild_id,
                    result.message,
                )
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

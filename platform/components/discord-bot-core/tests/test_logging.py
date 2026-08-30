"""Verify discord-bot-core emits the logs we expect (no discord.py needed).

The command-invocation INFO log lives inside the cog's ``_delegate`` (which
needs discord.py at runtime); here we cover the transport-agnostic pieces: the
PlaybackClient's outbound-hop DEBUG log and its transport-error DEBUG log.
"""

from __future__ import annotations

import logging

import pytest

from discord_bot_core.commands.playback_cog import build_request
from discord_bot_core.playback.client import (
    PlaybackAction,
    PlaybackClient,
    PlaybackError,
)


class _FakeTransport:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    async def post_json(self, url: str, payload: dict) -> dict:
        return self._payload


class _RaisingTransport:
    async def post_json(self, url: str, payload: dict) -> dict:
        raise RuntimeError("connection refused")


@pytest.mark.asyncio
async def test_submit_logs_outbound_hop_at_debug(caplog) -> None:
    client = PlaybackClient("http://orch:8080", _FakeTransport({"ok": True}))
    req = build_request(
        PlaybackAction.PLAY, guild_id=1, channel_id=2, requested_by=3, query="x"
    )
    with caplog.at_level(logging.DEBUG, logger="discord_bot_core.playback.client"):
        await client.submit(req)
    assert "POST http://orch:8080/v1/playback" in caplog.text
    assert "action=play" in caplog.text
    assert "guild=1" in caplog.text


@pytest.mark.asyncio
async def test_submit_logs_transport_error_at_debug(caplog) -> None:
    client = PlaybackClient("http://orch:8080", _RaisingTransport())
    req = build_request(
        PlaybackAction.SKIP, guild_id=9, channel_id=2, requested_by=3
    )
    with (
        caplog.at_level(logging.DEBUG, logger="discord_bot_core.playback.client"),
        pytest.raises(PlaybackError),
    ):
        await client.submit(req)
    assert "transport error" in caplog.text

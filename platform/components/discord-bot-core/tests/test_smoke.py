"""Minimal smoke tests for discord-bot-core.

These exercise the pure, dependency-free surfaces (config, secrets parsing,
guild policy state machine, playback request building, watchdog stall logic)
without requiring discord.py, wavelink, boto3, or aiohttp to be installed.
"""

from __future__ import annotations

import pytest

from discord_bot_core.commands.playback_cog import build_request
from discord_bot_core.config import BotConfig
from discord_bot_core.playback.client import (
    PlaybackAction,
    PlaybackClient,
    PlaybackError,
    PlaybackResult,
)
from discord_bot_core.policy.guild_policy import GuildPolicy, GuildStatus
from discord_bot_core.secrets import TokenProvider, get_discord_token
from discord_bot_core.watchdogs.gateway_health import GatewayHealthWatchdog

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #


def test_config_from_env_requires_secret_id() -> None:
    with pytest.raises(ValueError):
        BotConfig.from_env({})


def test_config_from_env_reads_values() -> None:
    cfg = BotConfig.from_env(
        {
            "HELLODJ_DISCORD_TOKEN_SECRET_ID": "arn:secret:discord",
            "HELLODJ_ORCHESTRATOR_URL": "http://orch:9000",
            "HELLODJ_TOKEN_REFRESH_INTERVAL_S": "42",
        }
    )
    assert cfg.discord_token_secret_id == "arn:secret:discord"
    assert cfg.orchestrator_base_url == "http://orch:9000"
    assert cfg.token_refresh_interval_s == 42.0


# --------------------------------------------------------------------------- #
# Secrets provider (mockable client)
# --------------------------------------------------------------------------- #


class _FakeSecrets:
    def __init__(self, payload: dict) -> None:
        self._payload = payload
        self.calls = 0

    def get_secret_value(self, *, SecretId: str) -> dict:
        self.calls += 1
        return self._payload


def test_get_discord_token_raw_string() -> None:
    client = _FakeSecrets({"SecretString": "raw-token"})
    assert get_discord_token(client, "sid") == "raw-token"


def test_get_discord_token_json_field() -> None:
    client = _FakeSecrets({"SecretString": '{"token": "json-token"}'})
    assert get_discord_token(client, "sid") == "json-token"


def test_token_provider_caches_until_refresh() -> None:
    client = _FakeSecrets({"SecretString": "t1"})
    provider = TokenProvider(client, "sid")
    assert provider.get() == "t1"
    assert provider.get() == "t1"
    assert client.calls == 1  # cached
    provider.refresh()
    assert client.calls == 2


# --------------------------------------------------------------------------- #
# Guild policy state machine
# --------------------------------------------------------------------------- #


def test_guild_policy_join_is_pending_then_approve() -> None:
    clock = {"t": 1000.0}
    policy = GuildPolicy(clock=lambda: clock["t"])

    assert policy.check_on_join(1, "Guild") is GuildStatus.PENDING
    assert policy.is_authorized(1) is False

    policy.approve(1)
    assert policy.is_authorized(1) is True
    # Re-join keeps approved status
    assert policy.check_on_join(1) is GuildStatus.APPROVED


def test_guild_policy_expiry_denies_pending() -> None:
    clock = {"t": 0.0}
    policy = GuildPolicy(clock=lambda: clock["t"])
    policy.check_on_join(7, "Old")
    assert policy.pending_guilds()

    clock["t"] = 60 * 60 * 25  # 25 hours later
    expired = policy.expire_and_deny()
    assert expired == [7]
    assert policy.is_authorized(7) is False
    assert not policy.pending_guilds()


# --------------------------------------------------------------------------- #
# Playback request building + client delegation (fake transport)
# --------------------------------------------------------------------------- #


def test_build_request_payload() -> None:
    req = build_request(
        PlaybackAction.PLAY,
        guild_id=1,
        channel_id=2,
        requested_by=3,
        query="daft punk",
    )
    payload = req.to_payload()
    assert payload["action"] == "play"
    assert payload["guildId"] == "1"
    assert payload["query"] == "daft punk"


class _FakeTransport:
    def __init__(self, response: dict) -> None:
        self._response = response
        self.last_url: str | None = None
        self.last_payload: dict | None = None

    async def post_json(self, url: str, payload: dict) -> dict:
        self.last_url = url
        self.last_payload = payload
        return self._response


@pytest.mark.asyncio
async def test_playback_client_submit() -> None:
    transport = _FakeTransport({"ok": True, "message": "Playing"})
    client = PlaybackClient("http://orch:8080/", transport)
    req = build_request(
        PlaybackAction.PLAY,
        guild_id=1,
        channel_id=2,
        requested_by=3,
        query="q",
    )
    result = await client.submit(req)
    assert isinstance(result, PlaybackResult)
    assert result.ok is True
    assert result.message == "Playing"
    assert transport.last_url == "http://orch:8080/v1/playback"


class _RaisingTransport:
    async def post_json(self, url: str, payload: dict) -> dict:
        raise ConnectionError("boom")


@pytest.mark.asyncio
async def test_playback_client_wraps_transport_error() -> None:
    client = PlaybackClient("http://orch:8080", _RaisingTransport())
    req = build_request(
        PlaybackAction.SKIP, guild_id=1, channel_id=2, requested_by=3
    )
    with pytest.raises(PlaybackError):
        await client.submit(req)


# --------------------------------------------------------------------------- #
# Gateway-health watchdog stall detection (pure logic)
# --------------------------------------------------------------------------- #


class _FakeProbe:
    def __init__(self, age: float | None) -> None:
        self._age = age
        self.reconnects = 0

    def seconds_since_last_heartbeat(self) -> float | None:
        return self._age

    async def force_reconnect(self) -> None:
        self.reconnects += 1


def test_gateway_health_is_stalled() -> None:
    wd = GatewayHealthWatchdog(_FakeProbe(None), interval_s=30, stall_timeout_s=90)
    assert wd.is_stalled(None) is False
    assert wd.is_stalled(10.0) is False
    assert wd.is_stalled(120.0) is True


# --------------------------------------------------------------------------- #
# BotClient gateway health probe + force_reconnect (crash-loop regression)
# --------------------------------------------------------------------------- #


class _FakeKeepAlive:
    """Stand-in for discord.py's KeepAliveHandler (only ``_last_ack``)."""

    def __init__(self, last_ack: float | None) -> None:
        self._last_ack = last_ack


class _FakeWebSocket:
    """Stand-in for discord.py's DiscordWebSocket."""

    def __init__(self, last_ack: float | None) -> None:
        self._keep_alive = (
            _FakeKeepAlive(last_ack) if last_ack is not None else None
        )
        self.closed_with: int | None = None

    async def close(self, code: int = 1000) -> None:
        self.closed_with = code


class _FakeBot:
    """Minimal fake of a discord.py Bot for probe/reconnect tests."""

    def __init__(self, ws: _FakeWebSocket | None, closed: bool = False) -> None:
        self.ws = ws
        self._closed = closed
        self.close_calls = 0

    def is_closed(self) -> bool:
        return self._closed

    async def close(self) -> None:
        # Terminal shutdown — must NOT be called by force_reconnect.
        self.close_calls += 1
        self._closed = True


def _bot_client(bot: _FakeBot | None):
    from discord_bot_core.gateway.client import BotClient

    client = BotClient.__new__(BotClient)  # bypass discord import in __init__
    client._bot = bot
    client._last_heartbeat_monotonic = None
    client._identity_applier = None
    return client


def test_probe_prefers_live_ws_heartbeat_ack() -> None:
    import time

    # A fresh ACK => tiny age, never stalled — this is the healthy-connection
    # case that the old on_ready-only timestamp got wrong.
    bot = _FakeBot(_FakeWebSocket(last_ack=time.perf_counter()))
    client = _bot_client(bot)
    age = client.seconds_since_last_heartbeat()
    assert age is not None
    assert age < 5.0


def test_probe_reports_stale_ws_heartbeat() -> None:
    import time

    bot = _FakeBot(_FakeWebSocket(last_ack=time.perf_counter() - 200.0))
    client = _bot_client(bot)
    age = client.seconds_since_last_heartbeat()
    assert age is not None
    assert age > 90.0


def test_probe_none_when_closed() -> None:
    bot = _FakeBot(_FakeWebSocket(last_ack=0.0), closed=True)
    client = _bot_client(bot)
    assert client.seconds_since_last_heartbeat() is None


def test_probe_falls_back_to_on_ready_timestamp_before_first_ack() -> None:
    import time

    bot = _FakeBot(_FakeWebSocket(last_ack=None))  # ws up, no ACK yet
    client = _bot_client(bot)
    assert client.seconds_since_last_heartbeat() is None  # no fallback set
    client._last_heartbeat_monotonic = time.monotonic()
    age = client.seconds_since_last_heartbeat()
    assert age is not None and age < 5.0


@pytest.mark.asyncio
async def test_force_reconnect_closes_ws_not_bot() -> None:
    ws = _FakeWebSocket(last_ack=0.0)
    bot = _FakeBot(ws)
    client = _bot_client(bot)
    await client.force_reconnect()
    # Regression: must close the WEBSOCKET with a resumable code, and must NOT
    # terminally close the bot (that crashed the process previously).
    assert ws.closed_with == 4000
    assert bot.close_calls == 0
    assert bot.is_closed() is False


@pytest.mark.asyncio
async def test_force_reconnect_noop_when_no_ws() -> None:
    bot = _FakeBot(ws=None)
    client = _bot_client(bot)
    await client.force_reconnect()  # must not raise
    assert bot.close_calls == 0

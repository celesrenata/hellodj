"""Tests for InstanceOrchestrator.

Covers:
- Property 11: Instance assignment picks first available
- Property 12: Existing instance routing
- Unit tests for release, health check, and initialization
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from hypothesis import given, settings, strategies as st

from bot.playback.orchestrator import (
    BotInstance,
    InstanceOrchestrator,
    _HEALTH_CHECK_TIMEOUT_S,
    _RELEASE_DEADLINE_S,
)


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


def _make_mock_client(*, ready: bool = True, latency: float = 0.05) -> MagicMock:
    """Create a mock discord.Client with controllable state."""
    client = MagicMock()
    client.is_closed.return_value = False
    client.is_ready.return_value = ready
    client.latency = latency
    client.voice_clients = []
    client.start = AsyncMock()
    return client


def _make_instance(
    index: int = 0,
    status: str = "available",
    guild_id: int | None = None,
    channel_id: int | None = None,
    *,
    ready: bool = True,
    latency: float = 0.05,
) -> BotInstance:
    """Create a BotInstance with a mock client for testing."""
    return BotInstance(
        index=index,
        client=_make_mock_client(ready=ready, latency=latency),
        token=f"token-{index}",
        application_id=1000 + index,
        status=status,
        guild_id=guild_id,
        channel_id=channel_id,
        display_name=f"Test Instance #{index + 1}",
    )


def _make_orchestrator(instances: list[BotInstance] | None = None) -> InstanceOrchestrator:
    """Create an orchestrator with pre-populated instances."""
    primary = MagicMock()
    registry = MagicMock()
    orch = InstanceOrchestrator(primary, registry)
    if instances:
        orch._instances = instances
    orch._initialized = True
    return orch


# ---------------------------------------------------------------------------
# Property 11: Instance assignment picks first available
# ---------------------------------------------------------------------------


class TestProperty11AssignmentPicksFirstAvailable:
    """Property 11: For any set of Bot_Instances where at least one has status
    'available', when a new channel needs an instance, the InstanceOrchestrator
    SHALL assign one of the available instances (never an instance with status
    'connected' or 'unhealthy').

    **Validates: Requirements 6.2, 6.8**
    """

    @pytest.mark.asyncio
    async def test_assigns_first_available_from_pool(self) -> None:
        """Picks the first available instance when multiple exist."""
        instances = [
            _make_instance(0, status="connected", guild_id=100, channel_id=200),
            _make_instance(1, status="available"),
            _make_instance(2, status="available"),
        ]
        orch = _make_orchestrator(instances)
        result = await orch.assign_instance(guild_id=100, channel_id=300)
        assert result is not None
        assert result.index == 1
        assert result.status == "connected"
        assert result.channel_id == 300
        assert result.guild_id == 100

    @pytest.mark.asyncio
    async def test_never_assigns_connected_instance(self) -> None:
        """Never assigns an instance already connected to another channel."""
        instances = [
            _make_instance(0, status="connected", guild_id=100, channel_id=200),
            _make_instance(1, status="connected", guild_id=100, channel_id=300),
        ]
        orch = _make_orchestrator(instances)
        result = await orch.assign_instance(guild_id=100, channel_id=400)
        assert result is None

    @pytest.mark.asyncio
    async def test_never_assigns_unhealthy_instance(self) -> None:
        """Never assigns an unhealthy instance."""
        instances = [
            _make_instance(0, status="unhealthy"),
            _make_instance(1, status="unhealthy"),
        ]
        orch = _make_orchestrator(instances)
        result = await orch.assign_instance(guild_id=100, channel_id=200)
        assert result is None

    @pytest.mark.asyncio
    async def test_skips_unhealthy_picks_available(self) -> None:
        """Skips unhealthy instances and picks the first available."""
        instances = [
            _make_instance(0, status="unhealthy"),
            _make_instance(1, status="connected", guild_id=100, channel_id=200),
            _make_instance(2, status="available"),
        ]
        orch = _make_orchestrator(instances)
        result = await orch.assign_instance(guild_id=100, channel_id=300)
        assert result is not None
        assert result.index == 2
        assert result.status == "connected"

    @pytest.mark.asyncio
    async def test_returns_none_when_no_instances_exist(self) -> None:
        """Returns None when the instance list is empty."""
        orch = _make_orchestrator([])
        result = await orch.assign_instance(guild_id=100, channel_id=200)
        assert result is None

    @given(
        num_available=st.integers(min_value=1, max_value=10),
        num_connected=st.integers(min_value=0, max_value=10),
        num_unhealthy=st.integers(min_value=0, max_value=10),
        guild_id=st.integers(min_value=1, max_value=2**63),
        channel_id=st.integers(min_value=1, max_value=2**63),
    )
    @settings(max_examples=100)
    def test_property_always_picks_available(
        self,
        num_available: int,
        num_connected: int,
        num_unhealthy: int,
        guild_id: int,
        channel_id: int,
    ) -> None:
        """Property: assignment always picks an 'available' instance when one exists."""
        # Feature: unified-playback, Property 11: Instance assignment picks first available
        # **Validates: Requirements 6.2, 6.8**
        instances: list[BotInstance] = []
        idx = 0

        for _ in range(num_connected):
            instances.append(
                _make_instance(idx, status="connected", guild_id=guild_id, channel_id=channel_id + idx + 1)
            )
            idx += 1

        for _ in range(num_unhealthy):
            instances.append(_make_instance(idx, status="unhealthy"))
            idx += 1

        first_available_idx = idx
        for _ in range(num_available):
            instances.append(_make_instance(idx, status="available"))
            idx += 1

        orch = _make_orchestrator(instances)

        # Use a fresh channel that no instance is connected to
        target_channel = channel_id + idx + 100

        result = asyncio.run(
            orch.assign_instance(guild_id=guild_id, channel_id=target_channel)
        )

        assert result is not None
        assert result.index == first_available_idx
        assert result.status == "connected"
        assert result.channel_id == target_channel


# ---------------------------------------------------------------------------
# Property 12: Existing instance routing
# ---------------------------------------------------------------------------


class TestProperty12ExistingInstanceRouting:
    """Property 12: For any channel that already has a Bot_Instance connected
    to it, the InstanceOrchestrator SHALL route new audio requests for that
    channel to the existing instance without reassignment.

    **Validates: Requirements 6.3**
    """

    @pytest.mark.asyncio
    async def test_returns_existing_instance_for_channel(self) -> None:
        """Returns the existing instance if one is already connected."""
        instances = [
            _make_instance(0, status="connected", guild_id=100, channel_id=200),
            _make_instance(1, status="available"),
        ]
        orch = _make_orchestrator(instances)
        result = await orch.assign_instance(guild_id=100, channel_id=200)
        assert result is not None
        assert result.index == 0
        # Should NOT have assigned instance 1
        assert instances[1].status == "available"

    @pytest.mark.asyncio
    async def test_existing_instance_not_reassigned(self) -> None:
        """The existing instance's state doesn't change on re-request."""
        instances = [
            _make_instance(0, status="connected", guild_id=100, channel_id=200),
        ]
        orch = _make_orchestrator(instances)
        result = await orch.assign_instance(guild_id=100, channel_id=200)
        assert result is not None
        assert result.index == 0
        assert result.guild_id == 100
        assert result.channel_id == 200

    @given(
        guild_id=st.integers(min_value=1, max_value=2**63),
        channel_id=st.integers(min_value=1, max_value=2**63),
    )
    @settings(max_examples=100)
    def test_property_existing_always_returned(
        self, guild_id: int, channel_id: int
    ) -> None:
        """Property: a connected instance is always returned for its channel."""
        # Feature: unified-playback, Property 12: Existing instance routing
        # **Validates: Requirements 6.3**
        existing = _make_instance(
            0, status="connected", guild_id=guild_id, channel_id=channel_id
        )
        spare = _make_instance(1, status="available")
        orch = _make_orchestrator([existing, spare])

        result = asyncio.run(
            orch.assign_instance(guild_id=guild_id, channel_id=channel_id)
        )

        assert result is existing
        # Spare should remain untouched
        assert spare.status == "available"
        assert spare.channel_id is None


# ---------------------------------------------------------------------------
# Unit tests: Release
# ---------------------------------------------------------------------------


class TestReleaseInstance:
    """Tests for release_instance: frees within 5s."""

    @pytest.mark.asyncio
    async def test_release_sets_available(self) -> None:
        """Releasing an instance sets it to available and clears channel."""
        instances = [
            _make_instance(0, status="connected", guild_id=100, channel_id=200),
        ]
        orch = _make_orchestrator(instances)
        await orch.release_instance(guild_id=100, channel_id=200)
        assert instances[0].status == "available"
        assert instances[0].channel_id is None
        assert instances[0].guild_id is None

    @pytest.mark.asyncio
    async def test_release_nonexistent_is_noop(self) -> None:
        """Releasing a non-existent channel assignment does nothing."""
        instances = [_make_instance(0, status="available")]
        orch = _make_orchestrator(instances)
        await orch.release_instance(guild_id=100, channel_id=200)
        assert instances[0].status == "available"

    @pytest.mark.asyncio
    async def test_release_disconnects_voice(self) -> None:
        """Releasing disconnects the instance from voice."""
        vc_mock = AsyncMock()
        instance = _make_instance(0, status="connected", guild_id=100, channel_id=200)
        instance.client.voice_clients = [vc_mock]
        orch = _make_orchestrator([instance])
        await orch.release_instance(guild_id=100, channel_id=200)
        vc_mock.disconnect.assert_called_once_with(force=True)


# ---------------------------------------------------------------------------
# Unit tests: Health check
# ---------------------------------------------------------------------------


class TestHealthCheck:
    """Tests for health_check marking instances unhealthy/recovered."""

    @pytest.mark.asyncio
    async def test_healthy_instance_stays_available(self) -> None:
        """A responsive instance remains available."""
        instance = _make_instance(0, status="available", ready=True, latency=0.05)
        orch = _make_orchestrator([instance])
        await orch.health_check()
        assert instance.status == "available"
        assert instance.last_health_check > 0

    @pytest.mark.asyncio
    async def test_closed_client_marked_unhealthy(self) -> None:
        """A closed client is marked unhealthy."""
        instance = _make_instance(0, status="available")
        instance.client.is_closed.return_value = True
        orch = _make_orchestrator([instance])
        await orch.health_check()
        assert instance.status == "unhealthy"

    @pytest.mark.asyncio
    async def test_not_ready_marked_unhealthy(self) -> None:
        """A not-ready client is marked unhealthy."""
        instance = _make_instance(0, status="available", ready=False)
        orch = _make_orchestrator([instance])
        await orch.health_check()
        assert instance.status == "unhealthy"

    @pytest.mark.asyncio
    async def test_inf_latency_marked_unhealthy(self) -> None:
        """Infinite latency means the gateway is stale."""
        instance = _make_instance(0, status="available", latency=float("inf"))
        orch = _make_orchestrator([instance])
        await orch.health_check()
        assert instance.status == "unhealthy"

    @pytest.mark.asyncio
    async def test_unhealthy_instance_recovers(self) -> None:
        """An unhealthy instance with no channel recovers when healthy."""
        instance = _make_instance(0, status="unhealthy", ready=True, latency=0.05)
        instance.channel_id = None
        instance.guild_id = None
        orch = _make_orchestrator([instance])
        await orch.health_check()
        assert instance.status == "available"

    @pytest.mark.asyncio
    async def test_unhealthy_clears_channel_assignment(self) -> None:
        """When marked unhealthy, channel assignment is cleared."""
        instance = _make_instance(0, status="connected", guild_id=100, channel_id=200)
        instance.client.is_closed.return_value = True
        orch = _make_orchestrator([instance])
        await orch.health_check()
        assert instance.status == "unhealthy"
        assert instance.channel_id is None
        assert instance.guild_id is None


# ---------------------------------------------------------------------------
# Unit tests: Initialization
# ---------------------------------------------------------------------------


class TestInitialize:
    """Tests for initialize() loading credentials from config."""

    @pytest.mark.asyncio
    async def test_no_instance_count_configured(self) -> None:
        """When playback.instance_count is not set, no instances are loaded."""
        orch = _make_orchestrator()
        orch._instances = []
        orch._initialized = False

        def mock_cfg(key, default=None):
            return default

        with patch("bot.config.cfg", mock_cfg):
            await orch.initialize()
        assert len(orch._instances) == 0
        assert orch._initialized is True

    @pytest.mark.asyncio
    async def test_missing_credentials_skipped(self) -> None:
        """Instances with missing credentials are skipped."""
        config_values = {
            "playback.instance_count": "3",
            "instance.0.token": "token-0",
            "instance.0.app_id": "1000",
            "instance.0.name": "Bot #1",
            "instance.1.token": None,  # Missing!
            "instance.1.app_id": "1001",
            "instance.1.name": "Bot #2",
            "instance.2.token": "token-2",
            "instance.2.app_id": "invalid",  # Not a number!
            "instance.2.name": "Bot #3",
        }

        def mock_cfg(key, default=None):
            return config_values.get(key, default)

        orch = _make_orchestrator()
        orch._instances = []
        orch._initialized = False

        with patch("bot.config.cfg", mock_cfg):
            with patch("discord.Client") as mock_client_cls:
                mock_client = _make_mock_client()
                mock_client_cls.return_value = mock_client
                # Patch _connect_instance to avoid actual connections
                with patch.object(orch, "_connect_instance", new_callable=AsyncMock):
                    await orch.initialize()

        # Only instance 0 should have loaded (1 missing token, 2 invalid app_id)
        assert len(orch._instances) == 1
        assert orch._instances[0].application_id == 1000


# ---------------------------------------------------------------------------
# Unit tests: get_instance_for_channel
# ---------------------------------------------------------------------------


class TestGetInstanceForChannel:
    """Tests for get_instance_for_channel lookup."""

    def test_finds_connected_instance(self) -> None:
        instances = [
            _make_instance(0, status="connected", guild_id=100, channel_id=200),
            _make_instance(1, status="connected", guild_id=100, channel_id=300),
        ]
        orch = _make_orchestrator(instances)
        result = orch.get_instance_for_channel(100, 200)
        assert result is not None
        assert result.index == 0

    def test_returns_none_for_unknown_channel(self) -> None:
        instances = [
            _make_instance(0, status="connected", guild_id=100, channel_id=200),
        ]
        orch = _make_orchestrator(instances)
        result = orch.get_instance_for_channel(100, 999)
        assert result is None

    def test_ignores_non_connected_instances(self) -> None:
        """Only finds instances with status 'connected'."""
        instances = [
            _make_instance(0, status="available", guild_id=100, channel_id=200),
        ]
        # Manually set guild/channel but status is "available" — shouldn't match
        instances[0].guild_id = 100
        instances[0].channel_id = 200
        orch = _make_orchestrator(instances)
        result = orch.get_instance_for_channel(100, 200)
        assert result is None


# ---------------------------------------------------------------------------
# Unit tests: Properties
# ---------------------------------------------------------------------------


class TestOrchestratorProperties:
    """Test convenience properties."""

    def test_available_count(self) -> None:
        instances = [
            _make_instance(0, status="available"),
            _make_instance(1, status="connected", guild_id=1, channel_id=1),
            _make_instance(2, status="unhealthy"),
            _make_instance(3, status="available"),
        ]
        orch = _make_orchestrator(instances)
        assert orch.available_count == 2

    def test_connected_instances(self) -> None:
        instances = [
            _make_instance(0, status="available"),
            _make_instance(1, status="connected", guild_id=1, channel_id=1),
            _make_instance(2, status="connected", guild_id=1, channel_id=2),
        ]
        orch = _make_orchestrator(instances)
        connected = orch.connected_instances
        assert len(connected) == 2
        assert all(inst.status == "connected" for inst in connected)

    def test_instances_property_returns_copy(self) -> None:
        instances = [_make_instance(0, status="available")]
        orch = _make_orchestrator(instances)
        copy = orch.instances
        copy.append(_make_instance(1))
        assert len(orch._instances) == 1  # Original not modified

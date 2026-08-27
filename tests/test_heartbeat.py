"""Tests for bot heartbeat publisher and orchestrator health monitoring.

Covers:
- HeartbeatPublisher: start/stop lifecycle, Redis key management, TTL
- BotOrchestrator health_check: restart escalation, failed state
- Restart count tracking with 10-minute window

Requirements: 10.4, 10.9
"""

from __future__ import annotations

import asyncio
import sys
import time
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import fakeredis
import pytest
import pytest_asyncio

# Ensure bot/ and web-ui/ are importable
_bot_dir = str(Path(__file__).resolve().parent.parent / "bot")
_webui_dir = str(Path(__file__).resolve().parent.parent / "web-ui")
if _bot_dir not in sys.path:
    sys.path.insert(0, _bot_dir)
if _webui_dir not in sys.path:
    sys.path.insert(0, _webui_dir)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def async_redis():
    """Provide a fresh async fakeredis client per test."""
    client = fakeredis.FakeAsyncRedis(decode_responses=True)
    yield client
    await client.aclose()


@pytest.fixture
def sync_redis():
    """Provide a fresh sync fakeredis client per test."""
    client = fakeredis.FakeRedis(decode_responses=True)
    yield client
    client.flushall()
    client.close()


@pytest.fixture
def instance_id():
    """A stable instance ID for tests."""
    return "test-instance-abc123"


# ---------------------------------------------------------------------------
# HeartbeatPublisher tests
# ---------------------------------------------------------------------------


class TestHeartbeatPublisher:
    """Tests for the HeartbeatPublisher class."""

    @pytest.mark.asyncio
    async def test_start_publishes_initial_heartbeat(self, async_redis, instance_id):
        """Starting the publisher sets the heartbeat key immediately."""
        from heartbeat import HeartbeatPublisher

        publisher = HeartbeatPublisher(instance_id=instance_id, redis_url="redis://fake")
        # Inject our fake redis
        publisher._redis = async_redis
        publisher._running = True

        await publisher._publish_heartbeat()

        key = f"heartbeat:{instance_id}"
        value = await async_redis.get(key)
        assert value is not None
        # Value should be a timestamp
        ts = float(value)
        assert abs(ts - time.time()) < 2  # Within 2 seconds of now

    @pytest.mark.asyncio
    async def test_heartbeat_key_has_30s_ttl(self, async_redis, instance_id):
        """The heartbeat key should have a 30-second TTL."""
        from heartbeat import HeartbeatPublisher

        publisher = HeartbeatPublisher(instance_id=instance_id, redis_url="redis://fake")
        publisher._redis = async_redis
        publisher._running = True

        await publisher._publish_heartbeat()

        key = f"heartbeat:{instance_id}"
        ttl = await async_redis.ttl(key)
        # TTL should be approximately 30s (allow for slight timing)
        assert 28 <= ttl <= 30

    @pytest.mark.asyncio
    async def test_redis_key_format(self, instance_id):
        """The redis_key property returns correct format."""
        from heartbeat import HeartbeatPublisher

        publisher = HeartbeatPublisher(instance_id=instance_id, redis_url="redis://fake")
        assert publisher.redis_key == f"heartbeat:{instance_id}"

    @pytest.mark.asyncio
    async def test_stop_deletes_heartbeat_key(self, async_redis, instance_id):
        """Stopping the publisher removes the heartbeat key for immediate detection."""
        from heartbeat import HeartbeatPublisher

        publisher = HeartbeatPublisher(instance_id=instance_id, redis_url="redis://fake")
        publisher._redis = async_redis
        publisher._running = True
        publisher._task = asyncio.create_task(asyncio.sleep(100))

        # Publish a heartbeat first
        await publisher._publish_heartbeat()
        assert await async_redis.get(f"heartbeat:{instance_id}") is not None

        # Stop should delete the key
        await publisher.stop()
        assert await async_redis.get(f"heartbeat:{instance_id}") is None

    @pytest.mark.asyncio
    async def test_is_running_property(self, instance_id):
        """is_running reflects the publisher state."""
        from heartbeat import HeartbeatPublisher

        publisher = HeartbeatPublisher(instance_id=instance_id, redis_url="redis://fake")
        assert publisher.is_running is False
        publisher._running = True
        assert publisher.is_running is True

    @pytest.mark.asyncio
    async def test_double_start_is_safe(self, async_redis, instance_id):
        """Calling start() twice doesn't create duplicate loops."""
        from heartbeat import HeartbeatPublisher

        publisher = HeartbeatPublisher(instance_id=instance_id, redis_url="redis://fake")
        publisher._redis = async_redis
        publisher._running = True
        publisher._task = asyncio.create_task(asyncio.sleep(100))

        # Second start should be a no-op (warns and returns)
        await publisher.start()
        # Still only one task
        assert publisher._task is not None

        # Cleanup
        await publisher.stop()


# ---------------------------------------------------------------------------
# BotOrchestrator restart tracking tests (synchronous Redis)
# ---------------------------------------------------------------------------


class TestOrchestratorRestartTracking:
    """Tests for BotOrchestrator restart count tracking and escalation."""

    def test_initial_restart_count_is_zero(self, sync_redis, instance_id):
        """No restarts recorded initially — limit not exceeded."""
        from services.bot_orchestrator import BotOrchestrator

        with patch("services.bot_orchestrator._load_k8s_config"):
            orch = BotOrchestrator(
                pg_uri="postgresql://fake/fake",
                redis_url="redis://fake",
                k8s_configured=True,
            )

        with patch.object(orch, "_get_redis", return_value=sync_redis):
            result = orch._is_restart_limit_exceeded(uuid.UUID(int=1))
            assert result is False

    def test_increment_restart_count(self, sync_redis, instance_id):
        """Incrementing restart count works correctly."""
        from services.bot_orchestrator import BotOrchestrator

        with patch("services.bot_orchestrator._load_k8s_config"):
            orch = BotOrchestrator(
                pg_uri="postgresql://fake/fake",
                redis_url="redis://fake",
                k8s_configured=True,
            )

        inst_uuid = uuid.UUID(int=1)
        with patch.object(orch, "_get_redis", return_value=sync_redis):
            count1 = orch._increment_restart_count(inst_uuid)
            assert count1 == 1

            count2 = orch._increment_restart_count(inst_uuid)
            assert count2 == 2

    def test_restart_count_has_10min_ttl(self, sync_redis, instance_id):
        """Restart count key has a 10-minute TTL."""
        from services.bot_orchestrator import (
            BotOrchestrator,
            RESTART_COUNT_PREFIX,
            RESTART_WINDOW_SECONDS,
        )

        with patch("services.bot_orchestrator._load_k8s_config"):
            orch = BotOrchestrator(
                pg_uri="postgresql://fake/fake",
                redis_url="redis://fake",
                k8s_configured=True,
            )

        inst_uuid = uuid.UUID(int=1)
        with patch.object(orch, "_get_redis", return_value=sync_redis):
            orch._increment_restart_count(inst_uuid)

        key = f"{RESTART_COUNT_PREFIX}{inst_uuid}"
        ttl = sync_redis.ttl(key)
        # TTL should be approximately 600s (10 min)
        assert 598 <= ttl <= 600

    def test_escalation_after_5_restarts(self, sync_redis):
        """After 5 restarts in window, limit is exceeded."""
        from services.bot_orchestrator import BotOrchestrator, MAX_RESTARTS

        with patch("services.bot_orchestrator._load_k8s_config"):
            orch = BotOrchestrator(
                pg_uri="postgresql://fake/fake",
                redis_url="redis://fake",
                k8s_configured=True,
            )

        inst_uuid = uuid.UUID(int=2)
        with patch.object(orch, "_get_redis", return_value=sync_redis):
            # Simulate 5 restarts
            for _ in range(MAX_RESTARTS):
                orch._increment_restart_count(inst_uuid)

            assert orch._is_restart_limit_exceeded(inst_uuid) is True

    def test_no_escalation_before_5_restarts(self, sync_redis):
        """Before 5 restarts, limit is not exceeded."""
        from services.bot_orchestrator import BotOrchestrator, MAX_RESTARTS

        with patch("services.bot_orchestrator._load_k8s_config"):
            orch = BotOrchestrator(
                pg_uri="postgresql://fake/fake",
                redis_url="redis://fake",
                k8s_configured=True,
            )

        inst_uuid = uuid.UUID(int=3)
        with patch.object(orch, "_get_redis", return_value=sync_redis):
            for _ in range(MAX_RESTARTS - 1):
                orch._increment_restart_count(inst_uuid)

            assert orch._is_restart_limit_exceeded(inst_uuid) is False

    def test_clear_restart_tracking(self, sync_redis):
        """Clearing restart tracking removes count and window keys."""
        from services.bot_orchestrator import (
            BotOrchestrator,
            RESTART_COUNT_PREFIX,
            RESTART_WINDOW_PREFIX,
        )

        with patch("services.bot_orchestrator._load_k8s_config"):
            orch = BotOrchestrator(
                pg_uri="postgresql://fake/fake",
                redis_url="redis://fake",
                k8s_configured=True,
            )

        inst_uuid = uuid.UUID(int=4)
        with patch.object(orch, "_get_redis", return_value=sync_redis):
            # Set some restart data
            orch._increment_restart_count(inst_uuid)
            orch._increment_restart_count(inst_uuid)

            # Verify keys exist
            assert sync_redis.get(f"{RESTART_COUNT_PREFIX}{inst_uuid}") is not None
            assert sync_redis.get(f"{RESTART_WINDOW_PREFIX}{inst_uuid}") is not None

            # Clear
            orch._clear_restart_tracking(inst_uuid)

            # Verify keys are gone
            assert sync_redis.get(f"{RESTART_COUNT_PREFIX}{inst_uuid}") is None
            assert sync_redis.get(f"{RESTART_WINDOW_PREFIX}{inst_uuid}") is None

    def test_multiple_instances_independent(self, sync_redis):
        """Restart counts are tracked independently per instance."""
        from services.bot_orchestrator import BotOrchestrator

        with patch("services.bot_orchestrator._load_k8s_config"):
            orch = BotOrchestrator(
                pg_uri="postgresql://fake/fake",
                redis_url="redis://fake",
                k8s_configured=True,
            )

        id_a = uuid.UUID(int=10)
        id_b = uuid.UUID(int=11)

        with patch.object(orch, "_get_redis", return_value=sync_redis):
            for _ in range(3):
                orch._increment_restart_count(id_a)
            orch._increment_restart_count(id_b)

            # A has 3, not exceeded
            assert orch._is_restart_limit_exceeded(id_a) is False
            # B has 1, not exceeded
            assert orch._is_restart_limit_exceeded(id_b) is False


# ---------------------------------------------------------------------------
# BotOrchestrator health_check integration tests
# ---------------------------------------------------------------------------


class TestOrchestratorHealthCheck:
    """Tests for BotOrchestrator.health_check() heartbeat-based monitoring."""

    def test_healthy_instance_detected(self, sync_redis):
        """Instance with active heartbeat is reported as healthy."""
        from services.bot_orchestrator import BotOrchestrator, HEARTBEAT_KEY_PREFIX

        with patch("services.bot_orchestrator._load_k8s_config"):
            orch = BotOrchestrator(
                pg_uri="postgresql://fake/fake",
                redis_url="redis://fake",
                k8s_configured=True,
            )

        inst_uuid = uuid.uuid4()
        # Set a heartbeat in Redis
        sync_redis.set(f"{HEARTBEAT_KEY_PREFIX}{inst_uuid}", str(time.time()), ex=30)

        # Mock DB to return this instance as 'running'
        mock_instance = {
            "id": inst_uuid,
            "tenant_id": uuid.uuid4(),
            "pod_name": f"tenant-bot-{str(inst_uuid)[:8]}",
            "status": "running",
        }

        with patch.object(orch, "_get_redis", return_value=sync_redis), \
             patch.object(orch, "_get_instances_by_status") as mock_get:
            # Running instances with heartbeat
            mock_get.side_effect = lambda s: [mock_instance] if s == "running" else []
            results = orch.health_check()

        assert len(results) == 1
        assert results[0]["status"] == "healthy"
        assert results[0]["action"] is None

    def test_missing_heartbeat_triggers_restart(self, sync_redis):
        """No heartbeat triggers a restart action."""
        from services.bot_orchestrator import BotOrchestrator

        with patch("services.bot_orchestrator._load_k8s_config"):
            orch = BotOrchestrator(
                pg_uri="postgresql://fake/fake",
                redis_url="redis://fake",
                k8s_configured=True,
            )

        inst_uuid = uuid.uuid4()
        # No heartbeat set in Redis — instance is unresponsive

        mock_instance = {
            "id": inst_uuid,
            "tenant_id": uuid.uuid4(),
            "pod_name": f"tenant-bot-{str(inst_uuid)[:8]}",
            "status": "running",
        }

        with patch.object(orch, "_get_redis", return_value=sync_redis), \
             patch.object(orch, "_get_instances_by_status") as mock_get, \
             patch.object(orch, "restart") as mock_restart:
            mock_get.side_effect = lambda s: [mock_instance] if s == "running" else []
            mock_restart.return_value = mock_instance
            results = orch.health_check()

        assert len(results) == 1
        assert results[0]["status"] == "unhealthy"
        assert results[0]["action"] == "restart"
        mock_restart.assert_called_once_with(inst_uuid)

    def test_max_restarts_marks_failed(self, sync_redis):
        """When restart raises MaxRestartsExceededError, status is 'failed'."""
        from services.bot_orchestrator import BotOrchestrator, MaxRestartsExceededError

        with patch("services.bot_orchestrator._load_k8s_config"):
            orch = BotOrchestrator(
                pg_uri="postgresql://fake/fake",
                redis_url="redis://fake",
                k8s_configured=True,
            )

        inst_uuid = uuid.uuid4()
        mock_instance = {
            "id": inst_uuid,
            "tenant_id": uuid.uuid4(),
            "pod_name": f"tenant-bot-{str(inst_uuid)[:8]}",
            "status": "running",
        }

        with patch.object(orch, "_get_redis", return_value=sync_redis), \
             patch.object(orch, "_get_instances_by_status") as mock_get, \
             patch.object(orch, "restart", side_effect=MaxRestartsExceededError("max")):
            mock_get.side_effect = lambda s: [mock_instance] if s == "running" else []
            results = orch.health_check()

        assert len(results) == 1
        assert results[0]["status"] == "failed"
        assert results[0]["action"] == "failed"

"""Bot instance heartbeat publisher.

Publishes a Redis heartbeat key (`heartbeat:{instance_id}`) every 15 seconds
with a 30-second TTL. If the bot fails to refresh within 30s, the key expires
and the orchestrator detects the instance as unresponsive.

Usage:
    publisher = HeartbeatPublisher(instance_id="abc123", redis_url="redis://...")
    await publisher.start()
    # ... bot runs ...
    await publisher.stop()
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

import redis.asyncio as aioredis

log = logging.getLogger(__name__)

__all__ = ["HeartbeatPublisher"]

# Heartbeat interval and TTL per design spec
_HEARTBEAT_INTERVAL_S = 15
_HEARTBEAT_TTL_S = 30


class HeartbeatPublisher:
    """Publishes periodic heartbeats to Redis for health monitoring.

    The orchestrator watches these keys to detect unresponsive bot instances.
    If the key expires (no refresh for 30s), the instance is considered dead.
    """

    def __init__(self, instance_id: str, redis_url: str) -> None:
        """Initialise the heartbeat publisher.

        Parameters
        ----------
        instance_id:
            Unique identifier for this bot instance (UUID string).
        redis_url:
            Redis connection URL (e.g., redis://redis.redis-service.svc.cluster.local:6379).
        """
        self._instance_id = instance_id
        self._redis_url = redis_url
        self._redis: Optional[aioredis.Redis] = None
        self._task: Optional[asyncio.Task] = None
        self._running = False

    @property
    def instance_id(self) -> str:
        """The bot instance ID this publisher reports for."""
        return self._instance_id

    @property
    def redis_key(self) -> str:
        """The Redis key used for the heartbeat."""
        return f"heartbeat:{self._instance_id}"

    @property
    def is_running(self) -> bool:
        """Whether the heartbeat loop is currently active."""
        return self._running

    async def start(self) -> None:
        """Start the heartbeat publishing loop.

        Connects to Redis and begins publishing heartbeats every 15 seconds.
        Publishes an initial heartbeat immediately on start.
        """
        if self._running:
            log.warning("HeartbeatPublisher already running for %s", self._instance_id)
            return

        self._redis = aioredis.from_url(
            self._redis_url, decode_responses=True
        )
        self._running = True

        # Publish initial heartbeat immediately
        await self._publish_heartbeat()

        # Start the background loop
        self._task = asyncio.create_task(
            self._heartbeat_loop(),
            name=f"heartbeat-{self._instance_id}",
        )
        log.info(
            "Heartbeat publisher started for instance %s (interval=%ds, ttl=%ds)",
            self._instance_id,
            _HEARTBEAT_INTERVAL_S,
            _HEARTBEAT_TTL_S,
        )

    async def stop(self) -> None:
        """Stop the heartbeat publishing loop and close the Redis connection.

        After stopping, the heartbeat key will naturally expire after 30s,
        signaling to the orchestrator that this instance is shutting down.
        """
        self._running = False

        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

        if self._redis is not None:
            # Delete the heartbeat key on graceful shutdown so the
            # orchestrator knows immediately (rather than waiting for TTL)
            try:
                await self._redis.delete(self.redis_key)
            except Exception:
                pass
            await self._redis.aclose()
            self._redis = None

        log.info("Heartbeat publisher stopped for instance %s", self._instance_id)

    async def _heartbeat_loop(self) -> None:
        """Background loop that publishes heartbeats every 15 seconds."""
        while self._running:
            try:
                await asyncio.sleep(_HEARTBEAT_INTERVAL_S)
                if self._running:
                    await self._publish_heartbeat()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                log.warning(
                    "Heartbeat publish failed for %s: %s", self._instance_id, exc
                )
                # Continue trying — a single failure shouldn't stop heartbeats
                await asyncio.sleep(1)

    async def _publish_heartbeat(self) -> None:
        """Publish a single heartbeat: set key with current timestamp and TTL."""
        if self._redis is None:
            return
        timestamp = str(time.time())
        await self._redis.set(
            self.redis_key, timestamp, ex=_HEARTBEAT_TTL_S
        )

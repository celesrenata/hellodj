"""Feature gating decorator and client for HelloDJ SaaS platform.

Enforces subscription-tier feature restrictions on bot commands.
Queries the feature flag API at startup, caches in-memory for 5 minutes,
and subscribes to Redis pub/sub for immediate cache invalidation.

Feature-to-Addon mapping:
    video, activity, hls, visualizer → Video_Addon
    tidal_hifi, lossless, priority_queue → Premium_Addon

Usage:
    from feature_gate import feature_required, get_feature_flags, start_feature_gate, stop_feature_gate

    @feature_required("video")
    @app_commands.command(name="video", description="Play a video")
    async def video_cmd(self, interaction: discord.Interaction, query: str):
        ...

    # At bot startup:
    await start_feature_gate(tenant_id, redis_url, api_base_url)

    # At bot shutdown:
    await stop_feature_gate()
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

import aiohttp
import discord
from discord import app_commands

log = logging.getLogger(__name__)

__all__ = [
    "feature_required",
    "get_feature_flags",
    "start_feature_gate",
    "stop_feature_gate",
    "check_tidal_fallback",
    "FEATURE_TO_ADDON",
    "BASE_PLAN_SOURCES",
]

# ---------------------------------------------------------------------------
# Feature → Addon name mapping (for user-facing messages)
# ---------------------------------------------------------------------------

FEATURE_TO_ADDON: dict[str, str] = {
    "video": "Video Addon",
    "activity": "Video Addon",
    "hls": "Video Addon",
    "visualizer": "Video Addon",
    "tidal_hifi": "Premium Addon",
    "lossless": "Premium Addon",
    "priority_queue": "Premium Addon",
}

# Sources available on Base_Plan (fallback when Premium not active)
BASE_PLAN_SOURCES: list[str] = ["youtube", "spotify", "soundcloud"]

# Default feature flags (Base_Plan restrictions)
_BASE_PLAN_DEFAULTS: dict[str, Any] = {
    "audio": True,
    "video": False,
    "activity": False,
    "hls": False,
    "visualizer": False,
    "tidal_hifi": False,
    "lossless": False,
    "priority_queue": False,
    "max_bot_instances": 1,
    "max_guilds_per_bot": 5,
}

# Cache TTL in seconds
_CACHE_TTL_SECONDS = 5 * 60  # 5 minutes


# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

_tenant_id: Optional[str] = None
_redis_url: Optional[str] = None
_api_base_url: Optional[str] = None

# In-memory feature flag cache
_feature_cache: dict[str, Any] = {}
_cache_timestamp: float = 0.0

# Redis subscriber task
_subscriber_task: Optional[asyncio.Task] = None
_http_session: Optional[aiohttp.ClientSession] = None


# ---------------------------------------------------------------------------
# Public API: lifecycle management
# ---------------------------------------------------------------------------


async def start_feature_gate(
    tenant_id: str,
    redis_url: str,
    api_base_url: str,
) -> None:
    """Initialize the feature gate client.

    Fetches initial feature flags from the API, populates the in-memory cache,
    and starts a Redis pub/sub subscriber for immediate invalidation.

    Args:
        tenant_id: The tenant UUID string for this bot instance.
        redis_url: Redis connection URL (e.g. redis://redis.redis-service.svc.cluster.local:6379).
        api_base_url: Base URL for the feature flags API
                      (e.g. http://hellodj-web-ui.hellodj-service.svc.cluster.local:8080).
    """
    global _tenant_id, _redis_url, _api_base_url, _http_session

    _tenant_id = tenant_id
    _redis_url = redis_url
    _api_base_url = api_base_url.rstrip("/")

    # Create a persistent HTTP session for API calls
    _http_session = aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=10)
    )

    # Fetch initial flags (best effort — defaults to Base_Plan if unreachable)
    await _refresh_cache()

    # Start Redis pub/sub subscriber for immediate invalidation
    _start_subscriber()

    log.info(
        "Feature gate started for tenant=%s (cached %d flags)",
        _tenant_id,
        len(_feature_cache),
    )


async def stop_feature_gate() -> None:
    """Shut down the feature gate client.

    Cancels the Redis subscriber task and closes the HTTP session.
    """
    global _subscriber_task, _http_session

    if _subscriber_task is not None:
        _subscriber_task.cancel()
        try:
            await _subscriber_task
        except asyncio.CancelledError:
            pass
        _subscriber_task = None

    if _http_session is not None:
        await _http_session.close()
        _http_session = None

    log.info("Feature gate stopped for tenant=%s", _tenant_id)


# ---------------------------------------------------------------------------
# Public API: feature flag access
# ---------------------------------------------------------------------------


async def get_feature_flags(tenant_id: Optional[str] = None) -> dict[str, Any]:
    """Get the current feature flags for this bot instance.

    Returns cached flags if still valid (< 5 min old), otherwise refreshes
    from the API. Falls back to Base_Plan defaults if API is unreachable
    and no cache exists.

    Args:
        tenant_id: Optional override for tenant_id (defaults to the module-level one).

    Returns:
        Dict of feature flag names to values.
    """
    global _feature_cache, _cache_timestamp

    # If cache is fresh, return it
    now = time.monotonic()
    if _feature_cache and (now - _cache_timestamp) < _CACHE_TTL_SECONDS:
        return _feature_cache.copy()

    # Cache is stale or empty — try to refresh
    await _refresh_cache()

    # Return whatever we have (refreshed or stale but still valid)
    if _feature_cache:
        return _feature_cache.copy()

    # No cache at all — return Base_Plan defaults
    return _BASE_PLAN_DEFAULTS.copy()


def get_feature_flags_sync() -> dict[str, Any]:
    """Synchronous access to the cached feature flags.

    Returns the currently cached flags without triggering a refresh.
    If no cache exists, returns Base_Plan defaults.
    """
    if _feature_cache:
        return _feature_cache.copy()
    return _BASE_PLAN_DEFAULTS.copy()


# ---------------------------------------------------------------------------
# Public API: decorator
# ---------------------------------------------------------------------------


def feature_required(feature: str):
    """Decorator that gates an app_commands command behind a feature flag.

    If the feature is not enabled for the current tenant, the command responds
    with an informational message about the required addon and does not execute.

    Works with discord.py app_commands.check().

    Args:
        feature: The feature flag key (e.g. "video", "tidal_hifi").

    Returns:
        A discord.py app_commands.check decorator.
    """

    async def predicate(interaction: discord.Interaction) -> bool:
        flags = await get_feature_flags()
        if not flags.get(feature, False):
            addon_name = FEATURE_TO_ADDON.get(feature, "a paid addon")
            await interaction.response.send_message(
                f"🔒 This feature requires the **{addon_name}**. "
                f"Upgrade at https://hellodj.celestium.life/dashboard/subscription",
                ephemeral=True,
            )
            return False
        return True

    return app_commands.check(predicate)


# ---------------------------------------------------------------------------
# Public API: Tidal fallback
# ---------------------------------------------------------------------------


async def check_tidal_fallback(source: str) -> tuple[bool, Optional[str]]:
    """Check if a Tidal request should fall back to Base_Plan sources.

    If the Premium_Addon is not active and the user requests Tidal,
    returns (True, message) indicating fallback should occur.
    If Premium is active, returns (False, None).

    Args:
        source: The requested source (e.g. "tidal").

    Returns:
        Tuple of (should_fallback, info_message).
        If should_fallback is True, the caller should use a Base_Plan source instead.
    """
    if source.lower() != "tidal":
        return False, None

    flags = await get_feature_flags()
    if flags.get("tidal_hifi", False):
        # Premium is active, Tidal is allowed
        return False, None

    # Premium not active — fall back
    message = (
        "ℹ️ Tidal HiFi requires the **Premium Addon**. "
        "Falling back to YouTube/Spotify/SoundCloud. "
        "Upgrade at https://hellodj.celestium.life/dashboard/subscription"
    )
    return True, message


# ---------------------------------------------------------------------------
# Internal: cache management
# ---------------------------------------------------------------------------


async def _refresh_cache() -> None:
    """Fetch feature flags from the API and update the in-memory cache.

    If the API is unreachable, retains existing cache or falls back to defaults.
    """
    global _feature_cache, _cache_timestamp

    if not _api_base_url or not _tenant_id:
        log.debug("Feature gate not configured — using defaults")
        if not _feature_cache:
            _feature_cache = _BASE_PLAN_DEFAULTS.copy()
            _cache_timestamp = time.monotonic()
        return

    try:
        url = f"{_api_base_url}/api/v1/features/{_tenant_id}"
        async with _http_session.get(url) as resp:
            if resp.status == 200:
                data = await resp.json()
                _feature_cache = data
                _cache_timestamp = time.monotonic()
                log.debug(
                    "Feature flags refreshed for tenant=%s: %s", _tenant_id, data
                )
            else:
                log.warning(
                    "Feature flag API returned status %d for tenant=%s",
                    resp.status,
                    _tenant_id,
                )
                # Keep existing cache if available; otherwise use defaults
                if not _feature_cache:
                    _feature_cache = _BASE_PLAN_DEFAULTS.copy()
                    _cache_timestamp = time.monotonic()
    except Exception as exc:
        log.warning(
            "Feature flag API unreachable for tenant=%s: %s", _tenant_id, exc
        )
        # Keep existing cache if available; otherwise use defaults
        if not _feature_cache:
            _feature_cache = _BASE_PLAN_DEFAULTS.copy()
            _cache_timestamp = time.monotonic()


def _invalidate_cache() -> None:
    """Invalidate the in-memory feature flag cache.

    The next call to get_feature_flags() will trigger a refresh from the API.
    """
    global _cache_timestamp
    _cache_timestamp = 0.0
    log.debug("Feature flags cache invalidated for tenant=%s", _tenant_id)


# ---------------------------------------------------------------------------
# Internal: Redis pub/sub subscriber
# ---------------------------------------------------------------------------


def _start_subscriber() -> None:
    """Start the Redis pub/sub subscriber task for immediate cache invalidation."""
    global _subscriber_task

    if not _redis_url or not _tenant_id:
        log.debug("Redis not configured — skipping feature change subscriber")
        return

    if _subscriber_task is not None and not _subscriber_task.done():
        return

    _subscriber_task = asyncio.create_task(
        _subscriber_loop(),
        name=f"feature-gate-subscriber-{_tenant_id}",
    )


async def _subscriber_loop() -> None:
    """Subscribe to Redis feature_change:{tenant_id} channel.

    On message receipt, invalidates the in-memory cache so the next
    feature check triggers a fresh API call.
    """
    import redis.asyncio as aioredis

    channel_name = f"feature_change:{_tenant_id}"

    while True:
        redis_client = None
        pubsub = None
        try:
            redis_client = aioredis.from_url(
                _redis_url, decode_responses=True
            )
            pubsub = redis_client.pubsub()
            await pubsub.subscribe(channel_name)
            log.info(
                "Feature gate subscribed to Redis channel: %s", channel_name
            )

            async for message in pubsub.listen():
                if message["type"] == "message":
                    log.info(
                        "Feature change notification received for tenant=%s — invalidating cache",
                        _tenant_id,
                    )
                    _invalidate_cache()
                    # Proactively refresh so the next check is fast
                    await _refresh_cache()

        except asyncio.CancelledError:
            log.debug("Feature gate subscriber cancelled")
            break
        except Exception as exc:
            log.warning(
                "Feature gate subscriber error (reconnecting in 5s): %s", exc
            )
            await asyncio.sleep(5)
        finally:
            if pubsub is not None:
                try:
                    await pubsub.unsubscribe(channel_name)
                    await pubsub.aclose()
                except Exception:
                    pass
            if redis_client is not None:
                try:
                    await redis_client.aclose()
                except Exception:
                    pass

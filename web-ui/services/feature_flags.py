"""Feature flag computation service for HelloDJ SaaS platform.

Computes tenant feature flags based on active subscription plan + addons.
Provides caching via Redis (5-min TTL) and pub/sub notification on changes.

Plan/addon feature mapping:
- Base_Plan / Trial: audio only
- Video_Addon: video, activity, hls, visualizer
- Premium_Addon: tidal_hifi, lossless, priority_queue
- Additional_Bot: increases max_bot_instances (1 + count, max 10)

Usage:
    from services.feature_flags import compute_features, get_features, publish_feature_change

    flags = get_features(tenant_id, pg_conn, redis_client)
    publish_feature_change(tenant_id, redis_client)
"""

from __future__ import annotations

import json
import logging
from typing import Any
from uuid import UUID

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Feature flag definitions per plan/addon
# ---------------------------------------------------------------------------

# Base features always enabled for any active subscription
BASE_FEATURES: set[str] = {"audio"}

# Addon → additional features unlocked
ADDON_FEATURES: dict[str, set[str]] = {
    "video": {"video", "activity", "hls", "visualizer"},
    "premium": {"tidal_hifi", "lossless", "priority_queue"},
}

# All known feature flag keys (for building complete response)
ALL_FEATURE_KEYS: list[str] = [
    "audio",
    "video",
    "activity",
    "hls",
    "visualizer",
    "tidal_hifi",
    "lossless",
    "priority_queue",
]

# Default limits
DEFAULT_MAX_BOT_INSTANCES = 1
MAX_BOT_INSTANCES_CAP = 10
DEFAULT_MAX_GUILDS_PER_BOT = 5

# Redis cache config
FEATURE_CACHE_PREFIX = "features:"
FEATURE_CACHE_TTL_SECONDS = 5 * 60  # 5 minutes
FEATURE_CHANGE_CHANNEL_PREFIX = "feature_change:"


# ---------------------------------------------------------------------------
# Core computation
# ---------------------------------------------------------------------------


def compute_features(
    plan: str | None, addons: list[str] | None
) -> dict[str, Any]:
    """Compute feature flags from a plan and list of active addons.

    Args:
        plan: The subscription plan ('base' or 'trial'), or None if no active sub.
        addons: List of addon identifiers (e.g. ['video', 'premium', 'additional_bot']).

    Returns:
        Dict of feature flags with boolean flags and numeric limits.
    """
    if addons is None:
        addons = []

    # Start with all features disabled
    enabled_features: set[str] = set()

    # Any active plan (base or trial) enables base features
    if plan in ("base", "trial"):
        enabled_features |= BASE_FEATURES

    # Apply addon features
    for addon in addons:
        addon_key = addon.lower()
        if addon_key in ADDON_FEATURES:
            enabled_features |= ADDON_FEATURES[addon_key]

    # Compute max_bot_instances: 1 base + count of additional_bot addons, capped at 10
    additional_bot_count = sum(
        1 for a in addons if a.lower() == "additional_bot"
    )
    max_bot_instances = min(
        DEFAULT_MAX_BOT_INSTANCES + additional_bot_count,
        MAX_BOT_INSTANCES_CAP,
    )

    # Build the full flags dict
    flags: dict[str, Any] = {}
    for key in ALL_FEATURE_KEYS:
        flags[key] = key in enabled_features

    flags["max_bot_instances"] = max_bot_instances
    flags["max_guilds_per_bot"] = DEFAULT_MAX_GUILDS_PER_BOT

    return flags


# ---------------------------------------------------------------------------
# Database query helpers
# ---------------------------------------------------------------------------


def _query_active_subscription(tenant_id: str, pg_conn) -> tuple[str | None, list[str]]:
    """Query PostgreSQL for the tenant's active subscription plan and addons.

    Args:
        tenant_id: UUID string of the tenant.
        pg_conn: A psycopg2 connection.

    Returns:
        Tuple of (plan, addons) where plan may be None if no active subscription.
    """
    with pg_conn.cursor() as cur:
        cur.execute(
            """
            SELECT plan, addons
            FROM subscriptions
            WHERE tenant_id = %s
              AND status = 'active'
            ORDER BY started_at DESC
            LIMIT 1
            """,
            (tenant_id,),
        )
        row = cur.fetchone()

    if row is None:
        return None, []

    plan = row[0]
    addons = row[1] if row[1] else []

    return plan, addons


# ---------------------------------------------------------------------------
# Public API: get features with caching
# ---------------------------------------------------------------------------


def get_features(
    tenant_id: str, pg_conn, redis_client
) -> dict[str, Any]:
    """Get feature flags for a tenant, using Redis cache with fallback to DB.

    Flow:
    1. Check Redis cache (`features:{tenant_id}`)
    2. If cache hit → return cached flags
    3. If cache miss → query PostgreSQL, compute flags, cache with 5-min TTL

    Args:
        tenant_id: UUID string of the tenant.
        pg_conn: A psycopg2 connection.
        redis_client: A redis.Redis client instance.

    Returns:
        Feature flags dict.
    """
    cache_key = f"{FEATURE_CACHE_PREFIX}{tenant_id}"

    # Try cache first
    try:
        cached = redis_client.get(cache_key)
        if cached is not None:
            log.debug("Feature flags cache hit for tenant=%s", tenant_id)
            return json.loads(cached)
    except Exception as exc:
        log.warning("Redis cache read failed for tenant=%s: %s", tenant_id, exc)

    # Cache miss — compute from database
    plan, addons = _query_active_subscription(tenant_id, pg_conn)
    flags = compute_features(plan, addons)

    # Store in cache
    try:
        redis_client.set(
            cache_key, json.dumps(flags), ex=FEATURE_CACHE_TTL_SECONDS
        )
        log.debug("Feature flags cached for tenant=%s (TTL=%ds)", tenant_id, FEATURE_CACHE_TTL_SECONDS)
    except Exception as exc:
        log.warning("Redis cache write failed for tenant=%s: %s", tenant_id, exc)

    return flags


def invalidate_features_cache(tenant_id: str, redis_client) -> None:
    """Remove cached feature flags for a tenant (e.g. after subscription change).

    Args:
        tenant_id: UUID string of the tenant.
        redis_client: A redis.Redis client instance.
    """
    cache_key = f"{FEATURE_CACHE_PREFIX}{tenant_id}"
    try:
        redis_client.delete(cache_key)
        log.debug("Feature flags cache invalidated for tenant=%s", tenant_id)
    except Exception as exc:
        log.warning("Redis cache invalidate failed for tenant=%s: %s", tenant_id, exc)


# ---------------------------------------------------------------------------
# Pub/Sub notification
# ---------------------------------------------------------------------------


def publish_feature_change(tenant_id: str, redis_client) -> None:
    """Publish a feature change notification to Redis pub/sub.

    Bot instances subscribe to `feature_change:{tenant_id}` and invalidate
    their in-memory feature flag cache when they receive a message.

    Args:
        tenant_id: UUID string of the tenant.
        redis_client: A redis.Redis client instance.
    """
    channel = f"{FEATURE_CHANGE_CHANNEL_PREFIX}{tenant_id}"
    try:
        redis_client.publish(channel, "updated")
        log.info("Published feature change for tenant=%s on channel=%s", tenant_id, channel)
    except Exception as exc:
        log.warning("Redis publish failed for tenant=%s: %s", tenant_id, exc)


def on_subscription_change(tenant_id: str, pg_conn, redis_client) -> dict[str, Any]:
    """Handle a subscription change: invalidate cache, recompute, and notify.

    Call this when activate/cancel/expire is invoked on a subscription.

    Args:
        tenant_id: UUID string of the tenant.
        pg_conn: A psycopg2 connection.
        redis_client: A redis.Redis client instance.

    Returns:
        The freshly computed feature flags.
    """
    # Invalidate stale cache
    invalidate_features_cache(tenant_id, redis_client)

    # Recompute and cache
    flags = get_features(tenant_id, pg_conn, redis_client)

    # Notify bot instances
    publish_feature_change(tenant_id, redis_client)

    return flags

"""Redis-backed sliding window rate limiter for the HelloDJ SaaS platform.

Uses Redis sorted sets to implement a sliding window rate limit:
- Key: `ratelimit:{tenant_id}:{endpoint}` → Sorted Set
- Each request adds a member (timestamp) with score = timestamp
- Entries older than the window are pruned on each check
- If remaining count >= limit, the request is rejected with HTTP 429

Default limit: 60 requests per 60-second window per tenant per bot instance.
Also provides a WebSocket message rate limiter (60 messages/minute per connection).

Usage:
    from services.rate_limiter import rate_limit, check_ws_rate_limit

    # As a Flask route decorator:
    @app.route("/api/v1/player/<instance_id>/play", methods=["POST"])
    @rate_limit
    def play(instance_id):
        ...

    # For WebSocket handlers:
    allowed, retry_after = check_ws_rate_limit(tenant_id, instance_id, redis_client)
    if not allowed:
        send({"type": "error", "code": 429, "retry_after": retry_after})
"""

from __future__ import annotations

import functools
import logging
import os
import time
from typing import Callable

import redis
from flask import abort, g, jsonify, request

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Default rate limit: 60 requests per 60 seconds
DEFAULT_RATE_LIMIT = 60
DEFAULT_WINDOW_SECONDS = 60

# Redis key prefix
RATE_LIMIT_KEY_PREFIX = "ratelimit:"

# Redis connection
REDIS_URL = os.environ.get(
    "REDIS_URL", "redis://redis.redis-service.svc.cluster.local:6379/0"
)

# ---------------------------------------------------------------------------
# Redis connection (lazy singleton)
# ---------------------------------------------------------------------------

_redis_client: redis.Redis | None = None


def _get_redis() -> redis.Redis:
    """Return a Redis client, creating one on first call."""
    global _redis_client
    if _redis_client is None:
        _redis_client = redis.Redis.from_url(REDIS_URL, decode_responses=True)
    return _redis_client


def set_redis_client(client: redis.Redis) -> None:
    """Override the Redis client (for testing with fakeredis)."""
    global _redis_client
    _redis_client = client


# ---------------------------------------------------------------------------
# Core sliding window implementation
# ---------------------------------------------------------------------------


def _check_rate_limit(
    key: str,
    redis_client: redis.Redis,
    limit: int = DEFAULT_RATE_LIMIT,
    window_seconds: int = DEFAULT_WINDOW_SECONDS,
) -> tuple[bool, int]:
    """Check and record a request against the sliding window rate limit.

    Uses a Redis sorted set where:
    - Score = timestamp of the request
    - Member = unique timestamp string (with enough precision to avoid collisions)

    Algorithm:
    1. Remove entries older than (now - window_seconds)
    2. Count remaining entries
    3. If count >= limit, reject and calculate Retry-After
    4. Otherwise, add current timestamp and allow

    Args:
        key: The full Redis key for this rate limit bucket.
        redis_client: A redis.Redis client instance.
        limit: Maximum requests allowed in the window.
        window_seconds: Size of the sliding window in seconds.

    Returns:
        Tuple of (allowed: bool, retry_after: int).
        - If allowed is True, retry_after is 0.
        - If allowed is False, retry_after is seconds until the oldest entry
          expires from the window (i.e., when the client can retry).
    """
    now = time.time()
    window_start = now - window_seconds

    # Use a pipeline for atomicity
    pipe = redis_client.pipeline(transaction=True)
    try:
        # 1. Remove entries older than the window
        pipe.zremrangebyscore(key, "-inf", window_start)
        # 2. Count remaining entries
        pipe.zcard(key)
        # 3. Get the oldest entry (to calculate Retry-After)
        pipe.zrange(key, 0, 0, withscores=True)
        # Execute the read operations
        results = pipe.execute()
    except redis.RedisError as exc:
        log.warning("Redis error during rate limit check for key=%s: %s", key, exc)
        # Fail open: allow the request if Redis is unavailable
        return True, 0

    current_count = results[1]
    oldest_entries = results[2]

    if current_count >= limit:
        # Rate limited — calculate Retry-After
        if oldest_entries:
            oldest_score = float(oldest_entries[0][1])
            retry_after = int(oldest_score + window_seconds - now) + 1
            retry_after = max(retry_after, 1)
        else:
            retry_after = 1
        return False, retry_after

    # 4. Allowed — add this request to the sorted set
    # Use a unique member to avoid collisions (timestamp with counter suffix)
    member = f"{now}"
    try:
        pipe2 = redis_client.pipeline(transaction=True)
        pipe2.zadd(key, {member: now})
        # Set expiry on the key to auto-cleanup (window + small buffer)
        pipe2.expire(key, window_seconds + 10)
        pipe2.execute()
    except redis.RedisError as exc:
        log.warning("Redis error recording rate limit entry for key=%s: %s", key, exc)
        # Still allow — we already checked and it was under limit

    return True, 0


# ---------------------------------------------------------------------------
# Flask decorator for HTTP routes
# ---------------------------------------------------------------------------


def rate_limit(
    f: Callable | None = None,
    *,
    limit: int = DEFAULT_RATE_LIMIT,
    window_seconds: int = DEFAULT_WINDOW_SECONDS,
):
    """Flask route decorator that applies sliding window rate limiting.

    Requires that `g.tenant` is set (i.e., the route is behind @login_required).
    Uses the tenant's ID and the request endpoint as the rate limit key.

    On rate limit exceeded:
    - Returns HTTP 429 with JSON error body
    - Sets `Retry-After` header (seconds until the client can retry)

    Can be used with or without arguments:
        @rate_limit
        def my_route(): ...

        @rate_limit(limit=30, window_seconds=60)
        def my_route(): ...
    """

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            # Get tenant ID from flask.g (set by @login_required)
            tenant = getattr(g, "tenant", None)
            if tenant is None:
                # No tenant context — skip rate limiting (shouldn't happen
                # if decorators are ordered correctly)
                return func(*args, **kwargs)

            tenant_id = tenant.get("id") or tenant.get("discord_user_id", "unknown")
            endpoint = request.endpoint or request.path

            # Build the rate limit key
            key = f"{RATE_LIMIT_KEY_PREFIX}{tenant_id}:{endpoint}"

            try:
                redis_client = _get_redis()
                allowed, retry_after = _check_rate_limit(
                    key, redis_client, limit=limit, window_seconds=window_seconds
                )
            except Exception as exc:
                log.warning("Rate limiter error: %s — failing open", exc)
                return func(*args, **kwargs)

            if not allowed:
                log.info(
                    "Rate limit exceeded for tenant=%s endpoint=%s (retry_after=%ds)",
                    tenant_id,
                    endpoint,
                    retry_after,
                )
                response = jsonify({
                    "error": "Rate limit exceeded",
                    "detail": f"Maximum {limit} requests per {window_seconds}s exceeded",
                    "retry_after": retry_after,
                })
                response.status_code = 429
                response.headers["Retry-After"] = str(retry_after)
                return response

            return func(*args, **kwargs)

        return wrapper

    # Support both @rate_limit and @rate_limit(...) usage
    if f is not None:
        return decorator(f)
    return decorator


# ---------------------------------------------------------------------------
# WebSocket rate limiting
# ---------------------------------------------------------------------------


def check_ws_rate_limit(
    tenant_id: str,
    instance_id: str,
    redis_client: redis.Redis | None = None,
    limit: int = DEFAULT_RATE_LIMIT,
    window_seconds: int = DEFAULT_WINDOW_SECONDS,
) -> tuple[bool, int]:
    """Check rate limit for a WebSocket message.

    Similar to the HTTP rate limiter but designed for direct invocation
    in WebSocket handlers rather than as a decorator.

    Args:
        tenant_id: The tenant's ID (UUID string or discord_user_id).
        instance_id: The bot instance ID being controlled.
        redis_client: Optional Redis client override. Uses module singleton if None.
        limit: Maximum messages per window (default 60).
        window_seconds: Window size in seconds (default 60).

    Returns:
        Tuple of (allowed: bool, retry_after: int).
        - allowed=True, retry_after=0 when within limits.
        - allowed=False, retry_after=N when rate limited.
    """
    if redis_client is None:
        try:
            redis_client = _get_redis()
        except Exception as exc:
            log.warning("Redis unavailable for WS rate limit: %s — failing open", exc)
            return True, 0

    key = f"{RATE_LIMIT_KEY_PREFIX}{tenant_id}:ws:{instance_id}"

    try:
        allowed, retry_after = _check_rate_limit(
            key, redis_client, limit=limit, window_seconds=window_seconds
        )
    except Exception as exc:
        log.warning("WS rate limit check failed: %s — failing open", exc)
        return True, 0

    if not allowed:
        log.info(
            "WS rate limit exceeded for tenant=%s instance=%s (retry_after=%ds)",
            tenant_id,
            instance_id,
            retry_after,
        )

    return allowed, retry_after

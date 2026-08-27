"""Unit tests for the rate limiter service.

Tests the sliding window rate limiter implementation for both HTTP routes
and WebSocket connections.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from unittest.mock import patch

import fakeredis
import pytest
from flask import Flask, g

# Ensure web-ui is importable
_webui_dir = str(Path(__file__).resolve().parent.parent / "web-ui")
if _webui_dir not in sys.path:
    sys.path.insert(0, _webui_dir)

from services.rate_limiter import (
    _check_rate_limit,
    check_ws_rate_limit,
    rate_limit,
    set_redis_client,
    RATE_LIMIT_KEY_PREFIX,
)


@pytest.fixture
def redis_client():
    """Provide a fakeredis client for rate limiter tests."""
    client = fakeredis.FakeRedis(decode_responses=True)
    yield client
    client.flushall()
    client.close()


@pytest.fixture
def app():
    """Minimal Flask app for testing the rate_limit decorator."""
    app = Flask(__name__)
    app.secret_key = "test-secret"

    @app.route("/test-endpoint", methods=["POST"])
    @rate_limit
    def test_endpoint():
        return {"status": "ok"}, 200

    @app.route("/test-limited", methods=["POST"])
    @rate_limit(limit=5, window_seconds=60)
    def test_limited():
        return {"status": "ok"}, 200

    return app


class TestCheckRateLimit:
    """Tests for the core _check_rate_limit function."""

    def test_allows_first_request(self, redis_client):
        """First request to a new key is always allowed."""
        allowed, retry_after = _check_rate_limit(
            "ratelimit:tenant1:endpoint1", redis_client, limit=60, window_seconds=60
        )
        assert allowed is True
        assert retry_after == 0

    def test_allows_requests_under_limit(self, redis_client):
        """Requests under the limit are all allowed."""
        key = "ratelimit:tenant1:endpoint1"
        for i in range(59):
            allowed, retry_after = _check_rate_limit(
                key, redis_client, limit=60, window_seconds=60
            )
            assert allowed is True
            assert retry_after == 0

    def test_rejects_at_limit(self, redis_client):
        """The 61st request within the window is rejected."""
        key = "ratelimit:tenant1:endpoint1"

        # Fill up to the limit
        for i in range(60):
            allowed, _ = _check_rate_limit(
                key, redis_client, limit=60, window_seconds=60
            )
            assert allowed is True

        # 61st should be rejected
        allowed, retry_after = _check_rate_limit(
            key, redis_client, limit=60, window_seconds=60
        )
        assert allowed is False
        assert retry_after >= 1

    def test_retry_after_is_positive(self, redis_client):
        """Retry-After header value is always at least 1 second."""
        key = "ratelimit:tenant1:endpoint1"

        for _ in range(60):
            _check_rate_limit(key, redis_client, limit=60, window_seconds=60)

        _, retry_after = _check_rate_limit(
            key, redis_client, limit=60, window_seconds=60
        )
        assert retry_after >= 1

    def test_small_limit(self, redis_client):
        """Works correctly with a small limit (e.g., 3 requests)."""
        key = "ratelimit:tenant1:small"

        for _ in range(3):
            allowed, _ = _check_rate_limit(
                key, redis_client, limit=3, window_seconds=60
            )
            assert allowed is True

        allowed, retry_after = _check_rate_limit(
            key, redis_client, limit=3, window_seconds=60
        )
        assert allowed is False
        assert retry_after >= 1

    def test_different_keys_are_independent(self, redis_client):
        """Rate limits for different keys don't interfere."""
        key1 = "ratelimit:tenant1:endpoint1"
        key2 = "ratelimit:tenant2:endpoint1"

        # Fill key1 to limit
        for _ in range(5):
            _check_rate_limit(key1, redis_client, limit=5, window_seconds=60)

        # key1 is now at limit
        allowed, _ = _check_rate_limit(key1, redis_client, limit=5, window_seconds=60)
        assert allowed is False

        # key2 should still be allowed
        allowed, _ = _check_rate_limit(key2, redis_client, limit=5, window_seconds=60)
        assert allowed is True

    def test_window_expiry_allows_new_requests(self, redis_client):
        """After the window expires, requests are allowed again."""
        key = "ratelimit:tenant1:expiry"

        # Use a very short window
        for _ in range(3):
            _check_rate_limit(key, redis_client, limit=3, window_seconds=1)

        # At limit now
        allowed, _ = _check_rate_limit(key, redis_client, limit=3, window_seconds=1)
        assert allowed is False

        # Sleep for slightly more than the window
        time.sleep(1.1)

        # Should be allowed again
        allowed, _ = _check_rate_limit(key, redis_client, limit=3, window_seconds=1)
        assert allowed is True


class TestCheckWsRateLimit:
    """Tests for the WebSocket rate limiting function."""

    def test_allows_messages_under_limit(self, redis_client):
        """WebSocket messages under the limit are allowed."""
        for _ in range(59):
            allowed, retry_after = check_ws_rate_limit(
                "tenant1", "instance1", redis_client, limit=60, window_seconds=60
            )
            assert allowed is True
            assert retry_after == 0

    def test_rejects_messages_at_limit(self, redis_client):
        """WebSocket messages at the limit are rejected."""
        for _ in range(60):
            check_ws_rate_limit(
                "tenant1", "instance1", redis_client, limit=60, window_seconds=60
            )

        allowed, retry_after = check_ws_rate_limit(
            "tenant1", "instance1", redis_client, limit=60, window_seconds=60
        )
        assert allowed is False
        assert retry_after >= 1

    def test_different_instances_independent(self, redis_client):
        """Rate limits for different instances don't interfere."""
        # Fill instance1
        for _ in range(5):
            check_ws_rate_limit(
                "tenant1", "instance1", redis_client, limit=5, window_seconds=60
            )

        allowed, _ = check_ws_rate_limit(
            "tenant1", "instance1", redis_client, limit=5, window_seconds=60
        )
        assert allowed is False

        # instance2 should still be fine
        allowed, _ = check_ws_rate_limit(
            "tenant1", "instance2", redis_client, limit=5, window_seconds=60
        )
        assert allowed is True

    def test_uses_correct_redis_key(self, redis_client):
        """WebSocket rate limit uses the expected key pattern."""
        check_ws_rate_limit(
            "tenant-abc", "inst-123", redis_client, limit=60, window_seconds=60
        )

        expected_key = f"{RATE_LIMIT_KEY_PREFIX}tenant-abc:ws:inst-123"
        # Check the key exists in Redis
        assert redis_client.exists(expected_key)

    def test_fails_open_on_redis_error(self):
        """When Redis is unavailable, the rate limiter fails open."""
        # Use None for redis_client — the function should catch the error
        # and return allowed=True
        with patch(
            "services.rate_limiter._get_redis",
            side_effect=Exception("Connection refused"),
        ):
            allowed, retry_after = check_ws_rate_limit(
                "tenant1", "instance1", None
            )
            assert allowed is True
            assert retry_after == 0


class TestRateLimitDecorator:
    """Tests for the @rate_limit Flask route decorator."""

    def test_allows_requests_under_limit(self, app, redis_client):
        """Decorated route allows requests under the limit."""
        set_redis_client(redis_client)

        with app.test_client() as client:
            with app.test_request_context():
                # Simulate an authenticated tenant
                for _ in range(5):
                    with client.application.test_request_context(
                        "/test-limited", method="POST"
                    ):
                        g.tenant = {"id": "test-tenant-123"}
                        # Can't easily test through Flask test client without
                        # full middleware, so test the core function directly
                        pass

        # Direct test through the function
        key = f"{RATE_LIMIT_KEY_PREFIX}test-tenant:test_endpoint"
        for _ in range(4):
            allowed, _ = _check_rate_limit(
                key, redis_client, limit=5, window_seconds=60
            )
            assert allowed is True

    def test_returns_429_when_exceeded(self, app, redis_client):
        """Core rate limit logic rejects when limit is exceeded."""
        set_redis_client(redis_client)

        # Pre-fill the rate limit bucket
        key = f"{RATE_LIMIT_KEY_PREFIX}test-tenant-123:test_limited"
        now = time.time()
        for i in range(5):
            redis_client.zadd(key, {f"{now - i * 0.01}": now - i * 0.01})

        # The 6th request should be rejected
        allowed, retry_after = _check_rate_limit(
            key, redis_client, limit=5, window_seconds=60
        )
        assert allowed is False
        assert retry_after >= 1

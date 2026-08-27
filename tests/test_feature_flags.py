"""Unit tests for the feature flag computation and API.

Tests:
- Feature computation from plan + addons (all combinations)
- Redis caching behavior (cache hit/miss/invalidation)
- Pub/sub notification on subscription change
- API endpoint response format and error handling
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import fakeredis
import pytest

# Ensure web-ui/ is importable
_webui_dir = str(Path(__file__).resolve().parent.parent / "web-ui")
if _webui_dir not in sys.path:
    sys.path.insert(0, _webui_dir)

from services.feature_flags import (
    ALL_FEATURE_KEYS,
    FEATURE_CACHE_PREFIX,
    FEATURE_CACHE_TTL_SECONDS,
    FEATURE_CHANGE_CHANNEL_PREFIX,
    compute_features,
    get_features,
    invalidate_features_cache,
    on_subscription_change,
    publish_feature_change,
)


# ---------------------------------------------------------------------------
# compute_features tests
# ---------------------------------------------------------------------------


class TestComputeFeatures:
    """Test the pure computation logic."""

    def test_base_plan_audio_only(self):
        """Base plan enables only audio."""
        flags = compute_features("base", [])
        assert flags["audio"] is True
        assert flags["video"] is False
        assert flags["activity"] is False
        assert flags["hls"] is False
        assert flags["visualizer"] is False
        assert flags["tidal_hifi"] is False
        assert flags["lossless"] is False
        assert flags["priority_queue"] is False
        assert flags["max_bot_instances"] == 1
        assert flags["max_guilds_per_bot"] == 5

    def test_trial_plan_same_as_base(self):
        """Trial plan has same features as base (audio only)."""
        flags = compute_features("trial", [])
        assert flags["audio"] is True
        assert flags["video"] is False
        assert flags["tidal_hifi"] is False
        assert flags["max_bot_instances"] == 1

    def test_no_active_plan(self):
        """No active plan means all features disabled."""
        flags = compute_features(None, [])
        assert flags["audio"] is False
        assert flags["video"] is False
        assert flags["tidal_hifi"] is False
        assert flags["max_bot_instances"] == 1

    def test_video_addon(self):
        """Video addon enables video, activity, hls, visualizer."""
        flags = compute_features("base", ["video"])
        assert flags["audio"] is True
        assert flags["video"] is True
        assert flags["activity"] is True
        assert flags["hls"] is True
        assert flags["visualizer"] is True
        assert flags["tidal_hifi"] is False
        assert flags["lossless"] is False
        assert flags["priority_queue"] is False

    def test_premium_addon(self):
        """Premium addon enables tidal_hifi, lossless, priority_queue."""
        flags = compute_features("base", ["premium"])
        assert flags["audio"] is True
        assert flags["video"] is False
        assert flags["tidal_hifi"] is True
        assert flags["lossless"] is True
        assert flags["priority_queue"] is True

    def test_both_addons(self):
        """Both addons together enable all features."""
        flags = compute_features("base", ["video", "premium"])
        assert flags["audio"] is True
        assert flags["video"] is True
        assert flags["activity"] is True
        assert flags["hls"] is True
        assert flags["visualizer"] is True
        assert flags["tidal_hifi"] is True
        assert flags["lossless"] is True
        assert flags["priority_queue"] is True

    def test_additional_bot_increases_max_instances(self):
        """Each additional_bot addon increases max_bot_instances by 1."""
        flags = compute_features("base", ["additional_bot", "additional_bot", "additional_bot"])
        assert flags["max_bot_instances"] == 4  # 1 base + 3 additional

    def test_additional_bot_capped_at_10(self):
        """max_bot_instances is capped at 10."""
        many_bots = ["additional_bot"] * 20
        flags = compute_features("base", many_bots)
        assert flags["max_bot_instances"] == 10

    def test_none_addons_treated_as_empty(self):
        """None addons parameter treated as empty list."""
        flags = compute_features("base", None)
        assert flags["audio"] is True
        assert flags["video"] is False
        assert flags["max_bot_instances"] == 1

    def test_unknown_addon_ignored(self):
        """Unknown addon names are safely ignored."""
        flags = compute_features("base", ["unknown_thing", "video"])
        assert flags["audio"] is True
        assert flags["video"] is True
        assert flags["tidal_hifi"] is False

    def test_all_flag_keys_present(self):
        """All known feature keys are present in the result."""
        flags = compute_features("base", [])
        for key in ALL_FEATURE_KEYS:
            assert key in flags
        assert "max_bot_instances" in flags
        assert "max_guilds_per_bot" in flags

    def test_case_insensitive_addons(self):
        """Addon matching is case-insensitive."""
        flags = compute_features("base", ["Video", "PREMIUM"])
        assert flags["video"] is True
        assert flags["tidal_hifi"] is True


# ---------------------------------------------------------------------------
# Redis caching tests
# ---------------------------------------------------------------------------


class TestFeatureFlagsCaching:
    """Test Redis cache integration."""

    @pytest.fixture
    def redis_client(self):
        client = fakeredis.FakeRedis(decode_responses=True)
        yield client
        client.flushall()
        client.close()

    @pytest.fixture
    def mock_pg_conn(self):
        """Mock PostgreSQL connection returning base plan with no addons."""
        conn = MagicMock()
        cursor = MagicMock()
        cursor.fetchone.return_value = ("base", [])
        cursor.__enter__ = MagicMock(return_value=cursor)
        cursor.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value = cursor
        return conn

    def test_cache_miss_queries_db(self, redis_client, mock_pg_conn):
        """Cache miss triggers database query and caches result."""
        tenant_id = "11111111-1111-1111-1111-111111111111"
        flags = get_features(tenant_id, mock_pg_conn, redis_client)

        assert flags["audio"] is True
        assert flags["video"] is False

        # Verify cache was populated
        cached = redis_client.get(f"{FEATURE_CACHE_PREFIX}{tenant_id}")
        assert cached is not None
        assert json.loads(cached) == flags

    def test_cache_hit_skips_db(self, redis_client):
        """Cache hit returns cached data without touching DB."""
        tenant_id = "22222222-2222-2222-2222-222222222222"
        expected = compute_features("base", ["video"])
        redis_client.set(
            f"{FEATURE_CACHE_PREFIX}{tenant_id}",
            json.dumps(expected),
            ex=FEATURE_CACHE_TTL_SECONDS,
        )

        # Pass a broken pg_conn — should never be called
        broken_conn = MagicMock()
        broken_conn.cursor.side_effect = RuntimeError("Should not be called")

        flags = get_features(tenant_id, broken_conn, redis_client)
        assert flags == expected

    def test_cache_invalidation(self, redis_client):
        """Invalidation removes the cached entry."""
        tenant_id = "33333333-3333-3333-3333-333333333333"
        redis_client.set(
            f"{FEATURE_CACHE_PREFIX}{tenant_id}",
            json.dumps({"audio": True}),
        )

        invalidate_features_cache(tenant_id, redis_client)
        assert redis_client.get(f"{FEATURE_CACHE_PREFIX}{tenant_id}") is None

    def test_cache_ttl_set(self, redis_client, mock_pg_conn):
        """Cache entry has the correct TTL."""
        tenant_id = "44444444-4444-4444-4444-444444444444"
        get_features(tenant_id, mock_pg_conn, redis_client)

        ttl = redis_client.ttl(f"{FEATURE_CACHE_PREFIX}{tenant_id}")
        assert ttl > 0
        assert ttl <= FEATURE_CACHE_TTL_SECONDS


# ---------------------------------------------------------------------------
# Pub/sub tests
# ---------------------------------------------------------------------------


class TestPubSub:
    """Test Redis pub/sub notification."""

    @pytest.fixture
    def redis_client(self):
        client = fakeredis.FakeRedis(decode_responses=True)
        yield client
        client.flushall()
        client.close()

    def test_publish_feature_change(self, redis_client):
        """Publishing sends a message to the correct channel."""
        tenant_id = "55555555-5555-5555-5555-555555555555"

        # Subscribe to the channel
        pubsub = redis_client.pubsub()
        channel = f"{FEATURE_CHANGE_CHANNEL_PREFIX}{tenant_id}"
        pubsub.subscribe(channel)

        # Consume the subscription confirmation message
        msg = pubsub.get_message(timeout=1)
        assert msg is not None and msg["type"] == "subscribe"

        # Publish
        publish_feature_change(tenant_id, redis_client)

        # Verify the message was received
        msg = pubsub.get_message(timeout=1)
        assert msg is not None
        assert msg["type"] == "message"
        assert msg["channel"] == channel
        assert msg["data"] == "updated"

        pubsub.close()


# ---------------------------------------------------------------------------
# on_subscription_change integration test
# ---------------------------------------------------------------------------


class TestOnSubscriptionChange:
    """Test the full subscription change handler."""

    @pytest.fixture
    def redis_client(self):
        client = fakeredis.FakeRedis(decode_responses=True)
        yield client
        client.flushall()
        client.close()

    def test_on_subscription_change_invalidates_and_recomputes(self, redis_client):
        """Subscription change invalidates cache, recomputes, and publishes."""
        tenant_id = "66666666-6666-6666-6666-666666666666"

        # Pre-populate stale cache
        stale_flags = compute_features("base", [])
        redis_client.set(
            f"{FEATURE_CACHE_PREFIX}{tenant_id}",
            json.dumps(stale_flags),
        )

        # Mock DB now returns video addon active
        mock_conn = MagicMock()
        cursor = MagicMock()
        cursor.fetchone.return_value = ("base", ["video"])
        cursor.__enter__ = MagicMock(return_value=cursor)
        cursor.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value = cursor

        # Subscribe to catch the notification
        pubsub = redis_client.pubsub()
        channel = f"{FEATURE_CHANGE_CHANNEL_PREFIX}{tenant_id}"
        pubsub.subscribe(channel)
        pubsub.get_message(timeout=1)  # consume subscribe confirmation

        # Execute
        flags = on_subscription_change(tenant_id, mock_conn, redis_client)

        # Verify new flags reflect video addon
        assert flags["audio"] is True
        assert flags["video"] is True
        assert flags["activity"] is True

        # Verify cache was updated
        cached = json.loads(redis_client.get(f"{FEATURE_CACHE_PREFIX}{tenant_id}"))
        assert cached["video"] is True

        # Verify pub/sub message was sent
        msg = pubsub.get_message(timeout=1)
        assert msg is not None
        assert msg["data"] == "updated"

        pubsub.close()


# ---------------------------------------------------------------------------
# API endpoint tests
# ---------------------------------------------------------------------------


class TestFeaturesAPI:
    """Test the Flask blueprint endpoint."""

    @pytest.fixture
    def app(self):
        """Create a test Flask app with the features blueprint."""
        from flask import Flask
        from blueprints.features import features_bp

        app = Flask(__name__)
        app.register_blueprint(features_bp)
        app.config["TESTING"] = True
        return app

    @pytest.fixture
    def client(self, app):
        return app.test_client()

    def test_invalid_tenant_id_format(self, client):
        """Invalid UUID returns 400."""
        resp = client.get("/api/v1/features/not-a-uuid")
        assert resp.status_code == 400
        data = resp.get_json()
        assert "error" in data

    def test_empty_tenant_id(self, client):
        """Empty tenant_id returns 400."""
        resp = client.get("/api/v1/features/ ")
        assert resp.status_code == 400

    @patch("blueprints.features._get_pg_conn")
    @patch("blueprints.features._get_redis")
    def test_valid_request_returns_flags(self, mock_redis_fn, mock_pg_fn, client):
        """Valid tenant_id returns computed feature flags."""
        # Set up mock Redis with cached data
        fake_redis = fakeredis.FakeRedis(decode_responses=True)
        tenant_id = "77777777-7777-7777-7777-777777777777"
        expected_flags = compute_features("base", ["video"])
        fake_redis.set(
            f"{FEATURE_CACHE_PREFIX}{tenant_id}",
            json.dumps(expected_flags),
        )
        mock_redis_fn.return_value = fake_redis

        # PG shouldn't be called (cache hit)
        mock_pg = MagicMock()
        mock_pg_fn.return_value = mock_pg

        resp = client.get(f"/api/v1/features/{tenant_id}")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["audio"] is True
        assert data["video"] is True
        assert data["activity"] is True
        assert data["max_bot_instances"] == 1
        assert data["max_guilds_per_bot"] == 5

    @patch("blueprints.features._get_pg_conn")
    def test_pg_connection_failure_returns_500(self, mock_pg_fn, client):
        """PostgreSQL connection failure returns 500."""
        mock_pg_fn.side_effect = Exception("Connection refused")

        tenant_id = "88888888-8888-8888-8888-888888888888"
        resp = client.get(f"/api/v1/features/{tenant_id}")
        assert resp.status_code == 500
        data = resp.get_json()
        assert "error" in data

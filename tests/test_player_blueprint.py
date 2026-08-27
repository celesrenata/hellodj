"""Unit tests for the web player blueprint.

Tests cover:
- GET /player — authenticated page route
- REST API endpoints for playback control (state, play, pause, resume, skip, etc.)
- Ownership validation (HTTP 403 for non-owner tenants)
- WebSocket authentication and command forwarding
- Redis pub/sub command forwarding
- Volume validation (0-100 range)
- Queue operations (add, remove, move, clear)

Requirements: 16.1, 16.6, 16.7, 17.1, 17.2, 17.3, 17.4, 17.6
"""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import fakeredis
import pytest
from flask import Flask

# Add web-ui directory to path so blueprints package is importable
_webui_dir = str(Path(__file__).resolve().parent.parent / "web-ui")
if _webui_dir not in sys.path:
    sys.path.insert(0, _webui_dir)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def redis_client():
    """Provide a fresh fakeredis instance per test."""
    client = fakeredis.FakeRedis(decode_responses=True)
    yield client
    client.flushall()
    client.close()


@pytest.fixture
def tenant_id():
    return str(uuid.uuid4())


@pytest.fixture
def instance_id():
    return str(uuid.uuid4())


@pytest.fixture
def session_token():
    return "test-session-token-abc123"


@pytest.fixture
def tenant_data(tenant_id):
    return {
        "tenant_id": tenant_id,
        "discord_user_id": "123456789012345678",
        "discord_username": "TestUser",
        "email": "test@example.com",
        "avatar": "abc123",
    }


@pytest.fixture
def app(redis_client, tenant_id, instance_id, session_token, tenant_data):
    """Create a Flask test app with the player blueprint registered."""
    from blueprints.auth import auth_bp
    from blueprints.player import player_bp, init_app

    flask_app = Flask(__name__)
    flask_app.config["TESTING"] = True
    flask_app.secret_key = "test-secret"

    flask_app.register_blueprint(auth_bp)
    flask_app.register_blueprint(player_bp)
    init_app(flask_app)

    # Store a valid session in Redis
    redis_client.set(
        f"session:{session_token}",
        json.dumps(tenant_data),
        ex=604800,
    )

    return flask_app


@pytest.fixture
def client(app, redis_client, session_token, tenant_id, instance_id):
    """Flask test client with auth mocks and ownership validation in place."""
    # Mock the ownership check and Redis/PG connections
    with patch("blueprints.player._get_redis", return_value=redis_client), \
         patch("blueprints.player._get_pg_conn") as mock_pg, \
         patch("auth_middleware._get_redis", return_value=redis_client):

        # Mock PG connection for ownership checks
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_conn.close = MagicMock()
        mock_pg.return_value = mock_conn

        # Instance belongs to tenant by default
        mock_cursor.fetchone.return_value = (1,)

        test_client = app.test_client()
        test_client.set_cookie("session_token", session_token, domain="localhost")

        # Attach mocks for test-specific overrides
        test_client._mock_cursor = mock_cursor
        test_client._mock_pg = mock_pg
        test_client._redis = redis_client

        yield test_client


@pytest.fixture
def unauthed_client(app, redis_client):
    """Flask test client without authentication."""
    with patch("blueprints.player._get_redis", return_value=redis_client), \
         patch("auth_middleware._get_redis", return_value=redis_client):
        yield app.test_client()


# ---------------------------------------------------------------------------
# Tests: GET /player (page route)
# ---------------------------------------------------------------------------


class TestPlayerPage:
    """Tests for GET /player page route."""

    def test_player_page_requires_auth(self, unauthed_client):
        """Unauthenticated request should redirect to login."""
        response = unauthed_client.get("/player")
        assert response.status_code == 302
        assert "/auth/login" in response.headers["Location"]

    def test_player_page_authenticated(self, client, instance_id):
        """Authenticated request should return 200."""
        # Mock the PG query for fetching bot instances
        with patch("blueprints.player._get_pg_conn") as mock_pg:
            mock_conn = MagicMock()
            mock_cursor = MagicMock()
            mock_cursor.fetchall.return_value = []
            mock_cursor.description = [("id",), ("guild_ids",), ("status",)]
            mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
            mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
            mock_conn.close = MagicMock()
            mock_pg.return_value = mock_conn

            # We need the template to exist for the render, so mock it
            with patch("blueprints.player.render_template", return_value="<html>Player</html>"):
                response = client.get("/player")
                assert response.status_code == 200


# ---------------------------------------------------------------------------
# Tests: REST API — State
# ---------------------------------------------------------------------------


class TestGetState:
    """Tests for GET /api/v1/player/{instance_id}/state."""

    def test_get_state_returns_player_state(self, client, instance_id, redis_client):
        """Should return cached player state from Redis."""
        state = {
            "playing": True,
            "current": {"title": "Test Track", "artist": "Test Artist"},
            "queue": [],
            "volume": 75,
            "repeat": "off",
            "shuffle": False,
            "position_ms": 30000,
            "duration_ms": 180000,
        }
        redis_client.set(f"player_state:{instance_id}", json.dumps(state))

        response = client.get(f"/api/v1/player/{instance_id}/state")
        assert response.status_code == 200
        data = response.get_json()
        assert data["playing"] is True
        assert data["volume"] == 75
        assert data["current"]["title"] == "Test Track"

    def test_get_state_default_when_no_cache(self, client, instance_id):
        """Should return default state when no cached state exists."""
        response = client.get(f"/api/v1/player/{instance_id}/state")
        assert response.status_code == 200
        data = response.get_json()
        assert data["playing"] is False
        assert data["current"] is None
        assert data["queue"] == []
        assert data["volume"] == 50


# ---------------------------------------------------------------------------
# Tests: REST API — Playback controls
# ---------------------------------------------------------------------------


class TestPlaybackControls:
    """Tests for POST /api/v1/player/{instance_id}/play|pause|resume|skip|previous."""

    def test_play_requires_query(self, client, instance_id):
        """POST /play without a query should return 400."""
        response = client.post(
            f"/api/v1/player/{instance_id}/play",
            json={},
            content_type="application/json",
        )
        assert response.status_code == 400
        assert "query" in response.get_json()["error"].lower()

    def test_play_publishes_command(self, client, instance_id, redis_client):
        """POST /play should publish to Redis command channel."""
        pubsub = redis_client.pubsub()
        pubsub.subscribe(f"player_command:{instance_id}")
        # Consume subscribe message
        pubsub.get_message()

        response = client.post(
            f"/api/v1/player/{instance_id}/play",
            json={"query": "test song", "source": "youtube"},
            content_type="application/json",
        )
        assert response.status_code == 202
        data = response.get_json()
        assert data["status"] == "command_sent"
        assert data["action"] == "play"

        # Check message was published
        msg = pubsub.get_message()
        assert msg is not None
        assert msg["type"] == "message"
        payload = json.loads(msg["data"])
        assert payload["action"] == "play"
        assert payload["query"] == "test song"
        assert payload["source"] == "youtube"
        pubsub.close()

    def test_pause_publishes_command(self, client, instance_id, redis_client):
        """POST /pause should publish pause command."""
        pubsub = redis_client.pubsub()
        pubsub.subscribe(f"player_command:{instance_id}")
        pubsub.get_message()

        response = client.post(f"/api/v1/player/{instance_id}/pause")
        assert response.status_code == 202

        msg = pubsub.get_message()
        assert msg is not None
        payload = json.loads(msg["data"])
        assert payload["action"] == "pause"
        pubsub.close()

    def test_resume_publishes_command(self, client, instance_id, redis_client):
        """POST /resume should publish resume command."""
        pubsub = redis_client.pubsub()
        pubsub.subscribe(f"player_command:{instance_id}")
        pubsub.get_message()

        response = client.post(f"/api/v1/player/{instance_id}/resume")
        assert response.status_code == 202

        msg = pubsub.get_message()
        payload = json.loads(msg["data"])
        assert payload["action"] == "resume"
        pubsub.close()

    def test_skip_publishes_command(self, client, instance_id, redis_client):
        """POST /skip should publish skip command."""
        pubsub = redis_client.pubsub()
        pubsub.subscribe(f"player_command:{instance_id}")
        pubsub.get_message()

        response = client.post(f"/api/v1/player/{instance_id}/skip")
        assert response.status_code == 202

        msg = pubsub.get_message()
        payload = json.loads(msg["data"])
        assert payload["action"] == "skip"
        pubsub.close()

    def test_previous_publishes_command(self, client, instance_id, redis_client):
        """POST /previous should publish previous command."""
        pubsub = redis_client.pubsub()
        pubsub.subscribe(f"player_command:{instance_id}")
        pubsub.get_message()

        response = client.post(f"/api/v1/player/{instance_id}/previous")
        assert response.status_code == 202

        msg = pubsub.get_message()
        payload = json.loads(msg["data"])
        assert payload["action"] == "previous"
        pubsub.close()

    def test_shuffle_publishes_command(self, client, instance_id, redis_client):
        """POST /shuffle should publish shuffle command."""
        pubsub = redis_client.pubsub()
        pubsub.subscribe(f"player_command:{instance_id}")
        pubsub.get_message()

        response = client.post(f"/api/v1/player/{instance_id}/shuffle")
        assert response.status_code == 202

        msg = pubsub.get_message()
        payload = json.loads(msg["data"])
        assert payload["action"] == "shuffle"
        pubsub.close()

    def test_repeat_publishes_command(self, client, instance_id, redis_client):
        """POST /repeat should publish repeat command with optional mode."""
        pubsub = redis_client.pubsub()
        pubsub.subscribe(f"player_command:{instance_id}")
        pubsub.get_message()

        response = client.post(
            f"/api/v1/player/{instance_id}/repeat",
            json={"mode": "one"},
            content_type="application/json",
        )
        assert response.status_code == 202

        msg = pubsub.get_message()
        payload = json.loads(msg["data"])
        assert payload["action"] == "repeat"
        assert payload["mode"] == "one"
        pubsub.close()


# ---------------------------------------------------------------------------
# Tests: REST API — Volume
# ---------------------------------------------------------------------------


class TestVolume:
    """Tests for POST /api/v1/player/{instance_id}/volume."""

    def test_volume_valid(self, client, instance_id, redis_client):
        """Should accept valid volume 0-100."""
        pubsub = redis_client.pubsub()
        pubsub.subscribe(f"player_command:{instance_id}")
        pubsub.get_message()

        response = client.post(
            f"/api/v1/player/{instance_id}/volume",
            json={"volume": 75},
            content_type="application/json",
        )
        assert response.status_code == 202
        data = response.get_json()
        assert data["value"] == 75

        msg = pubsub.get_message()
        payload = json.loads(msg["data"])
        assert payload["action"] == "volume"
        assert payload["value"] == 75
        pubsub.close()

    def test_volume_missing(self, client, instance_id):
        """Missing volume parameter should return 400."""
        response = client.post(
            f"/api/v1/player/{instance_id}/volume",
            json={},
            content_type="application/json",
        )
        assert response.status_code == 400
        assert "volume" in response.get_json()["error"].lower()

    def test_volume_out_of_range_high(self, client, instance_id):
        """Volume > 100 should return 400."""
        response = client.post(
            f"/api/v1/player/{instance_id}/volume",
            json={"volume": 150},
            content_type="application/json",
        )
        assert response.status_code == 400

    def test_volume_out_of_range_low(self, client, instance_id):
        """Volume < 0 should return 400."""
        response = client.post(
            f"/api/v1/player/{instance_id}/volume",
            json={"volume": -5},
            content_type="application/json",
        )
        assert response.status_code == 400

    def test_volume_invalid_type(self, client, instance_id):
        """Non-numeric volume should return 400."""
        response = client.post(
            f"/api/v1/player/{instance_id}/volume",
            json={"volume": "loud"},
            content_type="application/json",
        )
        assert response.status_code == 400

    def test_volume_zero(self, client, instance_id, redis_client):
        """Volume 0 (mute) should be accepted."""
        pubsub = redis_client.pubsub()
        pubsub.subscribe(f"player_command:{instance_id}")
        pubsub.get_message()

        response = client.post(
            f"/api/v1/player/{instance_id}/volume",
            json={"volume": 0},
            content_type="application/json",
        )
        assert response.status_code == 202
        pubsub.close()

    def test_volume_100(self, client, instance_id, redis_client):
        """Volume 100 (max) should be accepted."""
        pubsub = redis_client.pubsub()
        pubsub.subscribe(f"player_command:{instance_id}")
        pubsub.get_message()

        response = client.post(
            f"/api/v1/player/{instance_id}/volume",
            json={"volume": 100},
            content_type="application/json",
        )
        assert response.status_code == 202
        pubsub.close()


# ---------------------------------------------------------------------------
# Tests: REST API — Queue operations
# ---------------------------------------------------------------------------


class TestQueueOperations:
    """Tests for queue add/remove/move/clear endpoints."""

    def test_queue_add_with_query(self, client, instance_id, redis_client):
        """Should accept queue_add with query parameter."""
        pubsub = redis_client.pubsub()
        pubsub.subscribe(f"player_command:{instance_id}")
        pubsub.get_message()

        response = client.post(
            f"/api/v1/player/{instance_id}/queue/add",
            json={"query": "my song", "source": "spotify"},
            content_type="application/json",
        )
        assert response.status_code == 202

        msg = pubsub.get_message()
        payload = json.loads(msg["data"])
        assert payload["action"] == "queue_add"
        assert payload["query"] == "my song"
        assert payload["source"] == "spotify"
        pubsub.close()

    def test_queue_add_with_url(self, client, instance_id, redis_client):
        """Should accept queue_add with url parameter."""
        pubsub = redis_client.pubsub()
        pubsub.subscribe(f"player_command:{instance_id}")
        pubsub.get_message()

        response = client.post(
            f"/api/v1/player/{instance_id}/queue/add",
            json={"url": "https://youtube.com/watch?v=abc123"},
            content_type="application/json",
        )
        assert response.status_code == 202

        msg = pubsub.get_message()
        payload = json.loads(msg["data"])
        assert payload["action"] == "queue_add"
        assert payload["url"] == "https://youtube.com/watch?v=abc123"
        pubsub.close()

    def test_queue_add_missing_params(self, client, instance_id):
        """queue_add without query or url should return 400."""
        response = client.post(
            f"/api/v1/player/{instance_id}/queue/add",
            json={},
            content_type="application/json",
        )
        assert response.status_code == 400

    def test_queue_remove(self, client, instance_id, redis_client):
        """Should accept queue_remove with index."""
        pubsub = redis_client.pubsub()
        pubsub.subscribe(f"player_command:{instance_id}")
        pubsub.get_message()

        response = client.post(
            f"/api/v1/player/{instance_id}/queue/remove",
            json={"index": 2},
            content_type="application/json",
        )
        assert response.status_code == 202

        msg = pubsub.get_message()
        payload = json.loads(msg["data"])
        assert payload["action"] == "queue_remove"
        assert payload["index"] == 2
        pubsub.close()

    def test_queue_remove_missing_index(self, client, instance_id):
        """queue_remove without index should return 400."""
        response = client.post(
            f"/api/v1/player/{instance_id}/queue/remove",
            json={},
            content_type="application/json",
        )
        assert response.status_code == 400

    def test_queue_move(self, client, instance_id, redis_client):
        """Should accept queue_move with from/to."""
        pubsub = redis_client.pubsub()
        pubsub.subscribe(f"player_command:{instance_id}")
        pubsub.get_message()

        response = client.post(
            f"/api/v1/player/{instance_id}/queue/move",
            json={"from": 3, "to": 0},
            content_type="application/json",
        )
        assert response.status_code == 202

        msg = pubsub.get_message()
        payload = json.loads(msg["data"])
        assert payload["action"] == "queue_move"
        assert payload["from"] == 3
        assert payload["to"] == 0
        pubsub.close()

    def test_queue_move_missing_params(self, client, instance_id):
        """queue_move without from/to should return 400."""
        response = client.post(
            f"/api/v1/player/{instance_id}/queue/move",
            json={"from": 1},
            content_type="application/json",
        )
        assert response.status_code == 400

    def test_queue_clear(self, client, instance_id, redis_client):
        """DELETE /queue should publish queue_clear command."""
        pubsub = redis_client.pubsub()
        pubsub.subscribe(f"player_command:{instance_id}")
        pubsub.get_message()

        response = client.delete(f"/api/v1/player/{instance_id}/queue")
        assert response.status_code == 202

        msg = pubsub.get_message()
        payload = json.loads(msg["data"])
        assert payload["action"] == "queue_clear"
        pubsub.close()


# ---------------------------------------------------------------------------
# Tests: Ownership validation (HTTP 403)
# ---------------------------------------------------------------------------


class TestOwnershipValidation:
    """Tests for tenant ownership validation on all player endpoints."""

    def test_non_owner_gets_403(self, app, redis_client, session_token, tenant_data, instance_id):
        """Non-owner tenant should receive HTTP 403."""
        with patch("blueprints.player._get_redis", return_value=redis_client), \
             patch("blueprints.player._get_pg_conn") as mock_pg, \
             patch("auth_middleware._get_redis", return_value=redis_client):

            mock_conn = MagicMock()
            mock_cursor = MagicMock()
            mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
            mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
            mock_conn.close = MagicMock()
            mock_pg.return_value = mock_conn

            # Instance does NOT belong to tenant
            mock_cursor.fetchone.return_value = None

            test_client = app.test_client()
            test_client.set_cookie("session_token", session_token, domain="localhost")

            response = test_client.get(f"/api/v1/player/{instance_id}/state")
            assert response.status_code == 403
            assert "forbidden" in response.get_json()["error"].lower()

    def test_invalid_instance_id_gets_400(self, client):
        """Invalid UUID for instance_id should return 400."""
        response = client.get("/api/v1/player/not-a-uuid/state")
        assert response.status_code == 400
        assert "invalid" in response.get_json()["error"].lower()

    def test_unauthenticated_gets_redirect(self, unauthed_client, instance_id):
        """Unauthenticated request to player API should redirect to login."""
        response = unauthed_client.get(f"/api/v1/player/{instance_id}/state")
        assert response.status_code == 302
        assert "/auth/login" in response.headers["Location"]


# ---------------------------------------------------------------------------
# Tests: WebSocket authentication
# ---------------------------------------------------------------------------


class TestWebSocketAuth:
    """Tests for WebSocket token authentication."""

    def test_ws_auth_valid_token(self, redis_client, tenant_data, session_token):
        """Valid session token should authenticate successfully."""
        from blueprints.player import _authenticate_ws_token

        redis_client.set(f"session:{session_token}", json.dumps(tenant_data), ex=604800)

        with patch("blueprints.player._get_redis", return_value=redis_client):
            result = _authenticate_ws_token(session_token)

        assert result is not None
        assert result["tenant_id"] == tenant_data["tenant_id"]

    def test_ws_auth_invalid_token(self, redis_client):
        """Invalid token should return None."""
        from blueprints.player import _authenticate_ws_token

        with patch("blueprints.player._get_redis", return_value=redis_client):
            result = _authenticate_ws_token("invalid-token")

        assert result is None

    def test_ws_auth_empty_token(self, redis_client):
        """Empty token should return None."""
        from blueprints.player import _authenticate_ws_token

        with patch("blueprints.player._get_redis", return_value=redis_client):
            result = _authenticate_ws_token("")

        assert result is None

    def test_ws_auth_expired_token(self, redis_client):
        """Expired session (not in Redis) should return None."""
        from blueprints.player import _authenticate_ws_token

        # Token was never stored (simulating expiry)
        with patch("blueprints.player._get_redis", return_value=redis_client):
            result = _authenticate_ws_token("expired-token-abc")

        assert result is None


# ---------------------------------------------------------------------------
# Tests: WebSocket connection registry
# ---------------------------------------------------------------------------


class TestWSConnectionRegistry:
    """Tests for the WebSocket connection registry."""

    def test_register_and_unregister(self):
        """Registering and unregistering a WS connection should work."""
        from blueprints.player import (
            _register_ws, _unregister_ws, _ws_connections, _ws_lock,
        )

        instance_id = str(uuid.uuid4())
        mock_ws = MagicMock()

        # Patch _ensure_subscriber to avoid starting real threads
        with patch("blueprints.player._ensure_subscriber"):
            _register_ws(instance_id, mock_ws)

        with _ws_lock:
            assert instance_id in _ws_connections
            assert mock_ws in _ws_connections[instance_id]

        _unregister_ws(instance_id, mock_ws)

        with _ws_lock:
            assert instance_id not in _ws_connections

    def test_broadcast_to_instance(self):
        """Broadcasting should send to all registered WS clients."""
        from blueprints.player import (
            _broadcast_to_instance, _ws_connections, _ws_lock,
        )

        instance_id = str(uuid.uuid4())
        ws1 = MagicMock()
        ws2 = MagicMock()

        with _ws_lock:
            _ws_connections[instance_id] = {ws1, ws2}

        _broadcast_to_instance(instance_id, '{"type": "test"}')

        ws1.send.assert_called_once_with('{"type": "test"}')
        ws2.send.assert_called_once_with('{"type": "test"}')

        # Cleanup
        with _ws_lock:
            del _ws_connections[instance_id]

    def test_broadcast_removes_dead_connections(self):
        """Dead connections (send raises) should be removed."""
        from blueprints.player import (
            _broadcast_to_instance, _ws_connections, _ws_lock,
        )

        instance_id = str(uuid.uuid4())
        ws_alive = MagicMock()
        ws_dead = MagicMock()
        ws_dead.send.side_effect = Exception("Connection closed")

        with _ws_lock:
            _ws_connections[instance_id] = {ws_alive, ws_dead}

        _broadcast_to_instance(instance_id, '{"type": "test"}')

        ws_alive.send.assert_called_once_with('{"type": "test"}')

        with _ws_lock:
            conns = _ws_connections.get(instance_id, set())
            assert ws_dead not in conns
            assert ws_alive in conns
            # Cleanup
            del _ws_connections[instance_id]

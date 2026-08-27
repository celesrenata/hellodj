"""Web player blueprint for HelloDJ SaaS platform.

Provides:
- GET /player — authenticated route rendering the web player page
- REST endpoints at /api/v1/player/{instance_id}/... for playback control
- WebSocket at /ws/player/{instance_id} via flask-sock for real-time state

Commands are forwarded to bot instances via Redis pub/sub.
State updates from bots are broadcast to all connected WebSocket clients.

Requirements: 16.1, 16.6, 16.7, 17.1, 17.2, 17.3, 17.4, 17.6
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from typing import Any

import psycopg2
import psycopg2.extras
import redis
from flask import Blueprint, g, jsonify, render_template, request
from flask_sock import Sock

from auth_middleware import login_required

log = logging.getLogger(__name__)

player_bp = Blueprint("player", __name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PG_URI = os.environ.get(
    "HELLODJ_PG_URI",
    "postgresql://hellodj:hellodj@postgresql-rw.postgresql-service.svc.cluster.local:5432/hellodj",
)
REDIS_URL = os.environ.get(
    "REDIS_URL", "redis://redis.redis-service.svc.cluster.local:6379/0"
)

# Redis pub/sub channels
PLAYER_COMMAND_CHANNEL_PREFIX = "player_command:"
PLAYER_UPDATE_CHANNEL_PREFIX = "player_update:"

# WebSocket rate limit: 60 messages per minute per connection
WS_RATE_LIMIT_MESSAGES = 60
WS_RATE_LIMIT_WINDOW_SECONDS = 60

# ---------------------------------------------------------------------------
# flask-sock instance (attached to app in register function)
# ---------------------------------------------------------------------------

sock = Sock()

# ---------------------------------------------------------------------------
# WebSocket connection registry
# Maps instance_id -> set of WebSocket connections (ws objects)
# ---------------------------------------------------------------------------

_ws_connections: dict[str, set] = {}
_ws_lock = threading.Lock()

# Track active subscriber threads per instance_id
_subscriber_threads: dict[str, threading.Thread] = {}
_subscriber_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------


def _get_pg_conn():
    """Get a psycopg2 connection."""
    return psycopg2.connect(PG_URI)


def _get_redis() -> redis.Redis:
    """Get a Redis client."""
    return redis.from_url(REDIS_URL, decode_responses=True)


def _instance_belongs_to_tenant(instance_id: str, tenant_id: str) -> bool:
    """Verify that a bot instance belongs to the given tenant."""
    conn = _get_pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM bot_instances WHERE id = %s AND tenant_id = %s",
                (instance_id, tenant_id),
            )
            return cur.fetchone() is not None
    finally:
        conn.close()


def _validate_instance_id(instance_id: str) -> str | None:
    """Validate instance_id is a valid UUID. Returns error message or None."""
    try:
        uuid.UUID(instance_id)
        return None
    except (ValueError, TypeError):
        return "Invalid instance ID format"


def _get_tenant_id() -> str | None:
    """Extract tenant_id from the request context (set by @login_required)."""
    tenant = g.tenant
    return tenant.get("tenant_id") or tenant.get("id")


def _check_ownership(instance_id: str) -> tuple[bool, Any]:
    """Check instance_id validity and tenant ownership.

    Returns (success, error_response). If success is True, error_response is None.
    """
    err = _validate_instance_id(instance_id)
    if err:
        return False, (jsonify({"error": err}), 400)

    tenant_id = _get_tenant_id()
    if not tenant_id:
        return False, (jsonify({"error": "No tenant ID in session"}), 401)

    if not _instance_belongs_to_tenant(instance_id, tenant_id):
        return False, (jsonify({"error": "Forbidden: you do not own this instance"}), 403)

    return True, None


def _publish_command(instance_id: str, command: dict) -> None:
    """Publish a player command to the Redis channel for the bot instance."""
    r = _get_redis()
    channel = f"{PLAYER_COMMAND_CHANNEL_PREFIX}{instance_id}"
    payload = json.dumps(command)
    r.publish(channel, payload)
    log.debug("Published command to %s: %s", channel, payload)


def _get_player_state(instance_id: str) -> dict:
    """Get cached player state from Redis."""
    r = _get_redis()
    raw = r.get(f"player_state:{instance_id}")
    if raw:
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            pass
    return {
        "playing": False,
        "current": None,
        "queue": [],
        "volume": 50,
        "repeat": "off",
        "shuffle": False,
        "position_ms": 0,
        "duration_ms": 0,
    }


# ---------------------------------------------------------------------------
# WebSocket helpers
# ---------------------------------------------------------------------------


def _register_ws(instance_id: str, ws) -> None:
    """Register a WebSocket connection for an instance."""
    with _ws_lock:
        if instance_id not in _ws_connections:
            _ws_connections[instance_id] = set()
        _ws_connections[instance_id].add(ws)
    _ensure_subscriber(instance_id)


def _unregister_ws(instance_id: str, ws) -> None:
    """Unregister a WebSocket connection for an instance."""
    with _ws_lock:
        conns = _ws_connections.get(instance_id)
        if conns:
            conns.discard(ws)
            if not conns:
                del _ws_connections[instance_id]


def _broadcast_to_instance(instance_id: str, message: str) -> None:
    """Broadcast a message to all WebSocket clients for an instance."""
    with _ws_lock:
        conns = _ws_connections.get(instance_id, set()).copy()

    dead = []
    for ws in conns:
        try:
            ws.send(message)
        except Exception:
            dead.append(ws)

    # Clean up dead connections
    if dead:
        with _ws_lock:
            conns = _ws_connections.get(instance_id)
            if conns:
                for ws in dead:
                    conns.discard(ws)
                if not conns:
                    del _ws_connections[instance_id]


def _ensure_subscriber(instance_id: str) -> None:
    """Ensure a Redis subscriber thread is running for the given instance."""
    with _subscriber_lock:
        thread = _subscriber_threads.get(instance_id)
        if thread and thread.is_alive():
            return
        t = threading.Thread(
            target=_subscriber_loop,
            args=(instance_id,),
            daemon=True,
            name=f"player-sub-{instance_id[:8]}",
        )
        _subscriber_threads[instance_id] = t
        t.start()


def _subscriber_loop(instance_id: str) -> None:
    """Subscribe to Redis player_update:{instance_id} and broadcast updates.

    Runs in a daemon thread. Exits when there are no more connections for
    the instance (checked periodically).
    """
    channel = f"{PLAYER_UPDATE_CHANNEL_PREFIX}{instance_id}"
    log.info("Starting Redis subscriber for %s", channel)

    while True:
        # Check if there are still active connections
        with _ws_lock:
            if instance_id not in _ws_connections or not _ws_connections[instance_id]:
                log.info("No more WebSocket clients for %s, stopping subscriber", instance_id)
                with _subscriber_lock:
                    _subscriber_threads.pop(instance_id, None)
                return

        try:
            r = redis.from_url(REDIS_URL, decode_responses=True)
            pubsub = r.pubsub()
            pubsub.subscribe(channel)

            for message in pubsub.listen():
                # Check connections periodically
                with _ws_lock:
                    if instance_id not in _ws_connections or not _ws_connections[instance_id]:
                        pubsub.unsubscribe(channel)
                        pubsub.close()
                        with _subscriber_lock:
                            _subscriber_threads.pop(instance_id, None)
                        log.info("Subscriber %s stopping: no connections", channel)
                        return

                if message["type"] == "message":
                    _broadcast_to_instance(instance_id, message["data"])

        except redis.RedisError as exc:
            log.warning("Redis subscriber error for %s: %s", channel, exc)
            time.sleep(2)  # Brief backoff before reconnect
        except Exception as exc:
            log.error("Unexpected error in subscriber for %s: %s", channel, exc)
            time.sleep(2)


def _authenticate_ws_token(token: str) -> dict | None:
    """Validate a session token from WebSocket query param.

    Uses the same session lookup as cookie auth: Redis key session:{token}.
    Returns the tenant dict or None.
    """
    if not token:
        return None
    r = _get_redis()
    raw = r.get(f"session:{token}")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Page route
# ---------------------------------------------------------------------------


@player_bp.route("/player", methods=["GET"])
@login_required
def player_page():
    """Render the web player page.

    Displays the player UI for the tenant's bot instances.
    """
    tenant = g.tenant
    tenant_id = tenant.get("tenant_id") or tenant.get("id")

    if not tenant_id:
        return jsonify({"error": "No tenant ID in session"}), 401

    # Fetch tenant's bot instances
    conn = _get_pg_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id, guild_ids, status, pod_name, node_name, created_at
                FROM bot_instances
                WHERE tenant_id = %s
                ORDER BY created_at
                """,
                (tenant_id,),
            )
            instances = [dict(row) for row in cur.fetchall()]
    finally:
        conn.close()

    return render_template(
        "pages/player.html",
        active="player",
        instances=instances,
    )


# ---------------------------------------------------------------------------
# REST API — Player Control
# ---------------------------------------------------------------------------


@player_bp.route("/api/v1/player/<instance_id>/state", methods=["GET"])
@login_required
def get_state(instance_id: str):
    """Get the current playback state for a bot instance."""
    ok, err = _check_ownership(instance_id)
    if not ok:
        return err

    state = _get_player_state(instance_id)
    return jsonify(state), 200


@player_bp.route("/api/v1/player/<instance_id>/play", methods=["POST"])
@login_required
def play(instance_id: str):
    """Search and queue a track on the bot instance."""
    ok, err = _check_ownership(instance_id)
    if not ok:
        return err

    data = request.get_json(silent=True) or {}
    query = data.get("query", "").strip()
    source = data.get("source", "")

    if not query:
        return jsonify({"error": "Missing 'query' parameter"}), 400

    _publish_command(instance_id, {
        "action": "play",
        "query": query,
        "source": source,
    })
    return jsonify({"status": "command_sent", "action": "play"}), 202


@player_bp.route("/api/v1/player/<instance_id>/pause", methods=["POST"])
@login_required
def pause(instance_id: str):
    """Pause playback on the bot instance."""
    ok, err = _check_ownership(instance_id)
    if not ok:
        return err

    _publish_command(instance_id, {"action": "pause"})
    return jsonify({"status": "command_sent", "action": "pause"}), 202


@player_bp.route("/api/v1/player/<instance_id>/resume", methods=["POST"])
@login_required
def resume(instance_id: str):
    """Resume playback on the bot instance."""
    ok, err = _check_ownership(instance_id)
    if not ok:
        return err

    _publish_command(instance_id, {"action": "resume"})
    return jsonify({"status": "command_sent", "action": "resume"}), 202


@player_bp.route("/api/v1/player/<instance_id>/skip", methods=["POST"])
@login_required
def skip(instance_id: str):
    """Skip the current track."""
    ok, err = _check_ownership(instance_id)
    if not ok:
        return err

    _publish_command(instance_id, {"action": "skip"})
    return jsonify({"status": "command_sent", "action": "skip"}), 202


@player_bp.route("/api/v1/player/<instance_id>/previous", methods=["POST"])
@login_required
def previous(instance_id: str):
    """Go to the previous track."""
    ok, err = _check_ownership(instance_id)
    if not ok:
        return err

    _publish_command(instance_id, {"action": "previous"})
    return jsonify({"status": "command_sent", "action": "previous"}), 202


@player_bp.route("/api/v1/player/<instance_id>/shuffle", methods=["POST"])
@login_required
def shuffle(instance_id: str):
    """Toggle shuffle mode."""
    ok, err = _check_ownership(instance_id)
    if not ok:
        return err

    _publish_command(instance_id, {"action": "shuffle"})
    return jsonify({"status": "command_sent", "action": "shuffle"}), 202


@player_bp.route("/api/v1/player/<instance_id>/repeat", methods=["POST"])
@login_required
def repeat(instance_id: str):
    """Toggle repeat mode (off → one → all → off)."""
    ok, err = _check_ownership(instance_id)
    if not ok:
        return err

    data = request.get_json(silent=True) or {}
    mode = data.get("mode")  # optional: "off", "one", "all"

    cmd = {"action": "repeat"}
    if mode:
        cmd["mode"] = mode

    _publish_command(instance_id, cmd)
    return jsonify({"status": "command_sent", "action": "repeat"}), 202


@player_bp.route("/api/v1/player/<instance_id>/volume", methods=["POST"])
@login_required
def volume(instance_id: str):
    """Set the playback volume (0-100)."""
    ok, err = _check_ownership(instance_id)
    if not ok:
        return err

    data = request.get_json(silent=True) or {}
    vol = data.get("volume")

    if vol is None:
        return jsonify({"error": "Missing 'volume' parameter"}), 400

    try:
        vol = int(vol)
    except (ValueError, TypeError):
        return jsonify({"error": "Volume must be an integer"}), 400

    if vol < 0 or vol > 100:
        return jsonify({"error": "Volume must be between 0 and 100"}), 400

    _publish_command(instance_id, {"action": "volume", "value": vol})
    return jsonify({"status": "command_sent", "action": "volume", "value": vol}), 202


@player_bp.route("/api/v1/player/<instance_id>/queue/add", methods=["POST"])
@login_required
def queue_add(instance_id: str):
    """Add a track to the queue."""
    ok, err = _check_ownership(instance_id)
    if not ok:
        return err

    data = request.get_json(silent=True) or {}
    query = data.get("query", "").strip()
    url = data.get("url", "").strip()
    source = data.get("source", "")

    if not query and not url:
        return jsonify({"error": "Provide 'query' or 'url' parameter"}), 400

    _publish_command(instance_id, {
        "action": "queue_add",
        "query": query,
        "url": url,
        "source": source,
    })
    return jsonify({"status": "command_sent", "action": "queue_add"}), 202


@player_bp.route("/api/v1/player/<instance_id>/queue/remove", methods=["POST"])
@login_required
def queue_remove(instance_id: str):
    """Remove a track from the queue by index."""
    ok, err = _check_ownership(instance_id)
    if not ok:
        return err

    data = request.get_json(silent=True) or {}
    index = data.get("index")

    if index is None:
        return jsonify({"error": "Missing 'index' parameter"}), 400

    try:
        index = int(index)
    except (ValueError, TypeError):
        return jsonify({"error": "Index must be an integer"}), 400

    _publish_command(instance_id, {"action": "queue_remove", "index": index})
    return jsonify({"status": "command_sent", "action": "queue_remove"}), 202


@player_bp.route("/api/v1/player/<instance_id>/queue/move", methods=["POST"])
@login_required
def queue_move(instance_id: str):
    """Move a track in the queue from one position to another."""
    ok, err = _check_ownership(instance_id)
    if not ok:
        return err

    data = request.get_json(silent=True) or {}
    from_index = data.get("from")
    to_index = data.get("to")

    if from_index is None or to_index is None:
        return jsonify({"error": "Missing 'from' and/or 'to' parameters"}), 400

    try:
        from_index = int(from_index)
        to_index = int(to_index)
    except (ValueError, TypeError):
        return jsonify({"error": "from/to must be integers"}), 400

    _publish_command(instance_id, {
        "action": "queue_move",
        "from": from_index,
        "to": to_index,
    })
    return jsonify({"status": "command_sent", "action": "queue_move"}), 202


@player_bp.route("/api/v1/player/<instance_id>/queue", methods=["DELETE"])
@login_required
def queue_clear(instance_id: str):
    """Clear the entire queue."""
    ok, err = _check_ownership(instance_id)
    if not ok:
        return err

    _publish_command(instance_id, {"action": "queue_clear"})
    return jsonify({"status": "command_sent", "action": "queue_clear"}), 202


# ---------------------------------------------------------------------------
# WebSocket — Real-time player state
# ---------------------------------------------------------------------------


@sock.route("/ws/player/<instance_id>")
def player_ws(ws, instance_id: str):
    """WebSocket endpoint for real-time player state updates.

    Authentication via query param: ?token={session_token}
    Validates tenant owns the bot instance.

    Server → Client messages:
    - {"type": "track_change", "current": {...}, "queue": [...]}
    - {"type": "progress", "position_ms": 45000, "duration_ms": 210000}
    - {"type": "volume_change", "volume": 75}
    - {"type": "state_change", "playing": true, "repeat": "off", "shuffle": false}
    - {"type": "bot_status", "status": "running"}

    Client → Server messages:
    - {"type": "command", "action": "pause"}
    - {"type": "command", "action": "skip"}
    - {"type": "command", "action": "volume", "value": 80}
    - {"type": "ping"}
    """
    # Authenticate via query param token
    token = request.args.get("token", "")
    tenant = _authenticate_ws_token(token)

    if not tenant:
        try:
            ws.send(json.dumps({"type": "error", "message": "Authentication failed"}))
        except Exception:
            pass
        ws.close(1008, "Authentication failed")
        return

    # Validate instance_id
    err = _validate_instance_id(instance_id)
    if err:
        try:
            ws.send(json.dumps({"type": "error", "message": err}))
        except Exception:
            pass
        ws.close(1008, err)
        return

    # Validate ownership
    tenant_id = tenant.get("tenant_id") or tenant.get("id")
    if not tenant_id or not _instance_belongs_to_tenant(instance_id, tenant_id):
        try:
            ws.send(json.dumps({"type": "error", "message": "Forbidden"}))
        except Exception:
            pass
        ws.close(1008, "Forbidden")
        return

    log.info("WebSocket connected: instance=%s tenant=%s", instance_id[:8], tenant_id[:8])

    # Register the connection
    _register_ws(instance_id, ws)

    # Send initial state
    try:
        state = _get_player_state(instance_id)
        ws.send(json.dumps({"type": "state_change", **state}))
    except Exception:
        pass

    # Rate limiter for incoming messages
    message_times: list[float] = []

    try:
        while True:
            try:
                raw = ws.receive(timeout=30)
            except Exception:
                # Connection closed or timeout
                break

            if raw is None:
                # Connection closed
                break

            # Rate limit check
            now = time.time()
            # Evict old entries outside the window
            message_times = [
                t for t in message_times
                if now - t < WS_RATE_LIMIT_WINDOW_SECONDS
            ]
            if len(message_times) >= WS_RATE_LIMIT_MESSAGES:
                ws.send(json.dumps({
                    "type": "error",
                    "message": "Rate limit exceeded (60 messages/minute)",
                }))
                continue
            message_times.append(now)

            # Parse message
            try:
                msg = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                ws.send(json.dumps({"type": "error", "message": "Invalid JSON"}))
                continue

            msg_type = msg.get("type")

            if msg_type == "ping":
                ws.send(json.dumps({"type": "pong"}))
                continue

            if msg_type == "command":
                action = msg.get("action")
                if not action:
                    ws.send(json.dumps({"type": "error", "message": "Missing action"}))
                    continue

                # Build command payload
                cmd: dict[str, Any] = {"action": action}
                if "value" in msg:
                    cmd["value"] = msg["value"]
                if "query" in msg:
                    cmd["query"] = msg["query"]
                if "index" in msg:
                    cmd["index"] = msg["index"]
                if "from" in msg:
                    cmd["from"] = msg["from"]
                if "to" in msg:
                    cmd["to"] = msg["to"]
                if "mode" in msg:
                    cmd["mode"] = msg["mode"]
                if "source" in msg:
                    cmd["source"] = msg["source"]
                if "url" in msg:
                    cmd["url"] = msg["url"]

                _publish_command(instance_id, cmd)
                ws.send(json.dumps({
                    "type": "command_ack",
                    "action": action,
                }))
                continue

            # Unknown message type
            ws.send(json.dumps({
                "type": "error",
                "message": f"Unknown message type: {msg_type}",
            }))

    except Exception as exc:
        log.debug("WebSocket error for instance=%s: %s", instance_id[:8], exc)
    finally:
        _unregister_ws(instance_id, ws)
        log.info("WebSocket disconnected: instance=%s", instance_id[:8])


# ---------------------------------------------------------------------------
# Blueprint registration helper
# ---------------------------------------------------------------------------


def init_app(app):
    """Register the player blueprint and flask-sock with the Flask app.

    Call this from app.py when registering blueprints:
        from blueprints.player import player_bp, init_app as init_player
        app.register_blueprint(player_bp)
        init_player(app)
    """
    sock.init_app(app)

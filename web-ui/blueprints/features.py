"""Feature flags API blueprint for HelloDJ SaaS platform.

Exposes:
- GET /api/v1/features/{tenant_id} — Returns computed feature flags as JSON

The endpoint is intended for internal use by Bot_Instances querying their
tenant's enabled features at startup and on subscription change events.
"""

from __future__ import annotations

import logging
import os

import psycopg2
import redis
from flask import Blueprint, jsonify

from services.feature_flags import get_features

log = logging.getLogger(__name__)

features_bp = Blueprint("features", __name__, url_prefix="/api/v1/features")

# ---------------------------------------------------------------------------
# Connection configuration
# ---------------------------------------------------------------------------

REDIS_URL = os.environ.get(
    "REDIS_URL", "redis://redis.redis-service.svc.cluster.local:6379/0"
)
PG_URI = os.environ.get(
    "HELLODJ_PG_URI",
    "postgresql://hellodj:hellodj@postgresql-rw.postgresql-service.svc.cluster.local:5432/hellodj",
)


def _get_redis() -> redis.Redis:
    """Get a Redis client."""
    return redis.from_url(REDIS_URL, decode_responses=True)


def _get_pg_conn():
    """Get a psycopg2 connection to PostgreSQL."""
    return psycopg2.connect(PG_URI)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@features_bp.route("/<tenant_id>", methods=["GET"])
def get_tenant_features(tenant_id: str):
    """Return the computed feature flags for a tenant.

    Response format:
    {
        "audio": true,
        "video": false,
        "activity": false,
        "hls": false,
        "visualizer": false,
        "tidal_hifi": false,
        "lossless": false,
        "priority_queue": false,
        "max_bot_instances": 1,
        "max_guilds_per_bot": 5
    }

    Returns 404 if the tenant_id format is invalid.
    Returns 500 on database/Redis errors.
    """
    # Basic UUID format validation
    tenant_id = tenant_id.strip()
    if not tenant_id:
        return jsonify({"error": "tenant_id is required"}), 400

    # Validate UUID format (loose check — 32 hex chars with optional dashes)
    clean = tenant_id.replace("-", "")
    if len(clean) != 32 or not all(c in "0123456789abcdefABCDEF" for c in clean):
        return jsonify({"error": "Invalid tenant_id format"}), 400

    try:
        pg_conn = _get_pg_conn()
    except Exception as exc:
        log.error("PostgreSQL connection failed: %s", exc)
        return jsonify({"error": "Database unavailable"}), 500

    try:
        redis_client = _get_redis()
    except Exception as exc:
        log.error("Redis connection failed: %s", exc)
        # Fallback: query DB directly without cache
        redis_client = None

    try:
        if redis_client is not None:
            flags = get_features(tenant_id, pg_conn, redis_client)
        else:
            # No Redis available — compute directly from DB without caching
            from services.feature_flags import compute_features, _query_active_subscription
            plan, addons = _query_active_subscription(tenant_id, pg_conn)
            flags = compute_features(plan, addons)

        return jsonify(flags), 200
    except Exception as exc:
        log.error("Feature flag computation failed for tenant=%s: %s", tenant_id, exc)
        return jsonify({"error": "Internal server error"}), 500
    finally:
        try:
            pg_conn.close()
        except Exception:
            pass

"""Bot configuration blueprint for HelloDJ SaaS platform.

Allows tenants to configure their Bot_Instance settings:
- Source provider preference (youtube/spotify/tidal/soundcloud)
- Autoplay (on/off)
- Content filter level (none/mild/moderate/strict)
- Equalizer preset selection

On save:
1. Persist immediately to PostgreSQL `bot_configs` table
2. Publish to Redis `config_change:{instance_id}` for live application within 30s
3. If bot is offline, show notice that config applies on next start

Requirements: 12.6, 12.7, 12.8
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras
import redis
from flask import Blueprint, g, jsonify, render_template, request

from auth_middleware import login_required

log = logging.getLogger(__name__)

bot_config_bp = Blueprint("bot_config", __name__, url_prefix="/bot-config")

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

# Valid configuration options
VALID_SOURCE_PROVIDERS = ("youtube", "spotify", "tidal", "soundcloud")
VALID_CONTENT_FILTER_LEVELS = ("none", "mild", "moderate", "strict")
VALID_EQ_PRESETS = (
    "flat",
    "bass_boost",
    "treble_boost",
    "vocal",
    "nightcore",
    "vaporwave",
    "deep_bass",
    "classical",
    "rock",
    "electronic",
)

# Redis pub/sub channel prefix for config changes
CONFIG_CHANGE_CHANNEL_PREFIX = "config_change:"


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------


def _get_pg_conn():
    """Get a psycopg2 connection."""
    return psycopg2.connect(PG_URI)


def _get_redis() -> redis.Redis:
    """Get a Redis client."""
    return redis.from_url(REDIS_URL, decode_responses=True)


def _ensure_bot_configs_table(conn) -> None:
    """Idempotently create the bot_configs table if it doesn't exist."""
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS bot_configs (
                instance_id UUID PRIMARY KEY,
                tenant_id UUID NOT NULL,
                source_provider TEXT NOT NULL DEFAULT 'youtube',
                autoplay BOOLEAN NOT NULL DEFAULT true,
                content_filter_level TEXT NOT NULL DEFAULT 'none',
                eq_preset TEXT NOT NULL DEFAULT 'flat',
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                CONSTRAINT valid_source_provider
                    CHECK (source_provider IN ('youtube', 'spotify', 'tidal', 'soundcloud')),
                CONSTRAINT valid_content_filter
                    CHECK (content_filter_level IN ('none', 'mild', 'moderate', 'strict')),
                CONSTRAINT valid_eq_preset
                    CHECK (eq_preset IN (
                        'flat', 'bass_boost', 'treble_boost', 'vocal',
                        'nightcore', 'vaporwave', 'deep_bass', 'classical',
                        'rock', 'electronic'
                    ))
            )
        """)
        conn.commit()


def _get_tenant_instances(tenant_id: str, conn) -> list[dict]:
    """Fetch all bot instances belonging to a tenant."""
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
        return [dict(row) for row in cur.fetchall()]


def _get_instance_config(instance_id: str, conn) -> dict | None:
    """Fetch the config for a specific bot instance."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT instance_id, tenant_id, source_provider, autoplay,
                   content_filter_level, eq_preset, updated_at
            FROM bot_configs
            WHERE instance_id = %s
            """,
            (instance_id,),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def _get_instance_status(instance_id: str, conn) -> str | None:
    """Get the current status of a bot instance."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT status FROM bot_instances WHERE id = %s",
            (instance_id,),
        )
        row = cur.fetchone()
        return row[0] if row else None


def _instance_belongs_to_tenant(instance_id: str, tenant_id: str, conn) -> bool:
    """Verify that a bot instance belongs to the given tenant."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM bot_instances WHERE id = %s AND tenant_id = %s",
            (instance_id, tenant_id),
        )
        return cur.fetchone() is not None


def _save_config(
    instance_id: str,
    tenant_id: str,
    source_provider: str,
    autoplay: bool,
    content_filter_level: str,
    eq_preset: str,
    conn,
) -> dict:
    """Upsert bot config into PostgreSQL.

    Returns the saved config as a dict.
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            INSERT INTO bot_configs (
                instance_id, tenant_id, source_provider, autoplay,
                content_filter_level, eq_preset, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, now())
            ON CONFLICT (instance_id) DO UPDATE SET
                source_provider = EXCLUDED.source_provider,
                autoplay = EXCLUDED.autoplay,
                content_filter_level = EXCLUDED.content_filter_level,
                eq_preset = EXCLUDED.eq_preset,
                updated_at = now()
            RETURNING instance_id, tenant_id, source_provider, autoplay,
                      content_filter_level, eq_preset, updated_at
            """,
            (instance_id, tenant_id, source_provider, autoplay,
             content_filter_level, eq_preset),
        )
        row = cur.fetchone()
        conn.commit()
        return dict(row)


def _publish_config_change(instance_id: str, config: dict) -> None:
    """Publish config change to Redis pub/sub for live application.

    The bot instance subscribes to `config_change:{instance_id}` and applies
    the new config within 30 seconds.
    """
    try:
        r = _get_redis()
        channel = f"{CONFIG_CHANGE_CHANNEL_PREFIX}{instance_id}"
        payload = json.dumps({
            "source_provider": config["source_provider"],
            "autoplay": config["autoplay"],
            "content_filter_level": config["content_filter_level"],
            "eq_preset": config["eq_preset"],
            "updated_at": config["updated_at"].isoformat()
            if isinstance(config["updated_at"], datetime)
            else str(config["updated_at"]),
        })
        r.publish(channel, payload)
        log.info(
            "Published config change for instance=%s on channel=%s",
            instance_id, channel,
        )
    except Exception as exc:
        log.warning(
            "Failed to publish config change for instance=%s: %s",
            instance_id, exc,
        )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@bot_config_bp.route("", methods=["GET"])
@login_required
def config_page():
    """Render the bot configuration page.

    Shows config for the tenant's first bot instance, or an empty state
    if no instances exist.
    """
    tenant = g.tenant
    tenant_id = tenant.get("tenant_id") or tenant.get("id")

    if not tenant_id:
        return jsonify({"error": "No tenant ID in session"}), 401

    conn = _get_pg_conn()
    try:
        _ensure_bot_configs_table(conn)
        instances = _get_tenant_instances(tenant_id, conn)

        # If the tenant has instances, load config for the first one
        instance = None
        config = None
        bot_status = None
        if instances:
            instance = instances[0]
            instance_id = str(instance["id"])
            config = _get_instance_config(instance_id, conn)
            bot_status = instance.get("status")

        return render_template(
            "pages/bot_config.html",
            active="bot_config",
            instances=instances,
            selected_instance=instance,
            config=config,
            bot_status=bot_status,
            source_providers=VALID_SOURCE_PROVIDERS,
            content_filter_levels=VALID_CONTENT_FILTER_LEVELS,
            eq_presets=VALID_EQ_PRESETS,
        )
    finally:
        conn.close()


@bot_config_bp.route("/<instance_id>", methods=["GET"])
@login_required
def get_config(instance_id: str):
    """Get the config for a specific bot instance (HTMX partial or JSON)."""
    tenant = g.tenant
    tenant_id = tenant.get("tenant_id") or tenant.get("id")

    if not tenant_id:
        return jsonify({"error": "No tenant ID in session"}), 401

    # Validate UUID
    try:
        uuid.UUID(instance_id)
    except (ValueError, TypeError):
        return jsonify({"error": "Invalid instance ID"}), 400

    conn = _get_pg_conn()
    try:
        _ensure_bot_configs_table(conn)

        # Verify ownership
        if not _instance_belongs_to_tenant(instance_id, tenant_id, conn):
            return jsonify({"error": "Instance not found or not owned"}), 403

        config = _get_instance_config(instance_id, conn)
        bot_status = _get_instance_status(instance_id, conn)

        # If HTMX request, return the form partial
        if request.headers.get("HX-Request"):
            return render_template(
                "partials/bot_config_form.html",
                instance_id=instance_id,
                config=config,
                bot_status=bot_status,
                source_providers=VALID_SOURCE_PROVIDERS,
                content_filter_levels=VALID_CONTENT_FILTER_LEVELS,
                eq_presets=VALID_EQ_PRESETS,
            )

        # JSON response
        if config is None:
            config = {
                "instance_id": instance_id,
                "source_provider": "youtube",
                "autoplay": True,
                "content_filter_level": "none",
                "eq_preset": "flat",
            }

        return jsonify({
            "config": config,
            "bot_status": bot_status,
        }), 200
    finally:
        conn.close()


@bot_config_bp.route("/<instance_id>", methods=["POST"])
@login_required
def save_config(instance_id: str):
    """Save bot configuration for a specific instance.

    Persists to PostgreSQL immediately. If the bot is online, publishes
    a config change event to Redis pub/sub for application within 30s.
    If offline, config will apply on next start.

    Accepts form data (HTMX) or JSON.
    """
    tenant = g.tenant
    tenant_id = tenant.get("tenant_id") or tenant.get("id")

    if not tenant_id:
        return jsonify({"error": "No tenant ID in session"}), 401

    # Validate UUID
    try:
        uuid.UUID(instance_id)
    except (ValueError, TypeError):
        return jsonify({"error": "Invalid instance ID"}), 400

    # Parse input (support both form and JSON)
    if request.is_json:
        data = request.get_json()
    else:
        data = request.form.to_dict()

    # Extract and validate fields
    source_provider = data.get("source_provider", "youtube")
    autoplay_raw = data.get("autoplay", "true")
    content_filter_level = data.get("content_filter_level", "none")
    eq_preset = data.get("eq_preset", "flat")

    # Validate source_provider
    if source_provider not in VALID_SOURCE_PROVIDERS:
        msg = f"Invalid source_provider: {source_provider}"
        if request.headers.get("HX-Request"):
            return f'<div class="alert alert-danger">{msg}</div>', 400
        return jsonify({"error": msg}), 400

    # Validate content_filter_level
    if content_filter_level not in VALID_CONTENT_FILTER_LEVELS:
        msg = f"Invalid content_filter_level: {content_filter_level}"
        if request.headers.get("HX-Request"):
            return f'<div class="alert alert-danger">{msg}</div>', 400
        return jsonify({"error": msg}), 400

    # Validate eq_preset
    if eq_preset not in VALID_EQ_PRESETS:
        msg = f"Invalid eq_preset: {eq_preset}"
        if request.headers.get("HX-Request"):
            return f'<div class="alert alert-danger">{msg}</div>', 400
        return jsonify({"error": msg}), 400

    # Parse autoplay boolean
    if isinstance(autoplay_raw, bool):
        autoplay = autoplay_raw
    elif isinstance(autoplay_raw, str):
        autoplay = autoplay_raw.lower() in ("true", "1", "on", "yes")
    else:
        autoplay = True

    conn = _get_pg_conn()
    try:
        _ensure_bot_configs_table(conn)

        # Verify ownership
        if not _instance_belongs_to_tenant(instance_id, tenant_id, conn):
            msg = "Instance not found or not owned"
            if request.headers.get("HX-Request"):
                return f'<div class="alert alert-danger">{msg}</div>', 403
            return jsonify({"error": msg}), 403

        # Save config to PostgreSQL
        saved_config = _save_config(
            instance_id=instance_id,
            tenant_id=tenant_id,
            source_provider=source_provider,
            autoplay=autoplay,
            content_filter_level=content_filter_level,
            eq_preset=eq_preset,
            conn=conn,
        )

        # Check bot status and publish config change if online
        bot_status = _get_instance_status(instance_id, conn)
        is_online = bot_status == "running"

        if is_online:
            _publish_config_change(instance_id, saved_config)

        log.info(
            "Bot config saved: instance=%s provider=%s autoplay=%s "
            "filter=%s eq=%s online=%s",
            instance_id, source_provider, autoplay,
            content_filter_level, eq_preset, is_online,
        )

        # HTMX response
        if request.headers.get("HX-Request"):
            notice = ""
            if not is_online:
                notice = (
                    '<div class="alert alert-warning mt-3" role="alert">'
                    '<i class="bi bi-exclamation-triangle"></i> '
                    "Your bot is currently offline. "
                    "Configuration will apply when the bot next starts."
                    "</div>"
                )
            return (
                '<div class="alert alert-success mt-3" role="alert">'
                '<i class="bi bi-check-circle"></i> '
                "Configuration saved successfully."
                "</div>" + notice
            ), 200

        # JSON response
        return jsonify({
            "status": "saved",
            "config": {
                "instance_id": instance_id,
                "source_provider": source_provider,
                "autoplay": autoplay,
                "content_filter_level": content_filter_level,
                "eq_preset": eq_preset,
            },
            "bot_online": is_online,
            "notice": None if is_online else (
                "Bot is currently offline. "
                "Configuration will apply when the bot next starts."
            ),
        }), 200
    finally:
        conn.close()

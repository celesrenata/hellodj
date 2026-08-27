"""Property-based test: Authorization Enforcement.

**Validates: Requirements 9.1, 17.3**

Property 7: Authorization Enforcement
- Non-operator users rejected from all admin endpoints
- Non-owner tenants get HTTP 403 on player API
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path
from unittest.mock import patch

import fakeredis
import psycopg2
import psycopg2.extras
import pytest
from hypothesis import given, settings, HealthCheck
from hypothesis import strategies as st

from tests.strategies import discord_user_ids

# Ensure web-ui is importable
_webui_dir = str(Path(__file__).resolve().parent.parent / "web-ui")
if _webui_dir not in sys.path:
    sys.path.insert(0, _webui_dir)


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Admin endpoints that require operator access
admin_endpoints = st.sampled_from([
    "/api/v1/admin/trials",
    "/api/v1/admin/subscriptions",
    "/api/v1/admin/metrics",
    "/api/v1/admin/instances",
])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Fixed operator Discord ID used in tests
OPERATOR_DISCORD_ID = "999999999999999999"


def _psycopg2_url(url: str) -> str:
    """Ensure URL is psycopg2-compatible."""
    return url


def _create_tenant(conn, discord_user_id: int) -> str:
    """Create a tenant record and return its UUID."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            INSERT INTO tenants (discord_user_id, discord_username)
            VALUES (%s, %s)
            ON CONFLICT (discord_user_id) DO UPDATE SET discord_username = EXCLUDED.discord_username
            RETURNING id
            """,
            (discord_user_id, f"testuser_{discord_user_id}"),
        )
        tenant_id = str(cur.fetchone()["id"])
        conn.commit()
    return tenant_id


def _create_bot_instance(conn, tenant_id: str) -> str:
    """Create a bot instance for a tenant and return instance UUID."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            INSERT INTO bot_instances (tenant_id, status, guild_ids)
            VALUES (%s, 'running', %s)
            RETURNING id
            """,
            (tenant_id, [123456789]),
        )
        instance_id = str(cur.fetchone()["id"])
        conn.commit()
    return instance_id


def _truncate_tables(conn):
    """Truncate relevant tables for test isolation."""
    with conn.cursor() as cur:
        cur.execute(
            "TRUNCATE TABLE bot_instances, subscriptions, tenants CASCADE;"
        )
        conn.commit()


def _create_flask_app(fake_redis):
    """Create a minimal Flask app with admin and player blueprints for testing."""
    from flask import Flask

    app = Flask(__name__)
    app.config["TESTING"] = True
    app.config["SECRET_KEY"] = "test-secret-key"

    # Import and register blueprints
    from blueprints.admin import admin_bp
    from blueprints.auth import auth_bp
    from blueprints.player import player_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(player_bp)

    return app


def _set_session(fake_redis, token: str, tenant_data: dict) -> None:
    """Store a session in fakeredis."""
    key = f"session:{token}"
    fake_redis.set(key, json.dumps(tenant_data))


# ---------------------------------------------------------------------------
# Property 7a: Non-operator users rejected from all admin endpoints
# ---------------------------------------------------------------------------


@settings(
    max_examples=100,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    deadline=None,
)
@given(
    discord_id=discord_user_ids,
    endpoint=admin_endpoints,
)
def test_non_operator_rejected_from_admin_endpoints(
    pg_connection_url: str, _apply_schema, discord_id: int, endpoint: str
):
    """Property 7a: For any discord_user_id != OPERATOR_DISCORD_ID, accessing
    any admin endpoint returns a non-200 response (403 or redirect to login).

    **Validates: Requirements 9.1**
    """
    pg_url = _psycopg2_url(pg_connection_url)

    # Ensure the generated discord_id does NOT match the operator
    if str(discord_id) == OPERATOR_DISCORD_ID:
        return  # Skip this example (extremely unlikely)

    fake_redis = fakeredis.FakeRedis(decode_responses=True)
    session_token = str(uuid.uuid4())

    # Create a tenant session for this non-operator user
    tenant_data = {
        "discord_user_id": str(discord_id),
        "discord_username": f"user_{discord_id}",
        "tenant_id": str(uuid.uuid4()),
        "id": str(uuid.uuid4()),
    }
    _set_session(fake_redis, session_token, tenant_data)

    # Patch environment and Redis for the auth middleware
    with patch.dict(os.environ, {"OPERATOR_DISCORD_ID": OPERATOR_DISCORD_ID}):
        import auth_middleware
        auth_middleware.set_redis_client(fake_redis)

        app = _create_flask_app(fake_redis)

        with app.test_client() as client:
            # Set the session cookie
            client.set_cookie("session_token", session_token, domain="localhost")

            response = client.get(endpoint)

            # Non-operator users must get 403 (forbidden)
            assert response.status_code == 403, (
                f"Expected 403 for non-operator user {discord_id} on {endpoint}, "
                f"got {response.status_code}"
            )

    fake_redis.flushall()
    fake_redis.close()


# ---------------------------------------------------------------------------
# Property 7b: Non-owner tenants get HTTP 403 on player API
# ---------------------------------------------------------------------------


@settings(
    max_examples=100,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    deadline=None,
)
@given(
    owner_discord_id=discord_user_ids,
    intruder_discord_id=discord_user_ids,
)
def test_non_owner_tenant_gets_403_on_player_api(
    pg_connection_url: str,
    _apply_schema,
    owner_discord_id: int,
    intruder_discord_id: int,
):
    """Property 7b: For any tenant_id that does NOT own a bot instance,
    accessing the player API for that instance returns HTTP 403.

    **Validates: Requirements 17.3**
    """
    # Ensure the two users are distinct
    if owner_discord_id == intruder_discord_id:
        return  # Skip — same user can't be both owner and intruder

    pg_url = _psycopg2_url(pg_connection_url)
    conn = psycopg2.connect(pg_url)

    try:
        _truncate_tables(conn)

        # Create the owner tenant and a bot instance they own
        owner_tenant_id = _create_tenant(conn, owner_discord_id)
        instance_id = _create_bot_instance(conn, owner_tenant_id)

        # Create the intruder tenant (does NOT own the instance)
        intruder_tenant_id = _create_tenant(conn, intruder_discord_id)

        # Set up fakeredis with intruder's session
        fake_redis = fakeredis.FakeRedis(decode_responses=True)
        session_token = str(uuid.uuid4())

        intruder_session = {
            "discord_user_id": str(intruder_discord_id),
            "discord_username": f"intruder_{intruder_discord_id}",
            "tenant_id": intruder_tenant_id,
            "id": intruder_tenant_id,
        }
        _set_session(fake_redis, session_token, intruder_session)

        # Patch auth middleware Redis and player blueprint's DB connection
        with patch.dict(os.environ, {"OPERATOR_DISCORD_ID": OPERATOR_DISCORD_ID}):
            import auth_middleware
            auth_middleware.set_redis_client(fake_redis)

            # Patch the player blueprint's _get_pg_conn to use our test DB
            with patch(
                "blueprints.player._get_pg_conn",
                return_value=psycopg2.connect(pg_url),
            ):
                app = _create_flask_app(fake_redis)

                with app.test_client() as client:
                    client.set_cookie(
                        "session_token", session_token, domain="localhost"
                    )

                    # Intruder tries to access owner's bot instance state
                    response = client.get(
                        f"/api/v1/player/{instance_id}/state"
                    )

                    # Must get 403 Forbidden
                    assert response.status_code == 403, (
                        f"Expected 403 for non-owner tenant on player API, "
                        f"got {response.status_code}. "
                        f"Intruder tenant_id={intruder_tenant_id}, "
                        f"instance owner={owner_tenant_id}"
                    )

        fake_redis.flushall()
        fake_redis.close()
    finally:
        conn.close()

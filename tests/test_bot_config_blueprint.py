"""Unit tests for the bot configuration blueprint.

Tests cover:
- GET /bot-config: page render with and without instances
- GET /bot-config/<id>: config retrieval, ownership validation
- POST /bot-config/<id>: config saving, validation, Redis pub/sub
- Bot status offline notice
- HTMX partial responses
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

TENANT_ID = str(uuid.uuid4())
INSTANCE_ID = str(uuid.uuid4())


@pytest.fixture
def redis_client():
    """Provide a fresh fakeredis instance per test."""
    client = fakeredis.FakeRedis(decode_responses=True)
    yield client
    client.flushall()
    client.close()


@pytest.fixture
def app(redis_client):
    """Create a Flask test app with the bot_config and auth blueprints registered."""
    import auth_middleware
    from blueprints.auth import auth_bp
    from blueprints.bot_config import bot_config_bp

    flask_app = Flask(
        __name__,
        template_folder=str(Path(_webui_dir) / "templates"),
    )
    flask_app.config["TESTING"] = True
    flask_app.secret_key = "test-secret"

    flask_app.register_blueprint(auth_bp)
    flask_app.register_blueprint(bot_config_bp)

    # Register stub routes required by base.html template
    for name in (
        "index", "config_page", "guilds_page", "playlists_page",
        "backups_page", "blacklist_page", "metrics_page", "logs_page",
        "moderation_page", "instances_page", "admins_page",
        "auth_login", "auth_logout",
    ):
        flask_app.add_url_rule(
            f"/stub/{name}", endpoint=name, view_func=lambda: ""
        )

    # Use the provided setter for the auth_middleware singleton
    auth_middleware.set_redis_client(redis_client)

    yield flask_app

    auth_middleware.set_redis_client(None)


@pytest.fixture
def client(app):
    """Flask test client."""
    return app.test_client()


def _authenticate(client, redis_client, tenant_id=TENANT_ID):
    """Create a session and set the cookie on the test client."""
    token = "test-session-token"
    session_data = json.dumps({
        "tenant_id": tenant_id,
        "discord_user_id": "123456789",
        "discord_username": "testuser",
    })
    redis_client.set(f"session:{token}", session_data, ex=86400)
    client.set_cookie("session_token", token)
    return token


# ---------------------------------------------------------------------------
# Tests: GET /bot-config
# ---------------------------------------------------------------------------


class TestConfigPage:
    """Tests for GET /bot-config (page render)."""

    def test_unauthenticated_redirects_to_login(self, client):
        """Unauthenticated user should be redirected to login."""
        response = client.get("/bot-config")
        assert response.status_code == 302
        assert "/auth/login" in response.headers["Location"]

    def test_authenticated_no_instances_shows_empty(self, client, redis_client):
        """Authenticated user with no instances sees empty state."""
        _authenticate(client, redis_client)

        with (
            patch("blueprints.bot_config._get_pg_conn"),
            patch("blueprints.bot_config._ensure_bot_configs_table"),
            patch("blueprints.bot_config._get_tenant_instances", return_value=[]),
        ):
            response = client.get("/bot-config")

        assert response.status_code == 200
        html = response.data.decode()
        assert "No Bot Instances" in html

    def test_authenticated_with_instances_shows_form(self, client, redis_client):
        """Authenticated user with instances sees the config form."""
        _authenticate(client, redis_client)

        instances_data = [{
            "id": uuid.UUID(INSTANCE_ID),
            "guild_ids": [111, 222],
            "status": "running",
            "pod_name": "tenant-bot-abc",
            "node_name": "gremlin-1",
            "created_at": "2026-01-01T00:00:00Z",
        }]

        with (
            patch("blueprints.bot_config._get_pg_conn"),
            patch("blueprints.bot_config._ensure_bot_configs_table"),
            patch("blueprints.bot_config._get_tenant_instances", return_value=instances_data),
            patch("blueprints.bot_config._get_instance_config", return_value=None),
        ):
            response = client.get("/bot-config")

        assert response.status_code == 200
        html = response.data.decode()
        assert "Source Provider" in html
        assert "Autoplay" in html
        assert "Content Filter" in html
        assert "Equalizer Preset" in html

    def test_offline_bot_shows_warning(self, client, redis_client):
        """Offline bot instance should show warning banner."""
        _authenticate(client, redis_client)

        instances_data = [{
            "id": uuid.UUID(INSTANCE_ID),
            "guild_ids": [111],
            "status": "stopped",
            "pod_name": None,
            "node_name": None,
            "created_at": "2026-01-01T00:00:00Z",
        }]

        with (
            patch("blueprints.bot_config._get_pg_conn"),
            patch("blueprints.bot_config._ensure_bot_configs_table"),
            patch("blueprints.bot_config._get_tenant_instances", return_value=instances_data),
            patch("blueprints.bot_config._get_instance_config", return_value=None),
        ):
            response = client.get("/bot-config")

        assert response.status_code == 200
        html = response.data.decode()
        assert "Bot Offline" in html


# ---------------------------------------------------------------------------
# Tests: POST /bot-config/<instance_id>
# ---------------------------------------------------------------------------


class TestSaveConfig:
    """Tests for POST /bot-config/<instance_id>."""

    def test_save_config_valid_json(self, client, redis_client):
        """Valid JSON config save should return success."""
        _authenticate(client, redis_client)

        saved_config = {
            "instance_id": INSTANCE_ID,
            "tenant_id": TENANT_ID,
            "source_provider": "spotify",
            "autoplay": True,
            "content_filter_level": "moderate",
            "eq_preset": "bass_boost",
            "updated_at": "2026-01-01T00:00:00+00:00",
        }

        with (
            patch("blueprints.bot_config._get_pg_conn"),
            patch("blueprints.bot_config._ensure_bot_configs_table"),
            patch("blueprints.bot_config._instance_belongs_to_tenant", return_value=True),
            patch("blueprints.bot_config._save_config", return_value=saved_config),
            patch("blueprints.bot_config._get_instance_status", return_value="running"),
            patch("blueprints.bot_config._publish_config_change") as mock_publish,
        ):
            response = client.post(
                f"/bot-config/{INSTANCE_ID}",
                json={
                    "source_provider": "spotify",
                    "autoplay": True,
                    "content_filter_level": "moderate",
                    "eq_preset": "bass_boost",
                },
            )

        assert response.status_code == 200
        data = response.get_json()
        assert data["status"] == "saved"
        assert data["config"]["source_provider"] == "spotify"
        assert data["bot_online"] is True
        assert data["notice"] is None
        mock_publish.assert_called_once()

    def test_save_config_bot_offline_shows_notice(self, client, redis_client):
        """Saving config with offline bot should show notice."""
        _authenticate(client, redis_client)

        saved_config = {
            "instance_id": INSTANCE_ID,
            "tenant_id": TENANT_ID,
            "source_provider": "youtube",
            "autoplay": False,
            "content_filter_level": "none",
            "eq_preset": "flat",
            "updated_at": "2026-01-01T00:00:00+00:00",
        }

        with (
            patch("blueprints.bot_config._get_pg_conn"),
            patch("blueprints.bot_config._ensure_bot_configs_table"),
            patch("blueprints.bot_config._instance_belongs_to_tenant", return_value=True),
            patch("blueprints.bot_config._save_config", return_value=saved_config),
            patch("blueprints.bot_config._get_instance_status", return_value="stopped"),
            patch("blueprints.bot_config._publish_config_change") as mock_publish,
        ):
            response = client.post(
                f"/bot-config/{INSTANCE_ID}",
                json={
                    "source_provider": "youtube",
                    "autoplay": False,
                    "content_filter_level": "none",
                    "eq_preset": "flat",
                },
            )

        assert response.status_code == 200
        data = response.get_json()
        assert data["status"] == "saved"
        assert data["bot_online"] is False
        assert "offline" in data["notice"].lower()
        mock_publish.assert_not_called()

    def test_save_config_invalid_provider_rejected(self, client, redis_client):
        """Invalid source_provider should be rejected."""
        _authenticate(client, redis_client)

        with (
            patch("blueprints.bot_config._get_pg_conn"),
            patch("blueprints.bot_config._ensure_bot_configs_table"),
            patch("blueprints.bot_config._instance_belongs_to_tenant", return_value=True),
        ):
            response = client.post(
                f"/bot-config/{INSTANCE_ID}",
                json={"source_provider": "napster"},
            )

        assert response.status_code == 400
        data = response.get_json()
        assert "Invalid source_provider" in data["error"]

    def test_save_config_invalid_eq_preset_rejected(self, client, redis_client):
        """Invalid eq_preset should be rejected."""
        _authenticate(client, redis_client)

        with (
            patch("blueprints.bot_config._get_pg_conn"),
            patch("blueprints.bot_config._ensure_bot_configs_table"),
            patch("blueprints.bot_config._instance_belongs_to_tenant", return_value=True),
        ):
            response = client.post(
                f"/bot-config/{INSTANCE_ID}",
                json={"eq_preset": "magic_preset"},
            )

        assert response.status_code == 400
        data = response.get_json()
        assert "Invalid eq_preset" in data["error"]

    def test_save_config_invalid_content_filter_rejected(self, client, redis_client):
        """Invalid content_filter_level should be rejected."""
        _authenticate(client, redis_client)

        with (
            patch("blueprints.bot_config._get_pg_conn"),
            patch("blueprints.bot_config._ensure_bot_configs_table"),
            patch("blueprints.bot_config._instance_belongs_to_tenant", return_value=True),
        ):
            response = client.post(
                f"/bot-config/{INSTANCE_ID}",
                json={"content_filter_level": "ultra"},
            )

        assert response.status_code == 400
        data = response.get_json()
        assert "Invalid content_filter_level" in data["error"]

    def test_save_config_wrong_tenant_rejected(self, client, redis_client):
        """Saving config for instance owned by another tenant should be rejected."""
        _authenticate(client, redis_client)

        with (
            patch("blueprints.bot_config._get_pg_conn"),
            patch("blueprints.bot_config._ensure_bot_configs_table"),
            patch("blueprints.bot_config._instance_belongs_to_tenant", return_value=False),
        ):
            response = client.post(
                f"/bot-config/{INSTANCE_ID}",
                json={"source_provider": "youtube"},
            )

        assert response.status_code == 403

    def test_save_config_invalid_uuid_rejected(self, client, redis_client):
        """Invalid UUID instance_id should be rejected."""
        _authenticate(client, redis_client)

        with (
            patch("blueprints.bot_config._get_pg_conn"),
            patch("blueprints.bot_config._ensure_bot_configs_table"),
        ):
            response = client.post(
                "/bot-config/not-a-uuid",
                json={"source_provider": "youtube"},
            )

        assert response.status_code == 400

    def test_save_config_htmx_response(self, client, redis_client):
        """HTMX POST should return HTML fragment, not JSON."""
        _authenticate(client, redis_client)

        saved_config = {
            "instance_id": INSTANCE_ID,
            "tenant_id": TENANT_ID,
            "source_provider": "youtube",
            "autoplay": True,
            "content_filter_level": "none",
            "eq_preset": "flat",
            "updated_at": "2026-01-01T00:00:00+00:00",
        }

        with (
            patch("blueprints.bot_config._get_pg_conn"),
            patch("blueprints.bot_config._ensure_bot_configs_table"),
            patch("blueprints.bot_config._instance_belongs_to_tenant", return_value=True),
            patch("blueprints.bot_config._save_config", return_value=saved_config),
            patch("blueprints.bot_config._get_instance_status", return_value="running"),
            patch("blueprints.bot_config._publish_config_change"),
        ):
            response = client.post(
                f"/bot-config/{INSTANCE_ID}",
                data={
                    "source_provider": "youtube",
                    "autoplay": "true",
                    "content_filter_level": "none",
                    "eq_preset": "flat",
                },
                headers={"HX-Request": "true"},
            )

        assert response.status_code == 200
        html = response.data.decode()
        assert "Configuration saved successfully" in html
        assert "offline" not in html.lower()

    def test_save_config_htmx_offline_notice(self, client, redis_client):
        """HTMX POST with offline bot should include offline notice."""
        _authenticate(client, redis_client)

        saved_config = {
            "instance_id": INSTANCE_ID,
            "tenant_id": TENANT_ID,
            "source_provider": "youtube",
            "autoplay": True,
            "content_filter_level": "none",
            "eq_preset": "flat",
            "updated_at": "2026-01-01T00:00:00+00:00",
        }

        with (
            patch("blueprints.bot_config._get_pg_conn"),
            patch("blueprints.bot_config._ensure_bot_configs_table"),
            patch("blueprints.bot_config._instance_belongs_to_tenant", return_value=True),
            patch("blueprints.bot_config._save_config", return_value=saved_config),
            patch("blueprints.bot_config._get_instance_status", return_value="stopped"),
            patch("blueprints.bot_config._publish_config_change"),
        ):
            response = client.post(
                f"/bot-config/{INSTANCE_ID}",
                data={
                    "source_provider": "youtube",
                    "autoplay": "true",
                    "content_filter_level": "none",
                    "eq_preset": "flat",
                },
                headers={"HX-Request": "true"},
            )

        assert response.status_code == 200
        html = response.data.decode()
        assert "Configuration saved successfully" in html
        assert "offline" in html.lower()


# ---------------------------------------------------------------------------
# Tests: GET /bot-config/<instance_id>
# ---------------------------------------------------------------------------


class TestGetConfig:
    """Tests for GET /bot-config/<instance_id>."""

    def test_get_config_json_response(self, client, redis_client):
        """GET without HX-Request should return JSON."""
        _authenticate(client, redis_client)

        config_data = {
            "instance_id": INSTANCE_ID,
            "tenant_id": TENANT_ID,
            "source_provider": "spotify",
            "autoplay": False,
            "content_filter_level": "moderate",
            "eq_preset": "rock",
            "updated_at": "2026-01-01T00:00:00+00:00",
        }

        with (
            patch("blueprints.bot_config._get_pg_conn"),
            patch("blueprints.bot_config._ensure_bot_configs_table"),
            patch("blueprints.bot_config._instance_belongs_to_tenant", return_value=True),
            patch("blueprints.bot_config._get_instance_config", return_value=config_data),
            patch("blueprints.bot_config._get_instance_status", return_value="running"),
        ):
            response = client.get(f"/bot-config/{INSTANCE_ID}")

        assert response.status_code == 200
        data = response.get_json()
        assert data["config"]["source_provider"] == "spotify"
        assert data["bot_status"] == "running"

    def test_get_config_not_owned_rejected(self, client, redis_client):
        """GET for instance not owned by tenant should return 403."""
        _authenticate(client, redis_client)

        with (
            patch("blueprints.bot_config._get_pg_conn"),
            patch("blueprints.bot_config._ensure_bot_configs_table"),
            patch("blueprints.bot_config._instance_belongs_to_tenant", return_value=False),
        ):
            response = client.get(f"/bot-config/{INSTANCE_ID}")

        assert response.status_code == 403

    def test_get_config_no_existing_config_returns_defaults(self, client, redis_client):
        """GET for instance with no config should return defaults."""
        _authenticate(client, redis_client)

        with (
            patch("blueprints.bot_config._get_pg_conn"),
            patch("blueprints.bot_config._ensure_bot_configs_table"),
            patch("blueprints.bot_config._instance_belongs_to_tenant", return_value=True),
            patch("blueprints.bot_config._get_instance_config", return_value=None),
            patch("blueprints.bot_config._get_instance_status", return_value="running"),
        ):
            response = client.get(f"/bot-config/{INSTANCE_ID}")

        assert response.status_code == 200
        data = response.get_json()
        assert data["config"]["source_provider"] == "youtube"
        assert data["config"]["autoplay"] is True
        assert data["config"]["content_filter_level"] == "none"
        assert data["config"]["eq_preset"] == "flat"

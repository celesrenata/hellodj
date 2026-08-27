"""Unit tests for web-ui/blueprints/delegates.py.

Tests delegate management endpoints: list, grant, revoke, update.
Uses Flask test client with fakeredis + mocked RBACService.

Requirements: 6.1, 6.2, 6.4, 6.5, 6.6, 6.7, 6.8
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import fakeredis
import pytest

# Ensure web-ui/ is importable
_web_ui_dir = str(Path(__file__).resolve().parent.parent / "web-ui")
if _web_ui_dir not in sys.path:
    sys.path.insert(0, _web_ui_dir)


from flask import Flask


@pytest.fixture
def fake_redis():
    """Provide a fakeredis client."""
    client = fakeredis.FakeRedis(decode_responses=True)
    yield client
    client.flushall()
    client.close()


@pytest.fixture
def mock_rbac():
    """Provide a mock RBACService."""
    rbac = MagicMock()
    rbac.ROLE_HIERARCHY = {
        "operator": 5,
        "owner": 4,
        "admin": 3,
        "editor": 2,
        "viewer": 1,
    }
    return rbac


@pytest.fixture
def app(fake_redis, mock_rbac):
    """Create a minimal Flask app with the delegates blueprint registered."""
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.secret_key = "test-secret"

    # Register a minimal auth blueprint so url_for("auth.login") works
    from flask import Blueprint

    auth_bp = Blueprint("auth", __name__, url_prefix="/auth")

    @auth_bp.route("/login")
    def login():
        return "login page"

    app.register_blueprint(auth_bp)

    # Configure auth middleware to use fakeredis + mock RBAC
    import auth_middleware
    from services.session_service import SessionService

    auth_middleware.set_redis_client(fake_redis)
    session_service = SessionService(fake_redis)
    auth_middleware.set_session_service(session_service)
    auth_middleware.set_rbac_service(mock_rbac)

    # Register the delegates blueprint
    from blueprints.delegates import delegates_bp

    app.register_blueprint(delegates_bp)

    yield app

    # Cleanup
    auth_middleware.set_rbac_service(None)


@pytest.fixture
def client(app):
    """Flask test client."""
    return app.test_client()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TENANT_ID = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
OWNER_DISCORD_ID = "111222333444555"
DELEGATE_DISCORD_ID = 999888777666555


def _set_session_cookie(client, token: str):
    """Set the hellodj_session cookie on the test client."""
    client.set_cookie("hellodj_session", token, domain="localhost")


def _store_owner_session(fake_redis, token: str = "owner-token"):
    """Store a session with owner role for the test tenant."""
    session_data = {
        "tenant_id": TENANT_ID,
        "discord_user_id": OWNER_DISCORD_ID,
        "discord_username": "owner_user",
        "active_tenant_id": TENANT_ID,
        "is_operator": False,
        "ip_address": "127.0.0.1",
        "created_at": time.time(),
        "roles": [{"tenant_id": TENANT_ID, "role": "owner"}],
    }
    key = f"session:{token}"
    fake_redis.set(key, json.dumps(session_data), ex=86400)
    fake_redis.sadd(f"user_sessions:{OWNER_DISCORD_ID}", token)
    return session_data


# ---------------------------------------------------------------------------
# GET /api/v1/tenants/<tenant_id>/delegates
# ---------------------------------------------------------------------------


class TestListDelegates:
    """Tests for GET /<tenant_id>/delegates endpoint."""

    def test_list_delegates_success(self, client, fake_redis, mock_rbac):
        """Owner can list delegates for their tenant."""
        _store_owner_session(fake_redis)
        _set_session_cookie(client, "owner-token")
        mock_rbac.get_effective_role.return_value = "owner"

        # Mock the database query via _get_conn
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = [
            {
                "discord_user_id": 999888777,
                "role": "editor",
                "granted_at": datetime(2025, 1, 15, 12, 0, 0, tzinfo=timezone.utc),
                "granted_by": int(OWNER_DISCORD_ID),
            },
            {
                "discord_user_id": 555444333,
                "role": "viewer",
                "granted_at": datetime(2025, 2, 20, 8, 30, 0, tzinfo=timezone.utc),
                "granted_by": int(OWNER_DISCORD_ID),
            },
        ]
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_conn.close = MagicMock()
        mock_rbac._get_conn.return_value = mock_conn

        response = client.get(f"/api/v1/tenants/{TENANT_ID}/delegates")

        assert response.status_code == 200
        body = response.get_json()
        assert "delegates" in body
        assert len(body["delegates"]) == 2
        assert body["delegates"][0]["discord_user_id"] == 999888777
        assert body["delegates"][0]["role"] == "editor"
        assert body["delegates"][1]["role"] == "viewer"

    def test_list_delegates_empty(self, client, fake_redis, mock_rbac):
        """Returns empty list when tenant has no delegates."""
        _store_owner_session(fake_redis)
        _set_session_cookie(client, "owner-token")
        mock_rbac.get_effective_role.return_value = "owner"

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = []
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_conn.close = MagicMock()
        mock_rbac._get_conn.return_value = mock_conn

        response = client.get(f"/api/v1/tenants/{TENANT_ID}/delegates")

        assert response.status_code == 200
        body = response.get_json()
        assert body["delegates"] == []

    def test_list_delegates_invalid_tenant_id(self, client, fake_redis, mock_rbac):
        """Invalid tenant_id format returns 400."""
        _store_owner_session(fake_redis)
        _set_session_cookie(client, "owner-token")
        mock_rbac.get_effective_role.return_value = "owner"

        response = client.get("/api/v1/tenants/not-a-uuid/delegates")

        assert response.status_code == 400
        body = response.get_json()
        assert "Invalid tenant ID" in body["error"]

    def test_list_delegates_unauthenticated(self, client):
        """No session cookie → redirect to login."""
        response = client.get(f"/api/v1/tenants/{TENANT_ID}/delegates")

        assert response.status_code == 302
        assert "/auth/login" in response.headers["Location"]


# ---------------------------------------------------------------------------
# POST /api/v1/tenants/<tenant_id>/delegates
# ---------------------------------------------------------------------------


class TestGrantDelegate:
    """Tests for POST /<tenant_id>/delegates endpoint."""

    def test_grant_role_success(self, client, fake_redis, mock_rbac):
        """Owner can grant a role to a Discord user."""
        _store_owner_session(fake_redis)
        _set_session_cookie(client, "owner-token")
        mock_rbac.get_effective_role.return_value = "owner"
        mock_rbac.grant_role.return_value = None

        response = client.post(
            f"/api/v1/tenants/{TENANT_ID}/delegates",
            json={"discord_user_id": DELEGATE_DISCORD_ID, "role": "editor"},
        )

        assert response.status_code == 201
        body = response.get_json()
        assert body["discord_user_id"] == DELEGATE_DISCORD_ID
        assert body["role"] == "editor"
        assert body["tenant_id"] == TENANT_ID

        mock_rbac.grant_role.assert_called_once_with(
            tenant_id=TENANT_ID,
            discord_user_id=DELEGATE_DISCORD_ID,
            role="editor",
            granted_by=int(OWNER_DISCORD_ID),
        )

    def test_grant_role_missing_body(self, client, fake_redis, mock_rbac):
        """No JSON body returns 400."""
        _store_owner_session(fake_redis)
        _set_session_cookie(client, "owner-token")
        mock_rbac.get_effective_role.return_value = "owner"

        response = client.post(
            f"/api/v1/tenants/{TENANT_ID}/delegates",
            content_type="application/json",
            data="",
        )

        assert response.status_code == 400

    def test_grant_role_missing_discord_user_id(self, client, fake_redis, mock_rbac):
        """Missing discord_user_id field returns 400."""
        _store_owner_session(fake_redis)
        _set_session_cookie(client, "owner-token")
        mock_rbac.get_effective_role.return_value = "owner"

        response = client.post(
            f"/api/v1/tenants/{TENANT_ID}/delegates",
            json={"role": "editor"},
        )

        assert response.status_code == 400
        body = response.get_json()
        assert "discord_user_id" in body["error"]

    def test_grant_role_missing_role(self, client, fake_redis, mock_rbac):
        """Missing role field returns 400."""
        _store_owner_session(fake_redis)
        _set_session_cookie(client, "owner-token")
        mock_rbac.get_effective_role.return_value = "owner"

        response = client.post(
            f"/api/v1/tenants/{TENANT_ID}/delegates",
            json={"discord_user_id": DELEGATE_DISCORD_ID},
        )

        assert response.status_code == 400
        body = response.get_json()
        assert "role" in body["error"]

    def test_grant_role_invalid_role(self, client, fake_redis, mock_rbac):
        """Invalid role value returns 400 with correct message."""
        from services.rbac import InvalidRoleError

        _store_owner_session(fake_redis)
        _set_session_cookie(client, "owner-token")
        mock_rbac.get_effective_role.return_value = "owner"
        mock_rbac.grant_role.side_effect = InvalidRoleError("bad role")

        response = client.post(
            f"/api/v1/tenants/{TENANT_ID}/delegates",
            json={"discord_user_id": DELEGATE_DISCORD_ID, "role": "superadmin"},
        )

        assert response.status_code == 400
        body = response.get_json()
        assert body["error"] == "Invalid role. Must be admin, editor, or viewer"

    def test_grant_role_delegate_limit_reached(self, client, fake_redis, mock_rbac):
        """Exceeding 20 delegates returns 400 with limit message."""
        from services.rbac import DelegateLimitError

        _store_owner_session(fake_redis)
        _set_session_cookie(client, "owner-token")
        mock_rbac.get_effective_role.return_value = "owner"
        mock_rbac.grant_role.side_effect = DelegateLimitError("limit reached")

        response = client.post(
            f"/api/v1/tenants/{TENANT_ID}/delegates",
            json={"discord_user_id": DELEGATE_DISCORD_ID, "role": "viewer"},
        )

        assert response.status_code == 400
        body = response.get_json()
        assert body["error"] == "Maximum delegate limit of 20 reached"

    def test_grant_role_non_integer_discord_id(self, client, fake_redis, mock_rbac):
        """Non-integer discord_user_id returns 400."""
        _store_owner_session(fake_redis)
        _set_session_cookie(client, "owner-token")
        mock_rbac.get_effective_role.return_value = "owner"

        response = client.post(
            f"/api/v1/tenants/{TENANT_ID}/delegates",
            json={"discord_user_id": "not-a-number", "role": "editor"},
        )

        assert response.status_code == 400
        body = response.get_json()
        assert "integer" in body["error"]


# ---------------------------------------------------------------------------
# DELETE /api/v1/tenants/<tenant_id>/delegates/<discord_user_id>
# ---------------------------------------------------------------------------


class TestRevokeDelegate:
    """Tests for DELETE /<tenant_id>/delegates/<discord_user_id> endpoint."""

    def test_revoke_role_success(self, client, fake_redis, mock_rbac):
        """Owner can revoke a delegate's access — returns 204."""
        _store_owner_session(fake_redis)
        _set_session_cookie(client, "owner-token")
        mock_rbac.get_effective_role.return_value = "owner"
        mock_rbac.revoke_role.return_value = None

        response = client.delete(
            f"/api/v1/tenants/{TENANT_ID}/delegates/{DELEGATE_DISCORD_ID}"
        )

        assert response.status_code == 204
        assert response.data == b""

        mock_rbac.revoke_role.assert_called_once_with(
            tenant_id=TENANT_ID,
            discord_user_id=DELEGATE_DISCORD_ID,
        )

    def test_revoke_role_invalid_tenant_id(self, client, fake_redis, mock_rbac):
        """Invalid tenant_id format returns 400."""
        _store_owner_session(fake_redis)
        _set_session_cookie(client, "owner-token")
        mock_rbac.get_effective_role.return_value = "owner"

        response = client.delete(
            f"/api/v1/tenants/bad-uuid/delegates/{DELEGATE_DISCORD_ID}"
        )

        assert response.status_code == 400

    def test_revoke_role_db_error(self, client, fake_redis, mock_rbac):
        """Database error during revoke returns 500."""
        _store_owner_session(fake_redis)
        _set_session_cookie(client, "owner-token")
        mock_rbac.get_effective_role.return_value = "owner"
        mock_rbac.revoke_role.side_effect = Exception("DB connection failed")

        response = client.delete(
            f"/api/v1/tenants/{TENANT_ID}/delegates/{DELEGATE_DISCORD_ID}"
        )

        assert response.status_code == 500
        body = response.get_json()
        assert "Failed to revoke role" in body["error"]


# ---------------------------------------------------------------------------
# PATCH /api/v1/tenants/<tenant_id>/delegates/<discord_user_id>
# ---------------------------------------------------------------------------


class TestUpdateDelegate:
    """Tests for PATCH /<tenant_id>/delegates/<discord_user_id> endpoint."""

    def test_update_role_success(self, client, fake_redis, mock_rbac):
        """Owner can update a delegate's role — returns 200."""
        _store_owner_session(fake_redis)
        _set_session_cookie(client, "owner-token")
        mock_rbac.get_effective_role.return_value = "owner"
        mock_rbac.grant_role.return_value = None

        response = client.patch(
            f"/api/v1/tenants/{TENANT_ID}/delegates/{DELEGATE_DISCORD_ID}",
            json={"role": "admin"},
        )

        assert response.status_code == 200
        body = response.get_json()
        assert body["discord_user_id"] == DELEGATE_DISCORD_ID
        assert body["role"] == "admin"
        assert body["tenant_id"] == TENANT_ID

        mock_rbac.grant_role.assert_called_once_with(
            tenant_id=TENANT_ID,
            discord_user_id=DELEGATE_DISCORD_ID,
            role="admin",
            granted_by=int(OWNER_DISCORD_ID),
        )

    def test_update_role_missing_body(self, client, fake_redis, mock_rbac):
        """No JSON body returns 400."""
        _store_owner_session(fake_redis)
        _set_session_cookie(client, "owner-token")
        mock_rbac.get_effective_role.return_value = "owner"

        response = client.patch(
            f"/api/v1/tenants/{TENANT_ID}/delegates/{DELEGATE_DISCORD_ID}",
            content_type="application/json",
            data="",
        )

        assert response.status_code == 400

    def test_update_role_missing_role_field(self, client, fake_redis, mock_rbac):
        """Missing role field in body returns 400."""
        _store_owner_session(fake_redis)
        _set_session_cookie(client, "owner-token")
        mock_rbac.get_effective_role.return_value = "owner"

        response = client.patch(
            f"/api/v1/tenants/{TENANT_ID}/delegates/{DELEGATE_DISCORD_ID}",
            json={"something": "else"},
        )

        assert response.status_code == 400
        body = response.get_json()
        assert "role" in body["error"]

    def test_update_role_invalid_role(self, client, fake_redis, mock_rbac):
        """Invalid role value returns 400."""
        from services.rbac import InvalidRoleError

        _store_owner_session(fake_redis)
        _set_session_cookie(client, "owner-token")
        mock_rbac.get_effective_role.return_value = "owner"
        mock_rbac.grant_role.side_effect = InvalidRoleError("bad")

        response = client.patch(
            f"/api/v1/tenants/{TENANT_ID}/delegates/{DELEGATE_DISCORD_ID}",
            json={"role": "god_mode"},
        )

        assert response.status_code == 400
        body = response.get_json()
        assert body["error"] == "Invalid role. Must be admin, editor, or viewer"

    def test_update_role_delegate_limit(self, client, fake_redis, mock_rbac):
        """Delegate limit error on PATCH returns 400."""
        from services.rbac import DelegateLimitError

        _store_owner_session(fake_redis)
        _set_session_cookie(client, "owner-token")
        mock_rbac.get_effective_role.return_value = "owner"
        mock_rbac.grant_role.side_effect = DelegateLimitError("limit")

        response = client.patch(
            f"/api/v1/tenants/{TENANT_ID}/delegates/{DELEGATE_DISCORD_ID}",
            json={"role": "viewer"},
        )

        assert response.status_code == 400
        body = response.get_json()
        assert body["error"] == "Maximum delegate limit of 20 reached"

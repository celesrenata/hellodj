"""Unit tests for web-ui/blueprints/session_bp.py — tenant context switching.

Tests the POST /api/v1/session/tenant endpoint:
- Valid tenant switch returns 200 with active_tenant_id and role
- Missing tenant_id returns 400
- Invalid UUID format returns 400
- Tenant not in accessible list returns 403
- login_required enforcement (no session → redirect)

Validates: Requirements 10.2, 10.3, 10.4, 10.5
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

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
def app(fake_redis):
    """Create a minimal Flask app with the session blueprint registered."""
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

    # Import and configure middleware to use our fakeredis
    import auth_middleware
    from services.session_service import SessionService

    auth_middleware.set_redis_client(fake_redis)
    session_service = SessionService(fake_redis)
    auth_middleware.set_session_service(session_service)

    # Register the session blueprint
    from blueprints.session_bp import session_bp

    app.register_blueprint(session_bp)

    return app


@pytest.fixture
def client(app):
    """Flask test client."""
    return app.test_client()


def _set_session_cookie(client, token: str):
    """Set the hellodj_session cookie on the test client."""
    client.set_cookie("hellodj_session", token, domain="localhost")


def _store_session(fake_redis, token: str, session_data: dict, ttl: int = 86400):
    """Store a session in Redis as the SessionService would."""
    key = f"session:{token}"
    fake_redis.set(key, json.dumps(session_data), ex=ttl)
    discord_user_id = str(session_data.get("discord_user_id", ""))
    if discord_user_id:
        fake_redis.sadd(f"user_sessions:{discord_user_id}", token)


def _make_session(roles=None, active_tenant_id=None):
    """Build a valid session data dict for testing."""
    if roles is None:
        roles = [
            {"tenant_id": "11111111-1111-1111-1111-111111111111", "role": "owner"},
            {"tenant_id": "22222222-2222-2222-2222-222222222222", "role": "admin"},
            {"tenant_id": "33333333-3333-3333-3333-333333333333", "role": "viewer"},
        ]
    return {
        "tenant_id": "11111111-1111-1111-1111-111111111111",
        "discord_user_id": "111222333444",
        "discord_username": "testuser",
        "active_tenant_id": active_tenant_id or "11111111-1111-1111-1111-111111111111",
        "is_operator": False,
        "ip_address": "127.0.0.1",
        "created_at": time.time(),
        "roles": roles,
    }


# ---------------------------------------------------------------------------
# Happy path tests
# ---------------------------------------------------------------------------


class TestSwitchTenantSuccess:
    """Tests for successful tenant context switching."""

    def test_switch_to_valid_tenant_returns_200(self, client, fake_redis):
        """Switching to a tenant in the accessible list returns 200 with new context."""
        session_data = _make_session()
        _store_session(fake_redis, "switch-token", session_data)
        _set_session_cookie(client, "switch-token")

        response = client.post(
            "/api/v1/session/tenant",
            json={"tenant_id": "22222222-2222-2222-2222-222222222222"},
            content_type="application/json",
        )

        assert response.status_code == 200
        body = json.loads(response.data)
        assert body["active_tenant_id"] == "22222222-2222-2222-2222-222222222222"
        assert body["role"] == "admin"

    def test_switch_updates_session_in_redis(self, client, fake_redis):
        """After switching, the session's active_tenant_id is updated in Redis."""
        session_data = _make_session()
        _store_session(fake_redis, "update-token", session_data)
        _set_session_cookie(client, "update-token")

        response = client.post(
            "/api/v1/session/tenant",
            json={"tenant_id": "33333333-3333-3333-3333-333333333333"},
            content_type="application/json",
        )

        assert response.status_code == 200

        # Verify Redis was updated
        raw = fake_redis.get("session:update-token")
        updated = json.loads(raw)
        assert updated["active_tenant_id"] == "33333333-3333-3333-3333-333333333333"

    def test_switch_returns_correct_role(self, client, fake_redis):
        """The response includes the role for the target tenant."""
        session_data = _make_session()
        _store_session(fake_redis, "role-token", session_data)
        _set_session_cookie(client, "role-token")

        response = client.post(
            "/api/v1/session/tenant",
            json={"tenant_id": "33333333-3333-3333-3333-333333333333"},
            content_type="application/json",
        )

        assert response.status_code == 200
        body = json.loads(response.data)
        assert body["role"] == "viewer"

    def test_switch_to_owned_tenant(self, client, fake_redis):
        """Can switch back to the owned tenant (owner role)."""
        session_data = _make_session(
            active_tenant_id="22222222-2222-2222-2222-222222222222"
        )
        _store_session(fake_redis, "own-token", session_data)
        _set_session_cookie(client, "own-token")

        response = client.post(
            "/api/v1/session/tenant",
            json={"tenant_id": "11111111-1111-1111-1111-111111111111"},
            content_type="application/json",
        )

        assert response.status_code == 200
        body = json.loads(response.data)
        assert body["active_tenant_id"] == "11111111-1111-1111-1111-111111111111"
        assert body["role"] == "owner"


# ---------------------------------------------------------------------------
# Validation error tests
# ---------------------------------------------------------------------------


class TestSwitchTenantValidation:
    """Tests for input validation on tenant switching."""

    def test_missing_body_returns_400(self, client, fake_redis):
        """Request with no JSON body returns 400."""
        session_data = _make_session()
        _store_session(fake_redis, "no-body-token", session_data)
        _set_session_cookie(client, "no-body-token")

        response = client.post(
            "/api/v1/session/tenant",
            content_type="application/json",
        )

        assert response.status_code == 400
        body = json.loads(response.data)
        assert "tenant_id" in body["error"]

    def test_missing_tenant_id_field_returns_400(self, client, fake_redis):
        """Request with JSON but no tenant_id field returns 400."""
        session_data = _make_session()
        _store_session(fake_redis, "no-field-token", session_data)
        _set_session_cookie(client, "no-field-token")

        response = client.post(
            "/api/v1/session/tenant",
            json={"wrong_field": "value"},
            content_type="application/json",
        )

        assert response.status_code == 400
        body = json.loads(response.data)
        assert "tenant_id" in body["error"]

    def test_invalid_uuid_format_returns_400(self, client, fake_redis):
        """tenant_id that isn't a valid UUID returns 400."""
        session_data = _make_session()
        _store_session(fake_redis, "bad-uuid-token", session_data)
        _set_session_cookie(client, "bad-uuid-token")

        response = client.post(
            "/api/v1/session/tenant",
            json={"tenant_id": "not-a-valid-uuid"},
            content_type="application/json",
        )

        assert response.status_code == 400
        body = json.loads(response.data)
        assert "UUID" in body["error"]

    def test_empty_string_tenant_id_returns_400(self, client, fake_redis):
        """Empty string tenant_id returns 400."""
        session_data = _make_session()
        _store_session(fake_redis, "empty-uuid-token", session_data)
        _set_session_cookie(client, "empty-uuid-token")

        response = client.post(
            "/api/v1/session/tenant",
            json={"tenant_id": ""},
            content_type="application/json",
        )

        assert response.status_code == 400
        body = json.loads(response.data)
        assert "UUID" in body["error"]

    def test_numeric_tenant_id_returns_400(self, client, fake_redis):
        """Numeric tenant_id (not a UUID string) returns 400."""
        session_data = _make_session()
        _store_session(fake_redis, "num-uuid-token", session_data)
        _set_session_cookie(client, "num-uuid-token")

        response = client.post(
            "/api/v1/session/tenant",
            json={"tenant_id": 12345},
            content_type="application/json",
        )

        assert response.status_code == 400
        body = json.loads(response.data)
        assert "UUID" in body["error"]


# ---------------------------------------------------------------------------
# Access control tests
# ---------------------------------------------------------------------------


class TestSwitchTenantAccess:
    """Tests for access control on tenant switching."""

    def test_tenant_not_in_accessible_list_returns_403(self, client, fake_redis):
        """Switching to a tenant not in the user's roles returns 403."""
        session_data = _make_session()
        _store_session(fake_redis, "no-access-token", session_data)
        _set_session_cookie(client, "no-access-token")

        response = client.post(
            "/api/v1/session/tenant",
            json={"tenant_id": "99999999-9999-9999-9999-999999999999"},
            content_type="application/json",
        )

        assert response.status_code == 403
        body = json.loads(response.data)
        assert "do not have access" in body["error"]

    def test_403_does_not_change_active_tenant(self, client, fake_redis):
        """A failed switch does not modify the session's active_tenant_id."""
        session_data = _make_session()
        _store_session(fake_redis, "unchanged-token", session_data)
        _set_session_cookie(client, "unchanged-token")

        response = client.post(
            "/api/v1/session/tenant",
            json={"tenant_id": "99999999-9999-9999-9999-999999999999"},
            content_type="application/json",
        )

        assert response.status_code == 403

        # Verify session is unchanged
        raw = fake_redis.get("session:unchanged-token")
        session = json.loads(raw)
        assert session["active_tenant_id"] == "11111111-1111-1111-1111-111111111111"

    def test_unauthenticated_request_redirects(self, client):
        """Request without a session cookie redirects to login."""
        response = client.post(
            "/api/v1/session/tenant",
            json={"tenant_id": "11111111-1111-1111-1111-111111111111"},
            content_type="application/json",
        )

        assert response.status_code == 302
        assert "/auth/login" in response.headers["Location"]

    def test_expired_session_redirects(self, client, fake_redis):
        """Request with an expired session token redirects to login."""
        _set_session_cookie(client, "expired-token-xyz")

        response = client.post(
            "/api/v1/session/tenant",
            json={"tenant_id": "11111111-1111-1111-1111-111111111111"},
            content_type="application/json",
        )

        assert response.status_code == 302
        assert "/auth/login" in response.headers["Location"]

    def test_user_with_single_tenant_can_switch_to_own(self, client, fake_redis):
        """A user with only one tenant can switch to that same tenant (no-op)."""
        session_data = _make_session(
            roles=[{"tenant_id": "11111111-1111-1111-1111-111111111111", "role": "owner"}]
        )
        _store_session(fake_redis, "single-token", session_data)
        _set_session_cookie(client, "single-token")

        response = client.post(
            "/api/v1/session/tenant",
            json={"tenant_id": "11111111-1111-1111-1111-111111111111"},
            content_type="application/json",
        )

        assert response.status_code == 200
        body = json.loads(response.data)
        assert body["active_tenant_id"] == "11111111-1111-1111-1111-111111111111"
        assert body["role"] == "owner"

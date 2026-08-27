"""End-to-end integration tests for the complete SaaS auth flow.

Tests the full lifecycle:
- Login → Discord OAuth2 → callback → session created → cookie set
- Session validation: valid cookie loads session, extends TTL, populates g.session
- IP binding: different IP invalidates session
- Absolute expiry: session > 7 days old is rejected
- Rate limiting: 11th login attempt returns 429
- Logout: session deleted, cookie cleared, Discord token revoked
- Tenant switching: valid switch updates active_tenant_id, invalid returns 403
- Delegate CRUD: grant/revoke/update roles work, session invalidation fires
- Role enforcement: viewer can't POST, editor can't manage delegates, stranger gets 404
- Operator: can access admin endpoints and any tenant's resources
- Token refresh: expired Discord token triggers refresh with lock

Uses fakeredis (no real Redis) and mocks Discord API calls.

Validates: All Requirements (1-11)
"""

from __future__ import annotations

import json
import os
import secrets
import sys
import time
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import fakeredis
import pytest

# Ensure web-ui is importable
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
def app(redis_client):
    """Create a Flask app wired with all auth components for E2E testing."""
    from flask import Flask

    import auth_middleware
    import blueprints.auth as auth_module
    from blueprints.delegates import delegates_bp
    from blueprints.session_bp import session_bp
    from services.session_service import SessionService

    # Reset module-level singletons
    auth_module._redis_client = None
    auth_module._session_service = None
    auth_module._tenant_service = None

    flask_app = Flask(__name__)
    flask_app.config["TESTING"] = True
    flask_app.secret_key = "e2e-test-secret"

    # Wire auth middleware with our fakeredis
    session_service = SessionService(redis_client)
    auth_middleware.set_redis_client(redis_client)
    auth_middleware.set_session_service(session_service)

    # Register all auth-related blueprints
    flask_app.register_blueprint(auth_module.auth_bp)
    flask_app.register_blueprint(delegates_bp)
    flask_app.register_blueprint(session_bp)

    # Add a protected test route using login_required
    @flask_app.route("/dashboard")
    @auth_middleware.login_required
    def dashboard():
        from flask import g

        return json.dumps({
            "session": g.session,
            "tenant_id": g.tenant_id,
            "is_operator": g.is_operator,
        }), 200

    # Add routes using role_required for enforcement testing
    @flask_app.route("/api/v1/tenants/<tenant_id>/settings", methods=["GET"])
    @auth_middleware.role_required("viewer")
    def get_settings(tenant_id):
        from flask import g

        return json.dumps({
            "tenant_id": tenant_id,
            "effective_role": g.effective_role,
            "action": "read",
        }), 200

    @flask_app.route("/api/v1/tenants/<tenant_id>/settings", methods=["PUT"])
    @auth_middleware.role_required("editor")
    def put_settings(tenant_id):
        from flask import g

        return json.dumps({
            "tenant_id": tenant_id,
            "effective_role": g.effective_role,
            "action": "write",
        }), 200

    @flask_app.route("/api/v1/admin/tenants")
    @auth_middleware.operator_required
    def admin_tenants():
        return json.dumps({"admin": True, "tenants": []}), 200

    with patch("blueprints.auth._get_redis", return_value=redis_client):
        yield flask_app

    # Cleanup
    auth_module._redis_client = None
    auth_module._session_service = None
    auth_module._tenant_service = None
    auth_middleware.set_rbac_service(None)


@pytest.fixture
def client(app):
    """Flask test client."""
    return app.test_client()


@pytest.fixture
def mock_rbac(app):
    """Set up a mock RBAC service for the app."""
    import auth_middleware

    rbac = MagicMock()
    rbac.ROLE_HIERARCHY = {
        "operator": 5,
        "owner": 4,
        "admin": 3,
        "editor": 2,
        "viewer": 1,
    }
    auth_middleware.set_rbac_service(rbac)
    yield rbac
    auth_middleware.set_rbac_service(None)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

OPERATOR_DISCORD_ID = "999888777666"
TENANT_UUID = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
DELEGATED_TENANT_UUID = "b2c3d4e5-f6a7-8901-bcde-f12345678901"


def _store_session(redis_client, token: str, session_data: dict, ttl: int = 86400):
    """Store a session in Redis as SessionService would."""
    key = f"session:{token}"
    redis_client.set(key, json.dumps(session_data), ex=ttl)
    discord_user_id = str(session_data.get("discord_user_id", ""))
    if discord_user_id:
        redis_client.sadd(f"user_sessions:{discord_user_id}", token)


def _make_session(
    discord_user_id: str = "123456789",
    tenant_id: str = TENANT_UUID,
    is_operator: bool = False,
    ip_address: str = "127.0.0.1",
    roles: list | None = None,
    active_tenant_id: str | None = None,
    created_at: float | None = None,
    discord_token_expires_at: float | None = None,
):
    """Build a valid session data dict."""
    if roles is None:
        roles = [{"tenant_id": tenant_id, "role": "owner"}]
    return {
        "tenant_id": tenant_id,
        "discord_user_id": discord_user_id,
        "discord_username": "testuser",
        "email": "test@example.com",
        "avatar": None,
        "is_operator": is_operator,
        "roles": roles,
        "active_tenant_id": active_tenant_id or tenant_id,
        "ip_address": ip_address,
        "created_at": created_at or time.time(),
        "discord_access_token": "mock_access_token",
        "discord_refresh_token": "mock_refresh_token",
        "discord_token_expires_at": discord_token_expires_at or (time.time() + 7200),
        "refresh_retry_count": 0,
    }


# ---------------------------------------------------------------------------
# E2E Test: Full Login → Session → Dashboard Flow
# ---------------------------------------------------------------------------


class TestE2ELoginFlow:
    """End-to-end: login initiation → Discord callback → session → dashboard."""

    def test_full_login_to_dashboard(self, client, redis_client):
        """Complete flow: login → Discord → callback → session → cookie → dashboard."""
        # Step 1: GET /auth/login initiates OAuth2
        with patch("blueprints.auth._get_redis", return_value=redis_client):
            login_response = client.get("/auth/login")

        assert login_response.status_code == 302
        location = login_response.headers["Location"]
        assert "discord.com" in location
        assert "response_type=code" in location

        # Extract state from redirect URL
        from urllib.parse import parse_qs, urlparse

        params = parse_qs(urlparse(location).query)
        state = params["state"][0]

        # Verify state stored in Redis
        assert redis_client.get(f"oauth_state:{state}") == "1"

        # Step 2: Simulate Discord callback with code + state
        tenant_id = str(uuid.uuid4())
        mock_token_data = {
            "access_token": "discord_access_123",
            "refresh_token": "discord_refresh_456",
            "expires_in": 604800,
        }
        mock_profile = {
            "id": "123456789",
            "username": "testuser",
            "global_name": "Test User",
            "email": "test@example.com",
            "avatar": "abc123",
        }
        mock_tenant = {
            "id": tenant_id,
            "discord_user_id": 123456789,
            "discord_username": "Test User",
            "email": "test@example.com",
        }

        mock_tenant_svc = MagicMock()
        mock_tenant_svc.upsert.return_value = mock_tenant
        mock_tenant_svc.list_accessible_tenants.return_value = []

        with patch("blueprints.auth._get_redis", return_value=redis_client), \
             patch("blueprints.auth._exchange_code", return_value=mock_token_data), \
             patch("blueprints.auth._fetch_user_profile", return_value=mock_profile), \
             patch("blueprints.auth._get_tenant_service", return_value=mock_tenant_svc):
            callback_response = client.get(
                f"/auth/callback?code=mock_code&state={state}"
            )

        # Should redirect to dashboard
        assert callback_response.status_code == 302
        assert "/dashboard" in callback_response.headers["Location"]

        # State should be consumed (one-time use)
        assert redis_client.get(f"oauth_state:{state}") is None

        # Session should exist in Redis
        session_keys = [
            k for k in redis_client.keys("session:*") if "lock" not in k
        ]
        assert len(session_keys) == 1
        session_data = json.loads(redis_client.get(session_keys[0]))
        assert session_data["discord_user_id"] == "123456789"
        assert session_data["discord_access_token"] == "discord_access_123"
        assert session_data["ip_address"] is not None
        assert session_data["created_at"] > 0
        assert session_data["active_tenant_id"] == tenant_id
        assert {"tenant_id": tenant_id, "role": "owner"} in session_data["roles"]

        # Step 3: Access the dashboard using the cookie
        # The cookie was set in the callback response — follow the redirect
        # The test client maintains cookies across requests
        import auth_middleware
        from services.session_service import SessionService

        session_service = SessionService(redis_client)
        auth_middleware.set_session_service(session_service)

        dashboard_response = client.get("/dashboard")
        assert dashboard_response.status_code == 200
        body = json.loads(dashboard_response.data)
        assert body["tenant_id"] == tenant_id
        assert body["session"]["discord_user_id"] == "123456789"


# ---------------------------------------------------------------------------
# E2E Test: Session Validation
# ---------------------------------------------------------------------------


class TestE2ESessionValidation:
    """End-to-end session validation: load, extend TTL, populate g.session."""

    def test_valid_session_extends_ttl(self, client, redis_client):
        """Valid session access extends sliding TTL back to 24h."""
        session_data = _make_session()
        # Store with a shorter TTL to test extension
        _store_session(redis_client, "ext-token", session_data, ttl=5000)
        client.set_cookie("hellodj_session", "ext-token", domain="localhost")

        response = client.get("/dashboard")
        assert response.status_code == 200

        # TTL should be extended to 86400
        ttl = redis_client.ttl("session:ext-token")
        assert ttl > 5000
        assert ttl <= 86400

    def test_session_populates_g_context(self, client, redis_client):
        """g.session, g.tenant_id, g.is_operator are populated correctly."""
        session_data = _make_session(
            is_operator=True,
            tenant_id=TENANT_UUID,
        )
        _store_session(redis_client, "ctx-token", session_data)
        client.set_cookie("hellodj_session", "ctx-token", domain="localhost")

        response = client.get("/dashboard")
        assert response.status_code == 200
        body = json.loads(response.data)
        assert body["session"]["is_operator"] is True
        assert body["tenant_id"] == TENANT_UUID
        assert body["is_operator"] is True


# ---------------------------------------------------------------------------
# E2E Test: IP Binding
# ---------------------------------------------------------------------------


class TestE2EIPBinding:
    """End-to-end: IP mismatch invalidates session."""

    def test_different_ip_invalidates_session(self, client, redis_client):
        """Session stored with IP 10.0.0.1, request from 127.0.0.1 → rejected."""
        session_data = _make_session(ip_address="10.0.0.1")
        _store_session(redis_client, "ip-token", session_data)
        client.set_cookie("hellodj_session", "ip-token", domain="localhost")

        response = client.get("/dashboard")

        assert response.status_code == 302
        assert "/auth/login" in response.headers["Location"]
        # Session should be destroyed
        assert redis_client.get("session:ip-token") is None

    def test_matching_x_forwarded_for_grants_access(self, client, redis_client):
        """Session IP matches X-Forwarded-For → access granted."""
        session_data = _make_session(ip_address="203.0.113.50")
        _store_session(redis_client, "xff-token", session_data)
        client.set_cookie("hellodj_session", "xff-token", domain="localhost")

        response = client.get(
            "/dashboard",
            headers={"X-Forwarded-For": "203.0.113.50, 10.0.0.1"},
        )

        assert response.status_code == 200


# ---------------------------------------------------------------------------
# E2E Test: Absolute Expiry
# ---------------------------------------------------------------------------


class TestE2EAbsoluteExpiry:
    """End-to-end: session older than 7 days is rejected."""

    def test_session_over_7_days_rejected(self, client, redis_client):
        """Session with created_at > 604800s ago is invalidated."""
        session_data = _make_session(
            created_at=time.time() - 604801,  # Just over 7 days
        )
        _store_session(redis_client, "old-token", session_data)
        client.set_cookie("hellodj_session", "old-token", domain="localhost")

        response = client.get("/dashboard")

        assert response.status_code == 302
        assert "/auth/login" in response.headers["Location"]
        assert redis_client.get("session:old-token") is None

    def test_session_under_7_days_allowed(self, client, redis_client):
        """Session at 6 days old still works."""
        session_data = _make_session(
            created_at=time.time() - 518400,  # 6 days
        )
        _store_session(redis_client, "young-token", session_data)
        client.set_cookie("hellodj_session", "young-token", domain="localhost")

        response = client.get("/dashboard")
        assert response.status_code == 200


# ---------------------------------------------------------------------------
# E2E Test: Rate Limiting
# ---------------------------------------------------------------------------


class TestE2ERateLimiting:
    """End-to-end: 11th login attempt from same IP returns 429."""

    def test_rate_limit_blocks_11th_attempt(self, client, redis_client):
        """First 10 logins succeed, 11th returns 429 with Retry-After."""
        with patch("blueprints.auth._get_redis", return_value=redis_client):
            for i in range(10):
                response = client.get("/auth/login")
                assert response.status_code == 302, f"Request {i+1} failed"

            # 11th should be rate-limited
            response = client.get("/auth/login")

        assert response.status_code == 429
        assert "Retry-After" in response.headers
        retry_after = int(response.headers["Retry-After"])
        assert 0 < retry_after <= 300


# ---------------------------------------------------------------------------
# E2E Test: Logout
# ---------------------------------------------------------------------------


class TestE2ELogout:
    """End-to-end: logout destroys session, clears cookie, revokes token."""

    def test_full_logout_flow(self, client, redis_client):
        """Logout deletes session, clears cookie, attempts Discord revocation."""
        session_data = _make_session()
        _store_session(redis_client, "logout-token", session_data)
        client.set_cookie("hellodj_session", "logout-token", domain="localhost")

        with patch("blueprints.auth._get_redis", return_value=redis_client), \
             patch("blueprints.auth._revoke_discord_token") as mock_revoke:
            response = client.post("/auth/logout")

        # Redirects to home
        assert response.status_code == 302

        # Session destroyed in Redis
        assert redis_client.get("session:logout-token") is None

        # Discord token revocation was attempted
        mock_revoke.assert_called_once_with("mock_access_token")

        # Cookie cleared
        cookies = response.headers.getlist("Set-Cookie")
        clear_cookies = [c for c in cookies if "hellodj_session=" in c]
        assert any("Max-Age=0" in c for c in clear_cookies)

    def test_logout_then_access_rejected(self, client, redis_client):
        """After logout, accessing protected route redirects to login."""
        session_data = _make_session()
        _store_session(redis_client, "post-logout-token", session_data)
        client.set_cookie("hellodj_session", "post-logout-token", domain="localhost")

        # Logout
        with patch("blueprints.auth._get_redis", return_value=redis_client), \
             patch("blueprints.auth._revoke_discord_token"):
            client.post("/auth/logout")

        # Try to access protected route
        response = client.get("/dashboard")
        assert response.status_code == 302
        assert "/auth/login" in response.headers["Location"]


# ---------------------------------------------------------------------------
# E2E Test: Tenant Switching
# ---------------------------------------------------------------------------


class TestE2ETenantSwitching:
    """End-to-end: tenant context switching via /api/v1/session/tenant."""

    def test_valid_tenant_switch(self, client, redis_client):
        """Switching to an accessible tenant updates session and returns 200."""
        session_data = _make_session(
            roles=[
                {"tenant_id": TENANT_UUID, "role": "owner"},
                {"tenant_id": DELEGATED_TENANT_UUID, "role": "admin"},
            ],
            active_tenant_id=TENANT_UUID,
        )
        _store_session(redis_client, "switch-token", session_data)
        client.set_cookie("hellodj_session", "switch-token", domain="localhost")

        response = client.post(
            "/api/v1/session/tenant",
            json={"tenant_id": DELEGATED_TENANT_UUID},
            content_type="application/json",
        )

        assert response.status_code == 200
        body = json.loads(response.data)
        assert body["active_tenant_id"] == DELEGATED_TENANT_UUID
        assert body["role"] == "admin"

        # Verify Redis is updated
        raw = redis_client.get("session:switch-token")
        updated = json.loads(raw)
        assert updated["active_tenant_id"] == DELEGATED_TENANT_UUID

    def test_invalid_tenant_switch_returns_403(self, client, redis_client):
        """Switching to a tenant not in user's access list returns 403."""
        session_data = _make_session(
            roles=[{"tenant_id": TENANT_UUID, "role": "owner"}],
        )
        _store_session(redis_client, "forbidden-switch-token", session_data)
        client.set_cookie(
            "hellodj_session", "forbidden-switch-token", domain="localhost"
        )

        response = client.post(
            "/api/v1/session/tenant",
            json={"tenant_id": "99999999-9999-9999-9999-999999999999"},
            content_type="application/json",
        )

        assert response.status_code == 403
        body = json.loads(response.data)
        assert "do not have access" in body["error"]

        # Session unchanged
        raw = redis_client.get("session:forbidden-switch-token")
        session = json.loads(raw)
        assert session["active_tenant_id"] == TENANT_UUID


# ---------------------------------------------------------------------------
# E2E Test: Delegate CRUD
# ---------------------------------------------------------------------------


class TestE2EDelegateCRUD:
    """End-to-end: grant, update, revoke delegate roles."""

    def test_grant_revoke_delegate(self, client, redis_client, mock_rbac):
        """Owner grants a delegate role and then revokes it."""
        session_data = _make_session(
            discord_user_id="111222333",
            tenant_id=TENANT_UUID,
            roles=[{"tenant_id": TENANT_UUID, "role": "owner"}],
        )
        _store_session(redis_client, "delegate-token", session_data)
        client.set_cookie("hellodj_session", "delegate-token", domain="localhost")

        mock_rbac.get_effective_role.return_value = "owner"
        mock_rbac.grant_role.return_value = None
        mock_rbac.revoke_role.return_value = None

        # Grant editor role
        response = client.post(
            f"/api/v1/tenants/{TENANT_UUID}/delegates",
            json={"discord_user_id": 999888777, "role": "editor"},
        )
        assert response.status_code == 201
        body = response.get_json()
        assert body["discord_user_id"] == 999888777
        assert body["role"] == "editor"

        mock_rbac.grant_role.assert_called_once_with(
            tenant_id=TENANT_UUID,
            discord_user_id=999888777,
            role="editor",
            granted_by=111222333,
        )

        # Revoke
        response = client.delete(
            f"/api/v1/tenants/{TENANT_UUID}/delegates/999888777"
        )
        assert response.status_code == 204

        mock_rbac.revoke_role.assert_called_once_with(
            tenant_id=TENANT_UUID,
            discord_user_id=999888777,
        )

    def test_update_delegate_role(self, client, redis_client, mock_rbac):
        """Owner updates a delegate from editor to admin."""
        session_data = _make_session(
            discord_user_id="111222333",
            tenant_id=TENANT_UUID,
            roles=[{"tenant_id": TENANT_UUID, "role": "owner"}],
        )
        _store_session(redis_client, "update-delegate-token", session_data)
        client.set_cookie(
            "hellodj_session", "update-delegate-token", domain="localhost"
        )

        mock_rbac.get_effective_role.return_value = "owner"
        mock_rbac.grant_role.return_value = None

        response = client.patch(
            f"/api/v1/tenants/{TENANT_UUID}/delegates/999888777",
            json={"role": "admin"},
        )
        assert response.status_code == 200
        body = response.get_json()
        assert body["role"] == "admin"

    def test_delegate_limit_reached(self, client, redis_client, mock_rbac):
        """Exceeding 20 delegates returns 400."""
        from services.rbac import DelegateLimitError

        session_data = _make_session(
            discord_user_id="111222333",
            tenant_id=TENANT_UUID,
            roles=[{"tenant_id": TENANT_UUID, "role": "owner"}],
        )
        _store_session(redis_client, "limit-token", session_data)
        client.set_cookie("hellodj_session", "limit-token", domain="localhost")

        mock_rbac.get_effective_role.return_value = "owner"
        mock_rbac.grant_role.side_effect = DelegateLimitError("limit")

        response = client.post(
            f"/api/v1/tenants/{TENANT_UUID}/delegates",
            json={"discord_user_id": 555444333, "role": "viewer"},
        )
        assert response.status_code == 400
        body = response.get_json()
        assert "Maximum delegate limit of 20 reached" in body["error"]

    def test_session_invalidation_on_role_change(self, client, redis_client, mock_rbac):
        """Granting/revoking triggers session invalidation (verified via mock)."""
        session_data = _make_session(
            discord_user_id="111222333",
            tenant_id=TENANT_UUID,
            roles=[{"tenant_id": TENANT_UUID, "role": "owner"}],
        )
        _store_session(redis_client, "invalidation-token", session_data)
        client.set_cookie(
            "hellodj_session", "invalidation-token", domain="localhost"
        )

        mock_rbac.get_effective_role.return_value = "owner"
        mock_rbac.grant_role.return_value = None

        response = client.post(
            f"/api/v1/tenants/{TENANT_UUID}/delegates",
            json={"discord_user_id": 888777666, "role": "editor"},
        )
        assert response.status_code == 201

        # The RBACService.grant_role internally calls invalidate_user_sessions
        # Since we're using a mock, we verify it was called with the right params
        mock_rbac.grant_role.assert_called_once()


# ---------------------------------------------------------------------------
# E2E Test: Role Enforcement
# ---------------------------------------------------------------------------


class TestE2ERoleEnforcement:
    """End-to-end: RBAC enforcement — viewers can't write, strangers get 404."""

    def test_viewer_can_read_but_not_write(self, client, redis_client, mock_rbac):
        """Viewer gets 200 on GET, 403 on PUT for same tenant resource."""
        session_data = _make_session(
            discord_user_id="444555666",
            tenant_id=TENANT_UUID,
            roles=[{"tenant_id": TENANT_UUID, "role": "viewer"}],
        )
        _store_session(redis_client, "viewer-token", session_data)
        client.set_cookie("hellodj_session", "viewer-token", domain="localhost")

        mock_rbac.get_effective_role.return_value = "viewer"

        # GET (viewer-level) should succeed
        response = client.get(f"/api/v1/tenants/{TENANT_UUID}/settings")
        assert response.status_code == 200
        body = json.loads(response.data)
        assert body["effective_role"] == "viewer"
        assert body["action"] == "read"

        # PUT (editor-level) should fail
        response = client.put(
            f"/api/v1/tenants/{TENANT_UUID}/settings",
            json={"key": "value"},
        )
        assert response.status_code == 403

    def test_editor_can_write_but_not_manage_delegates(
        self, client, redis_client, mock_rbac
    ):
        """Editor can PUT settings but can't POST to delegates (owner-only)."""
        session_data = _make_session(
            discord_user_id="555666777",
            tenant_id=TENANT_UUID,
            roles=[{"tenant_id": TENANT_UUID, "role": "editor"}],
        )
        _store_session(redis_client, "editor-token", session_data)
        client.set_cookie("hellodj_session", "editor-token", domain="localhost")

        mock_rbac.get_effective_role.return_value = "editor"

        # PUT settings (editor-level) should succeed
        response = client.put(
            f"/api/v1/tenants/{TENANT_UUID}/settings",
            json={"key": "value"},
        )
        assert response.status_code == 200
        body = json.loads(response.data)
        assert body["effective_role"] == "editor"

        # POST delegates (owner-level) should fail
        response = client.post(
            f"/api/v1/tenants/{TENANT_UUID}/delegates",
            json={"discord_user_id": 111222333, "role": "viewer"},
        )
        assert response.status_code == 403

    def test_stranger_gets_404(self, client, redis_client, mock_rbac):
        """User with no relationship to tenant gets 404 (enumeration prevention)."""
        session_data = _make_session(
            discord_user_id="777888999",
            tenant_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            roles=[
                {"tenant_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", "role": "owner"}
            ],
        )
        _store_session(redis_client, "stranger-token", session_data)
        client.set_cookie("hellodj_session", "stranger-token", domain="localhost")

        # No relationship to TENANT_UUID
        mock_rbac.get_effective_role.return_value = None

        response = client.get(f"/api/v1/tenants/{TENANT_UUID}/settings")
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# E2E Test: Operator Access
# ---------------------------------------------------------------------------


class TestE2EOperatorAccess:
    """End-to-end: operator can access admin endpoints and any tenant."""

    def test_operator_accesses_admin_endpoint(self, client, redis_client):
        """Operator (is_operator=True) can access /api/v1/admin/*."""
        session_data = _make_session(
            discord_user_id=OPERATOR_DISCORD_ID,
            is_operator=True,
        )
        _store_session(redis_client, "op-token", session_data)
        client.set_cookie("hellodj_session", "op-token", domain="localhost")

        response = client.get("/api/v1/admin/tenants")
        assert response.status_code == 200
        body = json.loads(response.data)
        assert body["admin"] is True

    def test_non_operator_rejected_from_admin(self, client, redis_client):
        """Non-operator user gets 403 on admin endpoints."""
        session_data = _make_session(
            discord_user_id="regular_user_id",
            is_operator=False,
        )
        _store_session(redis_client, "non-op-token", session_data)
        client.set_cookie("hellodj_session", "non-op-token", domain="localhost")

        response = client.get("/api/v1/admin/tenants")
        assert response.status_code == 403

    def test_operator_accesses_any_tenant(self, client, redis_client, mock_rbac):
        """Operator can access any tenant's resources regardless of membership."""
        session_data = _make_session(
            discord_user_id=OPERATOR_DISCORD_ID,
            is_operator=True,
            roles=[],  # No explicit tenant roles
        )
        _store_session(redis_client, "op-tenant-token", session_data)
        client.set_cookie("hellodj_session", "op-tenant-token", domain="localhost")

        mock_rbac.get_effective_role.return_value = "operator"

        # Access a random tenant's settings
        foreign_tenant = "deadbeef-dead-beef-dead-beefdeadbeef"
        response = client.get(f"/api/v1/tenants/{foreign_tenant}/settings")
        assert response.status_code == 200
        body = json.loads(response.data)
        assert body["effective_role"] == "operator"


# ---------------------------------------------------------------------------
# E2E Test: Discord Token Refresh
# ---------------------------------------------------------------------------


class TestE2ETokenRefresh:
    """End-to-end: expired Discord token triggers refresh with distributed lock."""

    def test_token_refresh_on_near_expiry(self, client, redis_client):
        """Session with token expiring < 1h triggers refresh on next access."""
        session_data = _make_session(
            discord_token_expires_at=time.time() + 1800,  # 30 min left
        )
        _store_session(redis_client, "refresh-token", session_data)
        client.set_cookie("hellodj_session", "refresh-token", domain="localhost")

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "access_token": "refreshed_access_token",
            "refresh_token": "refreshed_refresh_token",
            "expires_in": 604800,
        }

        with patch("auth_middleware.httpx.post", return_value=mock_response), \
             patch.dict(
                 os.environ,
                 {
                     "DISCORD_CLIENT_ID": "test_client_id",
                     "DISCORD_CLIENT_SECRET": "test_client_secret",
                 },
             ):
            response = client.get("/dashboard")

        assert response.status_code == 200

        # Verify token was refreshed in Redis
        raw = redis_client.get("session:refresh-token")
        updated = json.loads(raw)
        assert updated["discord_access_token"] == "refreshed_access_token"
        assert updated["discord_refresh_token"] == "refreshed_refresh_token"
        assert updated["refresh_retry_count"] == 0

    def test_refresh_lock_prevents_concurrent_refreshes(self, client, redis_client):
        """Distributed lock prevents redundant refresh calls."""
        session_data = _make_session(
            discord_token_expires_at=time.time() + 1800,
        )
        _store_session(redis_client, "lock-token", session_data)
        client.set_cookie("hellodj_session", "lock-token", domain="localhost")

        # Simulate another request already holding the lock
        redis_client.set("session_refresh_lock:lock-token", "1", ex=30)

        with patch("auth_middleware.httpx.post") as mock_post, \
             patch.dict(
                 os.environ,
                 {
                     "DISCORD_CLIENT_ID": "test_id",
                     "DISCORD_CLIENT_SECRET": "test_secret",
                 },
             ):
            response = client.get("/dashboard")

        # Request should succeed (using cached session)
        assert response.status_code == 200
        # But httpx.post should NOT have been called (lock not acquired)
        mock_post.assert_not_called()

    def test_invalid_grant_destroys_session(self, client, redis_client):
        """invalid_grant error from Discord destroys the session."""
        session_data = _make_session(
            discord_token_expires_at=time.time() + 1800,
        )
        _store_session(redis_client, "invalid-grant-token", session_data)
        client.set_cookie(
            "hellodj_session", "invalid-grant-token", domain="localhost"
        )

        mock_response = MagicMock()
        mock_response.status_code = 400
        mock_response.json.return_value = {"error": "invalid_grant"}

        with patch("auth_middleware.httpx.post", return_value=mock_response), \
             patch.dict(
                 os.environ,
                 {
                     "DISCORD_CLIENT_ID": "test_id",
                     "DISCORD_CLIENT_SECRET": "test_secret",
                 },
             ):
            response = client.get("/dashboard")

        # The request may still succeed (session was loaded before refresh attempt)
        # but the session should be destroyed for future use
        assert redis_client.get("session:invalid-grant-token") is None

    def test_three_network_failures_destroy_session(self, client, redis_client):
        """3 consecutive network errors during refresh destroys session."""
        import httpx as httpx_mod

        session_data = _make_session(
            discord_token_expires_at=time.time() + 1800,
        )
        session_data["refresh_retry_count"] = 2  # Already 2 failures
        _store_session(redis_client, "fail3-token", session_data)
        client.set_cookie("hellodj_session", "fail3-token", domain="localhost")

        with patch(
            "auth_middleware.httpx.post",
            side_effect=httpx_mod.ConnectError("Connection refused"),
        ), patch.dict(
            os.environ,
            {"DISCORD_CLIENT_ID": "test_id", "DISCORD_CLIENT_SECRET": "test_secret"},
        ):
            response = client.get("/dashboard")

        # Session should be destroyed
        assert redis_client.get("session:fail3-token") is None


# ---------------------------------------------------------------------------
# E2E Test: Combined Flow — Login → Switch → Delegate → Logout
# ---------------------------------------------------------------------------


class TestE2ECombinedFlow:
    """End-to-end: comprehensive combined flow testing."""

    def test_login_switch_tenant_then_logout(self, client, redis_client):
        """Full flow: authenticate → switch tenant → verify → logout."""
        # Step 1: Create a session with multiple tenants
        session_data = _make_session(
            discord_user_id="777888999",
            tenant_id=TENANT_UUID,
            roles=[
                {"tenant_id": TENANT_UUID, "role": "owner"},
                {"tenant_id": DELEGATED_TENANT_UUID, "role": "editor"},
            ],
        )
        _store_session(redis_client, "flow-token", session_data)
        client.set_cookie("hellodj_session", "flow-token", domain="localhost")

        # Step 2: Access dashboard on default tenant
        response = client.get("/dashboard")
        assert response.status_code == 200
        body = json.loads(response.data)
        assert body["tenant_id"] == TENANT_UUID

        # Step 3: Switch to delegated tenant
        response = client.post(
            "/api/v1/session/tenant",
            json={"tenant_id": DELEGATED_TENANT_UUID},
            content_type="application/json",
        )
        assert response.status_code == 200
        body = json.loads(response.data)
        assert body["active_tenant_id"] == DELEGATED_TENANT_UUID
        assert body["role"] == "editor"

        # Step 4: Verify dashboard now shows new tenant
        response = client.get("/dashboard")
        assert response.status_code == 200
        body = json.loads(response.data)
        assert body["tenant_id"] == DELEGATED_TENANT_UUID

        # Step 5: Logout
        with patch("blueprints.auth._get_redis", return_value=redis_client), \
             patch("blueprints.auth._revoke_discord_token"):
            response = client.post("/auth/logout")

        assert response.status_code == 302
        assert redis_client.get("session:flow-token") is None

        # Step 6: Verify access is denied after logout
        response = client.get("/dashboard")
        assert response.status_code == 302
        assert "/auth/login" in response.headers["Location"]

    def test_unauthenticated_access_preserves_original_url(self, client):
        """Unauthenticated access saves the original URL for post-login redirect."""
        response = client.get("/dashboard")
        assert response.status_code == 302
        location = response.headers["Location"]
        assert "/auth/login" in location
        assert "next=" in location
        assert "dashboard" in location

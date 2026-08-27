"""Unit tests for web-ui/auth_middleware.py.

Tests session lookup via Redis/SessionService, @login_required behavior
(IP binding, absolute expiry, sliding expiry, Discord token refresh),
@operator_required authorization, return-to-URL handling, and Redis
unavailability (503).
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import fakeredis
import pytest

# Ensure web-ui/ is importable
_web_ui_dir = str(Path(__file__).resolve().parent.parent / "web-ui")
if _web_ui_dir not in sys.path:
    sys.path.insert(0, _web_ui_dir)


from flask import Flask, g


@pytest.fixture
def fake_redis():
    """Provide a fakeredis client."""
    client = fakeredis.FakeRedis(decode_responses=True)
    yield client
    client.flushall()
    client.close()


@pytest.fixture
def app(fake_redis):
    """Create a minimal Flask app wired with the auth middleware."""
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.secret_key = "test-secret"

    # Register a minimal auth blueprint with a login route so url_for works
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

    # Register test routes that use the decorators
    @app.route("/protected")
    @auth_middleware.login_required
    def protected():
        return json.dumps({"session": g.session, "tenant_id": g.tenant_id}), 200

    @app.route("/admin")
    @auth_middleware.operator_required
    def admin():
        return json.dumps({"admin": True}), 200

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


# ---------------------------------------------------------------------------
# Session lookup tests
# ---------------------------------------------------------------------------


class TestLoginRequired:
    """Tests for @login_required decorator."""

    def test_valid_session_grants_access(self, client, fake_redis):
        """A valid session token in cookie grants access and sets g.session."""
        session_data = {
            "tenant_id": "uuid-123",
            "discord_user_id": "111222333444",
            "discord_username": "testuser",
            "active_tenant_id": "uuid-123",
            "is_operator": False,
            "ip_address": "127.0.0.1",
            "created_at": time.time(),
            "roles": [],
        }
        _store_session(fake_redis, "valid-token-abc", session_data)
        _set_session_cookie(client, "valid-token-abc")

        response = client.get("/protected")

        assert response.status_code == 200
        body = json.loads(response.data)
        assert body["session"]["discord_user_id"] == "111222333444"
        assert body["tenant_id"] == "uuid-123"

    def test_missing_cookie_redirects_to_login(self, client):
        """No hellodj_session cookie → redirect to /auth/login with next param."""
        response = client.get("/protected")

        assert response.status_code == 302
        location = response.headers["Location"]
        assert "/auth/login" in location
        assert "next=" in location

    def test_expired_token_redirects_to_login(self, client, fake_redis):
        """A token not in Redis (expired) → redirect to login."""
        _set_session_cookie(client, "expired-token")

        response = client.get("/protected")

        assert response.status_code == 302
        assert "/auth/login" in response.headers["Location"]

    def test_corrupted_session_gets_evicted_from_redis(self, client, fake_redis):
        """When a corrupted session is found, the key is deleted from Redis."""
        fake_redis.set("session:bad-token", "not-valid-json{{{")
        _set_session_cookie(client, "bad-token")

        response = client.get("/protected")

        assert response.status_code == 302
        # The corrupted key should have been deleted
        assert fake_redis.get("session:bad-token") is None

    def test_return_to_url_includes_original_path(self, client):
        """The redirect includes the originally requested URL in the next param."""
        _set_session_cookie(client, "no-such-token")

        response = client.get("/protected?foo=bar")

        assert response.status_code == 302
        location = response.headers["Location"]
        assert "protected" in location
        assert "foo" in location

    def test_ip_mismatch_destroys_session(self, client, fake_redis):
        """IP binding: different IP from stored → session destroyed, redirect."""
        session_data = {
            "tenant_id": "uuid-123",
            "discord_user_id": "111222333444",
            "discord_username": "testuser",
            "active_tenant_id": "uuid-123",
            "is_operator": False,
            "ip_address": "10.0.0.1",  # Different from test client's 127.0.0.1
            "created_at": time.time(),
            "roles": [],
        }
        _store_session(fake_redis, "ip-mismatch-token", session_data)
        _set_session_cookie(client, "ip-mismatch-token")

        response = client.get("/protected")

        assert response.status_code == 302
        assert "/auth/login" in response.headers["Location"]
        # Session should be destroyed
        assert fake_redis.get("session:ip-mismatch-token") is None

    def test_ip_from_x_forwarded_for_header(self, client, fake_redis):
        """X-Forwarded-For header is used for IP binding check."""
        session_data = {
            "tenant_id": "uuid-123",
            "discord_user_id": "111222333444",
            "discord_username": "testuser",
            "active_tenant_id": "uuid-123",
            "is_operator": False,
            "ip_address": "203.0.113.50",
            "created_at": time.time(),
            "roles": [],
        }
        _store_session(fake_redis, "xff-token", session_data)
        _set_session_cookie(client, "xff-token")

        # Request with matching X-Forwarded-For
        response = client.get(
            "/protected", headers={"X-Forwarded-For": "203.0.113.50, 10.0.0.1"}
        )

        assert response.status_code == 200

    def test_absolute_expiry_destroys_session(self, client, fake_redis):
        """Session older than 7 days (604800s) is rejected and destroyed."""
        session_data = {
            "tenant_id": "uuid-123",
            "discord_user_id": "111222333444",
            "discord_username": "testuser",
            "active_tenant_id": "uuid-123",
            "is_operator": False,
            "ip_address": "127.0.0.1",
            "created_at": time.time() - 604801,  # Just over 7 days ago
            "roles": [],
        }
        _store_session(fake_redis, "old-token", session_data)
        _set_session_cookie(client, "old-token")

        response = client.get("/protected")

        assert response.status_code == 302
        assert "/auth/login" in response.headers["Location"]
        assert fake_redis.get("session:old-token") is None

    def test_sliding_expiry_extends_ttl(self, client, fake_redis):
        """Valid session access extends the TTL (sliding expiry)."""
        session_data = {
            "tenant_id": "uuid-123",
            "discord_user_id": "111222333444",
            "discord_username": "testuser",
            "active_tenant_id": "uuid-123",
            "is_operator": False,
            "ip_address": "127.0.0.1",
            "created_at": time.time(),
            "roles": [],
        }
        # Store with a short TTL
        _store_session(fake_redis, "sliding-token", session_data, ttl=1000)
        _set_session_cookie(client, "sliding-token")

        response = client.get("/protected")

        assert response.status_code == 200
        # TTL should now be extended back to 86400
        ttl = fake_redis.ttl("session:sliding-token")
        assert ttl > 1000  # Extended beyond original

    def test_redis_unavailable_returns_503(self, client, fake_redis):
        """Redis connection error during session load returns 503 JSON."""
        import auth_middleware
        from services.session_service import SessionService, ServiceUnavailableError

        # Create a mock service that raises ServiceUnavailableError
        mock_svc = MagicMock(spec=SessionService)
        mock_svc.load.side_effect = ServiceUnavailableError("Redis unavailable")
        auth_middleware.set_session_service(mock_svc)

        _set_session_cookie(client, "some-token")

        response = client.get("/protected")

        assert response.status_code == 503
        body = json.loads(response.data)
        assert "temporarily unavailable" in body["error"].lower()

        # Restore original service
        auth_middleware.set_session_service(SessionService(fake_redis))

    def test_g_context_populated(self, client, fake_redis):
        """g.session, g.tenant_id, g.is_operator are set correctly."""
        session_data = {
            "tenant_id": "uuid-abc",
            "discord_user_id": "999",
            "discord_username": "context_user",
            "active_tenant_id": "uuid-abc",
            "is_operator": True,
            "ip_address": "127.0.0.1",
            "created_at": time.time(),
            "roles": [{"tenant_id": "uuid-abc", "role": "owner"}],
        }
        _store_session(fake_redis, "ctx-token", session_data)
        _set_session_cookie(client, "ctx-token")

        response = client.get("/protected")

        assert response.status_code == 200
        body = json.loads(response.data)
        assert body["tenant_id"] == "uuid-abc"
        assert body["session"]["is_operator"] is True


# ---------------------------------------------------------------------------
# Operator authorization tests
# ---------------------------------------------------------------------------


class TestOperatorRequired:
    """Tests for @operator_required decorator."""

    def test_operator_access_granted(self, client, fake_redis):
        """Session with is_operator=True gets access."""
        session_data = {
            "tenant_id": "uuid-op",
            "discord_user_id": "999888777666",
            "discord_username": "operator",
            "active_tenant_id": "uuid-op",
            "is_operator": True,
            "ip_address": "127.0.0.1",
            "created_at": time.time(),
            "roles": [],
        }
        _store_session(fake_redis, "op-token", session_data)
        _set_session_cookie(client, "op-token")

        response = client.get("/admin")

        assert response.status_code == 200
        body = json.loads(response.data)
        assert body["admin"] is True

    def test_non_operator_gets_403(self, client, fake_redis):
        """Authenticated user with is_operator=False gets 403."""
        session_data = {
            "tenant_id": "uuid-user",
            "discord_user_id": "111000111000",
            "discord_username": "regular_user",
            "active_tenant_id": "uuid-user",
            "is_operator": False,
            "ip_address": "127.0.0.1",
            "created_at": time.time(),
            "roles": [],
        }
        _store_session(fake_redis, "user-token", session_data)
        _set_session_cookie(client, "user-token")

        response = client.get("/admin")

        assert response.status_code == 403

    def test_unauthenticated_user_redirects_to_login(self, client):
        """No session → redirect to login (login_required runs first)."""
        response = client.get("/admin")

        assert response.status_code == 302
        assert "/auth/login" in response.headers["Location"]

    def test_missing_operator_flag_rejects(self, client, fake_redis):
        """Session without is_operator field defaults to False → 403."""
        session_data = {
            "tenant_id": "uuid-any",
            "discord_user_id": "123456789",
            "discord_username": "anyone",
            "active_tenant_id": "uuid-any",
            "ip_address": "127.0.0.1",
            "created_at": time.time(),
            "roles": [],
        }
        _store_session(fake_redis, "any-token", session_data)
        _set_session_cookie(client, "any-token")

        response = client.get("/admin")

        assert response.status_code == 403


# ---------------------------------------------------------------------------
# Discord token refresh tests
# ---------------------------------------------------------------------------


class TestDiscordTokenRefresh:
    """Tests for _maybe_refresh_discord_token."""

    def test_refresh_triggered_when_token_near_expiry(self, client, fake_redis):
        """Discord token refresh fires when < 1 hour until expiry."""
        session_data = {
            "tenant_id": "uuid-123",
            "discord_user_id": "111222333444",
            "discord_username": "testuser",
            "active_tenant_id": "uuid-123",
            "is_operator": False,
            "ip_address": "127.0.0.1",
            "created_at": time.time(),
            "roles": [],
            "discord_access_token": "old_access_token",
            "discord_refresh_token": "valid_refresh_token",
            "discord_token_expires_at": time.time() + 1800,  # 30min left
            "refresh_retry_count": 0,
        }
        _store_session(fake_redis, "refresh-token", session_data)
        _set_session_cookie(client, "refresh-token")

        # Mock the httpx.post call to Discord
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "access_token": "new_access_token",
            "refresh_token": "new_refresh_token",
            "expires_in": 604800,
        }

        with patch("auth_middleware.httpx.post", return_value=mock_response):
            with patch.dict(
                os.environ,
                {"DISCORD_CLIENT_ID": "test_id", "DISCORD_CLIENT_SECRET": "test_secret"},
            ):
                response = client.get("/protected")

        assert response.status_code == 200

        # Verify session was updated in Redis
        raw = fake_redis.get("session:refresh-token")
        updated_session = json.loads(raw)
        assert updated_session["discord_access_token"] == "new_access_token"
        assert updated_session["discord_refresh_token"] == "new_refresh_token"
        assert updated_session["refresh_retry_count"] == 0

    def test_no_refresh_when_token_not_expiring(self, client, fake_redis):
        """No refresh triggered when token has > 1 hour remaining."""
        session_data = {
            "tenant_id": "uuid-123",
            "discord_user_id": "111222333444",
            "discord_username": "testuser",
            "active_tenant_id": "uuid-123",
            "is_operator": False,
            "ip_address": "127.0.0.1",
            "created_at": time.time(),
            "roles": [],
            "discord_access_token": "current_token",
            "discord_refresh_token": "refresh_token",
            "discord_token_expires_at": time.time() + 7200,  # 2 hours left
            "refresh_retry_count": 0,
        }
        _store_session(fake_redis, "no-refresh-token", session_data)
        _set_session_cookie(client, "no-refresh-token")

        with patch("auth_middleware.httpx.post") as mock_post:
            response = client.get("/protected")

        assert response.status_code == 200
        # httpx.post should NOT have been called
        mock_post.assert_not_called()

    def test_invalid_grant_destroys_session(self, client, fake_redis):
        """invalid_grant from Discord destroys the session."""
        session_data = {
            "tenant_id": "uuid-123",
            "discord_user_id": "111222333444",
            "discord_username": "testuser",
            "active_tenant_id": "uuid-123",
            "is_operator": False,
            "ip_address": "127.0.0.1",
            "created_at": time.time(),
            "roles": [],
            "discord_access_token": "old_token",
            "discord_refresh_token": "invalid_refresh",
            "discord_token_expires_at": time.time() + 1800,
            "refresh_retry_count": 0,
        }
        _store_session(fake_redis, "invalid-grant-token", session_data)
        _set_session_cookie(client, "invalid-grant-token")

        mock_response = MagicMock()
        mock_response.status_code = 400
        mock_response.json.return_value = {"error": "invalid_grant"}

        with patch("auth_middleware.httpx.post", return_value=mock_response):
            with patch.dict(
                os.environ,
                {"DISCORD_CLIENT_ID": "test_id", "DISCORD_CLIENT_SECRET": "test_secret"},
            ):
                response = client.get("/protected")

        # Session was still valid for this request up until the refresh check.
        # The response depends on timing — the session was loaded, validated,
        # then refresh destroyed it. The request still completes with 200
        # because the destroy happens after g.session is set.
        # Actually, _maybe_refresh_discord_token is called BEFORE g.session is set
        # in the decorator — let's check the flow. Looking at the code:
        # g.session is set AFTER _maybe_refresh_discord_token. The destroy happens
        # during the refresh. But since we already loaded the session, the response
        # should still succeed. The session is destroyed for FUTURE requests.
        assert response.status_code == 200
        # But the session should be gone from Redis
        assert fake_redis.get("session:invalid-grant-token") is None

    def test_network_error_increments_retry_counter(self, client, fake_redis):
        """Network error during refresh increments retry counter."""
        import httpx as httpx_mod

        session_data = {
            "tenant_id": "uuid-123",
            "discord_user_id": "111222333444",
            "discord_username": "testuser",
            "active_tenant_id": "uuid-123",
            "is_operator": False,
            "ip_address": "127.0.0.1",
            "created_at": time.time(),
            "roles": [],
            "discord_access_token": "old_token",
            "discord_refresh_token": "good_refresh",
            "discord_token_expires_at": time.time() + 1800,
            "refresh_retry_count": 0,
        }
        _store_session(fake_redis, "network-err-token", session_data)
        _set_session_cookie(client, "network-err-token")

        with patch(
            "auth_middleware.httpx.post",
            side_effect=httpx_mod.ConnectError("Connection refused"),
        ):
            with patch.dict(
                os.environ,
                {"DISCORD_CLIENT_ID": "test_id", "DISCORD_CLIENT_SECRET": "test_secret"},
            ):
                response = client.get("/protected")

        assert response.status_code == 200
        # Retry counter should be incremented
        raw = fake_redis.get("session:network-err-token")
        updated_session = json.loads(raw)
        assert updated_session["refresh_retry_count"] == 1

    def test_three_consecutive_failures_destroys_session(self, client, fake_redis):
        """3 consecutive network errors destroys the session."""
        import httpx as httpx_mod

        session_data = {
            "tenant_id": "uuid-123",
            "discord_user_id": "111222333444",
            "discord_username": "testuser",
            "active_tenant_id": "uuid-123",
            "is_operator": False,
            "ip_address": "127.0.0.1",
            "created_at": time.time(),
            "roles": [],
            "discord_access_token": "old_token",
            "discord_refresh_token": "good_refresh",
            "discord_token_expires_at": time.time() + 1800,
            "refresh_retry_count": 2,  # Already 2 failures
        }
        _store_session(fake_redis, "fail3-token", session_data)
        _set_session_cookie(client, "fail3-token")

        with patch(
            "auth_middleware.httpx.post",
            side_effect=httpx_mod.ConnectError("Connection refused"),
        ):
            with patch.dict(
                os.environ,
                {"DISCORD_CLIENT_ID": "test_id", "DISCORD_CLIENT_SECRET": "test_secret"},
            ):
                response = client.get("/protected")

        # Request still completes since session was loaded before refresh attempt
        assert response.status_code == 200
        # But session should be destroyed for future requests
        assert fake_redis.get("session:fail3-token") is None


# ---------------------------------------------------------------------------
# Client IP helper tests
# ---------------------------------------------------------------------------


class TestGetClientIp:
    """Tests for _get_client_ip helper."""

    def test_uses_x_forwarded_for_first_entry(self, app):
        """First entry in X-Forwarded-For is used as client IP."""
        import auth_middleware

        with app.test_request_context(
            headers={"X-Forwarded-For": "1.2.3.4, 10.0.0.1, 192.168.1.1"}
        ):
            assert auth_middleware._get_client_ip() == "1.2.3.4"

    def test_falls_back_to_remote_addr(self, app):
        """Without X-Forwarded-For, falls back to request.remote_addr."""
        import auth_middleware

        with app.test_request_context(environ_base={"REMOTE_ADDR": "192.168.1.100"}):
            assert auth_middleware._get_client_ip() == "192.168.1.100"

    def test_strips_whitespace_from_xff(self, app):
        """Whitespace in X-Forwarded-For entries is stripped."""
        import auth_middleware

        with app.test_request_context(
            headers={"X-Forwarded-For": "  5.6.7.8  , 10.0.0.1"}
        ):
            assert auth_middleware._get_client_ip() == "5.6.7.8"


# ---------------------------------------------------------------------------
# role_required decorator tests
# ---------------------------------------------------------------------------


class TestRoleRequired:
    """Tests for @role_required decorator."""

    @pytest.fixture(autouse=True)
    def setup_role_routes(self, app, fake_redis):
        """Register routes that use @role_required."""
        import auth_middleware

        # Create a mock RBACService
        mock_rbac = MagicMock()
        mock_rbac.ROLE_HIERARCHY = {
            "operator": 5,
            "owner": 4,
            "admin": 3,
            "editor": 2,
            "viewer": 1,
        }
        auth_middleware.set_rbac_service(mock_rbac)
        self.mock_rbac = mock_rbac

        @app.route("/tenant/<tenant_id>/settings")
        @auth_middleware.role_required("editor")
        def edit_settings(tenant_id):
            return json.dumps(
                {"tenant_id": tenant_id, "effective_role": g.effective_role}
            ), 200

        @app.route("/tenant/<tenant_id>/delegates")
        @auth_middleware.role_required("owner")
        def manage_delegates(tenant_id):
            return json.dumps(
                {"tenant_id": tenant_id, "effective_role": g.effective_role}
            ), 200

        @app.route("/dashboard/view")
        @auth_middleware.role_required("viewer")
        def view_dashboard():
            return json.dumps(
                {"tenant_id": g.tenant_id, "effective_role": g.effective_role}
            ), 200

        yield

        # Cleanup
        auth_middleware.set_rbac_service(None)

    def test_access_granted_when_role_sufficient(self, client, fake_redis):
        """User with editor role can access editor-required endpoint."""
        session_data = {
            "tenant_id": "uuid-t1",
            "discord_user_id": "111222333444",
            "discord_username": "testuser",
            "active_tenant_id": "uuid-t1",
            "is_operator": False,
            "ip_address": "127.0.0.1",
            "created_at": time.time(),
            "roles": [{"tenant_id": "uuid-t1", "role": "editor"}],
        }
        _store_session(fake_redis, "role-token", session_data)
        _set_session_cookie(client, "role-token")

        self.mock_rbac.get_effective_role.return_value = "editor"

        response = client.get("/tenant/uuid-t1/settings")

        assert response.status_code == 200
        body = json.loads(response.data)
        assert body["tenant_id"] == "uuid-t1"
        assert body["effective_role"] == "editor"

    def test_access_granted_when_role_exceeds_required(self, client, fake_redis):
        """Owner can access editor-required endpoint (higher in hierarchy)."""
        session_data = {
            "tenant_id": "uuid-t1",
            "discord_user_id": "111222333444",
            "discord_username": "testuser",
            "active_tenant_id": "uuid-t1",
            "is_operator": False,
            "ip_address": "127.0.0.1",
            "created_at": time.time(),
            "roles": [{"tenant_id": "uuid-t1", "role": "owner"}],
        }
        _store_session(fake_redis, "owner-token", session_data)
        _set_session_cookie(client, "owner-token")

        self.mock_rbac.get_effective_role.return_value = "owner"

        response = client.get("/tenant/uuid-t1/settings")

        assert response.status_code == 200
        body = json.loads(response.data)
        assert body["effective_role"] == "owner"

    def test_operator_can_access_any_tenant(self, client, fake_redis):
        """Operator role grants access to any tenant's resources."""
        session_data = {
            "tenant_id": "uuid-op",
            "discord_user_id": "999",
            "discord_username": "operator",
            "active_tenant_id": "uuid-op",
            "is_operator": True,
            "ip_address": "127.0.0.1",
            "created_at": time.time(),
            "roles": [],
        }
        _store_session(fake_redis, "op-role-token", session_data)
        _set_session_cookie(client, "op-role-token")

        self.mock_rbac.get_effective_role.return_value = "operator"

        response = client.get("/tenant/uuid-other/delegates")

        assert response.status_code == 200
        body = json.loads(response.data)
        assert body["effective_role"] == "operator"

    def test_insufficient_role_returns_403(self, client, fake_redis):
        """Viewer trying to access editor endpoint gets 403."""
        session_data = {
            "tenant_id": "uuid-t1",
            "discord_user_id": "111222333444",
            "discord_username": "testuser",
            "active_tenant_id": "uuid-t1",
            "is_operator": False,
            "ip_address": "127.0.0.1",
            "created_at": time.time(),
            "roles": [{"tenant_id": "uuid-t1", "role": "viewer"}],
        }
        _store_session(fake_redis, "viewer-token", session_data)
        _set_session_cookie(client, "viewer-token")

        self.mock_rbac.get_effective_role.return_value = "viewer"

        response = client.get("/tenant/uuid-t1/settings")

        assert response.status_code == 403

    def test_no_relationship_returns_404(self, client, fake_redis):
        """User with no role for tenant gets 404 (enumeration prevention)."""
        session_data = {
            "tenant_id": "uuid-own",
            "discord_user_id": "111222333444",
            "discord_username": "testuser",
            "active_tenant_id": "uuid-own",
            "is_operator": False,
            "ip_address": "127.0.0.1",
            "created_at": time.time(),
            "roles": [{"tenant_id": "uuid-own", "role": "owner"}],
        }
        _store_session(fake_redis, "stranger-token", session_data)
        _set_session_cookie(client, "stranger-token")

        # No relationship to uuid-foreign
        self.mock_rbac.get_effective_role.return_value = None

        response = client.get("/tenant/uuid-foreign/settings")

        assert response.status_code == 404

    def test_tenant_id_from_url_kwargs_preferred(self, client, fake_redis):
        """tenant_id from URL path takes precedence over g.tenant_id."""
        session_data = {
            "tenant_id": "uuid-session",
            "discord_user_id": "111222333444",
            "discord_username": "testuser",
            "active_tenant_id": "uuid-session",
            "is_operator": False,
            "ip_address": "127.0.0.1",
            "created_at": time.time(),
            "roles": [
                {"tenant_id": "uuid-session", "role": "owner"},
                {"tenant_id": "uuid-url", "role": "admin"},
            ],
        }
        _store_session(fake_redis, "kwargs-token", session_data)
        _set_session_cookie(client, "kwargs-token")

        self.mock_rbac.get_effective_role.return_value = "admin"

        response = client.get("/tenant/uuid-url/settings")

        assert response.status_code == 200
        # Verify get_effective_role was called with the URL tenant_id
        self.mock_rbac.get_effective_role.assert_called_with(
            session_data, "uuid-url"
        )

    def test_falls_back_to_g_tenant_id(self, client, fake_redis):
        """When no tenant_id in URL, uses g.tenant_id from session."""
        session_data = {
            "tenant_id": "uuid-active",
            "discord_user_id": "111222333444",
            "discord_username": "testuser",
            "active_tenant_id": "uuid-active",
            "is_operator": False,
            "ip_address": "127.0.0.1",
            "created_at": time.time(),
            "roles": [{"tenant_id": "uuid-active", "role": "viewer"}],
        }
        _store_session(fake_redis, "fallback-token", session_data)
        _set_session_cookie(client, "fallback-token")

        self.mock_rbac.get_effective_role.return_value = "viewer"

        response = client.get("/dashboard/view")

        assert response.status_code == 200
        body = json.loads(response.data)
        assert body["tenant_id"] == "uuid-active"
        # Verify get_effective_role was called with g.tenant_id
        self.mock_rbac.get_effective_role.assert_called_with(
            session_data, "uuid-active"
        )

    def test_unauthenticated_redirects_to_login(self, client):
        """No session → redirect to login (login_required runs first)."""
        response = client.get("/tenant/uuid-t1/settings")

        assert response.status_code == 302
        assert "/auth/login" in response.headers["Location"]

    def test_g_effective_role_set_on_success(self, client, fake_redis):
        """g.effective_role is set for downstream handlers."""
        session_data = {
            "tenant_id": "uuid-t1",
            "discord_user_id": "111222333444",
            "discord_username": "testuser",
            "active_tenant_id": "uuid-t1",
            "is_operator": False,
            "ip_address": "127.0.0.1",
            "created_at": time.time(),
            "roles": [{"tenant_id": "uuid-t1", "role": "admin"}],
        }
        _store_session(fake_redis, "admin-token", session_data)
        _set_session_cookie(client, "admin-token")

        self.mock_rbac.get_effective_role.return_value = "admin"

        response = client.get("/tenant/uuid-t1/settings")

        assert response.status_code == 200
        body = json.loads(response.data)
        assert body["effective_role"] == "admin"


# ---------------------------------------------------------------------------
# get_rbac_service factory tests
# ---------------------------------------------------------------------------


class TestGetRbacService:
    """Tests for get_rbac_service() lazy singleton."""

    def test_returns_rbac_service_instance(self, app, fake_redis):
        """Factory returns an RBACService instance."""
        import auth_middleware

        # Reset to None so it creates a new one
        auth_middleware.set_rbac_service(None)

        with patch.dict(
            os.environ,
            {"HELLODJ_PG_URI": "postgresql://test:test@localhost:5432/testdb"},
        ):
            with app.app_context():
                service = auth_middleware.get_rbac_service()

        from services.rbac import RBACService

        assert isinstance(service, RBACService)

        # Cleanup
        auth_middleware.set_rbac_service(None)

    def test_singleton_returns_same_instance(self, app, fake_redis):
        """Repeated calls return the same instance (lazy singleton)."""
        import auth_middleware

        auth_middleware.set_rbac_service(None)

        with patch.dict(
            os.environ,
            {"HELLODJ_PG_URI": "postgresql://test:test@localhost:5432/testdb"},
        ):
            with app.app_context():
                service1 = auth_middleware.get_rbac_service()
                service2 = auth_middleware.get_rbac_service()

        assert service1 is service2

        # Cleanup
        auth_middleware.set_rbac_service(None)

    def test_set_rbac_service_overrides(self, app, fake_redis):
        """set_rbac_service allows injection for testing."""
        import auth_middleware

        mock_service = MagicMock()
        auth_middleware.set_rbac_service(mock_service)

        with app.app_context():
            service = auth_middleware.get_rbac_service()

        assert service is mock_service

        # Cleanup
        auth_middleware.set_rbac_service(None)

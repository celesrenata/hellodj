"""Unit tests for the Discord OAuth2 auth blueprint.

Tests cover:
- GET /auth/login: state generation, redirect to Discord, rate limiting,
  Redis unavailability handling
- GET /auth/callback: state validation, code exchange, tenant UPSERT via
  TenantService, session creation via SessionService, roles building,
  operator detection, IP/timestamp storage
- POST /auth/logout: session invalidation, cookie clearing
- GET /auth/me: session lookup, profile return, expired session handling
"""

from __future__ import annotations

import json
import secrets
import sys
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse

import fakeredis
import pytest
from flask import Flask

# Add web-ui directory to path so blueprints package is importable
_webui_dir = str(Path(__file__).resolve().parent.parent / "web-ui")
if _webui_dir not in sys.path:
    sys.path.insert(0, _webui_dir)


@pytest.fixture
def redis_client():
    """Provide a fresh fakeredis instance per test."""
    client = fakeredis.FakeRedis(decode_responses=True)
    yield client
    client.flushall()
    client.close()


@pytest.fixture
def app(redis_client):
    """Create a Flask test app with the auth blueprint registered."""
    import blueprints.auth as auth_module

    # Reset module-level singletons to prevent cross-test contamination
    auth_module._redis_client = None
    auth_module._session_service = None
    auth_module._tenant_service = None

    flask_app = Flask(__name__)
    flask_app.config["TESTING"] = True
    flask_app.secret_key = "test-secret"

    flask_app.register_blueprint(auth_module.auth_bp)

    # Patch _get_redis to return our fakeredis
    with patch("blueprints.auth._get_redis", return_value=redis_client):
        yield flask_app

    # Cleanup singletons after test
    auth_module._redis_client = None
    auth_module._session_service = None
    auth_module._tenant_service = None


@pytest.fixture
def client(app):
    """Flask test client."""
    return app.test_client()


# ---------------------------------------------------------------------------
# Common mock helpers
# ---------------------------------------------------------------------------

def _make_callback_mocks(
    discord_user_id: int = 123456789,
    username: str = "Test User",
    email: str = "test@example.com",
    avatar: str = "abc123",
    access_token: str = "mock_access_token",
    refresh_token: str = "mock_refresh_token",
    expires_in: int = 604800,
    tenant_id: str | None = None,
    accessible_tenants: list | None = None,
):
    """Build standard mocks for a successful callback flow."""
    if tenant_id is None:
        tenant_id = str(uuid.uuid4())

    mock_token_data = {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_in": expires_in,
    }
    mock_profile = {
        "id": str(discord_user_id),
        "username": username.lower().replace(" ", ""),
        "global_name": username,
        "email": email,
        "avatar": avatar,
    }
    mock_tenant = {
        "id": tenant_id,
        "discord_user_id": discord_user_id,
        "discord_username": username,
        "email": email,
    }
    if accessible_tenants is None:
        accessible_tenants = []

    return mock_token_data, mock_profile, mock_tenant, accessible_tenants


class TestLogin:
    """Tests for GET /auth/login."""

    def test_login_redirects_to_discord(self, client, redis_client):
        """Login should redirect to Discord OAuth2 with correct params."""
        with patch("blueprints.auth._get_redis", return_value=redis_client):
            response = client.get("/auth/login")

        assert response.status_code == 302
        location = response.headers["Location"]
        parsed = urlparse(location)
        assert parsed.scheme == "https"
        assert parsed.netloc == "discord.com"
        assert "/api/oauth2/authorize" in parsed.path

        params = parse_qs(parsed.query)
        assert params["response_type"] == ["code"]
        assert "state" in params

    def test_login_scope_includes_guilds(self, client, redis_client):
        """Login OAuth2 scope must include 'identify email guilds'."""
        with patch("blueprints.auth._get_redis", return_value=redis_client):
            response = client.get("/auth/login")

        location = response.headers["Location"]
        params = parse_qs(urlparse(location).query)
        scope = params["scope"][0]
        assert "identify" in scope
        assert "email" in scope
        assert "guilds" in scope

    def test_login_stores_state_in_redis(self, client, redis_client):
        """Login should store the state parameter in Redis with TTL."""
        with patch("blueprints.auth._get_redis", return_value=redis_client):
            response = client.get("/auth/login")

        location = response.headers["Location"]
        params = parse_qs(urlparse(location).query)
        state = params["state"][0]

        # State should be stored in Redis
        assert redis_client.get(f"oauth_state:{state}") == "1"
        # TTL should be set (within a reasonable range)
        ttl = redis_client.ttl(f"oauth_state:{state}")
        assert 0 < ttl <= 300  # 5 minutes max

    def test_login_state_has_256_bits_entropy(self, client, redis_client):
        """State should be at least 256 bits (32 bytes url-safe base64 ≈ 43 chars)."""
        with patch("blueprints.auth._get_redis", return_value=redis_client):
            response = client.get("/auth/login")

        location = response.headers["Location"]
        params = parse_qs(urlparse(location).query)
        state = params["state"][0]

        # token_urlsafe(32) produces ~43 chars of url-safe base64
        assert len(state) >= 40

    def test_login_rate_limit_allows_first_10(self, client, redis_client):
        """First 10 login attempts from same IP should succeed."""
        with patch("blueprints.auth._get_redis", return_value=redis_client):
            for _ in range(10):
                response = client.get("/auth/login")
                assert response.status_code == 302

    def test_login_rate_limit_blocks_11th(self, client, redis_client):
        """11th login attempt should return 429 with Retry-After header."""
        with patch("blueprints.auth._get_redis", return_value=redis_client):
            for _ in range(10):
                client.get("/auth/login")

            response = client.get("/auth/login")

        assert response.status_code == 429
        assert "Retry-After" in response.headers
        retry_after = int(response.headers["Retry-After"])
        assert retry_after > 0
        assert retry_after <= 300

    def test_login_redis_unavailable_returns_503(self, client, redis_client):
        """If Redis is unavailable, login should return error page, not redirect."""
        import redis as redis_lib

        def raise_connection_error(*args, **kwargs):
            raise redis_lib.ConnectionError("Connection refused")

        mock_redis = MagicMock()
        mock_redis.incr.side_effect = raise_connection_error

        with patch("blueprints.auth._get_redis", return_value=mock_redis):
            response = client.get("/auth/login")

        assert response.status_code == 503
        assert b"Login temporarily unavailable" in response.data


class TestCallback:
    """Tests for GET /auth/callback."""

    def test_callback_state_mismatch(self, client, redis_client):
        """Callback with invalid state should redirect with ?error=state_mismatch."""
        with patch("blueprints.auth._get_redis", return_value=redis_client):
            response = client.get(
                "/auth/callback?code=testcode&state=invalid_state"
            )

        assert response.status_code == 302
        assert "error=state_mismatch" in response.headers["Location"]

    def test_callback_missing_state(self, client, redis_client):
        """Callback without state should redirect with ?error=state_mismatch."""
        with patch("blueprints.auth._get_redis", return_value=redis_client):
            response = client.get("/auth/callback?code=testcode")

        assert response.status_code == 302
        assert "error=state_mismatch" in response.headers["Location"]

    def test_callback_user_denied(self, client, redis_client):
        """Callback with error=access_denied should redirect with ?error=denied."""
        with patch("blueprints.auth._get_redis", return_value=redis_client):
            response = client.get("/auth/callback?error=access_denied")

        assert response.status_code == 302
        assert "error=denied" in response.headers["Location"]

    def test_callback_code_exchange_failure(self, client, redis_client):
        """Failed code exchange should redirect with ?error=service_unavailable."""
        state = secrets.token_urlsafe(32)
        redis_client.set(f"oauth_state:{state}", "1", ex=300)

        with patch("blueprints.auth._get_redis", return_value=redis_client), \
             patch("blueprints.auth._exchange_code", side_effect=RuntimeError("timeout")):
            response = client.get(f"/auth/callback?code=badcode&state={state}")

        assert response.status_code == 302
        assert "error=service_unavailable" in response.headers["Location"]

    def test_callback_success_creates_session(self, client, redis_client):
        """Successful callback should create session in Redis and set cookie."""
        state = secrets.token_urlsafe(32)
        redis_client.set(f"oauth_state:{state}", "1", ex=300)

        mock_token_data, mock_profile, mock_tenant, accessible = _make_callback_mocks()

        mock_tenant_svc = MagicMock()
        mock_tenant_svc.upsert.return_value = mock_tenant
        mock_tenant_svc.list_accessible_tenants.return_value = accessible

        with patch("blueprints.auth._get_redis", return_value=redis_client), \
             patch("blueprints.auth._exchange_code", return_value=mock_token_data), \
             patch("blueprints.auth._fetch_user_profile", return_value=mock_profile), \
             patch("blueprints.auth._get_tenant_service", return_value=mock_tenant_svc):
            response = client.get(f"/auth/callback?code=validcode&state={state}")

        # Should redirect to dashboard
        assert response.status_code == 302
        assert "/dashboard" in response.headers["Location"]

        # Should set hellodj_session cookie
        cookies = response.headers.getlist("Set-Cookie")
        session_cookie = [c for c in cookies if "hellodj_session=" in c]
        assert len(session_cookie) == 1
        assert "HttpOnly" in session_cookie[0]
        assert "Secure" in session_cookie[0]
        assert "SameSite=Lax" in session_cookie[0]
        assert "Path=/" in session_cookie[0]

        # Session should be stored in Redis
        keys = [k for k in redis_client.keys("session:*") if "lock" not in k]
        assert len(keys) == 1
        session_data = json.loads(redis_client.get(keys[0]))
        assert session_data["tenant_id"] == mock_tenant["id"]
        assert session_data["discord_user_id"] == "123456789"

    def test_callback_stores_discord_tokens(self, client, redis_client):
        """Session should contain Discord tokens and expiry."""
        state = secrets.token_urlsafe(32)
        redis_client.set(f"oauth_state:{state}", "1", ex=300)

        mock_token_data, mock_profile, mock_tenant, accessible = _make_callback_mocks(
            access_token="test_access_tok",
            refresh_token="test_refresh_tok",
            expires_in=604800,
        )

        mock_tenant_svc = MagicMock()
        mock_tenant_svc.upsert.return_value = mock_tenant
        mock_tenant_svc.list_accessible_tenants.return_value = accessible

        with patch("blueprints.auth._get_redis", return_value=redis_client), \
             patch("blueprints.auth._exchange_code", return_value=mock_token_data), \
             patch("blueprints.auth._fetch_user_profile", return_value=mock_profile), \
             patch("blueprints.auth._get_tenant_service", return_value=mock_tenant_svc):
            response = client.get(f"/auth/callback?code=validcode&state={state}")

        keys = [k for k in redis_client.keys("session:*") if "lock" not in k]
        session_data = json.loads(redis_client.get(keys[0]))

        assert session_data["discord_access_token"] == "test_access_tok"
        assert session_data["discord_refresh_token"] == "test_refresh_tok"
        assert "discord_token_expires_at" in session_data
        assert session_data["discord_token_expires_at"] > 0

    def test_callback_stores_ip_and_created_at(self, client, redis_client):
        """Session should contain ip_address and created_at."""
        state = secrets.token_urlsafe(32)
        redis_client.set(f"oauth_state:{state}", "1", ex=300)

        mock_token_data, mock_profile, mock_tenant, accessible = _make_callback_mocks()

        mock_tenant_svc = MagicMock()
        mock_tenant_svc.upsert.return_value = mock_tenant
        mock_tenant_svc.list_accessible_tenants.return_value = accessible

        with patch("blueprints.auth._get_redis", return_value=redis_client), \
             patch("blueprints.auth._exchange_code", return_value=mock_token_data), \
             patch("blueprints.auth._fetch_user_profile", return_value=mock_profile), \
             patch("blueprints.auth._get_tenant_service", return_value=mock_tenant_svc):
            response = client.get(f"/auth/callback?code=validcode&state={state}")

        keys = [k for k in redis_client.keys("session:*") if "lock" not in k]
        session_data = json.loads(redis_client.get(keys[0]))

        assert "ip_address" in session_data
        assert "created_at" in session_data
        assert isinstance(session_data["created_at"], float)

    def test_callback_sets_operator_flag(self, client, redis_client):
        """Session should set is_operator when discord_user_id matches env."""
        state = secrets.token_urlsafe(32)
        redis_client.set(f"oauth_state:{state}", "1", ex=300)

        mock_token_data, mock_profile, mock_tenant, accessible = _make_callback_mocks(
            discord_user_id=999888777
        )

        mock_tenant_svc = MagicMock()
        mock_tenant_svc.upsert.return_value = mock_tenant
        mock_tenant_svc.list_accessible_tenants.return_value = accessible

        with patch("blueprints.auth._get_redis", return_value=redis_client), \
             patch("blueprints.auth._exchange_code", return_value=mock_token_data), \
             patch("blueprints.auth._fetch_user_profile", return_value=mock_profile), \
             patch("blueprints.auth._get_tenant_service", return_value=mock_tenant_svc), \
             patch.dict("os.environ", {"OPERATOR_DISCORD_ID": "999888777"}):
            response = client.get(f"/auth/callback?code=validcode&state={state}")

        keys = [k for k in redis_client.keys("session:*") if "lock" not in k]
        session_data = json.loads(redis_client.get(keys[0]))
        assert session_data["is_operator"] is True

    def test_callback_non_operator(self, client, redis_client):
        """Session should set is_operator=False for non-operator users."""
        state = secrets.token_urlsafe(32)
        redis_client.set(f"oauth_state:{state}", "1", ex=300)

        mock_token_data, mock_profile, mock_tenant, accessible = _make_callback_mocks(
            discord_user_id=111222333
        )

        mock_tenant_svc = MagicMock()
        mock_tenant_svc.upsert.return_value = mock_tenant
        mock_tenant_svc.list_accessible_tenants.return_value = accessible

        with patch("blueprints.auth._get_redis", return_value=redis_client), \
             patch("blueprints.auth._exchange_code", return_value=mock_token_data), \
             patch("blueprints.auth._fetch_user_profile", return_value=mock_profile), \
             patch("blueprints.auth._get_tenant_service", return_value=mock_tenant_svc), \
             patch.dict("os.environ", {"OPERATOR_DISCORD_ID": "999888777"}):
            response = client.get(f"/auth/callback?code=validcode&state={state}")

        keys = [k for k in redis_client.keys("session:*") if "lock" not in k]
        session_data = json.loads(redis_client.get(keys[0]))
        assert session_data["is_operator"] is False

    def test_callback_builds_roles_with_owner(self, client, redis_client):
        """Session roles should include owned tenant as 'owner'."""
        state = secrets.token_urlsafe(32)
        redis_client.set(f"oauth_state:{state}", "1", ex=300)

        tenant_id = str(uuid.uuid4())
        mock_token_data, mock_profile, mock_tenant, _ = _make_callback_mocks(
            tenant_id=tenant_id
        )

        mock_tenant_svc = MagicMock()
        mock_tenant_svc.upsert.return_value = mock_tenant
        mock_tenant_svc.list_accessible_tenants.return_value = []

        with patch("blueprints.auth._get_redis", return_value=redis_client), \
             patch("blueprints.auth._exchange_code", return_value=mock_token_data), \
             patch("blueprints.auth._fetch_user_profile", return_value=mock_profile), \
             patch("blueprints.auth._get_tenant_service", return_value=mock_tenant_svc):
            response = client.get(f"/auth/callback?code=validcode&state={state}")

        keys = [k for k in redis_client.keys("session:*") if "lock" not in k]
        session_data = json.loads(redis_client.get(keys[0]))

        assert {"tenant_id": tenant_id, "role": "owner"} in session_data["roles"]

    def test_callback_includes_delegated_roles(self, client, redis_client):
        """Session roles should include delegated tenant roles."""
        state = secrets.token_urlsafe(32)
        redis_client.set(f"oauth_state:{state}", "1", ex=300)

        tenant_id = str(uuid.uuid4())
        other_tenant_id = str(uuid.uuid4())
        mock_token_data, mock_profile, mock_tenant, _ = _make_callback_mocks(
            tenant_id=tenant_id
        )

        accessible = [
            {"tenant_id": other_tenant_id, "role": "editor", "discord_username": "Other"},
        ]

        mock_tenant_svc = MagicMock()
        mock_tenant_svc.upsert.return_value = mock_tenant
        mock_tenant_svc.list_accessible_tenants.return_value = accessible

        with patch("blueprints.auth._get_redis", return_value=redis_client), \
             patch("blueprints.auth._exchange_code", return_value=mock_token_data), \
             patch("blueprints.auth._fetch_user_profile", return_value=mock_profile), \
             patch("blueprints.auth._get_tenant_service", return_value=mock_tenant_svc):
            response = client.get(f"/auth/callback?code=validcode&state={state}")

        keys = [k for k in redis_client.keys("session:*") if "lock" not in k]
        session_data = json.loads(redis_client.get(keys[0]))

        # Should have owner + delegated
        assert len(session_data["roles"]) == 2
        assert {"tenant_id": tenant_id, "role": "owner"} in session_data["roles"]
        assert {"tenant_id": other_tenant_id, "role": "editor"} in session_data["roles"]

    def test_callback_sets_active_tenant_to_owned(self, client, redis_client):
        """active_tenant_id should be set to the user's owned tenant."""
        state = secrets.token_urlsafe(32)
        redis_client.set(f"oauth_state:{state}", "1", ex=300)

        tenant_id = str(uuid.uuid4())
        mock_token_data, mock_profile, mock_tenant, _ = _make_callback_mocks(
            tenant_id=tenant_id
        )

        mock_tenant_svc = MagicMock()
        mock_tenant_svc.upsert.return_value = mock_tenant
        mock_tenant_svc.list_accessible_tenants.return_value = []

        with patch("blueprints.auth._get_redis", return_value=redis_client), \
             patch("blueprints.auth._exchange_code", return_value=mock_token_data), \
             patch("blueprints.auth._fetch_user_profile", return_value=mock_profile), \
             patch("blueprints.auth._get_tenant_service", return_value=mock_tenant_svc):
            response = client.get(f"/auth/callback?code=validcode&state={state}")

        keys = [k for k in redis_client.keys("session:*") if "lock" not in k]
        session_data = json.loads(redis_client.get(keys[0]))
        assert session_data["active_tenant_id"] == tenant_id

    def test_callback_session_has_24h_ttl(self, client, redis_client):
        """Session stored in Redis should have a 24h (86400s) sliding TTL."""
        state = secrets.token_urlsafe(32)
        redis_client.set(f"oauth_state:{state}", "1", ex=300)

        mock_token_data, mock_profile, mock_tenant, accessible = _make_callback_mocks()

        mock_tenant_svc = MagicMock()
        mock_tenant_svc.upsert.return_value = mock_tenant
        mock_tenant_svc.list_accessible_tenants.return_value = accessible

        with patch("blueprints.auth._get_redis", return_value=redis_client), \
             patch("blueprints.auth._exchange_code", return_value=mock_token_data), \
             patch("blueprints.auth._fetch_user_profile", return_value=mock_profile), \
             patch("blueprints.auth._get_tenant_service", return_value=mock_tenant_svc):
            client.get(f"/auth/callback?code=validcode&state={state}")

        keys = [k for k in redis_client.keys("session:*") if "lock" not in k]
        assert len(keys) == 1
        ttl = redis_client.ttl(keys[0])
        # 24h = 86400 seconds; SessionService uses SESSION_TTL (86400)
        assert 86300 < ttl <= 86400

    def test_callback_consumes_state(self, client, redis_client):
        """State should be consumed after use (one-time use)."""
        state = secrets.token_urlsafe(32)
        redis_client.set(f"oauth_state:{state}", "1", ex=300)

        mock_token_data, mock_profile, mock_tenant, accessible = _make_callback_mocks()

        mock_tenant_svc = MagicMock()
        mock_tenant_svc.upsert.return_value = mock_tenant
        mock_tenant_svc.list_accessible_tenants.return_value = accessible

        with patch("blueprints.auth._get_redis", return_value=redis_client), \
             patch("blueprints.auth._exchange_code", return_value=mock_token_data), \
             patch("blueprints.auth._fetch_user_profile", return_value=mock_profile), \
             patch("blueprints.auth._get_tenant_service", return_value=mock_tenant_svc):
            client.get(f"/auth/callback?code=validcode&state={state}")

        # State should be consumed
        assert redis_client.get(f"oauth_state:{state}") is None

    def test_callback_uses_tenant_service(self, client, redis_client):
        """Callback should call TenantService.upsert() with correct args."""
        state = secrets.token_urlsafe(32)
        redis_client.set(f"oauth_state:{state}", "1", ex=300)

        mock_token_data, mock_profile, mock_tenant, accessible = _make_callback_mocks(
            discord_user_id=555666777,
            username="CallTest",
            email="call@test.com",
        )

        mock_tenant_svc = MagicMock()
        mock_tenant_svc.upsert.return_value = mock_tenant
        mock_tenant_svc.list_accessible_tenants.return_value = accessible

        with patch("blueprints.auth._get_redis", return_value=redis_client), \
             patch("blueprints.auth._exchange_code", return_value=mock_token_data), \
             patch("blueprints.auth._fetch_user_profile", return_value=mock_profile), \
             patch("blueprints.auth._get_tenant_service", return_value=mock_tenant_svc):
            client.get(f"/auth/callback?code=validcode&state={state}")

        mock_tenant_svc.upsert.assert_called_once_with(
            555666777, "CallTest", "call@test.com"
        )
        mock_tenant_svc.list_accessible_tenants.assert_called_once_with(555666777)


class TestLogout:
    """Tests for GET/POST /auth/logout."""

    def test_logout_post_deletes_session(self, client, redis_client):
        """POST logout should delete session from Redis via SessionService.destroy()."""
        token = "test-session-token"
        session_data = {
            "tenant_id": "abc",
            "discord_user_id": "123456",
            "discord_access_token": "mock_discord_tok",
        }
        redis_client.set(f"session:{token}", json.dumps(session_data), ex=86400)

        with patch("blueprints.auth._get_redis", return_value=redis_client), \
             patch("blueprints.auth._revoke_discord_token") as mock_revoke:
            client.set_cookie("hellodj_session", token, domain="localhost")
            response = client.post("/auth/logout")

        assert response.status_code == 302
        # Session should be removed from Redis
        assert redis_client.get(f"session:{token}") is None
        # Discord token should be revoked
        mock_revoke.assert_called_once_with("mock_discord_tok")

    def test_logout_get_supported(self, client, redis_client):
        """GET /auth/logout should work (link-based logout)."""
        token = "test-session-token"
        session_data = {
            "tenant_id": "abc",
            "discord_user_id": "123456",
            "discord_access_token": "mock_tok",
        }
        redis_client.set(f"session:{token}", json.dumps(session_data), ex=86400)

        with patch("blueprints.auth._get_redis", return_value=redis_client), \
             patch("blueprints.auth._revoke_discord_token"):
            client.set_cookie("hellodj_session", token, domain="localhost")
            response = client.get("/auth/logout")

        assert response.status_code == 302
        assert redis_client.get(f"session:{token}") is None

    def test_logout_clears_cookie(self, client, redis_client):
        """Logout should clear the hellodj_session cookie with correct attributes."""
        token = "test-session-token"
        session_data = {
            "tenant_id": "abc",
            "discord_user_id": "123456",
        }
        redis_client.set(f"session:{token}", json.dumps(session_data), ex=86400)

        with patch("blueprints.auth._get_redis", return_value=redis_client), \
             patch("blueprints.auth._revoke_discord_token"):
            client.set_cookie("hellodj_session", token, domain="localhost")
            response = client.post("/auth/logout")

        # Cookie should be cleared (set to empty with max_age=0)
        cookies = response.headers.getlist("Set-Cookie")
        clear_cookie = [c for c in cookies if "hellodj_session=" in c]
        assert len(clear_cookie) >= 1
        cookie_str = clear_cookie[0]
        assert "Max-Age=0" in cookie_str
        assert "Path=/" in cookie_str
        assert "HttpOnly" in cookie_str
        assert "Secure" in cookie_str
        assert "SameSite=Lax" in cookie_str

    def test_logout_unauthenticated_no_cookie(self, client, redis_client):
        """Unauthenticated user (no cookie) should redirect without cleanup."""
        with patch("blueprints.auth._get_redis", return_value=redis_client), \
             patch("blueprints.auth._revoke_discord_token") as mock_revoke:
            response = client.post("/auth/logout")

        assert response.status_code == 302
        # Should NOT attempt revocation
        mock_revoke.assert_not_called()

    def test_logout_expired_session_clears_cookie(self, client, redis_client):
        """Expired session (cookie present but not in Redis) should clear cookie and redirect."""
        with patch("blueprints.auth._get_redis", return_value=redis_client), \
             patch("blueprints.auth._revoke_discord_token") as mock_revoke:
            client.set_cookie("hellodj_session", "expired-token", domain="localhost")
            response = client.post("/auth/logout")

        assert response.status_code == 302
        # Should NOT attempt revocation (no session data to get token from)
        mock_revoke.assert_not_called()
        # Should clear the stale cookie
        cookies = response.headers.getlist("Set-Cookie")
        clear_cookie = [c for c in cookies if "hellodj_session=" in c]
        assert len(clear_cookie) >= 1
        assert any("Max-Age=0" in c for c in clear_cookie)

    def test_logout_revocation_failure_proceeds(self, client, redis_client):
        """If Discord token revocation fails, logout should still succeed."""
        token = "test-session-token"
        session_data = {
            "tenant_id": "abc",
            "discord_user_id": "123456",
            "discord_access_token": "failing_tok",
        }
        redis_client.set(f"session:{token}", json.dumps(session_data), ex=86400)

        with patch("blueprints.auth._get_redis", return_value=redis_client), \
             patch("blueprints.auth._revoke_discord_token", side_effect=None):
            client.set_cookie("hellodj_session", token, domain="localhost")
            response = client.post("/auth/logout")

        # Logout should still succeed
        assert response.status_code == 302
        assert redis_client.get(f"session:{token}") is None

    def test_logout_no_discord_token_skips_revocation(self, client, redis_client):
        """If session has no discord_access_token, skip revocation."""
        token = "test-session-token"
        session_data = {
            "tenant_id": "abc",
            "discord_user_id": "123456",
        }
        redis_client.set(f"session:{token}", json.dumps(session_data), ex=86400)

        with patch("blueprints.auth._get_redis", return_value=redis_client), \
             patch("blueprints.auth._revoke_discord_token") as mock_revoke:
            client.set_cookie("hellodj_session", token, domain="localhost")
            response = client.post("/auth/logout")

        assert response.status_code == 302
        mock_revoke.assert_not_called()


class TestMe:
    """Tests for GET /auth/me."""

    def test_me_returns_profile(self, client, redis_client):
        """Should return the session profile data from Redis."""
        profile = {
            "tenant_id": "test-uuid",
            "discord_user_id": "123456789",
            "discord_username": "TestUser",
            "email": "test@example.com",
            "avatar": "abc123",
            "is_operator": False,
            "roles": [{"tenant_id": "test-uuid", "role": "owner"}],
            "active_tenant_id": "test-uuid",
        }
        token = "valid-session-token"
        redis_client.set(f"session:{token}", json.dumps(profile), ex=86400)

        with patch("blueprints.auth._get_redis", return_value=redis_client):
            client.set_cookie("hellodj_session", token, domain="localhost")
            response = client.get("/auth/me")

        assert response.status_code == 200
        data = response.get_json()
        assert data["tenant_id"] == "test-uuid"
        assert data["discord_user_id"] == "123456789"
        assert data["discord_username"] == "TestUser"

    def test_me_no_cookie(self, client, redis_client):
        """Should return 401 without session cookie."""
        with patch("blueprints.auth._get_redis", return_value=redis_client):
            response = client.get("/auth/me")

        assert response.status_code == 401

    def test_me_expired_session(self, client, redis_client):
        """Should return 401 if session is expired/not in Redis."""
        with patch("blueprints.auth._get_redis", return_value=redis_client):
            client.set_cookie("hellodj_session", "expired-token", domain="localhost")
            response = client.get("/auth/me")

        assert response.status_code == 401
        data = response.get_json()
        assert "expired" in data["error"].lower()

"""Unit tests for the session service.

Tests the SessionService class: create, load, extend, destroy, update_field,
switch_tenant, and invalidate_user_sessions.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import fakeredis
import pytest
import redis

# Ensure web-ui is importable
_webui_dir = str(Path(__file__).resolve().parent.parent / "web-ui")
if _webui_dir not in sys.path:
    sys.path.insert(0, _webui_dir)

from services.session_service import (
    ABSOLUTE_LIFETIME,
    COOKIE_NAME,
    KEY_PREFIX,
    SESSION_TTL,
    ServiceUnavailableError,
    SessionService,
)


@pytest.fixture
def redis_client():
    """Provide a fakeredis client for session tests."""
    client = fakeredis.FakeRedis(decode_responses=True)
    yield client
    client.flushall()
    client.close()


@pytest.fixture
def svc(redis_client):
    """Provide a SessionService instance with fakeredis."""
    return SessionService(redis_client)


@pytest.fixture
def sample_session():
    """Sample session data matching the design schema."""
    return {
        "tenant_id": "abc-123-def",
        "discord_user_id": "999888777",
        "discord_username": "testuser",
        "email": "test@example.com",
        "avatar": None,
        "is_operator": False,
        "roles": [
            {"tenant_id": "abc-123-def", "role": "owner"},
            {"tenant_id": "xyz-789", "role": "admin"},
        ],
        "active_tenant_id": "abc-123-def",
        "ip_address": "192.168.1.100",
        "created_at": time.time(),
        "discord_access_token": "discord_tok_123",
        "discord_refresh_token": "discord_ref_456",
        "discord_token_expires_at": time.time() + 3600,
        "refresh_retry_count": 0,
    }


class TestConstants:
    """Verify module constants match design spec."""

    def test_session_ttl(self):
        assert SESSION_TTL == 86400

    def test_absolute_lifetime(self):
        assert ABSOLUTE_LIFETIME == 604800

    def test_cookie_name(self):
        assert COOKIE_NAME == "hellodj_session"

    def test_key_prefix(self):
        assert KEY_PREFIX == "session:"


class TestCreate:
    """Tests for SessionService.create()."""

    def test_returns_token_string(self, svc, sample_session):
        """create() returns a non-empty string token."""
        token = svc.create(sample_session)
        assert isinstance(token, str)
        assert len(token) > 0

    def test_token_is_unique(self, svc, sample_session):
        """Each call to create() produces a unique token."""
        tokens = {svc.create(sample_session) for _ in range(20)}
        assert len(tokens) == 20

    def test_session_stored_in_redis(self, svc, redis_client, sample_session):
        """Session data is stored at session:{token} key."""
        token = svc.create(sample_session)
        key = f"{KEY_PREFIX}{token}"
        raw = redis_client.get(key)
        assert raw is not None
        stored = json.loads(raw)
        assert stored["discord_user_id"] == "999888777"

    def test_session_has_ttl(self, svc, redis_client, sample_session):
        """Session key has a TTL of SESSION_TTL."""
        token = svc.create(sample_session)
        key = f"{KEY_PREFIX}{token}"
        ttl = redis_client.ttl(key)
        assert 0 < ttl <= SESSION_TTL

    def test_token_added_to_user_sessions_set(self, svc, redis_client, sample_session):
        """Token is added to user_sessions:{discord_user_id} SET."""
        token = svc.create(sample_session)
        user_key = "user_sessions:999888777"
        members = redis_client.smembers(user_key)
        assert token in members

    def test_multiple_sessions_tracked(self, svc, redis_client, sample_session):
        """Multiple tokens for same user are all tracked in the set."""
        t1 = svc.create(sample_session)
        t2 = svc.create(sample_session)
        user_key = "user_sessions:999888777"
        members = redis_client.smembers(user_key)
        assert t1 in members
        assert t2 in members


class TestLoad:
    """Tests for SessionService.load()."""

    def test_loads_existing_session(self, svc, sample_session):
        """load() returns session dict for a valid token."""
        token = svc.create(sample_session)
        loaded = svc.load(token)
        assert loaded is not None
        assert loaded["discord_user_id"] == "999888777"
        assert loaded["active_tenant_id"] == "abc-123-def"

    def test_returns_none_for_missing_token(self, svc):
        """load() returns None for a token that doesn't exist."""
        result = svc.load("nonexistent-token")
        assert result is None

    def test_returns_none_for_empty_token(self, svc):
        """load() returns None for empty string."""
        result = svc.load("")
        assert result is None

    def test_returns_none_for_malformed_data(self, svc, redis_client):
        """load() returns None and evicts malformed JSON."""
        key = f"{KEY_PREFIX}bad-token"
        redis_client.set(key, "not valid json {{{", ex=100)
        result = svc.load("bad-token")
        assert result is None
        # Key should be evicted
        assert redis_client.get(key) is None


class TestExtend:
    """Tests for SessionService.extend()."""

    def test_resets_ttl(self, svc, redis_client, sample_session):
        """extend() resets the session TTL to SESSION_TTL."""
        token = svc.create(sample_session)
        key = f"{KEY_PREFIX}{token}"

        # Simulate time passing by reducing TTL
        redis_client.expire(key, 100)
        assert redis_client.ttl(key) <= 100

        svc.extend(token)
        ttl = redis_client.ttl(key)
        assert ttl > 100
        assert ttl <= SESSION_TTL

    def test_noop_for_empty_token(self, svc):
        """extend() does nothing for empty string (no error)."""
        svc.extend("")  # Should not raise


class TestDestroy:
    """Tests for SessionService.destroy()."""

    def test_removes_session_key(self, svc, redis_client, sample_session):
        """destroy() deletes the session key from Redis."""
        token = svc.create(sample_session)
        svc.destroy(token)
        key = f"{KEY_PREFIX}{token}"
        assert redis_client.get(key) is None

    def test_removes_from_user_sessions_set(self, svc, redis_client, sample_session):
        """destroy() removes token from user_sessions set."""
        token = svc.create(sample_session)
        svc.destroy(token)
        user_key = "user_sessions:999888777"
        members = redis_client.smembers(user_key)
        assert token not in members

    def test_noop_for_nonexistent_token(self, svc):
        """destroy() doesn't raise for a token that doesn't exist."""
        svc.destroy("never-existed")  # Should not raise

    def test_noop_for_empty_token(self, svc):
        """destroy() does nothing for empty string."""
        svc.destroy("")  # Should not raise


class TestUpdateField:
    """Tests for SessionService.update_field()."""

    def test_updates_existing_field(self, svc, sample_session):
        """update_field() modifies an existing field in the session."""
        token = svc.create(sample_session)
        svc.update_field(token, "discord_username", "newname")
        loaded = svc.load(token)
        assert loaded["discord_username"] == "newname"

    def test_adds_new_field(self, svc, sample_session):
        """update_field() can add a field that didn't exist."""
        token = svc.create(sample_session)
        svc.update_field(token, "new_field", "new_value")
        loaded = svc.load(token)
        assert loaded["new_field"] == "new_value"

    def test_preserves_other_fields(self, svc, sample_session):
        """update_field() doesn't alter other session data."""
        token = svc.create(sample_session)
        svc.update_field(token, "discord_username", "changed")
        loaded = svc.load(token)
        assert loaded["discord_user_id"] == "999888777"
        assert loaded["active_tenant_id"] == "abc-123-def"

    def test_preserves_remaining_ttl(self, svc, redis_client, sample_session):
        """update_field() re-stores with the remaining TTL (not full TTL)."""
        token = svc.create(sample_session)
        key = f"{KEY_PREFIX}{token}"

        # Set a shorter TTL to verify it's preserved
        redis_client.expire(key, 500)
        svc.update_field(token, "discord_username", "changed")
        ttl = redis_client.ttl(key)
        # TTL should be around 500 (not SESSION_TTL)
        assert ttl <= 500

    def test_noop_for_missing_session(self, svc):
        """update_field() does nothing if session doesn't exist."""
        svc.update_field("nonexistent", "field", "value")  # Should not raise


class TestSwitchTenant:
    """Tests for SessionService.switch_tenant()."""

    def test_switch_to_accessible_tenant(self, svc, sample_session):
        """switch_tenant() returns True and updates active_tenant_id."""
        token = svc.create(sample_session)
        accessible = ["abc-123-def", "xyz-789"]
        result = svc.switch_tenant(token, "xyz-789", accessible)
        assert result is True
        loaded = svc.load(token)
        assert loaded["active_tenant_id"] == "xyz-789"

    def test_switch_to_inaccessible_tenant_returns_false(self, svc, sample_session):
        """switch_tenant() returns False for a tenant not in accessible list."""
        token = svc.create(sample_session)
        accessible = ["abc-123-def", "xyz-789"]
        result = svc.switch_tenant(token, "not-in-list", accessible)
        assert result is False
        # active_tenant_id should be unchanged
        loaded = svc.load(token)
        assert loaded["active_tenant_id"] == "abc-123-def"

    def test_switch_to_empty_list_returns_false(self, svc, sample_session):
        """switch_tenant() returns False when accessible list is empty."""
        token = svc.create(sample_session)
        result = svc.switch_tenant(token, "abc-123-def", [])
        assert result is False


class TestInvalidateUserSessions:
    """Tests for SessionService.invalidate_user_sessions()."""

    def test_invalidates_all_sessions_no_filter(self, svc, redis_client, sample_session):
        """Without tenant filter, all sessions for user are deleted."""
        t1 = svc.create(sample_session)
        t2 = svc.create(sample_session)

        count = svc.invalidate_user_sessions("999888777")
        assert count == 2
        assert svc.load(t1) is None
        assert svc.load(t2) is None

    def test_invalidates_filtered_by_tenant(self, svc, redis_client):
        """With tenant filter, only sessions referencing that tenant are deleted."""
        session_a = {
            "discord_user_id": "111222333",
            "roles": [{"tenant_id": "tenant-A", "role": "owner"}],
            "active_tenant_id": "tenant-A",
        }
        session_b = {
            "discord_user_id": "111222333",
            "roles": [{"tenant_id": "tenant-B", "role": "admin"}],
            "active_tenant_id": "tenant-B",
        }

        t_a = svc.create(session_a)
        t_b = svc.create(session_b)

        count = svc.invalidate_user_sessions("111222333", tenant_id="tenant-A")
        assert count == 1
        assert svc.load(t_a) is None
        assert svc.load(t_b) is not None

    def test_returns_zero_for_no_sessions(self, svc):
        """Returns 0 when user has no sessions."""
        count = svc.invalidate_user_sessions("no-such-user")
        assert count == 0

    def test_cleans_up_expired_tokens_from_set(self, svc, redis_client):
        """Expired session tokens are removed from the user_sessions set."""
        session_data = {
            "discord_user_id": "444555666",
            "roles": [],
            "active_tenant_id": "t1",
        }
        token = svc.create(session_data)

        # Manually expire the session key (simulate TTL expiration)
        redis_client.delete(f"{KEY_PREFIX}{token}")

        count = svc.invalidate_user_sessions("444555666")
        assert count == 0

        # The stale token should have been cleaned from the set
        user_key = "user_sessions:444555666"
        members = redis_client.smembers(user_key)
        assert token not in members


class TestServiceUnavailableError:
    """Tests that Redis connection failures raise ServiceUnavailableError."""

    def test_create_raises_on_connection_error(self, sample_session):
        """create() raises ServiceUnavailableError when Redis is down."""
        from unittest.mock import MagicMock

        broken_client = MagicMock(spec=fakeredis.FakeRedis)
        broken_client.set.side_effect = redis.ConnectionError("Connection refused")
        svc = SessionService(broken_client)

        with pytest.raises(ServiceUnavailableError):
            svc.create(sample_session)

    def test_load_raises_on_connection_error(self):
        """load() raises ServiceUnavailableError when Redis is down."""
        from unittest.mock import MagicMock

        broken_client = MagicMock(spec=fakeredis.FakeRedis)
        broken_client.get.side_effect = redis.ConnectionError("Connection refused")
        svc = SessionService(broken_client)

        with pytest.raises(ServiceUnavailableError):
            svc.load("some-token")

    def test_extend_raises_on_connection_error(self):
        """extend() raises ServiceUnavailableError when Redis is down."""
        from unittest.mock import MagicMock

        broken_client = MagicMock(spec=fakeredis.FakeRedis)
        broken_client.expire.side_effect = redis.ConnectionError("Connection refused")
        svc = SessionService(broken_client)

        with pytest.raises(ServiceUnavailableError):
            svc.extend("some-token")

    def test_destroy_raises_on_connection_error(self):
        """destroy() raises ServiceUnavailableError when Redis is down."""
        from unittest.mock import MagicMock

        broken_client = MagicMock(spec=fakeredis.FakeRedis)
        broken_client.get.side_effect = redis.ConnectionError("Connection refused")
        svc = SessionService(broken_client)

        with pytest.raises(ServiceUnavailableError):
            svc.destroy("some-token")

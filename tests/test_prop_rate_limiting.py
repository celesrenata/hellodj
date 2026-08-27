"""Property-based test: Rate Limiting Correctness.

**Validates: Requirements 17.5**

Property 12: Requests within 60-per-minute accepted; 61st request in any
sliding 60s window returns HTTP 429 (allowed=False with positive retry_after).

Tests:
1. For any N <= 60, all N requests are accepted (allowed=True).
2. For any N > 60 within a 60-second window, the Nth request is rejected
   (allowed=False) with a positive retry_after value.
3. Rate limits for different tenants are independent.
"""

from __future__ import annotations

import sys
from pathlib import Path

import fakeredis
import pytest
from hypothesis import given, settings, HealthCheck
from hypothesis import strategies as st

# Ensure web-ui is importable
_webui_dir = str(Path(__file__).resolve().parent.parent / "web-ui")
if _webui_dir not in sys.path:
    sys.path.insert(0, _webui_dir)

from services.rate_limiter import _check_rate_limit


@pytest.fixture
def fake_redis():
    """Provide a fresh fakeredis client per test."""
    client = fakeredis.FakeRedis(decode_responses=True)
    yield client
    client.flushall()
    client.close()


# Strategy: number of requests within the allowed limit (1 to 60)
requests_within_limit = st.integers(min_value=1, max_value=60)

# Strategy: number of requests exceeding the limit (61 to 120)
requests_exceeding_limit = st.integers(min_value=61, max_value=120)

# Strategy: tenant identifiers (non-empty alphanumeric for valid Redis keys)
tenant_ids = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N")),
    min_size=1,
    max_size=30,
)


@settings(
    max_examples=100,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(n=requests_within_limit)
def test_requests_within_limit_all_accepted(fake_redis, n: int):
    """Property 12.1: For any N where N <= 60, all N requests are accepted.

    **Validates: Requirements 17.5**
    """
    key = "ratelimit:prop_test:endpoint"

    # Clear the key before each example
    fake_redis.delete(key)

    for i in range(n):
        allowed, retry_after = _check_rate_limit(
            key, fake_redis, limit=60, window_seconds=60
        )
        assert allowed is True, (
            f"Request {i + 1} of {n} was rejected, but should be accepted "
            f"(within 60-request limit)"
        )
        assert retry_after == 0, (
            f"Request {i + 1} of {n}: retry_after should be 0 when allowed, "
            f"got {retry_after}"
        )


@settings(
    max_examples=100,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(n=requests_exceeding_limit)
def test_request_exceeding_limit_rejected(fake_redis, n: int):
    """Property 12.2: For any N > 60, the Nth request is rejected with positive retry_after.

    **Validates: Requirements 17.5**
    """
    key = "ratelimit:prop_test_exceed:endpoint"

    # Clear the key before each example
    fake_redis.delete(key)

    # First 60 requests should all be accepted
    for i in range(60):
        allowed, _ = _check_rate_limit(
            key, fake_redis, limit=60, window_seconds=60
        )
        assert allowed is True, (
            f"Request {i + 1} should be accepted (within limit), but was rejected"
        )

    # The Nth request (n > 60) should be rejected
    # We need to make requests up to n; all from 61 onward should be rejected
    for i in range(60, n):
        allowed, retry_after = _check_rate_limit(
            key, fake_redis, limit=60, window_seconds=60
        )
        assert allowed is False, (
            f"Request {i + 1} should be rejected (over 60-request limit), "
            f"but was allowed"
        )
        assert retry_after > 0, (
            f"Request {i + 1}: retry_after must be positive when rejected, "
            f"got {retry_after}"
        )


@settings(
    max_examples=100,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    tenant_a=tenant_ids,
    tenant_b=tenant_ids,
    n=st.integers(min_value=1, max_value=60),
)
def test_rate_limits_per_tenant_independent(fake_redis, tenant_a: str, tenant_b: str, n: int):
    """Property 12.3: Rate limits for different tenants are independent.

    Filling tenant A's limit does not affect tenant B's allowance.

    **Validates: Requirements 17.5**
    """
    # Ensure tenants are distinct
    if tenant_a == tenant_b:
        return  # Skip — need distinct tenants for independence test

    key_a = f"ratelimit:{tenant_a}:endpoint"
    key_b = f"ratelimit:{tenant_b}:endpoint"

    # Clear both keys
    fake_redis.delete(key_a)
    fake_redis.delete(key_b)

    # Fill tenant A up to the limit (60 requests)
    for _ in range(60):
        _check_rate_limit(key_a, fake_redis, limit=60, window_seconds=60)

    # Tenant A is now at limit — should be rejected
    allowed_a, _ = _check_rate_limit(key_a, fake_redis, limit=60, window_seconds=60)
    assert allowed_a is False, "Tenant A should be rate limited after 60 requests"

    # Tenant B should still be able to make n requests (n <= 60)
    for i in range(n):
        allowed_b, retry_after_b = _check_rate_limit(
            key_b, fake_redis, limit=60, window_seconds=60
        )
        assert allowed_b is True, (
            f"Tenant B request {i + 1} should be accepted (independent of tenant A), "
            f"but was rejected"
        )
        assert retry_after_b == 0, (
            f"Tenant B request {i + 1}: retry_after should be 0, got {retry_after_b}"
        )
